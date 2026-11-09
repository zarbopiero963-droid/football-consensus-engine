"""WP-007 / WP-022a (audit G-03, G-04, G-12, G-23): OAuth hardening, offline.

RED on main a8fb228 (audit A_mcp_oauth.md): 60 wrong consent passwords in 0.19 s, all 401; DB
outage -> raw 500 on /oauth/token and authorize POST, 400 on DCR lookup; synchronous DB I/O on
the event loop; 1-char / non-ASCII PKCE verifier accepted / 500; no Cache-Control on tokens;
consent page frameable; arbitrary scopes stored; case-sensitive Bearer.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from urllib.parse import urlencode

import psycopg
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from bridge import oauth, oauth_store, oauth_throttle

PASSWORD = "LOCAL_TEST_ONLY_PASSWORD_5f2c"
DCR_ID = "fcbdcr1_" + "q" * 32
REDIRECT = oauth.CLAUDE_CALLBACK
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = oauth._pkce_s256(VERIFIER)


def _req(method, path, form=None, headers=None, client=("203.0.113.9", 4321)):
    body = urlencode(form or {}).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    hdrs = [(b"content-type", b"application/x-www-form-urlencoded")]
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    return Request({"type": "http", "method": method, "scheme": "https", "server": ("bridge", 443),
                    "path": path, "headers": hdrs, "query_string": b"", "client": client}, receive)


def _consent_form(password=PASSWORD, **over):
    form = {"client_id": DCR_ID, "redirect_uri": REDIRECT, "response_type": "code",
            "scope": "browser:use offline_access", "resource": oauth.resource_url(),
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "s",
            "password": password}
    form.update(over)
    return form


@pytest.fixture
def valid_client(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", PASSWORD)
    monkeypatch.setattr(oauth_store, "valid_claude_dcr_client", lambda cid, redirect: cid == DCR_ID)
    saved = []
    monkeypatch.setattr(oauth_store, "save_code", lambda code, **kw: saved.append((code, kw)))
    return saved


def _post(form, client=("203.0.113.9", 4321), headers=None):
    return asyncio.run(oauth.authorize_post(_req("POST", "/oauth/authorize", form, headers, client)))


# ---- G-03: consent brute force -----------------------------------------------------------

def test_consent_failures_are_throttled_per_address_even_for_the_right_password(valid_client):
    for _ in range(5):
        assert _post(_consent_form("wrong")).status_code == 401
    blocked = _post(_consent_form())           # the RIGHT password, after 5 failures
    assert blocked.status_code == 429
    assert json.loads(blocked.body) == {"error": "slow_down"}
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.headers["cache-control"] == "no-store"
    assert not valid_client                     # no code was issued
    other = _post(_consent_form(), client=("198.51.100.7", 1))  # another address is unaffected
    assert other.status_code == 302


def test_the_global_window_bounds_rotating_addresses(valid_client):
    for i in range(50):
        _post(_consent_form("wrong"), client=(f"192.0.2.{i}", 1))
    assert _post(_consent_form(), client=("198.51.100.99", 1)).status_code == 429


def test_the_cloudflare_client_header_keys_the_per_address_window(valid_client):
    """Render: client -> Cloudflare -> proxy; X-Forwarded-For's last hop is the CF edge, shared by
    unrelated users. CF-Connecting-IP (set by Cloudflare) is the key (review of #177, P1)."""
    edge = "172.70.1.1"
    for _ in range(5):
        _post(_consent_form("wrong"), headers={"cf-connecting-ip": "203.0.113.50",
                                               "x-forwarded-for": f"203.0.113.50, {edge}"})
    assert _post(_consent_form(), headers={"cf-connecting-ip": "203.0.113.50",
                                           "x-forwarded-for": f"203.0.113.50, {edge}"}).status_code == 429
    # Another user behind the SAME Cloudflare edge is not locked out by the first one.
    other = _post(_consent_form(), headers={"cf-connecting-ip": "198.51.100.8",
                                            "x-forwarded-for": f"198.51.100.8, {edge}"})
    assert other.status_code == 302


def test_without_the_cloudflare_header_the_first_forwarded_hop_is_the_key():
    from types import SimpleNamespace
    req = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.7, 172.70.1.1"}, client=None)
    assert oauth_throttle.client_address(req) == "203.0.113.7"
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.9"))
    assert oauth_throttle.client_address(req) == "10.0.0.9"


def test_window_expires():
    clock = [0.0]
    lim = oauth_throttle.WindowLimiter(2, 10, clock=lambda: clock[0])
    lim.record("a"); lim.record("a")
    assert lim.retry_after("a")
    clock[0] = 10.5
    assert lim.retry_after("a") is None


def test_non_ascii_password_is_a_plain_401_not_a_500(valid_client):
    assert _post(_consent_form("pässwörd")).status_code == 401


def test_consent_page_and_rejection_cannot_be_framed_or_cached(valid_client):
    r = _post(_consent_form("wrong"))
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


# ---- G-12: registration throttle ----------------------------------------------------------

def _register(client=("203.0.113.9", 1)):
    body = json.dumps({"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
                       "grant_types": ["authorization_code", "refresh_token"],
                       "response_types": ["code"], "client_name": "Claude"}).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request({"type": "http", "method": "POST", "scheme": "https", "server": ("b", 443),
                   "path": "/oauth/register", "headers": [(b"content-type", b"application/json")],
                   "query_string": b"", "client": client}, receive)
    return asyncio.run(oauth.register_claude_client(req))


def test_registration_is_throttled_per_address_and_globally(monkeypatch):
    rows = []
    monkeypatch.setattr(oauth_store, "register_claude_dcr_client", lambda redirect: rows.append(1) or DCR_ID)
    codes = [_register().status_code for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429 and len(rows) == 10
    for i in range(100):
        _register(client=(f"192.0.2.{i % 250}", i))
    assert _register(client=("198.51.100.1", 1)).status_code == 429


# ---- G-04: storage failures are fixed 503s, never raw 500/400 ------------------------------

def _boom(*_a, **_k):
    raise psycopg.OperationalError("connection to server at \"secret-host.example\" (10.1.2.3), port 5432 failed")


@pytest.mark.parametrize("target,call", [
    ("save_code", lambda: _post(_consent_form())),
    ("valid_claude_dcr_client", lambda: _post(_consent_form())),
    ("exchange_code", lambda: asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
        "grant_type": "authorization_code", "client_id": DCR_ID, "code": "c",
        "redirect_uri": REDIRECT, "code_verifier": VERIFIER})))),
    ("refresh_and_issue", lambda: asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
        "grant_type": "refresh_token", "client_id": DCR_ID, "refresh_token": "r"})))),
    ("register_claude_dcr_client", lambda: _register()),
])
def test_storage_outage_returns_a_fixed_503_without_detail(valid_client, monkeypatch, caplog, target, call):
    monkeypatch.setattr(oauth_store, target, _boom)
    with caplog.at_level("INFO"):
        r = call()
    assert r.status_code == 503
    assert json.loads(r.body) == {"error": "temporarily_unavailable"}
    assert r.headers["cache-control"] == "no-store" and r.headers["retry-after"]
    for leak in ("secret-host", "10.1.2.3", "5432", "OperationalError"):
        assert leak not in r.body.decode() and leak not in caplog.text
    assert "oauth_storage_error stage=" in caplog.text


