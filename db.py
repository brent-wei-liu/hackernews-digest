"""Shared database setup for Hacker News Digest.

Carries the hardening lessons accumulated across the four sibling digest
projects: WAL + busy_timeout=5000 to absorb concurrent api.py + fetcher
write windows, additive ALTER TABLE migrations only (no schema-rewrite
framework), and partial indexes for the starred / is_read hot paths.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get(
    "HN_DIGEST_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "hackernews.db"),
)

DEFAULT_FOCUS_PROFILES = [
    ("default", "All topics", json.dumps({
        "keywords": [],
        "instructions": "",
        "top_n": 20,
    })),
    ("ai-ml", "AI/ML focused", json.dumps({
        "keywords": [
            "ai", "ml", "llm", "gpt", "claude", "openai", "anthropic", "model",
            "neural", "transformer", "agent", "fine-tune", "training", "inference",
            "diffusion", "embedding",
        ],
        "instructions": "重点分析 AI/ML 相关帖子的技术深度和社区反应",
        "top_n": 20,
    })),
    ("startup", "Startup/business focused", json.dumps({
        "keywords": [
            "startup", "yc", "funding", "launch", "saas", "revenue", "founder",
            "vc", "seed", "series",
        ],
        "instructions": "重点分析创业相关帖子，关注商业模式和融资动态",
        "top_n": 20,
    })),
    ("systems", "Systems/infra focused", json.dumps({
        "keywords": [
            "rust", "go", "linux", "kernel", "database", "distributed",
            "performance", "compiler", "os", "network", "tcp", "memory",
        ],
        "instructions": "重点分析系统编程和基础设施相关内容",
        "top_n": 20,
    })),
]


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # 5s wait when a concurrent writer (api.py star/read toggle vs fetcher
    # cron) holds the lock — default 0 turns every overlap into instant
    # SQLITE_BUSY. Standard across the digest fleet.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stories (
            id          INTEGER PRIMARY KEY,
            title       TEXT NOT NULL,
            url         TEXT,
            domain      TEXT,
            author      TEXT,
            score       INTEGER DEFAULT 0,
            comments    INTEGER DEFAULT 0,
            type        TEXT DEFAULT 'story',
            time        INTEGER,
            first_seen  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rankings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id    INTEGER NOT NULL,
            list_type   TEXT NOT NULL,
            rank        INTEGER,
            score       INTEGER,
            comments    INTEGER,
            fetched_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS summaries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            focus       TEXT DEFAULT 'default',
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS focus_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            rules       TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            email       TEXT UNIQUE,
            focus       TEXT DEFAULT 'default',
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_rankings_story_date ON rankings(story_id, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_rankings_list ON rankings(list_type, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_summaries_date ON summaries(date);
    """)

    if conn.execute("SELECT COUNT(*) FROM focus_profiles").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO focus_profiles (name, description, rules, created_at) VALUES (?, ?, ?, ?)",
            [(n, d, r, now) for n, d, r in DEFAULT_FOCUS_PROFILES],
        )

    # ── Additive migrations ────────────────────────────────────────────
    # stories: starred / starred_at — lets the UI bookmark interesting
    # stories. Partial index keeps "show starred only" cheap.
    s_cols = {r[1] for r in conn.execute("PRAGMA table_info(stories)").fetchall()}
    for col, typedef in [
        ("starred",    "INTEGER NOT NULL DEFAULT 0"),
        ("starred_at", "TEXT DEFAULT NULL"),
    ]:
        if col not in s_cols:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {col} {typedef}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stories_starred "
        "ON stories(starred) WHERE starred = 1"
    )

    # summaries: is_read / read_at — same shape as the other 4 digest
    # projects so the same UI patterns transplant cleanly.
    sm_cols = {r[1] for r in conn.execute("PRAGMA table_info(summaries)").fetchall()}
    for col, typedef in [
        ("is_read", "INTEGER NOT NULL DEFAULT 0"),
        ("read_at", "TEXT DEFAULT NULL"),
    ]:
        if col not in sm_cols:
            conn.execute(f"ALTER TABLE summaries ADD COLUMN {col} {typedef}")

    conn.commit()


def star_story(conn, story_id):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE stories SET starred = 1, starred_at = ? WHERE id = ?",
        (now, story_id),
    )
    conn.commit()
    return cur.rowcount > 0


def unstar_story(conn, story_id):
    cur = conn.execute(
        "UPDATE stories SET starred = 0, starred_at = NULL WHERE id = ?",
        (story_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_summary_read(conn, summary_id):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE summaries SET is_read = 1, read_at = ? WHERE id = ?",
        (now, summary_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_summary_unread(conn, summary_id):
    cur = conn.execute(
        "UPDATE summaries SET is_read = 0, read_at = NULL WHERE id = ?",
        (summary_id,),
    )
    conn.commit()
    return cur.rowcount > 0


if __name__ == "__main__":
    conn = get_db()
    init_db(conn)
    print(f"initialized {DB_PATH}")
    for t in ("stories", "rankings", "summaries", "focus_profiles", "subscribers"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n} rows")
