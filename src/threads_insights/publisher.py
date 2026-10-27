"""Write-side Threads client (text posts only). Kept apart from the read-only ThreadsClient.

Every write is attempted exactly once: no automatic retries. A timeout/5xx/unparseable reply
raises AmbiguousOutcomeError because the request may have taken effect.
"""
from __future__ import annotations

import httpx

from .client import BASE, ThreadsClient
from .errors import AmbiguousOutcomeError, PermissionError_, ValidationError

PUBLISH_SCOPE = "threads_content_publish"
REPLY_CONTROLS = ("everyone", "accounts_you_follow", "mentioned_only", "followers_only")
MAX_TEXT = 500


def text_size(text: str) -> int:
    """Conservative size: astral chars (emoji) count by UTF-8 bytes, others as 1 char."""
    return sum(len(c.encode("utf-8")) if ord(c) > 0xFFFF else 1 for c in text)


def validate_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("text must be a non-empty string")
    if text_size(text) > MAX_TEXT:
        raise ValidationError(f"text exceeds {MAX_TEXT} characters (emoji count by UTF-8 bytes)")
    return text


def validate_topic_tag(tag: str | None) -> str | None:
    if tag is None:
        return None
    if not 1 <= len(tag) <= 50 or "." in tag or "&" in tag:
        raise ValidationError("topic_tag must be 1-50 chars without '.' or '&'")
    return tag


class ThreadsPublisher(ThreadsClient):
    def _require_scope(self) -> None:
        t = self._token()
        if t.scopes is not None and PUBLISH_SCOPE not in t.scopes:
            raise PermissionError_(
                "Stored token lacks threads_content_publish; run `threads-insights-auth login --publish`")

    def _post_once(self, path: str, data: dict) -> dict:
        self._require_scope()
        token = self._token().access_token
        try:
            r = self.http.post(f"{BASE}/{path}", data={**data, "access_token": token})
        except httpx.HTTPError:  # incl. timeouts: the request may have been processed
            raise AmbiguousOutcomeError("No response from Threads; the request may or may not have been processed") from None
        if r.status_code >= 500:
            raise AmbiguousOutcomeError(f"Threads returned HTTP {r.status_code}; outcome unknown")
        body = self._handle(r)  # definite errors raise Auth/Permission/RateLimit/API errors
        return body

    # --- reads needed by the publish flow ---
    def account_info(self) -> dict:
        b = self._get(self.user_id, {"fields": "id,username"})
        return {"id": b.get("id"), "username": b.get("username")}

    def container_status(self, container_id: str) -> tuple[str | None, str | None]:
        b = self._get(container_id, {"fields": "status,error_message"})
        return b.get("status"), b.get("error_message")

    def media_permalink(self, media_id: str) -> str | None:
        try:
            return self._get(media_id, {"fields": "permalink"}).get("permalink")
        except Exception:
            return None

    # --- writes ---
    def create_text_container(self, text: str, reply_control: str, topic_tag: str | None) -> str:
        data = {"media_type": "TEXT", "text": text}
        if reply_control != "everyone":
            data["reply_control"] = reply_control
        if topic_tag:
            data["topic_tag"] = topic_tag
        cid = self._post_once(f"{self.user_id}/threads", data).get("id")
        if not cid:
            raise AmbiguousOutcomeError("Container creation returned no id")
        return str(cid)

    def publish_container(self, container_id: str) -> str:
        mid = self._post_once(f"{self.user_id}/threads_publish", {"creation_id": container_id}).get("id")
        if not mid:
            raise AmbiguousOutcomeError("Publish returned no media id; outcome unknown")
        return str(mid)
