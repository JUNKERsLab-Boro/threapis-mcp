"""Unattended slot-based posting (publish_scheduled_post).

Layered on PublishService (which stays untouched for manual use). Guarantees:
- fixed target account, verified before any write
- only inside the daily JST slots (server clock decides; no caller-supplied time)
- at most one post per slot (slot_id is a primary key claimed atomically)
- an ambiguous outcome is reconciled against the account's recent posts, never blindly re-posted
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from .db import Database
from .errors import AmbiguousOutcomeError, ThreadsError, ValidationError
from .publish_service import DUP_WINDOW, PublishService, _iso, content_hash
from .publisher import validate_text

JST = timezone(timedelta(hours=9))
SLOT_HOURS = (7, 20)
EARLY = timedelta(minutes=5)
SCHEDULED_DUP_WINDOW = timedelta(days=30)
DRAFT_TTL = timedelta(minutes=30)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_slots (
    slot_id TEXT PRIMARY KEY,
    request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
# Content policy: never publish age / school-grade information.
FORBIDDEN = re.compile(r"(小学生|中学生|高校生|中坊|高専生|\d{1,2}\s*歳|[小中高]\s*[1-6１-６一二三]\s*年?生|\d{1,2}\s*年生|中[1-3１-３]|高[1-3１-３])")


def slot_for(now: datetime, grace: timedelta) -> tuple[str, datetime] | None:
    """Return (slot_id, slot_start) if `now` is inside a slot window, else None."""
    jst = now.astimezone(JST)
    for day in (jst.date(), jst.date() + timedelta(days=1)):
        for h in SLOT_HOURS:
            start = datetime(day.year, day.month, day.day, h, 0, tzinfo=JST)
            if start - EARLY <= jst < start + grace:
                return f"{start:%Y-%m-%d}@{h:02d}", start
    return None


class ScheduledService:
    def __init__(self, ps: PublishService, username: str, grace_minutes: int = 30,
                 now=lambda: datetime.now(timezone.utc), settle_seconds: float = 20, sleep=time.sleep):
        self.ps, self.username = ps, username.lstrip("@").lower()
        self.grace, self.now = timedelta(minutes=grace_minutes), now
        self.settle, self.sleep = settle_seconds, sleep
        with ps.store.db._conn() as c:
            c.executescript(SCHEMA)

    def publish(self, text: str) -> dict:
        validate_text(text)
        if FORBIDDEN.search(text):
            raise ValidationError("text contains age/school-grade information, which must not be posted")
        now = self.now()
        slot = slot_for(now, self.grace)
        if not slot:
            raise ValidationError("Outside the scheduled posting slots (07:00 / 20:00 JST). Nothing was posted.")
        slot_id = slot[0]
        existing = self._slot(slot_id)
        if existing and existing["request_id"]:
            return self._resume(slot_id, existing["request_id"], text)

        acct = self.ps.pub.account_info()
        if (acct.get("username") or "").lower() != self.username:
            raise ThreadsError(f"Token belongs to @{acct.get('username')}, not @{self.username}. Nothing was posted.")
        h = content_hash(text, "everyone", None, acct.get("id"))
        self._reject_duplicates(text, h, acct.get("id"), now)
        if not existing and not self._claim(slot_id):
            return self._resume(slot_id, self._slot(slot_id)["request_id"], text)
        return self._new_request(slot_id, text, h, acct, now)

    # --- internals ------------------------------------------------------
    def _new_request(self, slot_id: str, text: str, h: str, acct: dict, now: datetime) -> dict:
        rid = uuid.uuid4().hex
        self.ps.store.create({"request_id": rid, "content_hash": h, "text": text, "reply_control": "everyone",
                              "topic_tag": None, "account_user_id": acct.get("id"),
                              "account_username": acct["username"], "expires_at": _iso(now + DRAFT_TTL)})
        self._set_request(slot_id, rid)
        return self._run(slot_id, rid, h)

    def _run(self, slot_id: str, rid: str, h: str) -> dict:
        try:
            self.ps.publish(rid, h)
        except AmbiguousOutcomeError:
            self.sleep(self.settle)
            row = self.ps.status(rid)  # queries recent posts; flips to published if it went live
            if row["status"] == "prepared":  # container creation timed out: nothing is public
                return self._out(slot_id, self.ps.store.get(rid), note=(
                    "Container creation timed out; nothing was published. Calling again in this slot retries safely."))
            if row["status"] != "published":
                return self._out(slot_id, self.ps.store.get(rid), note=(
                    "Outcome unknown: the post may be live. Do NOT retry; a later call in this slot only "
                    "re-checks and never posts again."))
        return self._out(slot_id, self.ps.store.get(rid))

    def _resume(self, slot_id: str, rid: str | None, text: str) -> dict:
        row = self.ps.store.get(rid) if rid else None
        if row is None or row["status"] == "failed":  # nothing was published; one fresh attempt is safe
            acct = self.ps.pub.account_info()
            if (acct.get("username") or "").lower() != self.username:
                raise ThreadsError("Wrong posting account. Nothing was posted.")
            h = content_hash(text, "everyone", None, acct.get("id"))
            self._reject_duplicates(text, h, acct.get("id"), self.now())
            return self._new_request(slot_id, text, h, acct, self.now())
        if row["status"] == "published":
            return self._out(slot_id, row, already=True)
        if row["status"] in ("unknown", "publishing"):
            row = self.ps.status(rid) and self.ps.store.get(rid)
            if row["status"] == "published":
                return self._out(slot_id, row, already=True)
            return self._out(slot_id, row, note="Outcome still unknown; not retried. Check Threads manually.")
        # prepared / creating / container_created: resume the same text only
        if content_hash(text, "everyone", None, row["account_user_id"]) != row["content_hash"]:
            raise ValidationError("This slot already has a pending post with different text; refusing a second one")
        return self._run(slot_id, rid, row["content_hash"])

    def _reject_duplicates(self, text: str, h: str, account_id, now: datetime) -> None:
        dup = self.ps.store.published_duplicate(h, account_id, _iso(now - SCHEDULED_DUP_WINDOW))
        if dup:
            raise ValidationError("Identical text was already published; refusing duplicate")
        for p in self.ps.pub.list_posts(25).get("data", []):
            if (p.get("text") or "").strip() == text.strip():
                raise ValidationError("Identical text is already among the account's recent posts; refusing duplicate")

    def _slot(self, slot_id: str) -> dict | None:
        with self.ps.store.db._conn() as c:
            r = c.execute("SELECT * FROM scheduled_slots WHERE slot_id=?", (slot_id,)).fetchone()
        return dict(r) if r else None

    def _claim(self, slot_id: str) -> bool:
        ts = _iso(self.now())
        with self.ps.store.db._conn() as c:
            return c.execute("INSERT OR IGNORE INTO scheduled_slots VALUES (?,NULL,?,?)",
                             (slot_id, ts, ts)).rowcount == 1

    def _set_request(self, slot_id: str, rid: str) -> None:
        with self.ps.store.db._conn() as c:
            c.execute("UPDATE scheduled_slots SET request_id=?, updated_at=? WHERE slot_id=?",
                      (rid, _iso(self.now()), slot_id))

    @staticmethod
    def _out(slot_id: str, row: dict, already: bool = False, note: str | None = None) -> dict:
        ok = row["status"] == "published"
        return {"status": row["status"], "slot": slot_id, "account": f"@{row['account_username']}",
                "post_id": row["media_id"], "text": row["text"] if ok else None,
                "published_at": row["updated_at"] if ok else None,
                "published_at_jst": (datetime.strptime(row["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
                                     .replace(tzinfo=timezone.utc).astimezone(JST).isoformat()) if ok else None,
                "permalink": row["permalink"], "request_id": row["request_id"],
                **({"already_published": True} if already else {}), **({"note": note} if note else {})}
