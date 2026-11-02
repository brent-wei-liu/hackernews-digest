# Hacker News Digest

📰 A daily Chinese-language Hacker News digest, in a 1980s newsroom-terminal aesthetic.

Revived from a Hermes-era project and brought into line with the four sibling digest pipelines (`ai-leaders-digest`, `github-trending-digest`, `kaggle-digest`, `job-pipeline`):

- HN Firebase API → SQLite (top 30 + best 30, daily)
- Three-step pipeline (Extract → Context → Summarize) replaces the older Draft → Critique → Refine reflection — no forced "今日观察" twists on thin news days
- Single-file FastAPI on **port 8084**, retro phosphor-green + HN-orange UI
- Read/unread mark, ★ star, back-to-top — same UX as the other digests
- Email delivery via Gmail SMTP (`email_sender.py`), falling back to a Gmail draft via the Gmail MCP

## Quick start

```bash
cd ~/hackernews-digest
pip install -r requirements.txt
cp .env.example .env       # set DIGEST_RECIPIENT, GMAIL_SMTP_USER, GMAIL_APP_PASSWORD

# Initialize the DB schema
python3 db.py

# Pull HN top + best (~2 min)
python3 fetcher.py fetch

# Generate a digest via the skill (invokes 3 subagents)
# In a Claude Code session:
#   /skill hackernews-digest

# Start the UI
python3 api.py
# → http://127.0.0.1:8084  (or http://<mac-ip>:8084 from LAN/phone)
```

## Endpoints

| Path | Notes |
|---|---|
| `GET /` | Static UI (`static/index.html`) |
| `GET /api/stats` | story / starred / digest / unread counts + last fetch |
| `GET /api/stories?list_type=topstories\|beststories\|all&q=&starred=&days=` | deduplicated story list |
| `POST /api/stories/{id}/star` / `/unstar` | toggle star |
| `GET /api/digests` | summary list with `is_read` |
| `GET /api/digests/{id}` | full digest content |
| `POST /api/digests/{id}/read` / `/unread` | mark read state |

## Scheduled tasks

| Task | Cron (PT) | Purpose |
|---|---|---|
| `hackernews-fetch` | 09:50 daily | pull top + best stories into SQLite |
| `hackernews-digest` | 12:00 daily | run the Extract → Context → Summarize pipeline + save + send via `email_sender.py` |

Registered via the `mcp__scheduled-tasks__create_scheduled_task` MCP. Mirrored to both `~/.claude/scheduled-tasks/<task>/SKILL.md` and `~/Documents/Claude/Scheduled/<task>/SKILL.md`.

## Email delivery

Digests are sent by `email_sender.py` through `smtp.gmail.com:587` (STARTTLS) as plain text + rendered HTML, with no review step. Set `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` in `.env` (see `.env.example`); the App Password comes from https://myaccount.google.com/apppasswords and requires 2-Step Verification. Preview without sending using `python3 email_sender.py --dry-run`. Each digest date is sent once (logged in `data/email_sent.jsonl`); use `--force` to resend. If sending fails, the skill falls back to a Gmail draft via the Gmail MCP `create_draft` tool when that connector is available.

## Auto-start on boot

The web UI runs under launchd via `~/Library/LaunchAgents/local.hackernews-digest.plist`:

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/local.hackernews-digest.plist  # install
launchctl bootout   gui/$UID/local.hackernews-digest                                # stop
launchctl kickstart -k gui/$UID/local.hackernews-digest                              # restart
```

Logs: `/tmp/hackernews-digest.log` + `/tmp/hackernews-digest.err.log`.

## Architecture

```
~/hackernews-digest/
├── db.py                  # schema + busy_timeout=5000 + additive ALTER migrations
├── fetcher.py             # HN Firebase API → SQLite (per-story commit, integrity_check)
├── digest_generate.py     # query payload + Extract/Context/Summarize prompt templates
├── hackernews_digest.py   # legacy CLI (subscribers / focus profiles)
├── email_sender.py        # send a saved digest via Gmail SMTP (stdlib only)
├── api.py                 # FastAPI on port 8084
├── static/index.html      # 1980s newsroom terminal UI (single file)
├── data/                  # SQLite DB + per-day digest markdown
├── .claude/skills/hackernews-digest/SKILL.md     # the orchestration skill
└── tests/ui/              # Playwright UI regression suite
```

## Testing

```bash
python3 api.py &           # in another terminal
python3 -m pytest tests/ui/ -v

python3 -m pytest tests/test_email_sender.py -v   # no server or network needed
```

Tests skip cleanly if the DB hasn't been populated yet — useful right after first install.
