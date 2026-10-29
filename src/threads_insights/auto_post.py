"""Unattended posting (publish_auto_post), callable at any time; the schedule lives in the caller (ChatGPT).

Layered on PublishService (manual tools stay untouched). Guarantees:
- fixed target account, verified before any write
- every attempt is recorded in SQLite (auto_posts -> post_requests)
- frequency caps (min interval + rolling-24h maximum) against runaway posting
- identical text is never posted twice; an ambiguous outcome is reconciled, never blindly re-posted
- a new post is refused while an earlier auto post has an unresolved outcome
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from .db import Database
from .errors import AmbiguousOutcomeError, RateLimitError, ThreadsError, ValidationError
from .publish_service import PublishService, _iso, content_hash
from .publisher import validate_text

JST = timezone(timedelta(hours=9))
DUP_WINDOW = timedelta(days=30)
DRAFT_TTL = timedelta(minutes=30)
UNRESOLVED_BLOCK = timedelta(hours=6)

SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_posts (
    request_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""
# Content policy: never publish age / school-grade information.
FORBIDDEN = re.compile(r"(小学生|中学生|高校生|中坊|高専生|\d{1,2}\s*歳|[小中高]\s*[1-6１-６一二三]\s*年?生|\d{1,2}\s*年生|中[1-3１-３]|高[1-3１-３])")
COUNTED = ("creating", "container_created", "publishing", "published", "unknown")  # states that may be/are public


class AutoPostService:
    def __init__(self, ps: PublishService, username: str, min_interval_minutes: int = 60, max_per_day: int = 4,
                 now=lambda: datetime.now(timezone.utc), settle_seconds: float = 20, sleep=time.sleep):
        self.ps, self.username = ps, username.lstrip("@").lower()
        self.min_interval, self.max_per_day = timedelta(minutes=min_interval_minutes), max_per_day
        self.now, self.settle, self.sleep = now, settle_seconds, sleep
        with ps.store.db._conn() as c:
            c.executescript(SCHEMA)

    def publish(self, text: str) -> dict:
        validate_text(text)
        if FORBIDDEN.search(text):
            raise ValidationError("text contains age/school-grade information, which must not be posted")
        now = self.now()
        acct = self.ps.pub.account_info()
        if (acct.get("username") or "").lower() != self.username:
            raise ThreadsError(f"Token belongs to @{acct.get('username')}, not @{self.username}. Nothing was posted.")
        h = content_hash(text, "everyone", None, acct.get("id"))

        same = self._latest_same(h)  # resume / report an earlier attempt of this exact text
        if same and same["status"] == "published":
            raise ValidationError(f"Identical text was already published (post {same['media_id']}); refusing duplicate")
        if same and same["status"] in ("unknown", "publishing"):
            return self._reconciled(same)
        self._reject_duplicates(text, h, acct.get("id"), now)
        self._block_on_unresolved()
        if same and same["status"] in ("prepared", "creating", "container_created") \
                and now <= datetime.strptime(same["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc):
            return self._run(same["request_id"], h)  # crash/timeout earlier: same text, same request

        rid = uuid.uuid4().hex
        self._claim(rid, now)  # atomic frequency check + reservation
        self.ps.store.create({"request_id": rid, "content_hash": h, "text": text, "reply_control": "everyone",
                              "topic_tag": None, "account_user_id": acct.get("id"),
                              "account_username": acct["username"], "expires_at": _iso(now + DRAFT_TTL)})
        return self._run(rid, h)

    # --- internals ------------------------------------------------------
    def _run(self, rid: str, h: str) -> dict:
        try:
            self.ps.publish(rid, h)
        except AmbiguousOutcomeError:
            self.sleep(self.settle)
            row = self.ps.status(rid)  # queries recent posts; flips to published if it went live
            if row["status"] == "prepared":  # container creation timed out: nothing is public
                return self._out(self.ps.store.get(rid), note=(
                    "Container creation timed out; nothing was published. Calling again with the same text retries safely."))
            if row["status"] != "published":
                return self._out(self.ps.store.get(rid), note=(
                    "Outcome unknown: the post may be live. Do NOT retry; later calls only re-check."))
        return self._out(self.ps.store.get(rid))

    def _reconciled(self, row: dict) -> dict:
        self.ps.status(row["request_id"])
        row = self.ps.store.get(row["request_id"])
        if row["status"] == "published":
            return self._out(row, already=True)
        return self._out(row, note="Outcome still unknown; not retried. Check Threads manually.")

    def _latest_same(self, h: str) -> dict | None:
        with self.ps.store.db._conn() as c:
            r = c.execute("""SELECT p.* FROM post_requests p JOIN auto_posts a USING (request_id)
                             WHERE p.content_hash=? AND p.status!='failed' ORDER BY a.created_at DESC LIMIT 1""",
                          (h,)).fetchone()
        return dict(r) if r else None

    def _block_on_unresolved(self) -> None:
        since = _iso(self.now() - UNRESOLVED_BLOCK)
        with self.ps.store.db._conn() as c:
            ids = [r[0] for r in c.execute("""SELECT p.request_id FROM post_requests p JOIN auto_posts a USING (request_id)
                   WHERE p.status IN ('unknown','publishing') AND a.created_at>=?""", (since,))]
        for rid in ids:
            self.ps.status(rid)
            if self.ps.store.get(rid)["status"] in ("unknown", "publishing"):
                raise ThreadsError("An earlier auto post has an unresolved outcome (may be live). "
                                   "Refusing a new post until it is confirmed on Threads.")

    def _reject_duplicates(self, text: str, h: str, account_id, now: datetime) -> None:
        if self.ps.store.published_duplicate(h, account_id, _iso(now - DUP_WINDOW)):
            raise ValidationError("Identical text was already published; refusing duplicate")
        for p in self.ps.pub.list_posts(25).get("data", []):
            if (p.get("text") or "").strip() == text.strip():
                raise ValidationError("Identical text is already among the account's recent posts; refusing duplicate")

    def _claim(self, rid: str, now: datetime) -> None:
        """Check the frequency caps and record the attempt in one IMMEDIATE transaction."""
        day_ago, interval_ago = _iso(now - timedelta(hours=24)), _iso(now - self.min_interval)
        marks = ",".join("?" * len(COUNTED))
        with self.ps.store.db._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            q = f"""SELECT COUNT(*), MAX(a.created_at) FROM auto_posts a JOIN post_requests p USING (request_id)
                    WHERE p.status IN ({marks}) AND a.created_at>=?"""
            n, _ = c.execute(q, (*COUNTED, day_ago)).fetchone()
            recent = c.execute(q, (*COUNTED, interval_ago)).fetchone()[0]
            if recent:
                raise RateLimitError(f"Auto-post rate limit: at most one post per {int(self.min_interval.total_seconds() // 60)} "
                                     "minutes. Nothing was posted.")
            if n >= self.max_per_day:
                raise RateLimitError(f"Auto-post rate limit: at most {self.max_per_day} posts per rolling 24h. "
                                     "Nothing was posted.")
            c.execute("INSERT INTO auto_posts VALUES (?,?)", (rid, _iso(now)))

    @staticmethod
    def _out(row: dict, already: bool = False, note: str | None = None) -> dict:
        ok = row["status"] == "published"
        pub = datetime.strptime(row["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if ok else None
        return {"status": row["status"], "account": f"@{row['account_username']}",
                "post_id": row["media_id"], "text": row["text"] if ok else None,
                "published_at": row["updated_at"] if ok else None,
                "published_at_jst": pub.astimezone(JST).isoformat() if ok else None,
                "permalink": row["permalink"], "request_id": row["request_id"],
                **({"already_published": True} if already else {}), **({"note": note} if note else {})}
