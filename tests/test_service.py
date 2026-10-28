import httpx
import pytest

from threads_insights.client import ThreadsClient
from threads_insights.errors import AuthError, RateLimitError, ValidationError
from threads_insights.service import engagement_rate


def test_engagement_rate_rules():
    assert engagement_rate({"views": 100, "likes": 5, "replies": 3, "reposts": 1, "quotes": 1}) == 10.0
    assert engagement_rate({"views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0}) is None
    assert engagement_rate({"views": None, "likes": 1, "replies": 1, "reposts": 1, "quotes": 1}) is None
    assert engagement_rate({"views": 10, "likes": 1, "replies": None, "reposts": 1, "quotes": 1}) is None


def test_post_insights_null_for_missing_and_saved(svc):
    r = svc.post_insights("3")
    assert r["insights"] == {"views": 50, "likes": 10, "replies": None, "reposts": None, "quotes": None}
    assert r["engagement_rate"] is None
    assert svc.db.count("post_insights") == 1 and svc.db.count("posts") == 1


def test_post_id_validation(svc):
    for bad in ("abc", "1/../me", "1?x=2", "", "1" * 40):
        with pytest.raises(ValidationError):
            svc.post_insights(bad)


def test_recent_posts_pagination_and_limit(svc):
    r = svc.recent_posts(2)
    assert len(r["posts"]) == 2 and r["paging"] == {"next_cursor": "AFTERCUR", "has_next": True}
    r2 = svc.recent_posts(3, after="AFTERCUR")
    assert r2["paging"]["has_next"] is False
    for bad in (0, 101):
        with pytest.raises(ValidationError):
            svc.recent_posts(bad)
    with pytest.raises(ValidationError):
        svc.recent_posts(5, after="x&access_token=evil")
    assert svc.db.count("posts") == 3


def test_never_follows_paging_next(svc, fake):
    svc.recent_posts(2)
    assert all("never-followed" not in p for p, _ in fake.calls)


def test_account_insights(svc):
    r = svc.account_insights(days=7)
    assert r["views"] == 25 and r["likes"] == 7 and r["clicks"] == 4 and r["followers_count"] == 321
    assert r["reposts"] is None and r["quotes"] is None
    assert svc.db.count("account_insights") == 1
    r = svc.account_insights(breakdown="age")
    assert r["follower_demographics"] is not None
    with pytest.raises(ValidationError):
        svc.account_insights(days=0)
    with pytest.raises(ValidationError):
        svc.account_insights(breakdown="evil")


def test_compare_ids_and_ranking(svc):
    r = svc.compare(post_ids=["1", "2", "3"])
    rates = {p["id"]: p["engagement_rate"] for p in r["posts"]}
    assert rates == {"1": 10.0, "2": None, "3": None}
    assert r["ranking"]["by_views"] == ["1", "3", "2"]
    assert r["ranking"]["by_engagement_rate"] == ["1"]


def test_compare_recent(svc):
    r = svc.compare(recent=2)
    assert [p["id"] for p in r["posts"]] == ["1", "2"]
    with pytest.raises(ValidationError):
        svc.compare(post_ids=["1"], recent=2)
    with pytest.raises(ValidationError):
        svc.compare(recent=26)


def test_compare_isolates_per_post_api_error(svc, fake):
    fake.fail_insights_for["2"] = (400, {"error": {"message": "REPOST_FACADE", "code": 100}})
    r = svc.compare(post_ids=["1", "2"])
    assert r["posts"][1]["error"] and r["posts"][0]["error"] is None


def test_rate_limit_and_auth_errors_and_history_preserved(svc, fake):
    svc.post_insights("1")
    assert svc.db.count("post_insights") == 1
    fake.fail_insights_for["1"] = (429, {"error": {"message": "slow", "code": 4}})
    with pytest.raises(RateLimitError):
        svc.post_insights("1")
    fake.fail_insights_for["1"] = (400, {"error": {"message": "expired", "code": 190, "type": "OAuthException"}})
    with pytest.raises(AuthError):
        svc.post_insights("1")
    assert svc.db.count("post_insights") == 1  # untouched by failures


def test_token_not_in_error_messages(cfg):
    def boom(request):
        raise httpx.ConnectError("failed for " + str(request.url))
    c = ThreadsClient(cfg, httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(Exception) as e:
        c.get_post("1")
    assert "SECRET-TOKEN" not in str(e.value) and "SECRET-TOKEN" not in repr(e.value.__cause__)


def test_wal_and_duplicate_snapshot(svc):
    import sqlite3
    assert sqlite3.connect(svc.db.path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    svc.post_insights("1"); svc.post_insights("1")  # same-second snapshots are deduped by UNIQUE
    assert svc.db.count("post_insights") >= 1
    assert svc.db.count("posts") == 1


def test_account_insights_date_range(svc, fake):
    r = svc.account_insights(start_date="2026-10-21", end_date="2026-10-28")
    assert r["period"]["since"] == 1792540800  # 2026-10-21T00:00Z
    assert r["period"]["until"] <= 1793232000 and r["views"] == 25
    with pytest.raises(ValidationError):
        svc.account_insights(start_date="2026-10-28", end_date="2026-10-21")
    with pytest.raises(ValidationError):
        svc.account_insights(start_date="bad")
    with pytest.raises(ValidationError):
        svc.account_insights(days=3, start_date="2026-10-21")
