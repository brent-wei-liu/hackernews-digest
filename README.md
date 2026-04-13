# Hacker News Digest

Track and summarize Hacker News top/best stories using the official API + SQLite. Designed for use with [OpenClaw](https://github.com/openclaw/openclaw) but works standalone.

## What it does

- Pulls top 30 stories from HN top and best lists (official Firebase API, free)
- Stores all stories and rankings in a local SQLite database
- Tracks hot discussions (high comment counts)
- Detects stories appearing on multiple lists or multiple days
- Supports focus profiles (AI/ML, startup, systems, etc.)
- Delivers daily digests to email subscribers

## Requirements

- Python 3.9+
- No external dependencies (uses standard library only)

## Quick Start

```bash
# Fetch top + best stories
python3 hackernews_fetch.py

# Query today's stories, AI/ML focus
python3 hackernews_digest.py query 1 --focus ai-ml

# List focus profiles
python3 hackernews_digest.py focus-profiles

# Quick stats
python3 hackernews_fetch.py stats 7
```

## Files

| File | Responsibility |
|------|---------------|
| `db.py` | Shared database schema and connection |
| `hackernews_fetch.py` | Pull HN API → store in SQLite |
| `hackernews_digest.py` | Query data, manage focus profiles and subscribers |

## Commands

### hackernews_fetch.py (data collection)

| Command | Description |
|---------|-------------|
| `fetch` | Pull top + best stories → store in SQLite |
| `fetch --report-hour H` | Only output report when local hour == H |
| `stats [days]` | Quick stats |

### hackernews_digest.py (analysis & delivery)

| Command | Description |
|---------|-------------|
| `query [days] [--focus Z]` | Query stories, output JSON |
| `save-summary [focus]` | Save summary text from stdin |
| `focus-profiles` | List all focus profiles |
| `add-focus <name> <json>` | Add a custom focus profile |
| `subscribers` | List all subscribers |
| `add-subscriber --email <email> [--name <name>] [--focus <focus>]` | Add subscriber |
| `remove-subscriber <email>` | Remove subscriber |
| `toggle-subscriber <email>` | Enable/disable subscriber |

## Focus Profiles

| Profile | Keywords | Description |
|---------|----------|-------------|
| `default` | All | No filter |
| `ai-ml` | ai, llm, gpt, claude... | AI/ML focused |
| `startup` | startup, yc, funding... | Startup/business |
| `systems` | rust, go, linux, database... | Systems/infra |

## Database

SQLite database at `data/hackernews.db` with 5 tables:

- **stories** — unique stories (title, URL, domain, author, score, comments)
- **rankings** — rank snapshots per fetch (list type, rank, score, comments)
- **summaries** — generated digest history
- **focus_profiles** — saved focus configurations
- **subscribers** — email subscribers with per-person focus

## Architecture

```
hackernews_fetch.py (2x/day)     hackernews_digest.py (1x/day, 9pm)
┌──────────────────┐            ┌──────────────────┐
│ HN API           │            │ query DB         │
│ top 30 + best 30 │  SQLite    │ LLM draft        │
│                  │ ────────→  │ LLM review       │
└──────────────────┘  (db.py)   │ LLM final        │
                                │ save + email     │
                                └──────────────────┘
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HN_DIGEST_DB_PATH` | `./data/hackernews.db` | Override database location |

## License

MIT
