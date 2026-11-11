"""OAuth persistent throttle (owner decisions D01-D12), offline: modes, fail-closed paths,
key derivation, trusted proxies, continuity of /oauth/token and /mcp, connection bounds.

The SQL itself (GCRA, concurrency, GC, lock timeouts, grants, migration) runs on real PostgreSQL
in tests/integration/test_oauth_persistent_throttle_postgres.py.
The HMAC secret is generated at runtime by a fixture (never a committed literal).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time

import pytest
from starlette.testclient import TestClient

from bridge import oauth, oauth_persistent_throttle as pt, oauth_store, oauth_throttle
from tests.test_oauth_hardening import (DCR_ID, PASSWORD, REDIRECT, _consent_form, _post, _register,
                                        _req)

IP = "203.0.113.77"


@pytest.fixture
def throttle_key(monkeypatch):
    """A fresh CSPRNG key per test; never written anywhere but this process' environment."""
    value = "t1:" + secrets.token_urlsafe(48)
    monkeypatch.setenv(pt.KEY_ENV, value)
    monkeypatch.delenv(pt.PREVIOUS_KEY_ENV, raising=False)
    return value


@pytest.fixture(autouse=True)
def _clean_mode(monkeypatch):
    monkeypatch.delenv(pt.MODE_ENV, raising=False)
    monkeypatch.delenv(pt.KEY_ENV, raising=False)
    monkeypatch.delenv(pt.PREVIOUS_KEY_ENV, raising=False)


@pytest.fixture
def valid_client(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", PASSWORD)
    monkeypatch.setattr(oauth_store, "valid_claude_dcr_client", lambda cid, redirect: cid == DCR_ID)
    saved = []
    monkeypatch.setattr(oauth_store, "save_code", lambda code, **kw: saved.append((code, kw)))
    return saved


class FakeStore:
    """Replaces the SQL functions; records what reached 'the database'."""

    def __init__(self, allow=True, retry=120, fail=None, delay=0.0):
        self.allow, self.retry, self.fail, self.delay = allow, retry, fail, delay
        self.acquired, self.refunded = [], []
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def acquire(self, limit, key, prev=None, connect=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise self.fail
            self.acquired.append((limit.scope, key, prev))
            return self.allow, (0 if self.allow else self.retry)
        finally:
            with self.lock:
                self.active -= 1

    def refund(self, limit, key, connect=None):
        self.refunded.append((limit.scope, key))


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(pt, "acquire", fake.acquire)
    monkeypatch.setattr(pt, "refund", fake.refund)
    return fake


def _boom(*_a, **_k):
    import psycopg
    raise psycopg.OperationalError('connection to server at "secret-host.example" (10.9.9.9) failed')


# ------------------------------------------------------------------ modes

@pytest.mark.parametrize("raw,expected", [(None, "off"), ("", "off"), ("  ", "off"), ("off", "off"),
                                          ("OFF", "off"), ("shadow", "shadow"), (" Enforce ", "enforce"),
                                          ("enforced", pt.CONFIG_INVALID), ("1", pt.CONFIG_INVALID),
                                          ("false", pt.CONFIG_INVALID), ("disable", pt.CONFIG_INVALID)])
def test_mode_parsing_default_off_and_unknown_is_config_invalid_not_enforce(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv(pt.MODE_ENV, raw)
    assert pt.mode() == expected


def test_mode_off_is_todays_behaviour_and_never_touches_the_store(valid_client, monkeypatch):
    monkeypatch.setattr(pt, "acquire", lambda *a, **k: pytest.fail("off must not reach the database"))
    monkeypatch.setattr(pt, "_conn", lambda: pytest.fail("off must not connect"))
    for _ in range(5):
        assert _post(_consent_form("wrong")).status_code == 401
    assert _post(_consent_form()).status_code == 429          # layer 1 unchanged
    assert _post(_consent_form(), client=("198.51.100.7", 1)).status_code == 302
    assert pt.status()["oauth_throttle_mode"] == "off" and pt.status()["oauth_new_authorizations_ready"]


def test_the_shipped_default_is_off():
    assert pt.mode() == "off" and pt.REGISTRATION_ENFORCE is False
    assert (pt.CONSENT.per_key, pt.CONSENT.per_key_window, pt.CONSENT.global_, pt.CONSENT.global_window) == (5, 900, 50, 3600)
    assert (pt.REGISTRATION.per_key, pt.REGISTRATION.per_key_window,
            pt.REGISTRATION.global_, pt.REGISTRATION.global_window) == (10, 3600, 100, 3600)


@pytest.mark.parametrize("password", [PASSWORD, "wrong"])
def test_config_invalid_refuses_new_consent_uniformly_before_the_compare(valid_client, monkeypatch, store,
                                                                         caplog, password, throttle_key):
    monkeypatch.setenv(pt.MODE_ENV, "enforcd")
    compared = []
    monkeypatch.setattr(oauth.hmac, "compare_digest", lambda a, b: compared.append(1) or False)
    with caplog.at_level(logging.INFO):
        r = _post(_consent_form(password))
    assert r.status_code == 503 and json.loads(r.body) == {"error": "temporarily_unavailable"}
    assert not compared and not valid_client and not store.acquired
    assert "code=CONFIG_INVALID" in caplog.text
    assert oauth_throttle.CONSENT_FAILURES.per_key.retry_after(IP) is None  # nothing counted
    s = pt.status()
    assert s["oauth_throttle_mode"] == "CONFIG_INVALID" and s["oauth_new_authorizations_ready"] is False
    assert s["oauth_throttle_registration_mode"] == "CONFIG_INVALID"


def test_config_invalid_refuses_registration(monkeypatch, store):
    monkeypatch.setenv(pt.MODE_ENV, "on")
    rows = []
    monkeypatch.setattr(oauth_store, "register_claude_dcr_client", lambda r: rows.append(1) or DCR_ID)
    r = _register()
    assert r.status_code == 503 and json.loads(r.body) == {"error": "temporarily_unavailable"} and not rows


# ------------------------------------------------------------------ continuity (D12)

def _refresh():
    return asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
        "grant_type": "refresh_token", "client_id": DCR_ID, "refresh_token": "r"})))


