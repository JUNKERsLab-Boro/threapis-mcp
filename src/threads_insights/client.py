"""Minimal read-only Threads Graph API client.

Only GET requests to a fixed host with fixed path shapes are possible; paging uses
cursors, never `paging.next` URLs, so arbitrary URLs can never be requested.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from . import auth
from .config import Config
from .errors import (
    APIError,
    AuthError,
    NetworkError,
    PermissionError_,
    RateLimitError,
    ValidationError,
)
from .tokens import TokenInfo, TokenStore

log = logging.getLogger(__name__)
BASE = "https://graph.threads.com/v1.0"
POST_ID_RE = re.compile(r"^\d{1,30}$")
CURSOR_RE = re.compile(r"^[A-Za-z0-9_=\-]{1,512}$")
MAX_PAGE = 100  # Threads list endpoints cap `limit` at 100

POST_FIELDS = "id,text,timestamp,permalink,media_type,username,shortcode,is_quote_post"
MEDIA_METRICS = ("views", "likes", "replies", "reposts", "quotes")
ACCOUNT_RANGE_METRICS = ("views", "likes", "replies", "reposts", "quotes", "clicks")
MIN_SINCE = 1712991600  # API rejects earlier timestamps

# Keep httpx from logging request URLs (they carry the token as a query parameter).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def validate_post_id(post_id: str) -> str:
    if not isinstance(post_id, str) or not POST_ID_RE.match(post_id):
        raise ValidationError("post_id must be a numeric Threads media ID")
    return post_id


class ThreadsClient:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        self.store = TokenStore(config)
        self.http = http or httpx.Client(timeout=config.timeout)
        self._refresh_checked = False

    # --- token handling -------------------------------------------------
    def _token(self) -> TokenInfo:
        t = self.store.load()
        if not self._refresh_checked:
            self._refresh_checked = True  # at most one auto-check per process
            try:
                if auth.refresh_if_needed(self.config, self.http):
                    t = self.store.load()
            except AuthError:
                raise
            except Exception:
                log.warning("Token auto-refresh failed; continuing with current token")
        if t.expired:
            raise AuthError("Access token expired; run `threads-insights-auth login` again")
        return t

    @property
    def user_id(self) -> str:
        return self.config.user_id if self.config.user_id != "me" else (self.store.load().user_id or "me")

    # --- HTTP -----------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        token = self._token().access_token
        url = f"{BASE}/{path}"
        q = {**params, "access_token": token}
        for attempt in (0, 1):
            try:
                r = self.http.get(url, params=q)
            except httpx.TimeoutException:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                raise NetworkError("Threads API request timed out") from None
            except httpx.HTTPError:
                raise NetworkError("Network error calling Threads API") from None
            if r.status_code >= 500 and attempt == 0:
                time.sleep(0.5)
                continue
            return self._handle(r)
        raise APIError("unreachable")  # pragma: no cover

    @staticmethod
    def _handle(r: httpx.Response) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code < 400 and "error" not in body:
            return body
        err = body.get("error") or {}
        code, msg = err.get("code"), err.get("message") or f"HTTP {r.status_code}"
        if r.status_code == 429 or code in (4, 17, 32, 613, 368):
            raise RateLimitError(f"Threads API rate limit reached: {msg}. Retry later.")
        if r.status_code == 401 or code in (190, 102) or err.get("type") == "OAuthException" and code is None:
            raise AuthError(f"Access token invalid or expired ({msg}); re-run `threads-insights-auth login`")
        if r.status_code == 403 or code == 10 or (isinstance(code, int) and 200 <= code < 300):
            raise PermissionError_(f"Permission denied ({msg}); check scopes threads_basic/threads_manage_insights")
        raise APIError(f"Threads API error (HTTP {r.status_code}, code {code}): {msg}")

    # --- endpoints (all GET) -------------------------------------------
    def list_posts(self, limit: int = 25, after: str | None = None) -> dict:
        if not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            raise ValidationError(f"limit must be an integer 1..{MAX_PAGE}")
        params: dict = {"fields": POST_FIELDS, "limit": limit}
        if after is not None:
            if not CURSOR_RE.match(after):
                raise ValidationError("invalid pagination cursor")
            params["after"] = after
        return self._get(f"{self.user_id}/threads", params)

    def get_post(self, post_id: str) -> dict:
        return self._get(validate_post_id(post_id), {"fields": POST_FIELDS})

    def post_insights(self, post_id: str) -> dict[str, int | None]:
        """Returns all MEDIA_METRICS; metrics the API did not return are None."""
        body = self._get(f"{validate_post_id(post_id)}/insights", {"metric": ",".join(MEDIA_METRICS)})
        out: dict[str, int | None] = {m: None for m in MEDIA_METRICS}
        for item in body.get("data", []):
            name = item.get("name")
            vals = item.get("values") or []
            if name in out and vals and isinstance(vals[0].get("value"), (int, float)):
                out[name] = int(vals[0]["value"])
        return out

    def account_insights(self, since: int | None = None, until: int | None = None,
                         demographics_breakdown: str | None = None) -> dict:
        uid = self.user_id
        rng: dict = {}
        for k, v in (("since", since), ("until", until)):
            if v is not None:
                if not isinstance(v, int) or v < MIN_SINCE:
                    raise ValidationError(f"{k} must be a unix timestamp >= {MIN_SINCE}")
                rng[k] = v
        if demographics_breakdown not in (None, "country", "city", "age", "gender"):
            raise ValidationError("breakdown must be one of country, city, age, gender")

        res: dict = {m: None for m in ACCOUNT_RANGE_METRICS}
        res.update(followers_count=None, views_daily=None, clicks_by_link=None, follower_demographics=None)

        body = self._get(f"{uid}/threads_insights", {"metric": ",".join(ACCOUNT_RANGE_METRICS), **rng})
        for item in body.get("data", []):
            name = item.get("name")
            if name == "views":  # time series
                series = [{"end_time": v.get("end_time"), "value": v.get("value")} for v in item.get("values", [])]
                res["views_daily"] = series
                nums = [s["value"] for s in series if isinstance(s["value"], (int, float))]
                res["views"] = int(sum(nums)) if nums else None
            elif name == "clicks":
                links = item.get("link_total_values") or []
                res["clicks_by_link"] = [{"link_url": l.get("link_url"), "value": l.get("value")} for l in links]
                res["clicks"] = int(sum(l.get("value", 0) for l in links)) if links else None
            elif name in res and isinstance(item.get("total_value", {}).get("value"), (int, float)):
                res[name] = int(item["total_value"]["value"])

        # followers_count ignores since/until, so it is requested separately.
        fb = self._get(f"{uid}/threads_insights", {"metric": "followers_count"})
        for item in fb.get("data", []):
            if item.get("name") == "followers_count":
                v = item.get("total_value", {}).get("value")
                res["followers_count"] = int(v) if isinstance(v, (int, float)) else None

        if demographics_breakdown:
            db = self._get(f"{uid}/threads_insights",
                           {"metric": "follower_demographics", "breakdown": demographics_breakdown})
            for item in db.get("data", []):
                if item.get("name") == "follower_demographics":
                    res["follower_demographics"] = item.get("total_value")
        return res
