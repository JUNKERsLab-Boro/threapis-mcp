"""Two-step, approval-gated text publishing with duplicate protection.

prepare_text_post  -> stores a draft + returns a preview and a confirm_hash (no Threads write)
publish_text_post  -> PUBLIC ACTION; requires the confirm_hash of the exact previewed text
get_post_request_status -> reconciles ambiguous outcomes against the account's recent posts
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from .errors import (AmbiguousOutcomeError, AuthError, PermissionError_, RateLimitError,
                     ThreadsError, ValidationError)
from .publish_store import PublishStore, parse_iso
from .publisher import REPLY_CONTROLS, ThreadsPublisher, text_size, validate_text, validate_topic_tag

DRAFT_TTL = timedelta(minutes=30)
STALE_CREATING = timedelta(seconds=120)
DUP_WINDOW = timedelta(hours=24)
RECONCILE_LOOKBACK = 25
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(text: str, reply_control: str, topic_tag: str | None, account_id: str | None) -> str:
    payload = "\x1f".join([account_id or "", reply_control, topic_tag or "", text])
    return hashlib.sha256(payload.encode()).hexdigest()


class PublishService:
    def __init__(self, publisher: ThreadsPublisher, store: PublishStore,
                 wait_seconds: float = 45, poll_seconds: float = 3, sleep=time.sleep):
        self.pub, self.store = publisher, store
        self.wait_seconds, self.poll_seconds, self.sleep = wait_seconds, poll_seconds, sleep

    # --- 1. prepare (no write to Threads) -------------------------------
    def prepare(self, text: str, reply_control: str = "everyone", topic_tag: str | None = None) -> dict:
        validate_text(text)
        validate_topic_tag(topic_tag)
        if reply_control not in REPLY_CONTROLS:
            raise ValidationError(f"reply_control must be one of {', '.join(REPLY_CONTROLS)}")
        acct = self.pub.account_info()  # read-only; shows exactly which account will post
        if not acct.get("username"):
            raise ThreadsError("Could not determine the posting account; refusing to prepare")
        h = content_hash(text, reply_control, topic_tag, acct.get("id"))
        now = datetime.now(timezone.utc)
        rid = uuid.uuid4().hex
        self.store.create({"request_id": rid, "content_hash": h, "text": text, "reply_control": reply_control,
                           "topic_tag": topic_tag, "account_user_id": acct.get("id"),
                           "account_username": acct["username"], "expires_at": _iso(now + DRAFT_TTL)})
        dup = self.store.published_duplicate(h, acct.get("id"), _iso(now - DUP_WINDOW))
        return {
            "request_id": rid,
            "confirm_hash": h,
            "preview": {
                "account": f"@{acct['username']}",
                "text": text,
                "size": f"{text_size(text)}/500",
                "reply_control": reply_control,
                "topic_tag": topic_tag,
                "visibility": "PUBLIC post on the account's Threads profile (reply audience = reply_control)",
            },
            "expires_at": _iso(now + DRAFT_TTL),
            "duplicate_warning": (f"Identical text was already published (request {dup['request_id']}); "
                                  "publishing will be refused") if dup else None,
            "next_step": "Show this preview to the user. Only after the user explicitly approves THIS exact "
                         "account and text, call publish_text_post with request_id and confirm_hash.",
        }

    # --- 2. publish ------------------------------------------------------
    def publish(self, request_id: str, confirm_hash: str, allow_retry_after_unknown: bool = False) -> dict:
        row = self._load(request_id)
        if not secrets.compare_digest(str(confirm_hash).encode(), row["content_hash"].encode()):
            raise ValidationError("confirm_hash does not match the prepared draft; nothing was posted")
        if row["status"] == "published":
            return self._result(row, already_published=True)

        now = datetime.now(timezone.utc)
        status = row["status"]
        if status in ("unknown", "publishing"):
            if not allow_retry_after_unknown:
                raise ThreadsError(
                    "A previous publish attempt has an UNKNOWN outcome (the post may already be live). "
                    "Call get_post_request_status first. Retry only if the user confirms it is not on Threads, "
                    "by passing allow_retry_after_unknown=true.")
            reconciled = self._reconcile(row)
            if reconciled["status"] == "published":
                return self._result(reconciled, already_published=True)
            if not self.store.transition(request_id, ("unknown", "publishing"), "prepared", container_id=None):
                raise ThreadsError("Request state changed concurrently; re-check status")
            row = self._load(request_id)
            status = "prepared"
        if status == "creating" and now - parse_iso(row["updated_at"]) > STALE_CREATING:
            self.store.transition(request_id, ("creating",), "prepared")
            status = "prepared"

        if status in ("prepared", "failed"):
            if now > parse_iso(row["expires_at"]):
                raise ValidationError("Draft expired; call prepare_text_post again and re-confirm with the user")
            dup = self.store.published_duplicate(row["content_hash"], row["account_user_id"], _iso(now - DUP_WINDOW))
            if dup:
                raise ValidationError(f"Identical text was already published (request {dup['request_id']}); refusing to duplicate")
            self._create_container(row)
            row = self._load(request_id)
        elif status == "creating":
            raise ThreadsError("Another call is creating the container; wait and check status")

        if row["status"] == "container_created":
            self._await_ready(row)
            return self._publish_container(row)
        raise ThreadsError(f"Unexpected request state: {row['status']}")

    def _create_container(self, row: dict) -> None:
        rid = row["request_id"]
        if not self.store.transition(rid, ("prepared", "failed"), "creating"):
            raise ThreadsError("Request is already being processed")
        self.store.bump_attempts(rid)
        try:
            cid = self.pub.create_text_container(row["text"], row["reply_control"], row["topic_tag"])
        except AmbiguousOutcomeError as e:  # only an orphan container could exist; nothing is public
            self.store.transition(rid, ("creating",), "prepared", error=str(e))
            raise
        except ThreadsError as e:
            self.store.transition(rid, ("creating",), "failed", error=str(e))
            raise
        self.store.transition(rid, ("creating",), "container_created", container_id=cid, error=None)

    def _await_ready(self, row: dict) -> None:
        rid, cid = row["request_id"], row["container_id"]
        deadline = time.monotonic() + self.wait_seconds
        while True:
            status, err = self.pub.container_status(cid)
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                self.store.transition(rid, ("container_created",), "failed", container_id=None,
                                      error=f"container {status}: {err}")
                raise ThreadsError(f"Container {status}: {err}. Nothing was published.")
            if time.monotonic() >= deadline:
                raise ThreadsError("Container not ready yet (nothing published). Call publish_text_post again shortly.")
            self.sleep(self.poll_seconds)

    def _publish_container(self, row: dict) -> dict:
        rid = row["request_id"]
        # Persist 'publishing' BEFORE the call so a crash leaves an explicit unknown-outcome marker.
        if not self.store.transition(rid, ("container_created",), "publishing"):
            raise ThreadsError("Request is already being published; check status")
        try:
            mid = self.pub.publish_container(row["container_id"])
        except AmbiguousOutcomeError as e:
            self.store.transition(rid, ("publishing",), "unknown", error=str(e))
            raise AmbiguousOutcomeError(
                f"{e}. The post MAY be live. Do not retry blindly: call get_post_request_status.") from None
        except (AuthError, PermissionError_, RateLimitError, ThreadsError) as e:  # definite rejection
            self.store.transition(rid, ("publishing",), "failed", container_id=None, error=str(e))
            raise
        link = self.pub.media_permalink(mid)
        self.store.transition(rid, ("publishing",), "published", media_id=mid, permalink=link, error=None)
        return self._result(self._load(rid))

    # --- 3. status / reconcile ------------------------------------------
    def status(self, request_id: str) -> dict:
        row = self._load(request_id)
        if row["status"] in ("unknown", "publishing"):
            row = self._reconcile(row)
            if row["status"] != "published":
                return {**self._result(row), "note":
                        "Not found in the 25 most recent posts. It may still be processing: wait ~1 minute and "
                        "check again. Retry only after the user confirms, with allow_retry_after_unknown=true."}
        return self._result(row)

    def _reconcile(self, row: dict) -> dict:
        since = parse_iso(row["updated_at"]) - timedelta(minutes=10)
        listing = self.pub.list_posts(RECONCILE_LOOKBACK)
        for p in listing.get("data", []):
            if (p.get("text") or "").strip() == row["text"].strip() and self._after(p.get("timestamp"), since):
                self.store.transition(row["request_id"], ("unknown", "publishing"), "published",
                                      media_id=p["id"], permalink=p.get("permalink"), error=None)
                break
        return self._load(row["request_id"])

    @staticmethod
    def _after(ts: str | None, since: datetime) -> bool:
        if not ts:
            return False
        try:
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") >= since
        except ValueError:
            return False

    def _load(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
            raise ValidationError("invalid request_id")
        row = self.store.get(request_id)
        if not row:
            raise ValidationError("unknown request_id")
        return row

    @staticmethod
    def _result(row: dict, already_published: bool = False) -> dict:
        return {"request_id": row["request_id"], "status": row["status"],
                "account": f"@{row['account_username']}", "media_id": row["media_id"],
                "permalink": row["permalink"], "error": row["error"], "attempts": row["attempts"],
                **({"already_published": True} if already_published else {})}
