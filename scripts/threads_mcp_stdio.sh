#!/bin/sh
# Launch the Threads MCP server over stdio with absolute paths, independent of the caller's cwd/env.
# Used by tunnel-client (whose child-process env/cwd handling is undocumented).
# Publishing is OFF unless THREADS_ENABLE_PUBLISH=1 is exported before starting the tunnel.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
export THREADS_TOKEN_FILE="${THREADS_TOKEN_FILE:-$ROOT/data/token.json}"
export THREADS_DB_PATH="${THREADS_DB_PATH:-$ROOT/data/threads_insights.db}"
export THREADS_MCP_TRANSPORT=stdio
export THREADS_ENABLE_PUBLISH="${THREADS_ENABLE_PUBLISH:-0}"
export THREADS_ENABLE_AUTO_POST="${THREADS_ENABLE_AUTO_POST:-0}"
exec "$ROOT/.venv/bin/python" -m threads_insights.server
