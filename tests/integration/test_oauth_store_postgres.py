"""WP-007 / G-18: OAuth storage on REAL PostgreSQL (runs in CI on PG16 and PG18 via TEST_DATABASE_URL).

Before this file the only OAuth PostgreSQL test lived in tests/ behind DATABASE_URL, which no
CI job sets, so OAuth SQL never ran on PostgreSQL in CI. Each test gets its own schema with the
bridge migrations applied, exactly as production is initialized.
"""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier

from app.migrations import apply_migrations
from bridge import oauth_store

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://bridge.example/mcp"


@pytest.fixture
def store(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for real PostgreSQL integration")
    schema = "oauth_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row,
                               options=f"-c search_path={schema} -c statement_timeout=5000")

    apply_migrations(connect)
    monkeypatch.setattr(oauth_store, "_conn", connect)
    yield connect
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


def _code(store, code="c-" + uuid.uuid4().hex, challenge="CH", client="cli", redirect=CLAUDE_REDIRECT,
          scope="browser:use offline_access"):
    oauth_store.save_code(code, client_id=client, redirect_uri=redirect, code_challenge=challenge,
                          resource=RESOURCE, scope=scope)
    return code


def _count(store, table, where="TRUE", args=()):
    with store() as conn:
        return conn.execute(SQL("SELECT count(*) AS n FROM {} WHERE " + where).format(Identifier(table)),
                            args).fetchone()["n"]


def _exchange(code, **kw):
    args = dict(client_id="cli", redirect_uri=CLAUDE_REDIRECT, verifier_matches=lambda ch: ch == "CH",
                access="A-" + uuid.uuid4().hex, refresh="R-" + uuid.uuid4().hex)
    args.update(kw)
    return oauth_store.exchange_code(code, **args), args


def test_exchange_issues_both_tokens_atomically_and_only_hashes_are_stored(store):
    code = _code(store)
    row, args = _exchange(code)
    assert row == {"resource": RESOURCE, "scope": "browser:use offline_access"}
    assert oauth_store.valid_access_token(args["access"], RESOURCE, "browser:use")
    assert _count(store, "oauth_access_tokens", "token_hash=%s", (oauth_store.digest(args["access"]),)) == 1
    assert _count(store, "oauth_refresh_tokens", "token_hash=%s", (oauth_store.digest(args["refresh"]),)) == 1
    assert _count(store, "oauth_access_tokens", "token_hash=%s", (args["access"],)) == 0  # never plaintext
    assert _count(store, "oauth_authorization_codes", "code_hash=%s", (code,)) == 0


@pytest.mark.parametrize("change", [
    {"client_id": "other"}, {"redirect_uri": "https://attacker.example/cb"},
    {"verifier_matches": lambda ch: False},
])
def test_a_failed_binding_burns_the_code_and_issues_nothing(store, change):
    code = _code(store)
    row, _ = _exchange(code, **change)
    assert row is None
    assert _count(store, "oauth_access_tokens") == 0 and _count(store, "oauth_refresh_tokens") == 0
    again, _ = _exchange(code)  # single use: even the right client cannot redeem it now
    assert again is None


def test_a_storage_failure_mid_exchange_rolls_back_everything(store, monkeypatch):
    code = _code(store)
    original = oauth_store._insert_pair

    def explode(cur, **kw):
        original(cur, **kw)
        raise psycopg.OperationalError("simulated failure after the inserts")

    monkeypatch.setattr(oauth_store, "_insert_pair", explode)
    with pytest.raises(psycopg.OperationalError):
        _exchange(code)
    monkeypatch.setattr(oauth_store, "_insert_pair", original)
    assert _count(store, "oauth_access_tokens") == 0 and _count(store, "oauth_refresh_tokens") == 0
    row, _ = _exchange(code)  # the code was NOT burned by the failed attempt
    assert row is not None


def test_concurrent_redemption_has_exactly_one_winner(store):
    code = _code(store)
    barrier = threading.Barrier(8)

    def go(_):
        barrier.wait()
        return _exchange(code)[0]

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(go, range(8)))
    assert sum(r is not None for r in results) == 1
    assert _count(store, "oauth_access_tokens") == 1


def test_refresh_rotation_is_atomic_single_use_and_client_bound(store):
    code = _code(store)
    _, first = _exchange(code)
    rt = first["refresh"]
    wrong = oauth_store.refresh_and_issue(rt, client_id="intruder", access="A2", refresh="R2")
    assert wrong is None
    # A wrong client does not burn the owner's refresh token.
    ok = oauth_store.refresh_and_issue(rt, client_id="cli", access="A3", refresh="R3")
    assert ok == {"resource": RESOURCE, "scope": "browser:use offline_access"}
    assert oauth_store.valid_access_token("A3", RESOURCE, "browser:use")
    assert oauth_store.refresh_and_issue(rt, client_id="cli", access="A4", refresh="R4") is None  # replay


def test_concurrent_refresh_has_exactly_one_winner(store):
    code = _code(store)
    _, first = _exchange(code)
    barrier = threading.Barrier(6)

    def go(i):
        barrier.wait()
        return oauth_store.refresh_and_issue(first["refresh"], client_id="cli",
                                             access=f"AX{i}", refresh=f"RX{i}")

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(go, range(6)))
    assert sum(r is not None for r in results) == 1


def test_access_token_resource_scope_and_expiry(store):
    oauth_store.save_access_token("AT", client_id="cli", resource=RESOURCE, scope="browser:use")
    assert oauth_store.valid_access_token("AT", RESOURCE, "browser:use")
    assert not oauth_store.valid_access_token("AT", "https://other/mcp", "browser:use")
    assert not oauth_store.valid_access_token("AT", RESOURCE, "admin")
    with store() as conn:
        conn.execute("UPDATE oauth_access_tokens SET expires_at = NOW() - INTERVAL '1 second'")
    assert not oauth_store.valid_access_token("AT", RESOURCE, "browser:use")


def test_dcr_registration_round_trip(store):
    # Moved from tests/test_oauth_metadata.py, where it could only run with DATABASE_URL (never in CI).
    client_id = oauth_store.register_claude_dcr_client(CLAUDE_REDIRECT)
    assert client_id.startswith("fcbdcr1_")
    assert oauth_store.valid_claude_dcr_client(client_id, CLAUDE_REDIRECT)
    assert not oauth_store.valid_claude_dcr_client(client_id, "https://attacker.example/callback")
    assert not oauth_store.valid_claude_dcr_client("fcbdcr1_" + "z" * 32, CLAUDE_REDIRECT)
    assert _count(store, "oauth_claude_dcr_clients", "client_id_hash=%s", (oauth_store.digest(client_id),)) == 1
    assert _count(store, "oauth_claude_dcr_clients", "client_id_hash=%s", (client_id,)) == 0


def test_store_connections_are_bounded_by_timeouts(monkeypatch):
    """The production _conn() passes a connect timeout and a statement timeout (no 130 s hang)."""
    seen = {}

    def fake_connect(dsn, **kw):
        seen.update(kw)
        raise psycopg.OperationalError("refused")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u@127.0.0.1:1/db")
    monkeypatch.setattr(oauth_store.psycopg, "connect", fake_connect)
    with pytest.raises(psycopg.OperationalError):
        oauth_store.valid_access_token("x", RESOURCE, "browser:use")
    assert seen["connect_timeout"] == oauth_store.CONNECT_TIMEOUT_SECONDS <= 5
    assert f"statement_timeout={oauth_store.STATEMENT_TIMEOUT_MS}" in seen["options"]
