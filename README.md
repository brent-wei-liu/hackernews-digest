# Hacker News Digest

追踪 Hacker News Top 30 + Best 30 热门帖子，通过 Firebase API 抓取、SQLite 存储，由 Hermes cron job 编排三步隔离反思流水线（Draft → Critique → Refine）生成高质量中文摘要。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Hermes Cron Jobs                                           │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │ HN Fetch (4x/day)   │    │ Daily Digest (1x/day 22:00)│ │
│  │ 8:00 静默            │    │                             │ │
│  │ 14:00 静默           │    │  script: digest query       │ │
│  │ 18:00 静默           │    │       ↓ JSON 注入           │ │
│  │ 21:00 汇报           │    │  Agent 编排 delegate_task   │ │
│  │                     │    │       ↓                     │ │
│  │ HN Firebase API     │    │  ┌──────────────────────┐   │ │
│  │ top 30 + best 30    │    │  │ Subagent 1: Draft    │   │ │
│  │       ↓             │    │  │ (看得到原始帖子)       │   │ │
│  │    SQLite DB        │    │  └──────────┬───────────┘   │ │
│  └─────────────────────┘    │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 2: Critique │   │ │
│                             │  │ (只看得到初稿，隔离) │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 3: Refine   │   │ │
│                             │  │ (初稿 + 审稿意见)    │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Step 4: Save Summary │   │ │
│                             │  │ (终稿写入 SQLite DB) │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │     最终摘要 → Telegram     │ │
│                             └─────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## 文件结构

```
~/.hermes/hermes-agent/hackernews-digest/
├── db.py                   # 数据层：SQLite schema、连接、Focus Profile 默认值
├── hackernews_fetch.py     # 抓取层：HN Firebase API 抓取、SQLite 存储、统计
├── hackernews_digest.py    # 查询层：数据查询、Focus Profile 管理、订阅者管理
├── digest_generate.py      # 摘要层：数据加载 + 三步 Prompt 模板输出
├── data/
│   └── hackernews.db       # SQLite 数据库（830+ stories, 1920+ rankings, 15 summaries）
└── README.md

~/.hermes/scripts/
├── hn_fetch.py             # Cron 包装：调用 hackernews_fetch.py fetch
└── hn_digest.py            # Cron 包装：调用 digest_generate.py query
```

## 追踪的内容

通过 HN 官方 Firebase API（`hacker-news.firebaseio.com`）抓取两个榜单：

| 榜单 | API 端点 | 抓取数量 |
|------|----------|----------|
| Top Stories | `/v0/topstories.json` | 前 30 条 |
| Best Stories | `/v0/beststories.json` | 前 30 条 |

每次抓取自动去重（按 story ID），记录排名快照用于追踪热度变化。

## 核心文件说明

### db.py

共享数据库层。定义 SQLite schema、连接管理、默认 Focus Profile 初始化。

**特性：**
- WAL 模式 + 外键约束
- 4 个默认 Focus Profile（default, ai-ml, startup, systems）
- 支持 `HN_DIGEST_DB_PATH` 环境变量覆盖数据库路径

### hackernews_fetch.py

纯 Python 标准库，零外部依赖。通过 HN Firebase API 抓取帖子，存入 SQLite。

**命令：**

| 命令 | 说明 |
|------|------|
| `fetch` | 抓取 top + best 各 30 条帖子，存入 DB |
| `fetch --report-hour H` | 抓取数据，只在本地小时 == H 时输出完整报告 |
| `stats [天数]` | 统计信息 |

**特性：**
- Firebase API 免费、稳定，无需 API key
- story ID 自动去重，重复抓取只更新 rankings 快照
- `--report-hour` 支持静默抓取（非报告时间只存数据不输出）
- 自动提取 domain 用于来源分析

### hackernews_digest.py

数据查询 + Focus Profile 管理 + 订阅者管理。

**命令：**

| 命令 | 说明 |
|------|------|
| `query [天数] [--focus Z]` | 查询帖子，输出 JSON（按分数排序） |
| `save-summary [focus]` | 从 stdin 保存摘要到 DB |
| `focus-profiles` | 列出所有 Focus Profile |
| `add-focus <名> <JSON>` | 添加自定义 Focus Profile |
| `subscribers` | 列出订阅者 |
| `add-subscriber --email <email> [--name <name>] [--focus <focus>]` | 添加订阅者 |
| `remove-subscriber <email>` | 删除订阅者 |
| `toggle-subscriber <email>` | 启用/暂停订阅者 |

**特性：**
- 查询结果包含热门讨论（评论数 >50）和持续热门（多榜单/多日出现）
- 输出 Focus Profile 规则供 digest_generate.py 使用
- domain 分布统计

### digest_generate.py

数据加载 + 三步 Prompt 模板输出。不调用 LLM，LLM 调用由 Hermes cron agent 通过 delegate_task 完成。

**命令：**

