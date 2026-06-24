#!/usr/bin/env python3
"""Hacker News fetch — pull topstories + beststories and store in SQLite.

Usage:
  python3 fetcher.py                       # fetch top + best
  python3 fetcher.py --report-hour H       # only emit full JSON when local
                                           # hour matches; otherwise print
                                           # minimal status (for cron noise
                                           # reduction)
  python3 fetcher.py stats [days]          # quick stats

Hardening (carried over from the ai-leaders / sibling-project incidents):
  - PRAGMA integrity_check at fetch entry; abort if corrupt rather than
    write into a damaged DB and compound the lossiness of recovery
  - Per-story commit so the write lock + WAL are released promptly
    instead of held across the whole 60-item sweep
  - PRAGMA wal_checkpoint(TRUNCATE) at end so WAL stays bounded
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import urllib.request

from db import get_db, init_db

HN_API = "https://hacker-news.firebaseio.com/v0"
LISTS = ["topstories", "beststories"]
MAX_ITEMS = 30  # top 30 per list — keeps the daily sweep < ~2 minutes


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HN-Digest/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_item(item_id):
    return fetch_json(f"{HN_API}/item/{item_id}.json")


def extract_domain(url):
    if not url:
        return None
    try:
        d = urlparse(url).netloc
        if d.startswith("www."):
            d = d[4:]
        return d or None
    except Exception:
        return None


def cmd_fetch(conn, args=None):
    report_hour = None
    if args:
        for i, a in enumerate(args):
            if a == "--report-hour" and i + 1 < len(args):
                report_hour = int(args[i + 1])

    # Pre-flight: if the DB is corrupt, abort before piling fresh writes
    # on top of broken indexes. PRAGMA integrity_check is ~50ms on a
    # healthy DB and catches the page-level damage we used to see in
    # ai-leaders before per-iter commits stabilized it.
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        check_result = check[0] if check else "missing"
    except sqlite3.DatabaseError as e:
        check_result = f"error: {e}"
    if check_result != "ok":
        sys.stderr.write(
            f"FATAL: hackernews.db PRAGMA integrity_check returned "
            f"{check_result!r}; aborting fetch to avoid compounding "
            f"corruption. Run dump+restore before next fetch.\n"
        )
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    stats = {"lists": {}, "new_stories": 0, "total_rankings": 0, "failed": []}

    for list_type in LISTS:
        try:
            ids = fetch_json(f"{HN_API}/{list_type}.json")[:MAX_ITEMS]
        except Exception as e:
            stats["failed"].append({"list": list_type, "error": str(e)})
            continue

        count = 0
        for rank, item_id in enumerate(ids, 1):
            try:
                item = fetch_item(item_id)
                if not item or item.get("type") not in ("story", "job", "poll"):
                    continue

                title = item.get("title", "")
                url = item.get("url", "")
                author = item.get("by", "")
                score = item.get("score", 0)
                comments = item.get("descendants", 0)
                item_time = item.get("time", 0)
                item_type = item.get("type", "story")
                domain = extract_domain(url)

                # Upsert story
                existing = conn.execute("SELECT id FROM stories WHERE id = ?", (item_id,)).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO stories (id, title, url, domain, author,
                                                score, comments, type, time, first_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (item_id, title, url, domain, author, score, comments,
                         item_type, item_time, now),
                    )
                    stats["new_stories"] += 1
                else:
                    conn.execute(
                        "UPDATE stories SET score = ?, comments = ?, title = ? WHERE id = ?",
                        (score, comments, title, item_id),
                    )

                # Record this fetch's ranking
                conn.execute(
                    """INSERT INTO rankings (story_id, list_type, rank, score, comments, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (item_id, list_type, rank, score, comments, now),
                )
                count += 1

                # Per-story commit — shrinks the write window from
                # ~minutes to ~milliseconds, prevents WAL bloat and the
                # concurrent-writer corruption we tracked in ai-leaders.
                try:
                    conn.commit()
                except sqlite3.OperationalError as e:
                    sys.stderr.write(
                        f"commit failed for item {item_id}: {e}; continuing\n"
                    )

            except Exception:
                # Individual item failures are normal — HN API gives 404
                # on deleted stories. Skip and move on.
                continue

            if rank % 10 == 0:
                time.sleep(0.5)  # gentle on the Firebase API

        stats["lists"][list_type] = count
        stats["total_rankings"] += count
        time.sleep(1)

    # Final mass cleanup so the WAL doesn't accumulate from one cron
    # tick to the next; silently degrades to PASSIVE if api.py readers
    # are holding snapshots open.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass

    import zoneinfo
    local_hour = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles")).hour
    if report_hour is not None:
        stats["report"] = (local_hour == report_hour)
    else:
        stats["report"] = True

    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_stats(conn, args):
    days = int(args[0]) if args else 7
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    total_stories = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    total_rankings = conn.execute(
        "SELECT COUNT(*) FROM rankings WHERE fetched_at >= ?", (cutoff,)
    ).fetchone()[0]

    by_list = conn.execute(
        "SELECT list_type, COUNT(*) AS cnt FROM rankings WHERE fetched_at >= ? GROUP BY list_type",
        (cutoff,),
    ).fetchall()

    top_domains = conn.execute(
        """SELECT s.domain, COUNT(DISTINCT s.id) AS cnt
           FROM stories s JOIN rankings r ON s.id = r.story_id
           WHERE r.fetched_at >= ? AND s.domain IS NOT NULL AND s.domain != ''
           GROUP BY s.domain ORDER BY cnt DESC LIMIT 10""",
        (cutoff,),
    ).fetchall()

    print(f"📊 past {days} days:")
    print(f"   total stories (historical): {total_stories}")
    print(f"   ranking records:            {total_rankings}")
    for r in by_list:
        print(f"     {r['list_type']}: {r['cnt']}")
    print(f"   top domains:")
    for r in top_domains:
        print(f"     {r['domain']}: {r['cnt']}")


def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2 or sys.argv[1] == "fetch":
        cmd_fetch(conn, sys.argv[1:] if len(sys.argv) > 1 else None)
    elif sys.argv[1] == "stats":
        cmd_stats(conn, sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
