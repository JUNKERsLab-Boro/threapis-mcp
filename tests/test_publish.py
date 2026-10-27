import asyncio
import os
import sys
from urllib.parse import parse_qsl

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.conftest import make_config
from threads_insights.db import Database
from threads_insights.errors import AmbiguousOutcomeError, PermissionError_, ThreadsError, ValidationError
from threads_insights.publish_service import PublishService
from threads_insights.publish_store import PublishStore
from threads_insights.publisher import ThreadsPublisher
from threads_insights.tokens import TokenStore


class FakePublishAPI:
    def __init__(self):
        self.posts_made = []           # one entry per post that actually went live
        self.container_status = "FINISHED"
        self.create_error = None       # "timeout"
        self.publish_mode = "ok"       # ok | timeout | 500 | reject | timeout_but_published
        self.recent = []               # what /threads listing returns
        self.requests = []
        self.last_create = {}

    def count(self, method, path):
        return sum(1 for m, p in self.requests if (m, p) == (method, path))

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1.0/")
        self.requests.append((req.method, path))
        if req.method == "GET":
            if path == "42":
                return httpx.Response(200, json={"id": "42", "username": "myacct"})
            if path == "CONT1":
                return httpx.Response(200, json={"id": "CONT1", "status": self.container_status})
            if path == "42/threads":
                return httpx.Response(200, json={"data": self.recent})
            if path.startswith("MEDIA"):
                return httpx.Response(200, json={"permalink": "https://threads.com/@myacct/post/X"})
        if req.method == "POST":
            form = dict(parse_qsl(req.content.decode()))
            assert "access_token" in form
            if path == "42/threads":
                if self.create_error == "timeout":
                    raise httpx.ReadTimeout("t")
                self.last_create = form
                return httpx.Response(200, json={"id": "CONT1"})
            if path == "42/threads_publish":
                assert form["creation_id"] == "CONT1"
                if self.publish_mode == "timeout":
                    raise httpx.ReadTimeout("t")
                if self.publish_mode == "500":
                    return httpx.Response(502, text="bad gateway")
                if self.publish_mode == "reject":
                    return httpx.Response(400, json={"error": {"message": "invalid", "code": 100}})
                self.posts_made.append("x")
                if self.publish_mode == "timeout_but_published":
                    raise httpx.ReadTimeout("t")
                return httpx.Response(200, json={"id": "MEDIA1"})
        return httpx.Response(404, json={"error": {"message": "nope", "code": 100}})


@pytest.fixture
def api():
    return FakePublishAPI()


@pytest.fixture
def ps(cfg, api):
    pub = ThreadsPublisher(cfg, httpx.Client(transport=httpx.MockTransport(api)))
    return PublishService(pub, PublishStore(Database(cfg.db_path)), wait_seconds=0, sleep=lambda s: None)


def prep(ps, text="hello threads"):
    p = ps.prepare(text)
    return p["request_id"], p["confirm_hash"], p


def test_prepare_does_not_publish_and_shows_account(ps, api):
    rid, h, p = prep(ps)
    assert p["preview"]["account"] == "@myacct" and p["preview"]["text"] == "hello threads"
    assert "PUBLIC" in p["preview"]["visibility"]
    assert not any(m == "POST" for m, _ in api.requests)


def test_validation(ps):
    for bad in ("", "   ", "a" * 501, "😀" * 126):  # 126 emoji = 504 UTF-8 bytes
        with pytest.raises(ValidationError):
            ps.prepare(bad)
    with pytest.raises(ValidationError):
        ps.prepare("ok", reply_control="evil")
    with pytest.raises(ValidationError):
        ps.prepare("ok", topic_tag="a.b")
    ps.prepare("😀" * 125)


def test_publish_requires_matching_confirm_hash(ps, api):
    rid, h, _ = prep(ps)
    with pytest.raises(ValidationError):
        ps.publish(rid, "0" * 64)
    with pytest.raises(ValidationError):
        ps.publish("not-an-id", h)
    assert api.posts_made == [] and api.count("POST", "42/threads") == 0


def test_happy_path_and_second_call_does_not_repost(ps, api):
    rid, h, _ = prep(ps)
    r = ps.publish(rid, h)
    assert r["status"] == "published" and r["media_id"] == "MEDIA1" and r["permalink"]
    r2 = ps.publish(rid, h)
    assert r2["already_published"] and len(api.posts_made) == 1


def test_reply_control_sent(ps, api):
    p = ps.prepare("hi", reply_control="followers_only")
    ps.publish(p["request_id"], p["confirm_hash"])
    assert api.last_create["reply_control"] == "followers_only" and api.last_create["media_type"] == "TEXT"


def test_duplicate_text_refused(ps, api):
    rid, h, _ = prep(ps, "same text")
    ps.publish(rid, h)
    rid2, h2, p2 = prep(ps, "same text")
    assert p2["duplicate_warning"]
    with pytest.raises(ValidationError, match="Identical"):
        ps.publish(rid2, h2)
    assert len(api.posts_made) == 1