| 命令 | 说明 |
|------|------|
| `query [--days N] [--focus Z]` | 输出帖子数据 + 三步 Prompt 模板 JSON |
| `save-summary [--days N] [--focus Z]` | 从 stdin 保存摘要到 DB |
| `stats` | 简要统计 |

**query 输出 JSON 结构：**
```json
{
  "meta": { "date", "days", "focus", "focus_instructions", "total_stories", "focused_stories", "hot_discussions" },
  "stories": [ "按分数排序的帖子数据" ],
  "hot_discussions": [ "评论数 >50 的热门讨论" ],
  "domain_distribution": [ "来源域名分布" ],
  "repeat_appearances": [ "多榜单/多日出现的帖子" ],
  "prompts": {
    "draft": "完整的初稿 Prompt（帖子数据已嵌入）",
    "critique_template": "审稿模板（{draft} 占位符）",
    "refine_template": "精修模板（{draft} + {critique} 占位符）"
  }
}
```

## 三步隔离反思设计

核心思想：审稿人看不到原始数据，只能评估摘要质量。

| 步骤 | Subagent | 输入 | 输出 | 隔离 |
|------|----------|------|------|------|
| Draft | #1 | 原始帖子 + 格式指令 | 初稿 | 看得到原始帖子 |
| Critique | #2 | 只有初稿 | 审稿意见 + A/B/C 评分 | 看不到原始帖子 |
| Refine | #3 | 初稿 + 审稿意见 | 终稿 | 看不到原始帖子 |

每个 subagent 通过 Hermes `delegate_task` 创建，天然上下文隔离。

## Focus Profiles

控制摘要如何分配关注度。基于关键词匹配帖子标题和域名。

| Profile | 关键词 | 说明 | 非重点处理 |
|---------|--------|------|-----------|
| default | 全部 | 无过滤，均衡关注所有帖子 | normal |
| ai-ml | ai, llm, gpt, claude, openai, anthropic, model, neural... | AI/ML 相关技术深度和社区反应 | brief |
| startup | startup, yc, funding, launch, saas, revenue, founder... | 创业商业模式和融资动态 | brief |
| systems | rust, go, linux, kernel, database, distributed, performance... | 系统编程和基础设施 | brief |

自定义示例：
```bash
python3 hackernews_digest.py add-focus myprofile '{
  "keywords": ["ai", "llm", "agent"],
  "instructions": "重点分析 AI Agent 框架和应用",
  "top_n": 15
}'
```

## 数据库结构

SQLite（`data/hackernews.db`），5 张表：

| 表 | 说明 |
|----|------|
| stories | HN 帖子（id, title, url, domain, author, score, comments, type, time, first_seen） |
| rankings | 排名快照，每次抓取记录（story_id, list_type, rank, score, comments, fetched_at） |
| summaries | 生成的摘要历史（date, focus, content, created_at） |
| focus_profiles | Focus 配置（name, description, rules JSON, created_at） |
| subscribers | 订阅者（name, email, focus, enabled, created_at） |

## Cron Jobs

| Job | 时间 (PST) | 说明 |
|-----|-----------|------|
| HN Fetch | 8:00, 14:00, 18:00, 21:00 | 抓取 HN top+best 各 30 条，21 点发汇报 |
| HN Daily Digest | 22:00 | 三步反思生成 ai-ml Focus 摘要，保存到 DB，发送到 Telegram |

## 手动使用

```bash
cd ~/.hermes/hermes-agent/hackernews-digest

# 抓取最新帖子
python3 hackernews_fetch.py fetch

# 查看统计
python3 hackernews_fetch.py stats 7
python3 digest_generate.py stats

# 查询 AI/ML 方向最近 1 天
python3 hackernews_digest.py query 1 --focus ai-ml

# 生成摘要 JSON（含三步 Prompt）
python3 digest_generate.py query --days 1 --focus ai-ml

# 列出 Focus Profile
python3 hackernews_digest.py focus-profiles

# 列出订阅者
python3 hackernews_digest.py subscribers
```

## 迁移说明

从 OpenClaw workspace 迁移而来。主要改动：

- 新增 `db.py` 共享数据库层，从各文件中抽离 schema 定义
- 新增 `digest_generate.py`，输出 JSON + Prompt 模板，LLM 调用由 Hermes delegate_task 完成
- `hackernews_fetch.py` 和 `hackernews_digest.py` 改为引用 `db.py`
- Cron 脚本必须是 .py（Hermes scheduler 固定用 Python 解释器执行）
- Cron 脚本必须放在 `~/.hermes/scripts/`（路径校验限制）
- GitHub: https://github.com/brent-wei-liu/hackernews-digest

## 已知限制

- Firebase API 单条抓取（逐个 item），30×2=60 条需要约 30 秒
- 三步 delegate_task 串行执行，生成摘要需要几分钟
- digest_generate.py query 输出较大（包含完整帖子数据 + 三步 Prompt），JSON 体积随帖子数增长
- Focus Profile 关键词匹配仅基于标题和域名，不分析帖子正文内容
