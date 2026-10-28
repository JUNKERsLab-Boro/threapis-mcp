"""Tool logic: fetch from API, then persist (history is written only after a successful fetch)."""
from __future__ import annotations

import time

from .client import MEDIA_METRICS, ThreadsClient, validate_post_id
from .db import Database
from .errors import APIError, AuthError, PermissionError_, RateLimitError, ValidationError

MAX_COMPARE_IDS = 20
MAX_COMPARE_RECENT = 25
MAX_DAYS = 365


def engagement_rate(m: dict) -> float | None:
    """(likes+replies+reposts+quotes)/views*100; None if any input is unavailable or views == 0."""
    parts = [m.get(k) for k in ("likes", "replies", "reposts", "quotes")]
    views = m.get("views")
    if views is None or views == 0 or any(p is None for p in parts):
        return None
    return round(sum(parts) / views * 100, 4)


def _post_view(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "text": p.get("text"),
        "timestamp": p.get("timestamp"),
        "permalink": p.get("permalink"),
        "media_type": p.get("media_type"),
        "username": p.get("username"),
    }


class Service:
    def __init__(self, client: ThreadsClient, db: Database):
        self.client, self.db = client, db

    def account_insights(self, days: int | None = None, breakdown: str | None = None,
                         start_date: str | None = None, end_date: str | None = None) -> dict:
        since = until = None
        if (start_date or end_date) and days is not None:
            raise ValidationError("use either days or start_date/end_date, not both")
        if start_date or end_date:
            since, until = self._date_range(start_date, end_date)
        elif days is not None:
            if not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
                raise ValidationError(f"days must be an integer 1..{MAX_DAYS}")
            until = int(time.time())
            since = until - days * 86400
        m = self.client.account_insights(since, until, breakdown)
        fetched = self.db.save_account_insights(m, since, until)
        return {"fetched_at": fetched, "period": {"since": since, "until": until}, **m,
                "notes": "views is the sum over the period (daily series in views_daily); "
                         "unavailable metrics are null"}

    @staticmethod
    def _date_range(start_date: str | None, end_date: str | None) -> tuple[int, int]:
        """YYYY-MM-DD (UTC). end_date is inclusive: until = start of the following day."""
        from datetime import date, datetime, time as dtime, timedelta, timezone
        try:
            start = date.fromisoformat(start_date) if start_date else None
            end = date.fromisoformat(end_date) if end_date else date.today()
        except ValueError:
            raise ValidationError("dates must be YYYY-MM-DD") from None
        if start is None or end < start:
            raise ValidationError("start_date is required and must be <= end_date")
        if (end - start).days > MAX_DAYS:
            raise ValidationError(f"range must be at most {MAX_DAYS} days")
        ts = lambda d: int(datetime.combine(d, dtime.min, tzinfo=timezone.utc).timestamp())
        return ts(start), min(ts(end + timedelta(days=1)), int(time.time()))

    def recent_posts(self, limit: int = 25, after: str | None = None) -> dict:
        body = self.client.list_posts(limit, after)
        raw = [p for p in body.get("data", []) if p.get("id")]
        self.db.upsert_posts(raw)
        cursors = (body.get("paging") or {}).get("cursors") or {}
        has_next = bool((body.get("paging") or {}).get("next"))
        return {"posts": [_post_view(p) for p in raw],
                "paging": {"next_cursor": cursors.get("after") if has_next else None, "has_next": has_next}}

    def post_insights(self, post_id: str) -> dict:
        validate_post_id(post_id)
        metrics = self.client.post_insights(post_id)
        try:
            post = self.client.get_post(post_id)
        except (AuthError, RateLimitError):
            raise
        except Exception:
            post = {"id": post_id}
        fetched = self.db.save_post_insights(post, metrics)
        return {"post": _post_view(post), "fetched_at": fetched, "insights": metrics,
                "engagement_rate": engagement_rate(metrics)}

    def compare(self, post_ids: list[str] | None = None, recent: int | None = None) -> dict:
        if post_ids and recent:
            raise ValidationError("specify either post_ids or recent, not both")
        if post_ids:
            if len(post_ids) > MAX_COMPARE_IDS:
                raise ValidationError(f"at most {MAX_COMPARE_IDS} post_ids")
            ids = [validate_post_id(i) for i in dict.fromkeys(post_ids)]
            posts = {}
        else:
            n = 5 if recent is None else recent
            if not isinstance(n, int) or not 1 <= n <= MAX_COMPARE_RECENT:
                raise ValidationError(f"recent must be an integer 1..{MAX_COMPARE_RECENT}")
            listing = self.client.list_posts(n)["data"][:n]
            posts = {p["id"]: p for p in listing if p.get("id")}
            ids = list(posts)

        rows = []
        for pid in ids:
            try:
                metrics = self.client.post_insights(pid)
                post = posts.get(pid) or self._safe_get_post(pid)
                self.db.save_post_insights(post, metrics)
                rows.append({**_post_view(post), "insights": metrics,
                             "reactions": None if any(metrics[k] is None for k in
                                                      ("likes", "replies", "reposts", "quotes"))
                             else sum(metrics[k] for k in ("likes", "replies", "reposts", "quotes")),
                             "engagement_rate": engagement_rate(metrics), "error": None})
            except (AuthError, RateLimitError, PermissionError_):
                raise
            except APIError as e:
                rows.append({"id": pid, "insights": {k: None for k in MEDIA_METRICS},
                             "reactions": None, "engagement_rate": None, "error": str(e)})
        return {"posts": rows, "ranking": {
            "by_views": self._rank(rows, lambda r: r["insights"]["views"]),
            "by_reactions": self._rank(rows, lambda r: r["reactions"]),
            "by_engagement_rate": self._rank(rows, lambda r: r["engagement_rate"]),
        }, "engagement_rate_definition": "(likes+replies+reposts+quotes)/views*100; null if views is 0/unavailable"}

    def _safe_get_post(self, pid: str) -> dict:
        try:
            return self.client.get_post(pid)
        except (AuthError, RateLimitError):
            raise
        except Exception:
            return {"id": pid}

    @staticmethod
    def _rank(rows, key) -> list[str]:
        ok = [r for r in rows if key(r) is not None]
        return [r["id"] for r in sorted(ok, key=key, reverse=True)]