def test_authorize_get_dcr_lookup_outage_is_503_not_400(monkeypatch):
    monkeypatch.setattr(oauth_store, "valid_claude_dcr_client", _boom)
    q = urlencode({k: v for k, v in _consent_form().items() if k != "password"})
    req = Request({"type": "http", "method": "GET", "scheme": "https", "server": ("b", 443),
                   "path": "/oauth/authorize", "headers": [], "query_string": q.encode()})
    r = asyncio.run(oauth.authorize_get(req))
    assert r.status_code == 503


# ---- PKCE, scopes, token responses ---------------------------------------------------------

@pytest.mark.parametrize("verifier", ["a", "é" * 43, "x" * 129, VERIFIER[:-1] + " "])
def test_malformed_verifier_is_invalid_grant_and_never_reaches_storage(monkeypatch, verifier):
    called = []
    monkeypatch.setattr(oauth_store, "exchange_code", lambda *a, **k: called.append(1))
    r = asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
        "grant_type": "authorization_code", "client_id": DCR_ID, "code": "c",
        "redirect_uri": REDIRECT, "code_verifier": verifier})))
    assert r.status_code == 400 and json.loads(r.body) == {"error": "invalid_grant"} and not called


@pytest.mark.parametrize("challenge", ["", "short", "x" * 44, "!" * 43])
def test_malformed_challenge_is_refused_at_authorize(valid_client, challenge):
    r = _post(_consent_form(code_challenge=challenge))
    assert r.status_code == 400 and not valid_client


