"""SQLite history store. All SQL is parameterized and fixed; nothing is user-supplied."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    text TEXT,
    timestamp TEXT,
    permalink TEXT,
    media_type TEXT,
    username TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(id),
    fetched_at TEXT NOT NULL,
    views INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER,
    UNIQUE (post_id, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_post_insights_post ON post_insights(post_id, fetched_at);
CREATE TABLE IF NOT EXISTS account_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL UNIQUE,
    period_since INTEGER, period_until INTEGER,
    views INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER,
    clicks INTEGER, followers_count INTEGER,
    extra_json TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:  # one transaction: commit on success, rollback on error
                yield c
        finally:
            c.close()

    def upsert_posts(self, posts: list[dict]) -> None:
        ts = now_iso()
        with self._conn() as c:
            for p in posts:
                self._upsert_post(c, p, ts)

    @staticmethod
    def _upsert_post(c, p: dict, ts: str) -> None:
        c.execute(
            """INSERT INTO posts (id, text, timestamp, permalink, media_type, username, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 text=COALESCE(excluded.text, text),
                 timestamp=COALESCE(excluded.timestamp, timestamp),
                 permalink=COALESCE(excluded.permalink, permalink),
                 media_type=COALESCE(excluded.media_type, media_type),
                 username=COALESCE(excluded.username, username),
                 updated_at=excluded.updated_at""",
            (p["id"], p.get("text"), p.get("timestamp"), p.get("permalink"),
             p.get("media_type"), p.get("username"), ts, ts),
        )

    def save_post_insights(self, post: dict, metrics: dict) -> str:
        ts = now_iso()
        with self._conn() as c:
            self._upsert_post(c, post, ts)
            c.execute(
                """INSERT OR IGNORE INTO post_insights
                   (post_id, fetched_at, views, likes, replies, reposts, quotes) VALUES (?,?,?,?,?,?,?)""",
                (post["id"], ts, metrics.get("views"), metrics.get("likes"), metrics.get("replies"),
                 metrics.get("reposts"), metrics.get("quotes")),
            )
        return ts

    def save_account_insights(self, m: dict, since: int | None, until: int | None) -> str:
        ts = now_iso()
        extra = json.dumps({k: m.get(k) for k in ("views_daily", "clicks_by_link", "follower_demographics")})
        with self._conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO account_insights
                   (fetched_at, period_since, period_until, views, likes, replies, reposts, quotes,
                    clicks, followers_count, extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, since, until, m.get("views"), m.get("likes"), m.get("replies"), m.get("reposts"),
                 m.get("quotes"), m.get("clicks"), m.get("followers_count"), extra),
            )
        return ts

    # read helpers (used by tests / future features)
    def count(self, table: str) -> int:
        assert table in ("posts", "post_insights", "account_insights")
        with self._conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
