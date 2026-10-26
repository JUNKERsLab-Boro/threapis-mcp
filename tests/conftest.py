import json
import time

import httpx
import pytest

from threads_insights.client import ThreadsClient
from threads_insights.config import Config
from threads_insights.db import Database
from threads_insights.service import Service

TOKEN = "SECRET-TOKEN-123"


def make_config(tmp_path, **kw):
    base = dict(app_id="app", app_secret="sec", redirect_uri="https://example.com/cb",
                access_token=TOKEN, user_id="42", db_path=tmp_path / "t.db",
                token_file=tmp_path / "token.json", timeout=5)
    base.update(kw)
    return Config(**base)


class FakeThreads:
    """Routes mocked Graph API calls. `calls` records (path, params)."""

    def __init__(self):
        self.calls = []
        self.fail_insights_for = {}  # post_id -> (status, body)
        self.insights = {
            "1": {"views": 100, "likes": 5, "replies": 3, "reposts": 1, "quotes": 1},
            "2": {"views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0},
            "3": {"views": 50, "likes": 10},  # partial -> others null
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1.0/")
        params = dict(request.url.params)
        self.calls.append((path, params))
        assert request.url.host == "graph.threads.com" and request.method == "GET"
        if path == "42/threads":
            data = [{"id": i, "text": f"post {i}", "timestamp": "2025-01-01T00:00:00+0000",
                     "permalink": f"https://threads.com/@u/post/{i}", "media_type": "TEXT_POST",
                     "username": "u"} for i in ("1", "2", "3")]
            paging = {"cursors": {"before": "B", "after": "AFTERCUR"}}
            if params.get("after") != "AFTERCUR":
                paging["next"] = "https://graph.threads.com/never-followed"
            return httpx.Response(200, json={"data": data[: int(params["limit"])], "paging": paging})
        if path.endswith("/insights"):
            pid = path.split("/")[0]
            if pid in self.fail_insights_for:
                st, body = self.fail_insights_for[pid]
                return httpx.Response(st, json=body)
            vals = self.insights[pid]
            return httpx.Response(200, json={"data": [
                {"name": k, "period": "lifetime", "values": [{"value": v}]} for k, v in vals.items()]})
        if path == "42/threads_insights":
            m = params["metric"]
            if m == "followers_count":
                return httpx.Response(200, json={"data": [{"name": "followers_count", "total_value": {"value": 321}}]})
            if m == "follower_demographics":
                return httpx.Response(200, json={"data": [{"name": "follower_demographics", "total_value": {"breakdowns": []}}]})
            return httpx.Response(200, json={"data": [
                {"name": "views", "values": [{"value": 10, "end_time": "a"}, {"value": 15, "end_time": "b"}]},
                {"name": "likes", "total_value": {"value": 7}},
                {"name": "replies", "total_value": {"value": 2}},
                {"name": "clicks", "link_total_values": [{"link_url": "https://x.y", "value": 4}]},
            ]})
        if path in ("1", "2", "3"):
            return httpx.Response(200, json={"id": path, "text": f"post {path}", "timestamp": "t",
                                             "permalink": f"https://threads.com/@u/post/{path}",
                                             "media_type": "TEXT_POST", "username": "u"})
        return httpx.Response(404, json={"error": {"message": "nope", "code": 100}})


@pytest.fixture
def fake():
    return FakeThreads()


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def svc(cfg, fake):
    client = ThreadsClient(cfg, httpx.Client(transport=httpx.MockTransport(fake)))
    return Service(client, Database(cfg.db_path))