@pytest.mark.parametrize("mode", ["enforce", "shadow", "bogus"])
def test_refresh_tokens_and_authenticated_mcp_never_pass_through_the_throttle(monkeypatch, mode):
    """Throttle store raising, mode enforce / shadow / CONFIG_INVALID, no key: refresh and /mcp work."""
    import bridge.server as srv
    monkeypatch.setenv(pt.MODE_ENV, mode)
    monkeypatch.setattr(pt, "acquire", _boom)
    monkeypatch.setattr(pt, "_conn", _boom)
    monkeypatch.setattr(oauth_store, "refresh_and_issue",
                        lambda presented, **kw: {"resource": oauth.resource_url(), "scope": "browser:use"})
    for _ in range(30):                                      # far above every limit
        r = _refresh()
        assert r.status_code == 200 and json.loads(r.body)["access_token"]
    good = "M" * 43
    monkeypatch.setattr(srv.oauth_store, "valid_access_token", lambda token, r, s: token == good)
    client = TestClient(srv.app, raise_server_exceptions=False)
    for _ in range(30):
        r = client.post("/mcp/", headers={"Authorization": f"Bearer {good}",
                                          "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code not in (401, 429, 503)


def test_the_token_endpoint_and_bearer_check_do_not_reference_the_persistent_layer():
    import inspect
    import bridge.server as srv
    assert "oauth_persistent_throttle" not in inspect.getsource(oauth.token_endpoint)
    assert "oauth_persistent_throttle" not in inspect.getsource(oauth.bearer_valid)
    assert "oauth_persistent_throttle" not in inspect.getsource(srv.OAuthBearerMiddleware)


# ------------------------------------------------------------------ enforce

def test_enforce_without_a_key_refuses_new_operations_uniformly(valid_client, monkeypatch, store, caplog):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    with caplog.at_level(logging.INFO):
        a, b = _post(_consent_form()), _post(_consent_form("wrong"))
    assert a.status_code == b.status_code == 503 and a.body == b.body and not valid_client
    assert "code=KEY_MISSING" in caplog.text and not store.acquired
    assert _register().status_code == 503
    s = pt.status()
    assert s["oauth_throttle_key_state"] == "MISSING" and s["oauth_new_authorizations_ready"] is False


@pytest.mark.parametrize("current,previous", [
    ("nokeyid" + "x" * 40, None),                 # no key id
    ("t1:short", None),                           # secret below 32 characters
    ("T1:" + "x" * 40, None),                     # key id must be lowercase [a-z0-9]{1,8}
    ("t1:" + "x" * 40, "t0:short"),               # malformed previous key: whole keyring invalid
    ("t1:" + "x" * 40, "t1:" + "y" * 40),          # same key id twice
])
def test_invalid_keys_are_invalid_and_enforce_refuses(valid_client, monkeypatch, store, current, previous):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    monkeypatch.setenv(pt.KEY_ENV, current)
    if previous:
        monkeypatch.setenv(pt.PREVIOUS_KEY_ENV, previous)
    assert pt.keyring().state == "INVALID"
    assert _post(_consent_form()).status_code == 503 and not store.acquired


def test_shadow_without_a_key_reports_and_never_interrupts_logins(valid_client, monkeypatch, store, caplog):
    monkeypatch.setenv(pt.MODE_ENV, "shadow")
    with caplog.at_level(logging.INFO):
        assert _post(_consent_form()).status_code == 302
    assert "code=KEY_MISSING" in caplog.text and not store.acquired
    s = pt.status()
    assert s["oauth_throttle_key_state"] == "MISSING" and s["oauth_new_authorizations_ready"] is True


@pytest.mark.parametrize("password", [PASSWORD, "wrong"])
def test_enforce_store_unavailable_is_the_same_503_for_any_password(valid_client, monkeypatch, caplog,
                                                                    throttle_key, password):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    monkeypatch.setattr(pt, "_conn", _boom)                  # the real acquire(), failing to connect
    compared = []
    real = oauth.hmac.compare_digest
    monkeypatch.setattr(oauth.hmac, "compare_digest", lambda a, b: compared.append(1) or real(a, b))
    with caplog.at_level(logging.INFO):
        r = _post(_consent_form(password))
    assert r.status_code == 503 and json.loads(r.body) == {"error": "temporarily_unavailable"}
    assert r.headers["cache-control"] == "no-store" and r.headers["retry-after"] == "5"
    assert not compared and not valid_client                # no 401 oracle: compare never ran
    assert "code=STORE_UNAVAILABLE" in caplog.text
    for leak in ("secret-host", "10.9.9.9", "OperationalError", IP, throttle_key.split(":", 1)[1]):
        assert leak not in caplog.text and leak not in r.body.decode()


def test_enforce_deny_is_a_429_even_with_the_right_password(valid_client, monkeypatch, store, throttle_key):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    store.allow = False
    r = _post(_consent_form())
    assert r.status_code == 429 and json.loads(r.body) == {"error": "slow_down"}
    assert r.headers["retry-after"] == "120" and not valid_client and not store.refunded


def test_only_failures_count_success_is_refunded(valid_client, monkeypatch, store, throttle_key):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    assert _post(_consent_form("wrong")).status_code == 401
    assert store.refunded == []
    assert _post(_consent_form()).status_code == 302
    assert len(store.acquired) == 2 and store.refunded == [("consent", store.acquired[1][1])]


def test_shadow_records_but_never_blocks(valid_client, monkeypatch, store, throttle_key, caplog):
    monkeypatch.setenv(pt.MODE_ENV, "shadow")
    store.allow = False
    with caplog.at_level(logging.INFO):
        assert _post(_consent_form()).status_code == 302
    assert "outcome=would_deny" in caplog.text and store.acquired
    store.fail = RuntimeError("down")
    assert _post(_consent_form()).status_code == 302


def test_registration_is_observe_only_even_in_enforce(monkeypatch, store, throttle_key, caplog):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    monkeypatch.setattr(oauth_store, "register_claude_dcr_client", lambda r: DCR_ID)
    store.allow = False
    with caplog.at_level(logging.INFO):
        assert _register().status_code == 201
    assert ("registration", store.acquired[0][1], None) == store.acquired[0]
    assert "scope=registration mode=shadow outcome=would_deny" in caplog.text
    assert pt.status()["oauth_throttle_registration_mode"] == "shadow"


def test_layer_one_still_filters_first(valid_client, monkeypatch, store, throttle_key):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    for _ in range(5):
        _post(_consent_form("wrong"))
    n = len(store.acquired)
    assert _post(_consent_form()).status_code == 429 and len(store.acquired) == n  # no DB round trip


# ------------------------------------------------------------------ keys, addresses, proxies

def test_ipv4_and_ipv6_normalisation():
    assert pt.normalise_address("203.0.113.5") == "v4:203.0.113.5"
    assert pt.normalise_address("::ffff:203.0.113.5") == "v4:203.0.113.5"
    a = pt.normalise_address("2001:db8:aa:bb:1:2:3:4")
    assert a == pt.normalise_address("2001:db8:aa:bb:ffff::9") == "v6:2001:db8:aa:bb::/64"
    assert a != pt.normalise_address("2001:db8:aa:bc::1")             # neighbour /64 is another key
    assert pt.normalise_address("fe80::1%eth0") == "v6:fe80::/64"
    for junk in ("unknown", "", "999.1.1.1", "<script>", "a" * 64):
        assert pt.normalise_address(junk) == "unparsed"


def test_key_hash_is_keyed_hmac_with_key_id_and_never_contains_the_address():
    k1, k2 = secrets.token_bytes(32), secrets.token_bytes(32)
    h = pt.key_hash("t1", k1, "203.0.113.5")
    assert re.fullmatch(r"[a-z0-9]{1,8}:[0-9a-f]{64}", h)            # the table's CHECK
    assert "203.0.113.5" not in h and "v4:" not in h
    assert h != pt.key_hash("t1", k2, "203.0.113.5")                  # keyed: another secret, another hash
    assert h != pt.key_hash("t1", k1, "203.0.113.6")
    assert pt.key_hash("t1", k1, "2001:db8::1") == pt.key_hash("t1", k1, "2001:db8::2")


def _scope(peer, headers):
    from types import SimpleNamespace
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None, headers=headers)


def test_forwarding_headers_only_from_a_private_or_loopback_hop():
    cf = {"cf-connecting-ip": "198.51.100.20", "x-forwarded-for": "198.51.100.20, 172.70.1.1"}
    assert pt.client_key_source(_scope("10.0.0.5", cf)) == ("198.51.100.20", True)
    assert pt.client_key_source(_scope("127.0.0.1", cf)) == ("198.51.100.20", True)
    assert pt.client_key_source(_scope("203.0.113.9", cf)) == ("203.0.113.9", False)   # direct hit
    assert pt.client_key_source(_scope(None, cf)) == ("unknown", False)
    assert pt.client_key_source(_scope("8.8.8.8", cf)) == ("8.8.8.8", False)
    assert pt.client_key_source(_scope("192.0.2.1", cf)) == ("192.0.2.1", False)       # TEST-NET is not internal
    xff = {"x-forwarded-for": "198.51.100.21, 10.1.1.1"}
    assert pt.client_key_source(_scope("10.0.0.5", xff)) == ("198.51.100.21", True)


def test_no_user_agent_exemption():
    a = _scope("10.0.0.5", {"cf-connecting-ip": "198.51.100.30", "user-agent": "Claude-User"})
    b = _scope("10.0.0.5", {"cf-connecting-ip": "198.51.100.30", "user-agent": "ChatGPT-User"})
    assert pt.client_key_source(a) == pt.client_key_source(b)
    import inspect
    assert "user-agent" not in inspect.getsource(pt).lower().replace("never consults the user-agent", "")


def test_shared_egress_users_share_only_their_own_key(valid_client, monkeypatch, store, throttle_key):
    """Users behind one Claude.ai/ChatGPT egress share ONE key (per-address), but a different
    egress (or a different IPv6 /64) is a different key; the global bucket is shared by design."""
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    cf = lambda ip: {"cf-connecting-ip": ip}
    _post(_consent_form(), client=("10.0.0.5", 1), headers=cf("2001:db8:1:1::10"))
    _post(_consent_form(), client=("10.0.0.5", 1), headers=cf("2001:db8:1:1::99"))
    _post(_consent_form(), client=("10.0.0.5", 1), headers=cf("2001:db8:1:2::10"))
    keys = [k for _s, k, _p in store.acquired]
    assert keys[0] == keys[1] != keys[2]


def test_rotation_checks_the_previous_key_too(valid_client, monkeypatch, store, throttle_key):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    monkeypatch.setenv(pt.PREVIOUS_KEY_ENV, "t0:" + secrets.token_urlsafe(48))
    _post(_consent_form())
    (_scope_, key, prev), = store.acquired
    assert key.startswith("t1:") and prev.startswith("t0:")
    assert pt.status()["oauth_throttle_rotation_active"] is True


def test_logs_never_carry_address_key_hash_or_secret(valid_client, monkeypatch, store, throttle_key, caplog):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    with caplog.at_level(logging.DEBUG):
        _post(_consent_form("wrong"), client=("10.0.0.5", 1), headers={"cf-connecting-ip": IP})
        store.allow = False
        _post(_consent_form(), client=("10.0.0.5", 1), headers={"cf-connecting-ip": IP})
    key = store.acquired[0][1]
    for leak in (IP, key, key.split(":", 1)[1][:16], throttle_key, throttle_key.split(":", 1)[1]):
        assert leak not in caplog.text


# ------------------------------------------------------------------ anti-DoS: connection bounds

def test_a_many_key_flood_opens_at_most_db_concurrency_connections(monkeypatch, throttle_key):
    """Each check would open one connection; a flood from 200 keys at once may hold at most
    DB_CONCURRENCY of them (the rest are refused without connecting), leaving OAuth store
    workers free for /oauth/token and bearer checks."""
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    fake = FakeStore(delay=0.3)
    monkeypatch.setattr(pt, "acquire", fake.acquire)
    bearer_calls = []
    monkeypatch.setattr(oauth_store, "valid_access_token", lambda *a: bearer_calls.append(1) or True)

    async def scenario():
        reqs = [_req("POST", "/oauth/authorize", {}, {"cf-connecting-ip": f"198.51.{i // 250}.{i % 250}"},
                     ("10.0.0.5", 1)) for i in range(200)]
        flood = [asyncio.create_task(pt.check(pt.CONSENT, r)) for r in reqs]
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        ok = await oauth.bearer_valid("Z" * 43)       # authenticated traffic during the flood
        elapsed = time.monotonic() - t0
        decisions = await asyncio.gather(*flood)
        return ok, elapsed, decisions

    ok, elapsed, decisions = asyncio.run(scenario())
    assert fake.peak <= pt.DB_CONCURRENCY
    assert len(fake.acquired) <= pt.DB_CONCURRENCY
    assert ok and elapsed < 0.25 and bearer_calls == [1]
    assert sum(d.action == "refuse" and d.code == "STORE_BUSY" for d in decisions) >= 200 - pt.DB_CONCURRENCY


def test_storage_timeouts_are_bounded():
    assert pt.CONNECT_TIMEOUT_SECONDS <= 2 and pt.STATEMENT_TIMEOUT_MS <= 1500 and pt.LOCK_TIMEOUT_MS <= 500
    assert pt.DB_CONCURRENCY < oauth.STORE_CONCURRENCY and pt.GC_BATCH <= 100
    assert "FOR UPDATE SKIP LOCKED" in pt._GC and "LIMIT" in pt._GC
    # The database clock is the only clock: no timestamp is computed or sent by the application.
    for sql in (pt._ACQUIRE, pt._REFUND, *pt._LOCK_GLOBAL, pt._GC):
        assert "now()" not in sql.lower().replace("statement_timestamp()", "")


# ------------------------------------------------------------------ healthz, D06

def test_healthz_reports_throttle_state_with_fixed_codes_only(monkeypatch, throttle_key):
    import bridge.server as srv
    monkeypatch.setenv(pt.MODE_ENV, "shadow")
    body = TestClient(srv.app).get("/healthz").json()
    assert body["oauth_throttle_mode"] == "shadow" and body["oauth_throttle_key_state"] == "OK"
    assert body["oauth_new_authorizations_ready"] is True and "schema_deferred_versions" in body
    text = json.dumps(body)
    assert throttle_key not in text and throttle_key.split(":", 1)[1] not in text
    monkeypatch.setenv(pt.MODE_ENV, "maybe")
    body = TestClient(srv.app).get("/healthz").json()
    assert body["oauth_throttle_mode"] == "CONFIG_INVALID" and body["oauth_new_authorizations_ready"] is False


def test_password_policy_is_a_boolean_only(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "x" * 19)
    assert pt.password_meets_policy() is False
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", secrets.token_urlsafe(15))   # 20 characters
    assert pt.password_meets_policy() is True
    import inspect
    import bridge.server as srv
    src = inspect.getsource(srv.lifespan)
    assert "OAUTH_PASSWORD_POLICY meets_policy=false" in src and "MCP_OAUTH_PASSWORD" not in src
