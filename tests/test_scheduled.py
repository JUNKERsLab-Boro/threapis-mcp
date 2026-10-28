from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.test_publish import FakePublishAPI
from threads_insights.db import Database
from threads_insights.errors import ThreadsError, ValidationError
from threads_insights.publish_service import PublishService
from threads_insights.publish_store import PublishStore
from threads_insights.publisher import ThreadsPublisher
from threads_insights.scheduled import JST, ScheduledService, slot_for

AT_7 = datetime(2026, 10, 30, 7, 3, tzinfo=JST)
AT_20 = datetime(2026, 10, 30, 20, 1, tzinfo=JST)


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t.astimezone(timezone.utc)


@pytest.fixture
def api():
    return FakePublishAPI()


@pytest.fixture
def clock():
    return Clock(AT_7)


def make(cfg, api, clock, username="myacct"):
    ps = PublishService(ThreadsPublisher(cfg, httpx.Client(transport=httpx.MockTransport(api))),
                        PublishStore(Database(cfg.db_path)), wait_seconds=0, sleep=lambda s: None)
    return ScheduledService(ps, username, now=clock, settle_seconds=0, sleep=lambda s: None)


@pytest.fixture
def sch(cfg, api, clock):
    return make(cfg, api, clock)


def test_slot_windows():
    g = timedelta(minutes=30)
    assert slot_for(AT_7, g)[0] == "2026-10-30@07"
    assert slot_for(datetime(2026, 10, 30, 6, 56, tzinfo=JST), g)[0] == "2026-10-30@07"
    assert slot_for(datetime(2026, 10, 30, 19, 58, tzinfo=JST), g)[0] == "2026-10-30@20"
    for t in [(6, 50), (7, 30), (12, 0), (20, 45), (23, 59)]:
        assert slot_for(datetime(2026, 10, 30, *t, tzinfo=JST), g) is None
    # UTC input is converted to JST
    assert slot_for(datetime(2026, 10, 29, 22, 5, tzinfo=timezone.utc), g)[0] == "2026-10-30@07"


def test_success_returns_id_text_time(sch, api):
    r = sch.publish("今日の修理メモ")
    assert r["status"] == "published" and r["post_id"] == "MEDIA1" and r["text"] == "今日の修理メモ"
    assert r["published_at"] and r["account"] == "@myacct" and r["slot"] == "2026-10-30@07"
    assert len(api.posts_made) == 1


def test_outside_slot_rejected_without_api_calls(cfg, api):
    s = make(cfg, api, Clock(datetime(2026, 10, 30, 12, 0, tzinfo=JST)))
    with pytest.raises(ValidationError, match="Outside"):
        s.publish("hello")
    assert api.requests == [] and api.posts_made == []


def test_one_post_per_slot_and_second_call_is_idempotent(sch, api):
    sch.publish("first")
    r = sch.publish("first")
    assert r["already_published"] and r["post_id"] == "MEDIA1"
    r2 = sch.publish("different text")  # same slot: still returns the first, never posts again
    assert r2["already_published"] and r2["text"] == "first" and len(api.posts_made) == 1


def test_next_slot_allows_new_post(cfg, api, clock):
    s = make(cfg, api, clock)
    s.publish("morning")
    clock.t = AT_20
    r = s.publish("evening")
    assert r["slot"] == "2026-10-30@20" and len(api.posts_made) == 2


def test_empty_and_overlong_rejected(sch, api):
    for bad in ("", "  \n", "a" * 501):
        with pytest.raises(ValidationError):
            sch.publish(bad)
    assert api.requests == []


@pytest.mark.parametrize("bad", ["中学生が直した", "15歳のPC修理", "高校2年生の記録", "中2の夏"])
def test_age_grade_text_rejected(sch, api, bad):
    with pytest.raises(ValidationError, match="age"):
        sch.publish(bad)
    assert api.requests == []


def test_audience_age_words_are_allowed(sch, api):
    assert sch.publish("40代のPC好きへ。古いノートのSSD換装メモ")["status"] == "published"


def test_duplicate_of_earlier_slot_rejected(cfg, api, clock):
    s = make(cfg, api, clock)
    s.publish("same body")
    clock.t = AT_20
    with pytest.raises(ValidationError, match="duplicate"):
        s.publish("same body")
    assert len(api.posts_made) == 1


