#!/usr/bin/env python3
"""
Hacker News Digest Generator — outputs story data + 3-step prompt templates.

Designed for Hermes cron: outputs JSON to stdout, agent orchestrates
Draft → Critique → Refine via delegate_task.

Usage:
  python3 digest_generate.py query [--days 1] [--focus ai-ml]
  python3 digest_generate.py save-summary [--days 1] [--focus default]  # stdin
  python3 digest_generate.py stats
"""

import json
import sys
from datetime import datetime, timezone, timedelta

from db import get_db, init_db


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

    # Focus profile
    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (focus_name,)
    ).fetchone()
    focus_rules = json.loads(profile_row["rules"]) if profile_row else {}
    focus_instructions = focus_rules.get("instructions", "")
    keywords = focus_rules.get("keywords", [])
    top_n = focus_rules.get("top_n", 20)

    # Top stories (deduplicated)
    sql = """
        SELECT s.id, s.title, s.url, s.domain, s.author,
               MIN(r.rank) as best_rank,
               MAX(r.score) as max_score, MAX(r.comments) as max_comments,
               GROUP_CONCAT(DISTINCT r.list_type) as lists
        FROM rankings r
        JOIN stories s ON r.story_id = s.id
        WHERE r.fetched_at >= ?
        GROUP BY s.id
        ORDER BY max_score DESC
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    stories = []
    for r in rows:
        story = {
            "id": r["id"],
            "title": r["title"],
            "url": r["url"] or "",
            "domain": r["domain"] or "",
            "author": r["author"] or "",
            "score": r["max_score"],
            "comments": r["max_comments"],
            "best_rank": r["best_rank"],
            "lists": r["lists"],
            "hn_url": f"https://news.ycombinator.com/item?id={r['id']}",
        }
        stories.append(story)

    # Filter by focus keywords if any
    if keywords:
        def matches(s):
            text = (s["title"] + " " + s["domain"]).lower()
            return any(kw in text for kw in keywords)
        focused = [s for s in stories if matches(s)]
        other = [s for s in stories if not matches(s)]
    else:
        focused = stories
        other = []

    # Hot discussions
    discussion_sql = """
        SELECT s.id, s.title, s.url, s.domain, MAX(r.comments) as comments, MAX(r.score) as score
        FROM rankings r JOIN stories s ON r.story_id = s.id
        WHERE r.fetched_at >= ? AND r.comments > 50
        GROUP BY s.id
        ORDER BY comments DESC
        LIMIT 10
    """
    hot_discussions = [dict(r) for r in conn.execute(discussion_sql, (cutoff,)).fetchall()]

    # Repeat appearances
    repeat_sql = """
        SELECT s.id, s.title, COUNT(DISTINCT r.list_type) as list_count,
               COUNT(DISTINCT DATE(r.fetched_at)) as days_on_list
        FROM rankings r JOIN stories s ON r.story_id = s.id
        WHERE r.fetched_at >= ?
        GROUP BY s.id
        HAVING list_count > 1 OR days_on_list > 1
        ORDER BY list_count DESC, days_on_list DESC
        LIMIT 10
    """
    repeat_cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    repeats = [dict(r) for r in conn.execute(repeat_sql, (repeat_cutoff,)).fetchall()]

    # Domain distribution
    domain_sql = """
        SELECT s.domain, COUNT(DISTINCT s.id) as count
        FROM stories s JOIN rankings r ON s.id = r.story_id
        WHERE r.fetched_at >= ? AND s.domain IS NOT NULL AND s.domain != ''
        GROUP BY s.domain ORDER BY count DESC LIMIT 10
    """
    domains = [dict(r) for r in conn.execute(domain_sql, (cutoff,)).fetchall()]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build story text for prompts
    story_lines = []
    for i, s in enumerate(focused[:top_n], 1):
        story_lines.append(
            f"{i}. [{s['title']}]({s['hn_url']}) — {s['score']}⬆ {s['comments']}💬 | {s['domain']}"
        )
    if other and not keywords:
        pass  # no separation needed
    elif other:
        story_lines.append(f"\n--- 其他热门（非 {focus_name} 重点）---")
        for i, s in enumerate(other[:10], len(focused[:top_n]) + 1):
            story_lines.append(
                f"{i}. [{s['title']}]({s['hn_url']}) — {s['score']}⬆ {s['comments']}💬 | {s['domain']}"
            )

    discussion_lines = []
    for d in hot_discussions[:5]:
        discussion_lines.append(f"- {d['title']} ({d['comments']}💬, {d['score']}⬆)")

    repeat_lines = []
    for r in repeats[:5]:
        repeat_lines.append(f"- {r['title']} (出现在 {r['list_count']} 个列表, 连续 {r['days_on_list']} 天)")

    stories_text = "\n".join(story_lines)
    discussions_text = "\n".join(discussion_lines) if discussion_lines else "无"
    repeats_text = "\n".join(repeat_lines) if repeat_lines else "无"

    # 3-step prompts
    draft_prompt = f"""你是 Hacker News 中文日报的撰稿人。请根据以下数据撰写一份精炼的中文摘要。