def test_container_not_ready_publishes_nothing_then_resumes(ps, api):
    api.container_status = "IN_PROGRESS"
    rid, h, _ = prep(ps)
    with pytest.raises(ThreadsError, match="not ready"):
        ps.publish(rid, h)
    assert ps.status(rid)["status"] == "container_created" and api.posts_made == []
    api.container_status = "FINISHED"
    assert ps.publish(rid, h)["status"] == "published"
    assert api.count("POST", "42/threads") == 1  # container reused, not recreated


def test_container_error_marks_failed_no_publish(ps, api):
    api.container_status = "ERROR"
    rid, h, _ = prep(ps)
    with pytest.raises(ThreadsError, match="Nothing was published"):
        ps.publish(rid, h)
    assert ps.status(rid)["status"] == "failed" and api.posts_made == []


def test_create_timeout_is_safe_to_retry(ps, api):
    api.create_error = "timeout"
    rid, h, _ = prep(ps)
    with pytest.raises(AmbiguousOutcomeError):
        ps.publish(rid, h)
    assert ps.status(rid)["status"] == "prepared"
    api.create_error = None
    assert ps.publish(rid, h)["status"] == "published"


def test_definite_rejection_marks_failed_without_auto_retry(ps, api):
    api.publish_mode = "reject"
    rid, h, _ = prep(ps)
    with pytest.raises(ThreadsError):
        ps.publish(rid, h)
    assert ps.status(rid)["status"] == "failed"
    assert api.count("POST", "42/threads_publish") == 1  # exactly one attempt


@pytest.mark.parametrize("mode", ["timeout", "500"])
def test_ambiguous_publish_blocks_blind_retry(ps, api, mode):
    api.publish_mode = mode
    rid, h, _ = prep(ps)
    with pytest.raises(AmbiguousOutcomeError, match="MAY be live"):
        ps.publish(rid, h)
    assert ps.status(rid)["status"] == "unknown"
    with pytest.raises(ThreadsError, match="UNKNOWN"):
        ps.publish(rid, h)  # refused without the explicit flag
    assert api.count("POST", "42/threads_publish") == 1


def test_unknown_reconciles_to_published_when_post_is_live(ps, api):
    api.publish_mode = "timeout_but_published"
    rid, h, _ = prep(ps, "reconcile me")
    with pytest.raises(AmbiguousOutcomeError):
        ps.publish(rid, h)
    api.recent = [{"id": "LIVE9", "text": "reconcile me", "timestamp": "2099-01-01T00:00:00+0000",
                   "permalink": "https://threads.com/@myacct/post/LIVE9"}]
    st = ps.status(rid)
    assert st["status"] == "published" and st["media_id"] == "LIVE9"
    r = ps.publish(rid, h, allow_retry_after_unknown=True)  # even with the flag: no second post
    assert r["already_published"] and len(api.posts_made) == 1


def test_unknown_not_found_then_explicit_retry(ps, api):
    api.publish_mode = "timeout"
    rid, h, _ = prep(ps)
    with pytest.raises(AmbiguousOutcomeError):
        ps.publish(rid, h)
    st = ps.status(rid)
    assert st["status"] == "unknown" and "note" in st
    api.publish_mode = "ok"
    r = ps.publish(rid, h, allow_retry_after_unknown=True)
    assert r["status"] == "published" and len(api.posts_made) == 1


def test_draft_expiry(ps, api):
    rid, h, _ = prep(ps)
    with ps.store.db._conn() as c:
        c.execute("UPDATE post_requests SET expires_at='2000-01-01T00:00:00Z'")
    with pytest.raises(ValidationError, match="expired"):
        ps.publish(rid, h)
    assert api.posts_made == []


def test_token_without_publish_scope_is_rejected(tmp_path, api):
    cfg = make_config(tmp_path, access_token=None)
    TokenStore(cfg).save("TOK", 5_000_000, "42", ["threads_basic", "threads_manage_insights"])
    pub = ThreadsPublisher(cfg, httpx.Client(transport=httpx.MockTransport(api)))
    svc = PublishService(pub, PublishStore(Database(cfg.db_path)), wait_seconds=0, sleep=lambda s: None)
    p = svc.prepare("hi")  # the read-only account lookup still works
    with pytest.raises(PermissionError_, match="login --publish"):
        svc.publish(p["request_id"], p["confirm_hash"])
    assert api.posts_made == [] and api.count("POST", "42/threads") == 0


def test_publish_tools_only_registered_when_enabled(tmp_path):
    base = {**os.environ, "THREADS_ACCESS_TOKEN": "x", "THREADS_TOKEN_FILE": str(tmp_path / "n.json"),
            "THREADS_DB_PATH": str(tmp_path / "x.db")}

    async def names(flag):
        params = StdioServerParameters(command=sys.executable, args=["-m", "threads_insights.server"],
                                       env={**base, "THREADS_ENABLE_PUBLISH": flag}, cwd=str(tmp_path))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name: t for t in (await s.list_tools()).tools}

    off, on = asyncio.run(names("0")), asyncio.run(names("1"))
    assert set(off) == {"get_account_insights", "get_post_insights", "get_recent_posts", "compare_posts"}
    assert set(on) - set(off) == {"prepare_text_post", "publish_text_post", "get_post_request_status"}
    assert "PUBLIC" in on["publish_text_post"].description
