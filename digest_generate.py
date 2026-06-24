#!/usr/bin/env python3
"""Hacker News Digest — data loader + three-step pipeline prompt templates.

Pipeline: Extract → Context → Summarize (three isolated subagents),
mirroring the ai-leaders-digest rework. Replaces the older Draft →
Critique → Refine reflection design that forced the Critique step to
invent insights on thin tweet/story days.

Usage:
  python3 digest_generate.py query [--days 1] [--focus default]
      → emit story data JSON + extract/context_template/summarize_template

  python3 digest_generate.py save-summary [--days 1] [--focus default]
      → read summary text from stdin, write to summaries table

  python3 digest_generate.py stats
      → quick stats
"""

import json
import sys
from datetime import datetime, timezone, timedelta

from db import get_db, init_db


# ── Three-step pipeline prompt templates ────────────────────────────

EXTRACT_PROMPT = """你是 Hacker News 日报流水线的第 1 步：分拣。

任务：把按列表/排名汇总的故事按"信号值"分类。不要写摘要，不要做洞察 ——
只做忠实分拣。

输入元信息：
- 时间范围：过去 {days} 天
- 数据集中故事数：{total_stories} 条
- 焦点 profile：{focus}
- 焦点指令：{focus_instructions}
- 日期：{date}

输入数据：见下方 stories 列表。每条故事包含 title / url / domain /
score / comments / hn_url / lists。

{stories_block}

---

请对每条故事判断：

**substantive（实质性）** —— 满足任一即可：
- 技术深度（论文 / 项目 / 工具 / 库的具体技术内容）
- 公司/产品动态（融资、发布、收购、停服、招聘信号）
- 战略表态（开源 vs 闭源、定价、行业趋势）
- 实操经验（performance numbers、benchmarks、postmortems）
- 高质量讨论（评论数 >100 且非纯吐槽）

**filler（噪声）** —— 全部归入：
- 标题党 / 通用观点贴 / 一句话感想
- 重复历史话题（"why X is broken" 的第 N 次复述）
- 政治 / 社会 / 非技术议题（除非有具体技术影响）
- 链接已死或 paywall 重要内容（无法判断）

按 HN 帖子类型分组（Show HN / Ask HN / 普通文章 / 讨论），每组分别列
substantive 列表 + filler 计数。

可选 web 调用（最多 **2 次**）：仅当某条故事的标题或 domain 让你完全
认不出实体（例如冷门工具或不熟悉的公司），做一次 WebSearch 确认 type
归类。**不要**用 web 调用做事实校验 —— 那是下一步的事。

输出格式（**严格 JSON**，放在一个 ```json 代码块里）：

```json
{{
  "by_type": {{
    "show_hn": {{
      "substantive": [{{"id": 12345, "title": "...", "tag": "tool|paper|project|launch|other"}}],
      "filler_count": 5,
      "filler_summary": "<一句话概括 filler 主题>"
    }},
    "ask_hn":   {{ ... }},
    "article":  {{ ... }},
    "discussion": {{ ... }}
  }}
}}
```

预算：2 次 web 调用，硬上限。"""


CONTEXT_PROMPT = """你是 Hacker News 日报流水线的第 2 步：背景调研。

任务：为每条 substantive 故事添加事实背景。

**重要**：你看不到原始故事完整列表 —— 只看到上一步的分拣 JSON。

上一步输出（已替换占位符）：

{extracted}

对**每条 substantive 故事**，目标是回答：
- 这个项目/工具/公司**是什么**？（1-2 句话）
- 关键的客观数字 / benchmark / 价格 / 融资金额能不能补全？
- 跟既有 HN 话题的关系？（前作 / 竞品 / fork / replied-to）
- 评论里有没有高质量反驳或补充？（如能从 hn_url 拉到讨论页则采样）

可选 web 调用（最多 **12 次，硬上限**）：
- WebSearch / WebFetch: 拿 README / 产品页 / 论文 abstract / 公司新闻
- WebFetch hn_url: 抓 HN 评论页主线讨论（仅当评论数 >100 且讨论看上去
  有实质内容时再 fetch）
- 12 次平均分配 ≈ 一组约 2-3 次；优先级：含具体名词（工具名 / 公司 /
  benchmark 数字）的 > 抽象观点贴

输出格式（**严格 JSON**，放在一个 ```json 代码块里）：保持上一步的
结构，在每条 substantive 后**新增**两个字段：
- `context`: 一段中文背景（1-3 句），如不需要 web 调用可写 "自明"
- `sources`: URL 数组；没用就空数组 `[]`

**不要**修改 type / filler_* 字段。**不要**删任何 substantive 项目。

预算：12 次 web 调用，硬上限。"""


