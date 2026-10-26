"""FastMCP server (stdio by default). Read-only: no publish/reply/delete tools exist."""
from __future__ import annotations

import logging
import os
import sys
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import ThreadsClient
from .config import Config
from .db import Database
from .service import Service

logging.basicConfig(level=logging.INFO, stream=sys.stderr)  # stdout is the MCP channel

mcp = FastMCP("threads-insights", instructions="Read-only Threads posts and insights.")
_service: Service | None = None


def get_service() -> Service:
    global _service
    if _service is None:
        cfg = Config.from_env()
        _service = Service(ThreadsClient(cfg), Database(cfg.db_path))
    return _service


@mcp.tool()
def get_account_insights(
    days: Annotated[int | None, Field(ge=1, le=365, description="Look-back window in days (default: API default, ~yesterday to today)")] = None,
    follower_breakdown: Annotated[str | None, Field(description="Optional: country|city|age|gender (needs >=100 followers)")] = None,
) -> dict:
    """Account-wide Threads insights: views, likes, replies, reposts, quotes, clicks, followers_count."""
    return get_service().account_insights(days, follower_breakdown)


@mcp.tool()
def get_post_insights(post_id: Annotated[str, Field(pattern=r"^\d{1,30}$", description="Numeric Threads media ID")]) -> dict:
    """Insights for one post: views, likes, replies, reposts, quotes (null if unavailable)."""
    return get_service().post_insights(post_id)


@mcp.tool()
def get_recent_posts(
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    after: Annotated[str | None, Field(description="Pagination cursor from a previous paging.next_cursor")] = None,
) -> dict:
    """Recent posts (id, text, timestamp, permalink...) with cursor pagination."""
    return get_service().recent_posts(limit, after)


@mcp.tool()
def compare_posts(
    post_ids: Annotated[list[str] | None, Field(max_length=20, description="Posts to compare")] = None,
    recent: Annotated[int | None, Field(ge=1, le=25, description="Or compare the N latest posts (default 5)")] = None,
) -> dict:
    """Compare posts by views, reactions and engagement rate = (likes+replies+reposts+quotes)/views*100."""
    return get_service().compare(post_ids, recent)


def main() -> None:
    transport = os.environ.get("THREADS_MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "streamable-http"):
        raise SystemExit("THREADS_MCP_TRANSPORT must be stdio or streamable-http")
    mcp.run(transport=transport)  # streamable-http binds 127.0.0.1 by default


if __name__ == "__main__":
    main()