日期：{today}
Focus: {focus_name}
{f'Focus 说明：{focus_instructions}' if focus_instructions else ''}

## 今日热门帖子

{stories_text}

## 热门讨论

{discussions_text}

## 持续热门（多日/多榜单）

{repeats_text}

## 要求

1. 用中文撰写，标题保留英文原文
2. 按主题分类（如 AI/ML、系统、创业、开源等），每个类别 3-5 条
3. 每条包含：标题（带 HN 链接）、一句话中文摘要、分数和评论数
4. 特别标注评论数 >100 的热门讨论
5. 末尾加一段 "今日观察"（2-3 句话总结趋势）
6. 总长控制在 800-1200 字"""

    critique_template = """你是一位资深科技编辑。请审阅以下 Hacker News 中文日报初稿，给出改进建议。

## 初稿

{draft}

## 审稿要求

1. 分类是否合理？有没有更好的分组方式？
2. 摘要是否准确传达了原帖重点？有没有误导性描述？
3. "今日观察" 是否有洞察力，还是只是简单罗列？
4. 文字是否简洁流畅？有没有冗余或口水话？
5. 有没有遗漏重要的热门话题？

请按 A/B/C 评级：
- A：可以直接发布
- B：需要小幅修改
- C：需要大幅重写

给出具体修改建议，列出需要改的地方。"""

    refine_template = """你是 Hacker News 中文日报的终稿编辑。请根据审稿意见修改初稿，生成终稿。

## 初稿

{draft}

## 审稿意见

{critique}

## 要求

1. 根据审稿意见逐条修改
2. 保持原有格式和链接
3. 如果审稿评级为 A，只做微调
4. 如果评级为 B/C，按建议大幅修改
5. 终稿直接输出，不要包含修改说明"""

    output = {
        "meta": {
            "date": today,
            "days": days,
            "focus": focus_name,
            "focus_instructions": focus_instructions,
            "total_stories": len(stories),
            "focused_stories": len(focused),
            "hot_discussions": len(hot_discussions),
        },
        "stories": stories[:top_n],
        "hot_discussions": hot_discussions,
        "domain_distribution": domains,
        "repeat_appearances": repeats,
        "prompts": {
            "draft": draft_prompt,
            "critique_template": critique_template,
            "refine_template": refine_template,
        },
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_save_summary(conn, args):
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "no content on stdin"}')
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
    conn.execute(
        "INSERT INTO summaries (date, focus, content, created_at) VALUES (?, ?, ?, ?)",
        (today, focus, content, now),
    )
    conn.commit()
    print(json.dumps({"saved": True, "date": today, "focus": focus}))


def cmd_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    rankings = conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0]
    summaries = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    last_fetch = conn.execute(
        "SELECT MAX(fetched_at) FROM rankings"
    ).fetchone()[0]
    print(json.dumps({
        "total_stories": total,
        "total_rankings": rankings,
        "total_summaries": summaries,
        "last_fetch": last_fetch,
    }, indent=2))


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