SUMMARIZE_PROMPT = """你是 Hacker News 日报流水线的第 3 步：写作。

任务：基于上一步加了背景的 JSON，写一份**忠实**的中文 digest。
**不要**硬挤跨条 insight，**不要**强行做"今日观察"二阶推论。
如果某天 HN 列表平淡，digest 就应该读起来平淡 —— 这是诚实。

**重要**：你只看 contexted JSON，不重新读原始故事。**不要**做 web 调用。

上一步输出（已替换占位符）：

{contexted}

输出格式（严格按以下 4 段）：

📰 Hacker News Digest - {date}
(过去 {days} 天，{total_stories} 条故事，focus={focus})

---

✨ 本期亮点（2-3 条）

挑信息密度最高的 2-3 条 substantive 故事。判断标准是**有具体新东西**
（具体工具/产品名、明确数字、人事变动、基准成绩） —— **不是**评分高低。
每条 1 段：
- 第一句：故事说了什么（中文转述 + 关键英文术语保留 + HN 链接）
- 第二句起：context（从 JSON 里来的 1-2 句背景）
- 最后一句（可选）：显然的从业者含义，如果觉得勉强**不写**

---

📖 逐类小记

按 type 排序（Show HN / Ask HN / Article / Discussion），每类一个 section：

### Show HN
**[标题](hn_url)** ({score}⬆ {comments}💬)
- **<关键词>**: 描述 (1-2 句)。（背景：context summary。）
- **<关键词>**: 同上结构。
（2-5 个 bullet 按重要性排序；substantive 少的类型 1-2 个就够。）

### Ask HN
同上。

### Article
同上。

### Discussion
同上。

对每个 type，如果当天没 substantive，写一句 "本期无显著 Show HN 内容
（仅日常推广，无 substantive 信号）。"

---

🔇 Sleeper picks（可选）

如果上下文 JSON 里某条 substantive 的 HN 分数较低（<100）但内容质量高
（来自 context 字段判断），单列 1-2 条："可能被低估的好帖"。如果都没有，
跳过此段。

---

写作要求：
- 中文为主，英文人名 / 产品名 / 术语保留
- 每个 bullet 顶头一个**加粗的关键词标签**（如 `**TabPFN-3**:`,
  `**Rust 编译器**:`, `**OpenAI Codex 移动端**:`），可扫读
- 每条 substantive 都要被覆盖
- 字数不限 —— 该长则长，该短则短

预算：0 次 web 调用。"""


# ── Commands ────────────────────────────────────────────────────────

