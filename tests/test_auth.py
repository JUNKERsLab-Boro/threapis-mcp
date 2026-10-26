import json
import stat
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tests.conftest import TOKEN, make_config
from threads_insights import auth
from threads_insights.client import ThreadsClient
from threads_insights.errors import AuthError
from threads_insights.tokens import TokenStore


def test_authorize_url_minimal_scopes_and_state(cfg):
    q = parse_qs(urlparse(auth.build_authorize_url(cfg, "STATE1")).query)
    assert q["scope"] == ["threads_basic,threads_manage_insights"]
    assert q["state"] == ["STATE1"] and q["response_type"] == ["code"]


def test_callback_state_validation():
    assert auth.parse_callback("https://x/cb?code=abc&state=S", "S") == "abc"
    with pytest.raises(AuthError):
        auth.parse_callback("https://x/cb?code=abc&state=EVIL", "S")
    with pytest.raises(AuthError):
        auth.parse_callback("https://x/cb?code=abc", "S")
    with pytest.raises(AuthError):
        auth.parse_callback("https://x/cb?error=access_denied&error_reason=user_denied&state=S", "S")


def test_login_flow_saves_0600_token(tmp_path):
    cfg = make_config(tmp_path, access_token=None)
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/oauth/access_token":
            body = parse_qs(req.content.decode())
            assert body["grant_type"] == ["authorization_code"] and body["code"] == ["thecode"]
            return httpx.Response(200, json={"access_token": "short", "user_id": 42})
        if req.url.path == "/access_token":
            assert req.url.params["grant_type"] == "th_exchange_token"
            assert req.url.params["access_token"] == "short"
            return httpx.Response(200, json={"access_token": "LONG", "token_type": "bearer", "expires_in": 5183944})
        raise AssertionError(req.url)

    out = []
    def ask(_):
        state = parse_qs(urlparse(out[1]).query)["state"][0]
        return f"https://example.com/cb?code=thecode&state={state}"
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        auth.login(cfg, http, ask=ask, out=out.append)
    t = TokenStore(cfg).load()
    assert t.access_token == "LONG" and t.user_id == "42" and 59 < (t.expires_at - time.time()) / 86400 <= 60
    assert stat.S_IMODE(cfg.token_file.stat().st_mode) == 0o600
    assert all("LONG" not in line for line in out)


def _save(cfg, expires_in, age_s):
    s = TokenStore(cfg)
    s.save("OLD", expires_in, "42")
    d = json.loads(cfg.token_file.read_text()); d["refreshed_at"] -= age_s
    cfg.token_file.write_text(json.dumps(d))


def refresh_handler(req):
    assert req.url.path == "/refresh_access_token" and req.url.params["grant_type"] == "th_refresh_token"
    return httpx.Response(200, json={"access_token": "NEW", "token_type": "bearer", "expires_in": 5183944})


def test_refresh_when_near_expiry(tmp_path):
    cfg = make_config(tmp_path, access_token=None)
    _save(cfg, 3 * 86400, age_s=57 * 86400)
    with httpx.Client(transport=httpx.MockTransport(refresh_handler)) as http:
        assert auth.refresh_if_needed(cfg, http) is True
    assert TokenStore(cfg).load().access_token == "NEW"


def test_no_refresh_when_fresh_or_too_young(tmp_path):
    cfg = make_config(tmp_path, access_token=None)
    _save(cfg, 50 * 86400, age_s=10 * 86400)
    with httpx.Client(transport=httpx.MockTransport(refresh_handler)) as http:
        assert auth.refresh_if_needed(cfg, http) is False
    _save(cfg, 3 * 86400, age_s=3600)  # near expiry but < 24h old
    with httpx.Client(transport=httpx.MockTransport(refresh_handler)) as http:
        assert auth.refresh_if_needed(cfg, http) is False


def test_expired_token_detected_without_api_call(tmp_path):
    cfg = make_config(tmp_path, access_token=None)
    _save(cfg, -10, age_s=0)
    def nope(req): raise AssertionError("should not call API")
    c = ThreadsClient(cfg, httpx.Client(transport=httpx.MockTransport(nope)))
    with pytest.raises(AuthError, match="expired"):
        c.get_post("1")


def test_missing_token(tmp_path):
    cfg = make_config(tmp_path, access_token=None)
    with pytest.raises(AuthError):
        TokenStore(cfg).load()
