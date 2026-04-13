#!/usr/bin/env python3
"""
Hacker News Digest — query, summarize, and manage subscribers.

Usage:
  python3 hackernews_digest.py query [days] [--focus Z]
  python3 hackernews_digest.py save-summary [focus]       # Save summary from stdin
  python3 hackernews_digest.py focus-profiles              # List focus profiles
  python3 hackernews_digest.py add-focus <name> <json>     # Add a focus profile
  python3 hackernews_digest.py subscribers                 # List subscribers
  python3 hackernews_digest.py add-subscriber --email <email> [--name <name>] [--focus <focus>]
  python3 hackernews_digest.py remove-subscriber <email>
  python3 hackernews_digest.py toggle-subscriber <email>
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

from db import get_db, init_db


def cmd_query(conn, args):
    days = 1
    focus_name = "default"

    i = 0
    while i < len(args):
        if args[i] == "--focus":
            focus_name = args[i + 1]; i += 2
        elif args[i].isdigit():
            days = int(args[i]); i += 1
        else:
            i += 1

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (focus_name,)
    ).fetchone()
    focus_rules = json.loads(profile_row["rules"]) if profile_row else {}

    # Get top stories (deduplicated, best rank)
    sql = """
        SELECT s.id, s.title, s.url, s.domain, s.author,
               r.list_type, MIN(r.rank) as best_rank,
               MAX(r.score) as max_score, MAX(r.comments) as max_comments
        FROM rankings r
        JOIN stories s ON r.story_id = s.id
        WHERE r.fetched_at >= ?
        GROUP BY s.id
        ORDER BY max_score DESC
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    stories = []
    for r in rows:
        stories.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "domain": r["domain"],
            "author": r["author"],
            "score": r["max_score"],
            "comments": r["max_comments"],
            "best_rank": r["best_rank"],
            "hn_url": f"https://news.ycombinator.com/item?id={r['id']}",
        })

    # Domain distribution
    domain_sql = """
        SELECT s.domain, COUNT(DISTINCT s.id) as count
        FROM stories s JOIN rankings r ON s.id = r.story_id
        WHERE r.fetched_at >= ? AND s.domain IS NOT NULL AND s.domain != ''
        GROUP BY s.domain ORDER BY count DESC LIMIT 10
    """
    domains = [dict(r) for r in conn.execute(domain_sql, (cutoff,)).fetchall()]

    # Stories with most comments (discussion-worthy)
    discussion_sql = """
        SELECT s.id, s.title, s.url, s.domain, MAX(r.comments) as comments, MAX(r.score) as score
        FROM rankings r JOIN stories s ON r.story_id = s.id
        WHERE r.fetched_at >= ? AND r.comments > 50
        GROUP BY s.id
        ORDER BY comments DESC
        LIMIT 10
    """
    hot_discussions = [dict(r) for r in conn.execute(discussion_sql, (cutoff,)).fetchall()]

    # Repeat appearances (stories on multiple lists or fetches)
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

    output = {
        "query": {
            "days_back": days,
            "cutoff": cutoff,
            "focus": focus_name,
        },
        "focus_rules": focus_rules,
        "total_stories": len(stories),
        "stories": stories,
        "hot_discussions": hot_discussions,
        "domain_distribution": domains,
        "repeat_appearances": repeats,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_save_summary(conn, args):
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "no content on stdin"}')
        return
    focus = args[0] if args else "default"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO summaries (date, focus, content, created_at) VALUES (?, ?, ?, ?)",
        (today, focus, content, now),
    )
    conn.commit()
    print(json.dumps({"saved": True, "date": today, "focus": focus}))


def cmd_focus_profiles(conn):
    rows = conn.execute("SELECT name, description, rules FROM focus_profiles ORDER BY name").fetchall()
    for r in rows:
        rules = json.loads(r["rules"])
        kw = ", ".join(rules.get("keywords", [])[:5]) or "all"
        print(f"  {r['name']}: {r['description']} (keywords: {kw}...)")


def cmd_add_focus(conn, args):
    if len(args) < 2:
        print('Usage: add-focus <name> <json-rules>')
        return
    name, rules = args[0], args[1]
    now = datetime.now(timezone.utc).isoformat()
    try:
        json.loads(rules)
    except json.JSONDecodeError:
        print('{"error": "invalid JSON"}')
        return
    conn.execute(
        "INSERT OR REPLACE INTO focus_profiles (name, description, rules, created_at) VALUES (?, ?, ?, ?)",
        (name, "", rules, now),
    )
    conn.commit()
    print(json.dumps({"added": name}))


def cmd_subscribers(conn):
    rows = conn.execute(
        "SELECT name, email, focus, enabled FROM subscribers ORDER BY name"
    ).fetchall()
    if not rows:
        print("No subscribers yet. Use: add-subscriber --email <email> [--name <name>] [--focus <focus>]")
        return
    for r in rows:
        status = "✅" if r["enabled"] else "⏸️"
        name = r["name"] or "(no name)"
        print(f"  {status} {r['email']:35s}  {name:20s}  focus={r['focus']}")


def cmd_add_subscriber(conn, args):
    email = None
    name = None
    focus = "default"

    i = 0
    while i < len(args):
        if args[i] == "--email" and i + 1 < len(args):
            email = args[i + 1]; i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            name = args[i + 1]; i += 2
        elif args[i] == "--focus" and i + 1 < len(args):
            focus = args[i + 1]; i += 2
        else:
            if not email and "@" in args[i]:
                email = args[i]
            i += 1

    if not email:
        print('Usage: add-subscriber --email <email> [--name <name>] [--focus <focus>]')
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO subscribers (name, email, focus, created_at) VALUES (?, ?, ?, ?)",
            (name, email, focus, now),
        )
        conn.commit()
        print(json.dumps({"added": email, "name": name, "focus": focus}))
    except sqlite3.IntegrityError:
        print(json.dumps({"error": f"{email} already subscribed"}))


def cmd_remove_subscriber(conn, args):
    if not args:
        print('Usage: remove-subscriber <email>')
        return
    conn.execute("DELETE FROM subscribers WHERE email = ?", (args[0],))
    conn.commit()
    print(json.dumps({"removed": args[0]}))


def cmd_toggle_subscriber(conn, args):
    if not args:
        print('Usage: toggle-subscriber <email>')
        return
    row = conn.execute("SELECT enabled FROM subscribers WHERE email = ?", (args[0],)).fetchone()
    if not row:
        print(json.dumps({"error": f"{args[0]} not found"}))
        return
    new_val = 0 if row["enabled"] else 1
    conn.execute("UPDATE subscribers SET enabled = ? WHERE email = ?", (new_val, args[0]))
    conn.commit()
    print(json.dumps({"email": args[0], "status": "enabled" if new_val else "disabled"}))


def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "query": lambda: cmd_query(conn, args),
        "save-summary": lambda: cmd_save_summary(conn, args),
        "focus-profiles": lambda: cmd_focus_profiles(conn),
        "add-focus": lambda: cmd_add_focus(conn, args),
        "subscribers": lambda: cmd_subscribers(conn),
        "add-subscriber": lambda: cmd_add_subscriber(conn, args),
        "remove-subscriber": lambda: cmd_remove_subscriber(conn, args),
        "toggle-subscriber": lambda: cmd_toggle_subscriber(conn, args),
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
