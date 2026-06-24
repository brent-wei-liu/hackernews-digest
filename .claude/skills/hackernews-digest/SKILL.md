---
name: hackernews-digest
description: >
  Generate a daily Chinese-language Hacker News digest via a three-step
  Extract → Context → Summarize pipeline. Use when the user asks for an
  HN digest, daily HN report, Hacker News 日报, HN 摘要, or when this task
  fires on schedule.
---

# Hacker News Digest

Generate a Chinese-language digest of Hacker News top + best stories, using a three-step **pipeline** (Extract → Context → Summarize) with isolated subagents and explicit per-step responsibilities. Same shape as the four sibling digest projects (ai-leaders, github-trending, kaggle, job-pipeline) so the patterns transplant cleanly.

**Project root:** the directory containing this SKILL.md's grand-grandparent (`<project>/.claude/skills/hackernews-digest/SKILL.md`). The scheduled-task wrapper or invoking session should `cd` there before running any of the steps below.

## Workflow

Always `cd` to the project root first.

### Web tool budget (Steps 3 / 4 / 5)

Each subagent may use `WebSearch` and `WebFetch` for verification or enrichment. **Hard cap = 14 calls total**, weighted toward Context which does the real fact-grounding:

| Step | Budget | Use it for |
|------|--------|------------|
| Extract (3) | **2** | only when an unfamiliar named entity in a story title or domain makes substantive-vs-filler classification ambiguous — NOT for fact-checking |
| Context (4) | **12** | per-substantive-story background: project README / product page / paper abstract / original thread of a reply / HN comment-page sample |
| Summarize (5) | **0** | pure synthesis from the contexted JSON — no new lookups |

Tell each subagent its budget explicitly; do not exceed the per-step ceiling. Using fewer calls is fine if the content is solid.

### Step 1 — Refresh story data (idempotent)

```bash
cd <project-root> && python3 fetcher.py
```

Pulls HN Firebase API `topstories.json` + `beststories.json`, item-fetches the top 30 from each, upserts `stories` and appends `rankings`. `PRAGMA integrity_check` runs first — aborts if the DB is corrupt rather than compounding damage. Per-story commit + WAL truncate at the end (standard sibling-project hardening). Safe to re-run; rankings are append-only.

### Step 2 — Build orchestration payload

```bash
python3 digest_generate.py query --days 1 --focus default
```

Returns JSON with:
- `meta`: `date` / `days` / `focus` / `focus_instructions` / `total_stories`
- `stories`: deduplicated top N with title / score / comments / type / hn_url
- `prompts.extract`: prompt with placeholders filled (date, days, focus, story list)
- `prompts.context_template`: raw template, expects `{extracted}`
- `prompts.summarize_template`: prompt with date/focus filled, expects `{contexted}`

If `total_stories` is 0, abort — no fresh data.

### Step 3 — Extract (subagent #1, **reads the story list inline**)

This is the sorting step. Classifies each story into `substantive` vs `filler`, grouped by HN type (Show HN / Ask HN / Article / Discussion). Does NOT write summaries.

Spawn an Agent with `subagent_type=general-purpose`. Give it:
- **Prompt**: `prompts.extract` from Step 2 (the formatted story list is already inline)
- **Goal**: emit a strict JSON code block with the four type buckets and substantive/filler arrays

**Append**: "You may use WebSearch up to **2 times** if an unfamiliar entity makes substantive-vs-filler ambiguous. Do NOT use web for fact-checking — that is the Context step's job."

