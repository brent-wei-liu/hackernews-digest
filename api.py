"""hackernews-digest — FastAPI server for the web UI.

Run: python3 api.py
URL: http://127.0.0.1:8084 (local), http://<mac-ip>:8084 (LAN/phone)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from db import (
    get_db, init_db,
    star_story, unstar_story,
    mark_summary_read, mark_summary_unread,
)

PORT = 8084
HOST = "0.0.0.0"  # bind so phones on the same Wi-Fi can reach it
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="hackernews-digest")


def _conn():
    conn = get_db()
    init_db(conn)
    return conn


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(str(index))


# ── Stats ────────────────────────────────────────────────────────────
@app.get("/api/stats")
def api_stats():
    conn = _conn()
    try:
        return {
            "stories_total":     conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
            "stories_starred":   conn.execute("SELECT COUNT(*) FROM stories WHERE starred = 1").fetchone()[0],
            "summaries_total":   conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0],
            "summaries_unread":  conn.execute("SELECT COUNT(*) FROM summaries WHERE is_read = 0").fetchone()[0],
            "last_fetch":        conn.execute("SELECT MAX(fetched_at) FROM rankings").fetchone()[0],
        }
    finally:
        conn.close()


# ── Stories ──────────────────────────────────────────────────────────
@app.get("/api/stories")
def api_stories(
    list_type: str = Query("topstories", pattern="^(topstories|beststories|all)$"),
    starred: bool = False,
    q: str | None = None,
    days: int = Query(1, ge=1, le=30),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    conn = _conn()
    try:
        params: list = []
        where = ["r.fetched_at >= datetime('now', ?)"]
        params.append(f"-{days} days")

        if list_type != "all":
            where.append("r.list_type = ?")
            params.append(list_type)
        if starred:
            where.append("s.starred = 1")
        if q:
            where.append("(s.title LIKE ? OR s.domain LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]

        where_sql = " WHERE " + " AND ".join(where)

        # Deduplicate via GROUP BY id, keeping best rank + max score/comments
        sql = (
            "SELECT s.id, s.title, s.url, s.domain, s.author, s.starred, s.starred_at, "
            "  MIN(r.rank) AS best_rank, MAX(r.score) AS score, MAX(r.comments) AS comments, "
            "  GROUP_CONCAT(DISTINCT r.list_type) AS lists "
            "FROM rankings r JOIN stories s ON r.story_id = s.id "
            f"{where_sql} "
            "GROUP BY s.id "
            "ORDER BY score DESC "
            "LIMIT ? OFFSET ?"
        )
        offset = max(0, (page - 1) * page_size)
        rows = conn.execute(sql, params + [page_size, offset]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["hn_url"] = f"https://news.ycombinator.com/item?id={r['id']}"
            out.append(d)
        return {"stories": out, "list_type": list_type, "page": page, "page_size": page_size}
    finally:
        conn.close()


@app.post("/api/stories/{story_id}/star")
def api_story_star(story_id: int):
    conn = _conn()
    try:
        if not star_story(conn, story_id):
            raise HTTPException(404, "story not found")
        return {"ok": True, "starred": True}
    finally:
        conn.close()


@app.post("/api/stories/{story_id}/unstar")
def api_story_unstar(story_id: int):
    conn = _conn()
    try:
        if not unstar_story(conn, story_id):
            raise HTTPException(404, "story not found")
        return {"ok": True, "starred": False}
    finally:
        conn.close()


# ── Digests ──────────────────────────────────────────────────────────
@app.get("/api/digests")
def api_digests():
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, date, focus, created_at, is_read, read_at,
                      LENGTH(content) AS content_length
               FROM summaries
               ORDER BY date DESC, id DESC"""
        ).fetchall()
        return {"digests": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/digests/{digest_id}")
def api_digest(digest_id: int):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM summaries WHERE id = ?", (digest_id,)).fetchone()
        if not row:
            raise HTTPException(404, "digest not found")
        return dict(row)
    finally:
        conn.close()


@app.post("/api/digests/{digest_id}/read")
def api_digest_read(digest_id: int):
    conn = _conn()
    try:
        if not mark_summary_read(conn, digest_id):
            raise HTTPException(404, "digest not found")
        return {"ok": True, "is_read": True}
    finally:
        conn.close()


@app.post("/api/digests/{digest_id}/unread")
def api_digest_unread(digest_id: int):
    conn = _conn()
    try:
        if not mark_summary_unread(conn, digest_id):
            raise HTTPException(404, "digest not found")
        return {"ok": True, "is_read": False}
    finally:
        conn.close()


# Mount /static AFTER the route handlers so /api/* doesn't get shadowed
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    print(f"hackernews-digest UI on http://127.0.0.1:{PORT}  (LAN: http://<mac-ip>:{PORT})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