def cmd_query(conn, args):
    days = 1
    focus_name = "default"
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        elif args[i] == "--focus" and i + 1 < len(args):
            focus_name = args[i + 1]; i += 2
        else:
            i += 1

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (focus_name,)
    ).fetchone()
    focus_rules = json.loads(profile_row["rules"]) if profile_row else {}
    focus_instructions = focus_rules.get("instructions", "")
    keywords = focus_rules.get("keywords", [])
    top_n = focus_rules.get("top_n", 20)

    # Top stories (deduplicated, with rankings rolled up)
    rows = conn.execute(
        """SELECT s.id, s.title, s.url, s.domain, s.author,
                  MIN(r.rank) AS best_rank,
                  MAX(r.score) AS max_score,
                  MAX(r.comments) AS max_comments,
                  GROUP_CONCAT(DISTINCT r.list_type) AS lists
           FROM rankings r
           JOIN stories s ON r.story_id = s.id
           WHERE r.fetched_at >= ?
           GROUP BY s.id
           ORDER BY max_score DESC""",
        (cutoff,),
    ).fetchall()

    stories = []
    for r in rows:
        title = r["title"] or ""
        tl = title.lower()
        if tl.startswith("show hn"):
            stype = "show_hn"
        elif tl.startswith("ask hn"):
            stype = "ask_hn"
        elif (r["max_comments"] or 0) >= 100:
            stype = "discussion"
        else:
            stype = "article"
        stories.append({
            "id": r["id"],
            "title": title,
            "url": r["url"] or "",
            "domain": r["domain"] or "",
            "author": r["author"] or "",
            "score": r["max_score"],
            "comments": r["max_comments"],
            "best_rank": r["best_rank"],
            "lists": r["lists"],
            "type": stype,
            "hn_url": f"https://news.ycombinator.com/item?id={r['id']}",
        })

    # Apply focus filter if keywords present
    if keywords:
        def matches(s):
            text = (s["title"] + " " + s["domain"]).lower()
            return any(kw in text for kw in keywords)
        stories = [s for s in stories if matches(s)] + [
            s for s in stories if not matches(s)
        ]

    stories = stories[:top_n]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build a compact human-readable block for the EXTRACT prompt
    lines = []
    for s in stories:
        lines.append(
            f"- id={s['id']} type={s['type']} score={s['score']} "
            f"comments={s['comments']} | [{s['title']}]({s['hn_url']}) "
            f"| {s['domain']}"
        )
    stories_block = "\n".join(lines) if lines else "(no stories in window)"

    output = {
        "meta": {
            "date": today,
            "days": days,
            "focus": focus_name,
            "focus_instructions": focus_instructions,
            "total_stories": len(stories),
        },
        "stories": stories,
        "prompts": {
            "extract": EXTRACT_PROMPT.format(
                date=today,
                days=days,
                focus=focus_name,
                focus_instructions=focus_instructions or "(none)",
                total_stories=len(stories),
                stories_block=stories_block,
            ),
            "context_template": CONTEXT_PROMPT,
            "summarize_template": SUMMARIZE_PROMPT.format(
                date=today,
                days=days,
                focus=focus_name,
                total_stories=len(stories),
                contexted="{contexted}",  # left as placeholder; SKILL.md fills it
            ),
        },
    }

    print(json.dumps(output, ensure_ascii=False))


def cmd_save_summary(conn, args):
    content = sys.stdin.read().strip()
    if not content:
        print(json.dumps({"error": "no content on stdin"}))
        return
    days = 1
    focus = "default"
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        elif args[i] == "--focus" and i + 1 < len(args):
            focus = args[i + 1]; i += 2
        else:
            i += 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO summaries (date, focus, content, created_at) VALUES (?, ?, ?, ?)",
        (today, focus, content, now),
    )
    conn.commit()
    print(json.dumps({
        "saved": True, "id": cur.lastrowid, "date": today, "focus": focus,
        "chars": len(content),
    }, ensure_ascii=False))


def cmd_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    rankings = conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0]
    summaries = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    last_fetch = conn.execute("SELECT MAX(fetched_at) FROM rankings").fetchone()[0]
    print(json.dumps({
        "total_stories": total,
        "total_rankings": rankings,
        "total_summaries": summaries,
        "last_fetch": last_fetch,
    }, ensure_ascii=False, indent=2))


def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2 or sys.argv[1] == "query":
        cmd_query(conn, sys.argv[2:] if len(sys.argv) > 2 else [])
    elif sys.argv[1] == "save-summary":
        cmd_save_summary(conn, sys.argv[2:])
    elif sys.argv[1] == "stats":
        cmd_stats(conn)
    else:
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
