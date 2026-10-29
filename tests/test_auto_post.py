import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.test_publish import FakePublishAPI
from threads_insights.auto_post import AutoPostService
from threads_insights.db import Database
from threads_insights.errors import RateLimitError, ThreadsError, ValidationError
from threads_insights.publish_service import PublishService
from threads_insights.publish_store import PublishStore
from threads_insights.publisher import ThreadsPublisher

T0 = datetime(2026, 10, 30, 3, 17, tzinfo=timezone.utc)  # deliberately not 07:00/20:00 JST


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


@pytest.fixture
def api():
    return FakePublishAPI()


@pytest.fixture
def clock():
    return Clock(T0)


def make(cfg, api, clock, username="myacct", interval=60, per_day=4):
    ps = PublishService(ThreadsPublisher(cfg, httpx.Client(transport=httpx.MockTransport(api))),
                        PublishStore(Database(cfg.db_path)), wait_seconds=0, sleep=lambda s: None)
    return AutoPostService(ps, username, interval, per_day, now=clock, settle_seconds=0, sleep=lambda s: None)


@pytest.fixture
def auto(cfg, api, clock):
    return make(cfg, api, clock)


def test_any_time_of_day_allowed(cfg, api, clock):
    s = make(cfg, api, clock, interval=1, per_day=24)
    for i, h in enumerate((0, 3, 12, 15, 23)):
        clock.t = T0.replace(hour=h, minute=i)
        assert s.publish(f"post {i}")["status"] == "published"


def test_success_returns_id_text_time_and_history(auto, api, cfg):
    r = auto.publish("今日の修理メモ")
    assert r["status"] == "published" and r["post_id"] == "MEDIA1" and r["text"] == "今日の修理メモ"
    assert r["published_at"] and r["published_at_jst"].endswith("+09:00") and r["account"] == "@myacct"
    with auto.ps.store.db._conn() as c:  # persisted in SQLite
        assert c.execute("SELECT COUNT(*) FROM auto_posts").fetchone()[0] == 1
        assert c.execute("SELECT status FROM post_requests").fetchone()[0] == "published"


def test_min_interval_rate_limit(auto, api, clock):
    auto.publish("one")
    clock.t = T0 + timedelta(minutes=59)
    with pytest.raises(RateLimitError, match="minutes"):
        auto.publish("two")
    clock.t = T0 + timedelta(minutes=61)
    assert auto.publish("two")["status"] == "published"
    assert len(api.posts_made) == 2


def test_daily_cap(cfg, api, clock):
    s = make(cfg, api, clock, interval=1, per_day=3)
    for i in range(3):
        clock.t = T0 + timedelta(minutes=10 * i)
        s.publish(f"p{i}")
    clock.t = T0 + timedelta(hours=2)
    with pytest.raises(RateLimitError, match="24h"):
        s.publish("p3")
    clock.t = T0 + timedelta(hours=25)  # window rolls
    assert s.publish("p3")["status"] == "published"


def test_runaway_loop_posts_once(auto, api):
    posted = 0
    for i in range(10):
        try:
            auto.publish(f"loop {i}"); posted += 1
        except RateLimitError:
            pass
    assert posted == 1 and len(api.posts_made) == 1


def test_empty_and_overlong_rejected(auto, api):
    for bad in ("", "  \n", "a" * 501):
        with pytest.raises(ValidationError):
            auto.publish(bad)
    assert api.requests == []


@pytest.mark.parametrize("bad", ["中学生が直した", "15歳のPC修理", "高校2年生の記録", "中2の夏"])
def test_age_grade_text_rejected(auto, api, bad):
    with pytest.raises(ValidationError, match="age"):
        auto.publish(bad)
    assert api.requests == []


def test_audience_age_words_are_allowed(auto):
    assert auto.publish("40代のPC好きへ。古いノートのSSD換装メモ")["status"] == "published"


def test_duplicate_text_rejected_even_after_interval(auto, api, clock):
    auto.publish("same body")
    clock.t = T0 + timedelta(hours=3)
    with pytest.raises(ValidationError, match="duplicate"):
        auto.publish("same body")
    assert len(api.posts_made) == 1


