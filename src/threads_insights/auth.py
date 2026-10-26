"""Threads OAuth (authorization-code flow) and long-lived token handling.

CLI:  threads-insights-auth login | refresh | status
"""
from __future__ import annotations

import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Config
from .errors import AuthError, ConfigError, NetworkError, ThreadsError
from .tokens import TokenStore

AUTHORIZE_URL = "https://threads.com/oauth/authorize"
GRAPH = "https://graph.threads.com"
# Least privilege: basic (required everywhere) + insights. No publish/reply scopes.
SCOPES = "threads_basic,threads_manage_insights"
REFRESH_MIN_AGE = 24 * 3600  # docs: token must be >= 24h old to refresh
REFRESH_WITHIN = 7 * 24 * 3600  # auto-refresh when < 7 days remain


def build_authorize_url(config: Config, state: str) -> str:
    if not (config.app_id and config.redirect_uri):
        raise ConfigError("THREADS_APP_ID and THREADS_REDIRECT_URI are required")
    q = urlencode(
        {
            "client_id": config.app_id,
            "redirect_uri": config.redirect_uri,
            "scope": SCOPES,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{q}"


def parse_callback(callback_url: str, expected_state: str) -> str:
    """Validate state (constant-time) and return the authorization code."""
    qs = parse_qs(urlparse(callback_url.strip()).query)
    if "error" in qs:
        raise AuthError(f"Authorization denied: {qs.get('error_reason', qs['error'])[0]}")
    got = qs.get("state", [""])[0]
    if not secrets.compare_digest(got.encode(), expected_state.encode()):
        raise AuthError("OAuth state mismatch (possible CSRF); aborting")
    code = qs.get("code", [""])[0]
    if code.endswith("#_"):
        code = code[:-2]
    if not code:
        raise AuthError("No authorization code in callback URL")
    return code


def _call(http: httpx.Client, method: str, url: str, **kw) -> dict:
    try:
        r = http.request(method, url, **kw)
    except httpx.TimeoutException as e:
        raise NetworkError("Timed out contacting Threads token endpoint") from None
    except httpx.HTTPError:
        raise NetworkError("Network error contacting Threads token endpoint") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        msg = (body.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        raise AuthError(f"Token endpoint rejected the request: {msg}")
    return body


def exchange_code(config: Config, code: str, http: httpx.Client) -> tuple[str, str | None]:
    """code -> short-lived token. Returns (token, user_id)."""
    if not (config.app_id and config.app_secret and config.redirect_uri):
        raise ConfigError("THREADS_APP_ID, THREADS_APP_SECRET, THREADS_REDIRECT_URI are required")
    b = _call(
        http,
        "POST",
        f"{GRAPH}/oauth/access_token",
        data={
            "client_id": config.app_id,
            "client_secret": config.app_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": config.redirect_uri,
        },
    )
    return b["access_token"], str(b["user_id"]) if b.get("user_id") is not None else None


def to_long_lived(config: Config, short_token: str, http: httpx.Client) -> tuple[str, int]:
    if not config.app_secret:
        raise ConfigError("THREADS_APP_SECRET is required")
    b = _call(
        http,
        "GET",
        f"{GRAPH}/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": config.app_secret,
            "access_token": short_token,
        },
    )
    return b["access_token"], int(b["expires_in"])


def refresh_long_lived(token: str, http: httpx.Client) -> tuple[str, int]:
    b = _call(
        http,
        "GET",
        f"{GRAPH}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token},
    )
    return b["access_token"], int(b["expires_in"])


def login(config: Config, http: httpx.Client, ask=input, out=print) -> None:
    store = TokenStore(config)
    state = secrets.token_urlsafe(24)
    out("1. Open this URL in a browser and approve access:\n")
    out(build_authorize_url(config, state))
    out("\n2. After approval you are redirected to your redirect URI (the page may fail to load).")
    cb = ask("3. Paste the full redirected URL here: ")
    code = parse_callback(cb, state)
    short, user_id = exchange_code(config, code, http)
    long, expires_in = to_long_lived(config, short, http)
    store.save(long, expires_in, user_id)
    out(f"Saved long-lived token to {config.token_file} (valid ~{expires_in // 86400} days).")


def refresh_if_needed(config: Config, http: httpx.Client, force: bool = False) -> bool:
    """Refresh the stored long-lived token when allowed and due. Returns True if refreshed."""
    store = TokenStore(config)
    t = store.load()
    if not t.from_file or t.expires_at is None:
        return False  # env tokens are managed by the user
    if t.expired:
        raise AuthError("Access token expired; run `threads-insights-auth login` again")
    now = time.time()
    if not force and t.expires_at - now > REFRESH_WITHIN:
        return False
    if t.refreshed_at and now - t.refreshed_at < REFRESH_MIN_AGE:
        if force:
            raise ThreadsError("Token is less than 24h old; Threads does not allow refresh yet")
        return False
    new, expires_in = refresh_long_lived(t.access_token, http)
    store.save(new, expires_in, t.user_id)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    config = Config.from_env()
    try:
        with httpx.Client(timeout=config.timeout) as http:
            if cmd == "login":
                login(config, http)
            elif cmd == "refresh":
                print("Refreshed." if refresh_if_needed(config, http, force=True) else "Nothing to do.")
            elif cmd == "status":
                t = TokenStore(config).load()
                left = None if t.expires_at is None else round((t.expires_at - time.time()) / 86400, 1)
                print(f"source={'file' if t.from_file else 'env'} days_left={left}")
            else:
                print("usage: threads-insights-auth login|refresh|status", file=sys.stderr)
                return 2
    except ThreadsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
