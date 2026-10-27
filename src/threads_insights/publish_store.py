"""SQLite state for publish requests. Status transitions are atomic compare-and-set."""
from __future__ import annotations

from datetime import datetime, timezone

from .db import Database, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_requests (
    request_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_control TEXT NOT NULL,
    topic_tag TEXT,
    account_user_id TEXT,
    account_username TEXT,
    status TEXT NOT NULL,
    container_id TEXT,
    media_id TEXT,
    permalink TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_post_requests_hash ON post_requests(content_hash, status);
"""
# prepared -> creating -> container_created -> publishing -> published
#   failed: API definitely rejected (nothing published); unknown: publish outcome unknown.
COLUMNS = {"container_id", "media_id", "permalink", "error", "attempts"}


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class PublishStore:
    def __init__(self, db: Database):
        self.db = db
        with db._conn() as c:
            c.executescript(SCHEMA)

    def create(self, row: dict) -> None:
        ts = now_iso()
        with self.db._conn() as c:
            c.execute(
                """INSERT INTO post_requests (request_id, content_hash, text, reply_control, topic_tag,
                   account_user_id, account_username, status, created_at, updated_at, expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row["request_id"], row["content_hash"], row["text"], row["reply_control"], row.get("topic_tag"),
                 row.get("account_user_id"), row.get("account_username"), "prepared", ts, ts, row["expires_at"]))

    def get(self, request_id: str) -> dict | None:
        with self.db._conn() as c:
            r = c.execute("SELECT * FROM post_requests WHERE request_id=?", (request_id,)).fetchone()
        return dict(r) if r else None

    def transition(self, request_id: str, from_statuses: tuple[str, ...], to: str, **fields) -> bool:
        """Atomically move status; returns False if the current status was not in from_statuses."""
        assert set(fields) <= COLUMNS
        sets = "".join(f", {k}=?" for k in fields)
        marks = ",".join("?" * len(from_statuses))
        with self.db._conn() as c:
            cur = c.execute(
                f"UPDATE post_requests SET status=?, updated_at=?{sets} WHERE request_id=? AND status IN ({marks})",
                (to, now_iso(), *fields.values(), request_id, *from_statuses))
            return cur.rowcount == 1

    def bump_attempts(self, request_id: str) -> None:
        with self.db._conn() as c:
            c.execute("UPDATE post_requests SET attempts=attempts+1 WHERE request_id=?", (request_id,))

    def published_duplicate(self, content_hash: str, account_user_id: str | None, since_iso: str) -> dict | None:
        with self.db._conn() as c:
            r = c.execute(
                """SELECT request_id, media_id FROM post_requests WHERE content_hash=? AND status='published'
                   AND COALESCE(account_user_id,'')=COALESCE(?,'') AND updated_at>=? LIMIT 1""",
                (content_hash, account_user_id, since_iso)).fetchone()
        return dict(r) if r else None