def test_only_known_scopes_are_granted(valid_client):
    r = _post(_consent_form(scope="browser:use admin:all offline_access"))
    assert r.status_code == 302
    assert valid_client[0][1]["scope"] == "browser:use offline_access"


def test_token_success_is_never_cached(monkeypatch):
    monkeypatch.setattr(oauth_store, "exchange_code",
                        lambda code, **kw: {"resource": oauth.resource_url(), "scope": "browser:use"})
    r = asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
        "grant_type": "authorization_code", "client_id": DCR_ID, "code": "c",
        "redirect_uri": REDIRECT, "code_verifier": VERIFIER})))
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    body = json.loads(r.body)
    assert body["token_type"] == "Bearer" and body["access_token"] and body["refresh_token"]


# ---- middleware: non-blocking DB I/O, case-insensitive scheme ------------------------------

def test_a_slow_token_check_does_not_block_other_requests(monkeypatch):
    import bridge.server as srv
    release = threading.Event()

    def slow(*_a):
        release.wait(5)
        return False

    monkeypatch.setattr(srv.oauth_store, "valid_access_token", slow)
    with TestClient(srv.app) as client:
        result = {}

        def stalled():
            result["mcp"] = client.post("/mcp", headers={"Authorization": "Bearer " + "A" * 43}, json={}).status_code

        t = threading.Thread(target=stalled)
        t.start()
        time.sleep(0.3)
        start = time.monotonic()
        health = client.get("/healthz")
        elapsed = time.monotonic() - start
        release.set()
        t.join(10)
    assert health.status_code == 200 and elapsed < 2.0
    assert result["mcp"] == 401


def test_bearer_scheme_is_case_insensitive(monkeypatch):
    import bridge.server as srv
    good = "G" * 43
    monkeypatch.setattr(srv.oauth_store, "valid_access_token", lambda token, r, s: token == good)
    # No lifespan here (the MCP session manager can start only once per process): the
    # middleware's verdict is what matters, anything but 401 means the token was accepted.
    client = TestClient(srv.app, raise_server_exceptions=False)
    if True:
        for scheme in ("Bearer", "bearer", "BEARER"):
            r = client.post("/mcp/", headers={"Authorization": f"{scheme} {good}", "Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert r.status_code != 401, scheme
        assert client.post("/mcp/", headers={"Authorization": f"Basic {good}"}, json={}).status_code == 401


def test_malformed_bearers_never_touch_the_database(monkeypatch):
    import bridge.server as srv
    calls = []
    monkeypatch.setattr(srv.oauth_store, "valid_access_token", lambda *a: calls.append(1) or False)
    client = TestClient(srv.app, raise_server_exceptions=False)
    for token in ("x", "A" * 42, "A" * 44, "A" * 42 + "!", "A" * 41 + "=="):
        assert client.post("/mcp/", headers={"Authorization": "Bearer " + token}, json={}).status_code == 401
    assert calls == []


def test_storage_concurrency_is_capped_and_fails_fast(monkeypatch):
    """Review of #177 P2: at most STORE_CONCURRENCY storage calls in flight; more get an
    immediate 503 instead of opening more connections against Aiven's 15."""
    gate = threading.Event()
    started = []

    def slow(*_a, **_k):
        started.append(1)
        gate.wait(5)
        return False

    async def scenario():
        tasks = [asyncio.create_task(oauth._store(slow)) for _ in range(oauth.STORE_CONCURRENCY)]
        await asyncio.sleep(0.2)
        with pytest.raises(oauth.StorageBusy):
            await oauth._store(slow)
        gate.set()
        await asyncio.gather(*tasks)
        assert await oauth._store(lambda: "ok") == "ok"   # slots released

    asyncio.run(scenario())
    assert len(started) == oauth.STORE_CONCURRENCY


def test_a_saturated_store_returns_the_fixed_503(valid_client, monkeypatch):
    async def busy(*_a, **_k):
        raise oauth.StorageBusy()
    monkeypatch.setattr(oauth, "_store", busy)
    r = _post(_consent_form())
    assert r.status_code == 503 and json.loads(r.body) == {"error": "temporarily_unavailable"}


def test_consent_csp_has_no_form_action():
    """Review of #177 P0: form-action would block the 302 to the client's cross-origin callback."""
    assert "form-action" not in oauth.CONSENT_HEADERS["Content-Security-Policy"]
