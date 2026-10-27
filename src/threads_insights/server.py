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


# --- Publishing (opt-in: THREADS_ENABLE_PUBLISH=1). Separate from the read-only analytics tools. ---
if Config.from_env().enable_publish:
    from .publish_service import PublishService
    from .publish_store import PublishStore
    from .publisher import ThreadsPublisher

    _pub: PublishService | None = None

    def get_publish_service() -> PublishService:
        global _pub
        if _pub is None:
            cfg = Config.from_env()
            _pub = PublishService(ThreadsPublisher(cfg), PublishStore(Database(cfg.db_path)))
        return _pub

    @mcp.tool()
    def prepare_text_post(
        text: Annotated[str, Field(min_length=1, max_length=500, description="Post text (max 500; emoji count by UTF-8 bytes)")],
        reply_control: Annotated[str, Field(description="everyone|accounts_you_follow|mentioned_only|followers_only")] = "everyone",
        topic_tag: Annotated[str | None, Field(max_length=50)] = None,
    ) -> dict:
        """Step 1 of posting. Does NOT publish. Validates a text post and returns a preview (account, text,
        reply audience) plus request_id and confirm_hash. You MUST show the preview to the user and get their
        explicit approval before calling publish_text_post."""
        return get_publish_service().prepare(text, reply_control, topic_tag)

    @mcp.tool()
    def publish_text_post(
        request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
        confirm_hash: Annotated[str, Field(description="confirm_hash from prepare_text_post for the exact text the user approved")],
        allow_retry_after_unknown: Annotated[bool, Field(description="Only after get_post_request_status showed the post is NOT live AND the user agreed to retry")] = False,
    ) -> dict:
        """PUBLIC, IRREVERSIBLE-ISH ACTION: publishes the prepared text post to the user's live Threads account.
        Call only after the user has explicitly approved the exact account and text shown by prepare_text_post.
        Never auto-retry: on an 'unknown outcome' error, call get_post_request_status and ask the user first.
        Calling again for an already published request returns the existing post and does not post twice."""
        return get_publish_service().publish(request_id, confirm_hash, allow_retry_after_unknown)

    @mcp.tool()
    def get_post_request_status(request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> dict:
        """Read-only status of a post request (prepared/published/failed/unknown). For an unknown outcome it
        checks the account's recent posts to see whether the post actually went live."""
        return get_publish_service().status(request_id)


def main() -> None:
    transport = os.environ.get("THREADS_MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "streamable-http"):
        raise SystemExit("THREADS_MCP_TRANSPORT must be stdio or streamable-http")
    mcp.run(transport=transport)  # streamable-http binds 127.0.0.1 by default


if __name__ == "__main__":
    main()