Capture the JSON output; parse the ```json block. Downstream Context receives this JSON only.

### Step 4 — Context (subagent #2, **isolated from raw story list**)

Takes the Extract JSON and adds factual background to each substantive item. Real fact-grounding (project page / paper abstract / HN comment thread) replaces the old Critique step's prose-quality nits.

Spawn an Agent. Give it:
- The Context template with `{extracted}` substituted to the Step 3 JSON output
- **Do NOT pass the raw story list**, **do NOT mention digest_generate.py**

**Append**: "You may use WebSearch / WebFetch up to **12 times, hard ceiling**. Spend more on stories naming a specific tool / paper / company / benchmark; spend less on abstract opinions. For very-high-comment stories (>200💬) it can be worth one WebFetch to the `hn_url` to sample the comment-page discussion."

Capture the JSON output — same structure as Extract with `context` (1-3 Chinese sentences) and `sources` (URL list, possibly empty) on every substantive item.

### Step 5 — Summarize (subagent #3, **no web, no raw stories**)

Reads only the Context JSON. Writes the final Chinese digest. **Not allowed** to invent cross-story "insights" or "今日观察" twists — the mandate is faithful transcription + background.

Spawn an Agent. Give it:
- The Summarize template with `{contexted}` substituted to the Step 4 JSON
- **Do NOT pass anything else**

**Append**: "You have **0** web calls. All facts come from the contexted JSON. If something is missing, omit it — don't invent."

Capture the final text. This is the digest that goes to Step 6 (save) and Step 7 (Gmail).

### Step 6 — Save to DB (MANDATORY)

```bash
TMP=$(mktemp -t hn_digest.XXXXXX.md)
printf '%s' "$FINAL_TEXT" > "$TMP"
SAVE_RESULT=$(python3 digest_generate.py save-summary --days 1 --focus default < "$TMP")
echo "$SAVE_RESULT"   # expect {"saved": true, "id": N, ...}
```

Verify the row landed:

```bash
NEW_ID=$(sqlite3 data/hackernews.db \
  "SELECT id FROM summaries WHERE date='$(date -u +%Y-%m-%d)' ORDER BY id DESC LIMIT 1")
test -n "$NEW_ID" || { echo "VERIFY FAILED"; exit 1; }
echo "saved as summary id=$NEW_ID"
```

**Fallback on failure**: write the markdown to `data/orphan_digest_<date>.md` and prepend `[ORPHAN: not in DB] ` to the Step 7 email subject so the user notices.

### Step 7 — Create Gmail draft

Use the Gmail MCP `create_draft` tool. **Convert markdown to HTML** before calling (`body` = markdown, `htmlBody` = rendered HTML). Recipient = `DIGEST_RECIPIENT` from `<project>/.env`. Subject = `HN Digest YYYY-MM-DD` (with `[ORPHAN: not in DB] ` prefix on save failure).

Renderer requirements match the other digest projects: `#`/`##`/`###` → `<h1>`/`<h2>`/`<h3>`; `- ` → `<ul><li>`; `**text**` → `<strong>`; `` `x` `` → `<code>`; `[t](u)` → `<a>`; HTML-escape source first. Wrap in a minimal styled shell.

### Step 8 — Report

Print briefly:
- Story count + per-list breakdown
- Per-step counts (extract substantive vs filler / context web calls used / summarize chars)
- Saved summary id (or `[ORPHAN]: ...` reason)
- Draft creation status (or error)

## Why this pipeline replaces Draft → Critique → Refine

The old Critique step was tasked with grading "insight quality" of a draft it couldn't trace back to source. On thin HN days (a Tuesday where all 30 top stories are repeats), that pushed Refine to invent harder cross-story claims to placate the critique — producing strained "今日观察" lines unsupported by the data.

The new pipeline factors the work cleanly:
- **Sorting** is its own step, no analysis pressure
- **Fact-grounding** is its own step, real WebFetch on referenced material instead of post-hoc prose-quality nits
- **Writing** has no analytic mandate beyond faithful transcription. If the data is thin, the digest is short — honest

Isolation between steps is preserved (Context can't see raw story list; Summarize can't see raw stories and can't do web). The mechanism that made the old design work — denying each step access to upstream raw material — is kept; the failure mode (forced insight on thin data) is removed.
