#!/usr/bin/env python3
"""
Hacker News Fetch — pull top/best/new stories and store in SQLite.

Usage:
  python3 hackernews_fetch.py                    # Fetch top + best stories
  python3 hackernews_fetch.py --report-hour H    # Only report when local hour == H
  python3 hackernews_fetch.py stats [days]       # Quick stats
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import urllib.request

from db import get_db, init_db

HN_API = "https://hacker-news.firebaseio.com/v0"
LISTS = ["topstories", "beststories"]
MAX_ITEMS = 30  # top 30 per list


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
                        """INSERT INTO stories (id, title, url, domain, author, score, comments, type, time, first_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (item_id, title, url, domain, author, score, comments, item_type, item_time, now),
                    )
                    stats["new_stories"] += 1
                else:
                    conn.execute(
                        "UPDATE stories SET score = ?, comments = ?, title = ? WHERE id = ?",
                        (score, comments, title, item_id),
                    )

                # Insert ranking
                conn.execute(
                    """INSERT INTO rankings (story_id, list_type, rank, score, comments, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (item_id, list_type, rank, score, comments, now),
                )
                count += 1

            except Exception as e:
                continue  # skip individual failures

            if rank % 10 == 0:
                time.sleep(0.5)  # rate limit

        stats["lists"][list_type] = count
        stats["total_rankings"] += count
        time.sleep(1)

    conn.commit()

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
        "SELECT list_type, COUNT(*) as cnt FROM rankings WHERE fetched_at >= ? GROUP BY list_type",
        (cutoff,),
    ).fetchall()

    top_domains = conn.execute(
        """SELECT s.domain, COUNT(DISTINCT s.id) as cnt
           FROM stories s JOIN rankings r ON s.id = r.story_id
           WHERE r.fetched_at >= ? AND s.domain IS NOT NULL AND s.domain != ''
           GROUP BY s.domain ORDER BY cnt DESC LIMIT 10""",
        (cutoff,),
    ).fetchall()

    print(f"📊 过去 {days} 天统计：")
    print(f"   总故事数（历史）：{total_stories}")
    print(f"   排名记录数：{total_rankings}")
    for r in by_list:
        print(f"     {r['list_type']}: {r['cnt']} 条")
    print(f"   热门域名：")
    for r in top_domains:
        print(f"     {r['domain']}: {r['cnt']} 篇")


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