def test_duplicate_of_recent_threads_post_rejected(auto, api):
    api.recent = [{"id": "9", "text": "already there", "timestamp": "2026-10-29T00:00:00+0000"}]
    with pytest.raises(ValidationError, match="recent posts"):
        auto.publish("already there")
    assert api.posts_made == []


def test_wrong_account_rejected_before_any_write(cfg, api, clock):
    s = make(cfg, api, clock, username="junkerslabtech")  # token is @myacct
    with pytest.raises(ThreadsError, match="not @junkerslabtech"):
        s.publish("hello")
    assert api.count("POST", "42/threads") == 0


def test_timeout_but_published_is_reconciled_not_reposted(cfg, clock):
    class LiveAfterTimeout(FakePublishAPI):
        def __call__(self, req):
            try:
                return super().__call__(req)
            finally:
                if self.posts_made and not self.recent:
                    self.recent = [{"id": "LIVE9", "text": "recover me", "timestamp": "2099-01-01T00:00:00+0000",
                                    "permalink": "https://threads.com/@myacct/post/LIVE9"}]
    api = LiveAfterTimeout()
    api.publish_mode = "timeout_but_published"
    s = make(cfg, api, clock)
    r = s.publish("recover me")
    assert r["status"] == "published" and r["post_id"] == "LIVE9" and len(api.posts_made) == 1
    with pytest.raises(ValidationError):  # retry of the same text is refused
        s.publish("recover me")
    assert len(api.posts_made) == 1


def test_timeout_not_found_stays_unknown_and_blocks_everything(auto, api, clock):
    api.publish_mode = "timeout"
    r = auto.publish("maybe live")
    assert r["status"] == "unknown" and "Do NOT retry" in r["note"]
    api.publish_mode = "ok"
    again = auto.publish("maybe live")  # same text: re-check only
    assert again["status"] == "unknown" and api.count("POST", "42/threads_publish") == 1
    clock.t = T0 + timedelta(hours=2)  # past the interval, but outcome unresolved: new text refused
    with pytest.raises(ThreadsError, match="unresolved"):
        auto.publish("something else")
    assert api.posts_made == []


def test_unknown_resolves_when_found_live(auto, api, clock):
    api.publish_mode = "timeout"
    auto.publish("late arrival")
    api.recent = [{"id": "L1", "text": "late arrival", "timestamp": "2099-01-01T00:00:00+0000"}]
    r = auto.publish("late arrival")
    assert r["status"] == "published" and r["already_published"] and r["post_id"] == "L1"


def test_definite_failure_not_counted_and_retry_safe(auto, api):
    api.publish_mode = "reject"
    with pytest.raises(ThreadsError):
        auto.publish("try again")
    api.publish_mode = "ok"
    r = auto.publish("try again")
    assert r["status"] == "published" and len(api.posts_made) == 1


def test_create_timeout_recovers_on_next_call(auto, api):
    api.create_error = "timeout"
    r = auto.publish("flaky")
    assert r["status"] == "prepared" and api.posts_made == []
    api.create_error = None
    assert auto.publish("flaky")["status"] == "published" and len(api.posts_made) == 1


def test_token_never_in_results(auto):
    assert "TOK" not in str(auto.publish("no secrets here"))


def test_tool_registered_only_when_enabled(tmp_path):
    base = {**os.environ, "THREADS_ACCESS_TOKEN": "x", "THREADS_TOKEN_FILE": str(tmp_path / "n.json"),
            "THREADS_DB_PATH": str(tmp_path / "x.db"), "THREADS_ENABLE_PUBLISH": "0"}

    async def names(flag):
        params = StdioServerParameters(command=sys.executable, args=["-m", "threads_insights.server"],
                                       env={**base, "THREADS_ENABLE_AUTO_POST": flag}, cwd=str(tmp_path))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name: t for t in (await s.list_tools()).tools}

    off, on = asyncio.run(names("0")), asyncio.run(names("1"))
    assert "publish_auto_post" not in off and len(off) == 4
    assert set(on) - set(off) == {"publish_auto_post"}  # manual tools stay off unless enabled
    t = on["publish_auto_post"]
    assert "PUBLIC" in t.description and t.annotations.readOnlyHint is False
    assert all(on[n].annotations.readOnlyHint for n in off)