def test_duplicate_of_recent_threads_post_rejected(sch, api):
    api.recent = [{"id": "9", "text": "already there", "timestamp": "2026-10-29T00:00:00+0000"}]
    with pytest.raises(ValidationError, match="recent posts"):
        sch.publish("already there")
    assert api.posts_made == []


def test_wrong_account_rejected_before_any_write(cfg, api, clock):
    s = make(cfg, api, clock, username="junkerslabtech")  # API says the token is @myacct
    with pytest.raises(ThreadsError, match="not @junkerslabtech"):
        s.publish("hello")
    assert api.count("POST", "42/threads") == 0 and api.posts_made == []


def test_timeout_but_published_is_reconciled_not_reposted(cfg, clock):
    class LiveAfterTimeout(FakePublishAPI):
        def __call__(self, req):
            try:
                return super().__call__(req)
            finally:
                if self.posts_made and not self.recent:  # the post really went live despite the timeout
                    self.recent = [{"id": "LIVE9", "text": "recover me", "timestamp": "2099-01-01T00:00:00+0000",
                                    "permalink": "https://threads.com/@myacct/post/LIVE9"}]
    api = LiveAfterTimeout()
    api.publish_mode = "timeout_but_published"
    s = make(cfg, api, clock)
    r = s.publish("recover me")
    assert r["status"] == "published" and r["post_id"] == "LIVE9"
    assert len(api.posts_made) == 1
    assert s.publish("recover me")["already_published"] and len(api.posts_made) == 1


def test_timeout_not_found_stays_unknown_and_never_reposts(sch, api):
    api.publish_mode = "timeout"
    r = sch.publish("maybe live")
    assert r["status"] == "unknown" and "Do NOT retry" in r["note"]
    api.publish_mode = "ok"
    r2 = sch.publish("maybe live")
    assert r2["status"] == "unknown" and api.count("POST", "42/threads_publish") == 1
    assert api.posts_made == []


def test_definite_failure_allows_fresh_attempt_in_same_slot(sch, api):
    api.publish_mode = "reject"
    with pytest.raises(ThreadsError):
        sch.publish("try again")
    api.publish_mode = "ok"
    r = sch.publish("try again")
    assert r["status"] == "published" and len(api.posts_made) == 1


def test_create_timeout_recovers_on_next_call(sch, api):
    api.create_error = "timeout"
    r = sch.publish("flaky")
    assert r["status"] == "prepared" and api.posts_made == []
    api.create_error = None
    assert sch.publish("flaky")["status"] == "published" and len(api.posts_made) == 1


def test_pending_slot_refuses_different_text(sch, api):
    api.container_status = "IN_PROGRESS"
    with pytest.raises(ThreadsError, match="not ready"):
        sch.publish("pending text")
    api.container_status = "FINISHED"
    with pytest.raises(ValidationError, match="different text"):
        sch.publish("other text")
    assert sch.publish("pending text")["status"] == "published" and len(api.posts_made) == 1


def test_token_never_in_results(sch):
    assert "TOK" not in str(sch.publish("no secrets here"))


def test_scheduled_tool_registered_only_when_enabled(tmp_path):
    import asyncio, os, sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    base = {**os.environ, "THREADS_ACCESS_TOKEN": "x", "THREADS_TOKEN_FILE": str(tmp_path / "n.json"),
            "THREADS_DB_PATH": str(tmp_path / "x.db"), "THREADS_ENABLE_PUBLISH": "0"}

    async def names(flag):
        params = StdioServerParameters(command=sys.executable, args=["-m", "threads_insights.server"],
                                       env={**base, "THREADS_ENABLE_SCHEDULED": flag}, cwd=str(tmp_path))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name: t for t in (await s.list_tools()).tools}

    off, on = asyncio.run(names("0")), asyncio.run(names("1"))
    assert "publish_scheduled_post" not in off and len(off) == 4
    assert set(on) - set(off) == {"publish_scheduled_post"}  # manual tools stay off unless enabled
    assert "PUBLIC" in on["publish_scheduled_post"].description
    assert on["publish_scheduled_post"].annotations.readOnlyHint is False
    assert all(on[n].annotations.readOnlyHint for n in off)
