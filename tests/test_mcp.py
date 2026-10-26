import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from threads_insights import server


def test_tools_registered_readonly():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"get_account_insights", "get_post_insights", "get_recent_posts", "compare_posts"}


def test_tool_call_in_process(svc, monkeypatch):
    monkeypatch.setattr(server, "get_service", lambda: svc)
    res = asyncio.run(server.mcp.call_tool("compare_posts", {"post_ids": ["1", "3"]}))
    content, structured = res if isinstance(res, tuple) else (res, None)
    data = structured or content
    assert "engagement_rate" in str(data)


def test_tool_rejects_bad_id(svc, monkeypatch):
    monkeypatch.setattr(server, "get_service", lambda: svc)
    with pytest.raises(Exception):
        asyncio.run(server.mcp.call_tool("get_post_insights", {"post_id": "1/../x"}))


def test_stdio_roundtrip_without_token(tmp_path):
    """Real subprocess over stdio: lists tools; a call without a token returns a clean error."""
    env = {**os.environ, "THREADS_ACCESS_TOKEN": "", "THREADS_TOKEN_FILE": str(tmp_path / "none.json"),
           "THREADS_DB_PATH": str(tmp_path / "x.db")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "threads_insights.server"], env=env, cwd=str(tmp_path))

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                res = await s.call_tool("get_recent_posts", {"limit": 3})
                return [t.name for t in tools.tools], res

    names, res = asyncio.run(run())
    assert len(names) == 4 and res.isError
    assert "No Threads access token" in res.content[0].text
