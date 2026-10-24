import asyncio
import json

from bridge import oauth


def test_oauth_metadata(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_HOST", "example.com")
    protected = oauth.protected_resource_metadata()
    assert protected["resource"] == "https://example.com/mcp"
    assert protected["authorization_servers"] == ["https://example.com"]
    meta = oauth.authorization_server_metadata()
    assert meta["issuer"] == "https://example.com"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["client_id_metadata_document_supported"] is True
    assert "offline_access" in meta["scopes_supported"]


def test_pkce_s256_known_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth._pkce_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


CHATGPT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
CLAUDE_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _metadata(client_id, redirect_uri, *, auth_method="none"):
    return {
        "client_id": client_id,
        "client_name": "Test client",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }


class _Response:
    def __init__(self, data, *, status_code=200):
        self._data = data
        self.status_code = status_code
        self.content = json.dumps(data).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._data


class _Client:
    response = None
    calls = []

    def __init__(self, *, timeout, follow_redirects):
        assert timeout == 15
        assert follow_redirects is False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers):
        self.calls.append((url, headers))
        return self.response


def _validate(monkeypatch, client_id, redirect_uri, document):
    _Client.response = _Response(document)
    _Client.calls = []
    monkeypatch.setattr(oauth.httpx, "AsyncClient", _Client)
    result = asyncio.run(oauth.validate_cimd(client_id, redirect_uri))
    return result, list(_Client.calls)


def test_validate_cimd_accepts_chatgpt_without_regression(monkeypatch):
    result, calls = _validate(
        monkeypatch,
        CHATGPT_ID,
        CHATGPT_REDIRECT,
        _metadata(CHATGPT_ID, CHATGPT_REDIRECT, auth_method="private_key_jwt"),
    )
    assert result is True
    assert calls == [(CHATGPT_ID, {"Accept": "application/json"})]


def test_validate_cimd_accepts_claude_published_identity(monkeypatch):
    result, calls = _validate(
        monkeypatch,
        CLAUDE_ID,
        CLAUDE_REDIRECT,
        _metadata(CLAUDE_ID, CLAUDE_REDIRECT),
    )
    assert result is True
    assert calls == [(CLAUDE_ID, {"Accept": "application/json"})]


def test_validate_cimd_rejects_unregistered_redirect(monkeypatch):
    result, _ = _validate(
        monkeypatch,
        CLAUDE_ID,
        "https://attacker.example/callback",
        _metadata(CLAUDE_ID, CLAUDE_REDIRECT),
    )
    assert result is False


def test_validate_cimd_rejects_invalid_documents(monkeypatch):
    invalid = [
        {},
        _metadata("https://attacker.example/client", CLAUDE_REDIRECT),
        {**_metadata(CLAUDE_ID, CLAUDE_REDIRECT), "redirect_uris": CLAUDE_REDIRECT},
        {**_metadata(CLAUDE_ID, CLAUDE_REDIRECT), "response_types": ["token"]},
        {**_metadata(CLAUDE_ID, CLAUDE_REDIRECT), "grant_types": ["refresh_token"]},
        {**_metadata(CLAUDE_ID, CLAUDE_REDIRECT), "token_endpoint_auth_method": "client_secret_post"},
    ]
    for document in invalid:
        result, _ = _validate(monkeypatch, CLAUDE_ID, CLAUDE_REDIRECT, document)
        assert result is False


def test_validate_cimd_rejects_impersonation_before_network(monkeypatch):
    class FailIfCalled:
        def __init__(self, **_kwargs):
            raise AssertionError("untrusted client_id must not be fetched")

    monkeypatch.setattr(oauth.httpx, "AsyncClient", FailIfCalled)
    lookalikes = [
        "http://claude.ai/oauth/mcp-oauth-client-metadata",
        "https://claude.ai.evil.example/oauth/mcp-oauth-client-metadata",
        "https://claude.ai@evil.example/oauth/mcp-oauth-client-metadata",
        "https://claude.ai/oauth/other-client-metadata",
        "https://claude.ai/oauth/mcp-oauth-client-metadata?redirect=evil",
        "https://chatgpt.com.evil.example/oauth/client.json",
    ]
    for client_id in lookalikes:
        assert asyncio.run(oauth.validate_cimd(client_id, CLAUDE_REDIRECT)) is False


def test_claude_transport_origins_are_explicitly_allowed():
    from bridge.server import transport_security

    assert "https://chatgpt.com" in transport_security.allowed_origins
    assert "https://chat.openai.com" in transport_security.allowed_origins
    assert "https://claude.ai" in transport_security.allowed_origins
    assert "https://claude.com" in transport_security.allowed_origins


def test_official_claude_metadata_shape_still_accepted_with_extra_grant(monkeypatch, caplog):
    # Actual publicly documented Claude CIMD shape; no credential needed.
    document = _metadata(CLAUDE_ID, CLAUDE_REDIRECT)
    document["client_name"] = "Claude"
    document["grant_types"].append("urn:ietf:params:oauth:grant-type:jwt-bearer")
    with caplog.at_level("WARNING", logger=oauth.__name__):
        result, _ = _validate(monkeypatch, CLAUDE_ID, CLAUDE_REDIRECT, document)
    assert result is True
    assert "oauth_rejected" not in caplog.text


def test_cimd_http_status_failure_is_classified_and_redacted(monkeypatch, caplog):
    import httpx

    class ForbiddenResponse(_Response):
        def raise_for_status(self):
            request = httpx.Request("GET", CLAUDE_ID)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                "SENSITIVE_BODY_DO_NOT_LOG", request=request, response=response,
            )

    _Client.response = ForbiddenResponse(_metadata(CLAUDE_ID, CLAUDE_REDIRECT))
    _Client.calls = []
    monkeypatch.setattr(oauth.httpx, "AsyncClient", _Client)
    with caplog.at_level("WARNING", logger=oauth.__name__):
        assert asyncio.run(oauth.validate_cimd(CLAUDE_ID, CLAUDE_REDIRECT)) is False
    assert "client=claude" in caplog.text
    assert "reason=metadata_http_403" in caplog.text
    assert "SENSITIVE_BODY_DO_NOT_LOG" not in caplog.text
    assert CLAUDE_ID not in caplog.text
    assert CLAUDE_REDIRECT not in caplog.text


def test_cimd_transport_failure_is_classified_and_redacted(monkeypatch, caplog):
    import httpx

    class ConnectFailure(_Client):
        async def get(self, url, *, headers):
            raise httpx.ConnectError(
                "SENSITIVE_DNS_DETAILS_DO_NOT_LOG",
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(oauth.httpx, "AsyncClient", ConnectFailure)
    with caplog.at_level("WARNING", logger=oauth.__name__):
        assert asyncio.run(oauth.validate_cimd(CLAUDE_ID, CLAUDE_REDIRECT)) is False
    assert "reason=metadata_transport_error" in caplog.text
    assert "SENSITIVE_DNS_DETAILS_DO_NOT_LOG" not in caplog.text


def test_metadata_failure_reason_is_field_specific_without_leaking_values(monkeypatch, caplog):
    bad = {**_metadata(CLAUDE_ID, CLAUDE_REDIRECT), "redirect_uris": ["https://untrusted.invalid/cb"]}
    with caplog.at_level("WARNING", logger=oauth.__name__):
        result, _ = _validate(monkeypatch, CLAUDE_ID, CLAUDE_REDIRECT, bad)
    assert result is False
    assert "reason=metadata_redirect_not_registered" in caplog.text
    assert "https://untrusted.invalid/cb" not in caplog.text


def test_authorize_get_returns_same_400_and_logs_only_fixed_code(monkeypatch, caplog):
    from urllib.parse import urlencode
    from starlette.requests import Request

    data = {
        "client_id": CLAUDE_ID,
        "redirect_uri": CLAUDE_REDIRECT,
        "response_type": "code",
        "scope": "wrong-scope",
        "resource": oauth.resource_url(),
        "code_challenge": "SENSITIVE_CHALLENGE_DO_NOT_LOG",
        "code_challenge_method": "S256",
        "state": "SENSITIVE_STATE_DO_NOT_LOG",
    }
    scope = {
        "type": "http", "method": "GET", "scheme": "https",
        "server": ("bridge.example", 443),
        "path": "/oauth/authorize",
        "headers": [],
        "query_string": urlencode(data).encode("utf-8"),
    }
    with caplog.at_level("WARNING", logger=oauth.__name__):
        response = asyncio.run(oauth.authorize_get(Request(scope)))
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "invalid_request"}
    assert "reason=required_scope_missing" in caplog.text
    assert "SENSITIVE_STATE_DO_NOT_LOG" not in caplog.text
    assert "SENSITIVE_CHALLENGE_DO_NOT_LOG" not in caplog.text
    assert CLAUDE_ID not in caplog.text


def test_authorize_get_valid_claude_and_chatgpt_still_show_consent(monkeypatch, caplog):
    from urllib.parse import urlencode
    from starlette.requests import Request

    for client_id, redirect_uri, auth_method in (
        (CLAUDE_ID, CLAUDE_REDIRECT, "none"),
        (CHATGPT_ID, CHATGPT_REDIRECT, "private_key_jwt"),
    ):
        _Client.response = _Response(_metadata(client_id, redirect_uri, auth_method=auth_method))
        _Client.calls = []
        monkeypatch.setattr(oauth.httpx, "AsyncClient", _Client)
        data = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "browser:use offline_access",
            "resource": oauth.resource_url(),
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "state": "state",
        }
        scope = {
            "type": "http", "method": "GET", "scheme": "https",
            "server": ("bridge.example", 443), "path": "/oauth/authorize",
            "headers": [], "query_string": urlencode(data).encode("utf-8"),
        }
        with caplog.at_level("WARNING", logger=oauth.__name__):
            response = asyncio.run(oauth.authorize_get(Request(scope)))
        assert response.status_code == 200
        assert b"Autorizza il client OAuth" in response.body
    assert "oauth_rejected" not in caplog.text


def test_authorize_post_logs_rejection_without_password_or_state(monkeypatch, caplog):
    from urllib.parse import urlencode
    from starlette.requests import Request

    password = "SENSITIVE_PASSWORD_DO_NOT_LOG"
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", password)
    fields = {
        "client_id": CLAUDE_ID,
        "redirect_uri": CLAUDE_REDIRECT,
        "response_type": "code",
        "scope": "browser:use",
        "resource": "https://wrong-resource.invalid/mcp",
        "code_challenge": "SENSITIVE_PKCE_DO_NOT_LOG",
        "code_challenge_method": "S256",
        "state": "SENSITIVE_STATE_DO_NOT_LOG",
        "password": password,
    }
    body = urlencode(fields).encode("utf-8")
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "scheme": "https",
        "server": ("bridge.example", 443),
        "path": "/oauth/authorize",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "query_string": b"",
    }
    with caplog.at_level("WARNING", logger=oauth.__name__):
        response = asyncio.run(oauth.authorize_post(Request(scope, receive)))
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "invalid_request"}
    assert "reason=resource_mismatch" in caplog.text
    for item in (password, fields["state"], fields["code_challenge"], fields["resource"], CLAUDE_ID):
        assert item not in caplog.text



# Claude's dynamic registration does not rely on a server-to-Claude CIMD
# download, which can be blocked by a datacenter Cloudflare challenge.
def _make_http_request(method, path, *, json_body=None, form_body=None, query=None):
    from urllib.parse import urlencode
    from starlette.requests import Request

    headers = []
    if json_body is not None:
        payload = json.dumps(json_body).encode()
        headers = [(b"content-type", b"application/json")]
    elif form_body is not None:
        payload = urlencode(form_body).encode()
        headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    else:
        payload = b""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request({
        "type": "http", "method": method, "scheme": "https",
        "server": ("football-consensus-browser-bridge.onrender.com", 443),
        "path": path, "headers": headers,
        "query_string": urlencode(query or {}).encode(),
    }, receive)


def _claude_registration_payload():
    return {
        "client_name": "Claude",
        "client_uri": "https://claude.ai",
        "redirect_uris": [CLAUDE_REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def test_registration_endpoint_is_published():
    assert oauth.authorization_server_metadata()["registration_endpoint"].endswith("/oauth/register")
    from bridge.server import app
    assert any(getattr(route, "path", None) == "/oauth/register" for route in app.routes)


def test_claude_dcr_registration_issues_opaque_public_id_no_secret(monkeypatch):
    recorded = []
    client_id = "fcbdcr1_" + "a" * 32
    monkeypatch.setattr(
        oauth.oauth_store, "register_claude_dcr_client",
        lambda redirect: recorded.append(redirect) or client_id,
    )
    response = asyncio.run(oauth.register_claude_client(
        _make_http_request("POST", "/oauth/register", json_body=_claude_registration_payload())
    ))
    assert response.status_code == 201
    data = json.loads(response.body)
    assert data["client_id"] == client_id
    assert data["redirect_uris"] == [CLAUDE_REDIRECT]
    assert data["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in data
    assert response.headers["cache-control"] == "no-store"
    assert recorded == [CLAUDE_REDIRECT]


def test_claude_dcr_rejects_untrusted_registration_metadata(monkeypatch):
    def forbidden_write(*_args):
        raise AssertionError("invalid clients cannot be registered")

    monkeypatch.setattr(oauth.oauth_store, "register_claude_dcr_client", forbidden_write)
    invalid = [
        {},
        {**_claude_registration_payload(), "redirect_uris": ["https://attacker.example/callback"]},
        {**_claude_registration_payload(), "redirect_uris": [CLAUDE_REDIRECT, "https://attacker.example"]},
        {**_claude_registration_payload(), "token_endpoint_auth_method": "client_secret_post"},
        {**_claude_registration_payload(), "response_types": ["token"]},
        {**_claude_registration_payload(), "grant_types": ["refresh_token"]},
        {**_claude_registration_payload(), "scope": "browser:use admin"},
        {**_claude_registration_payload(), "client_uri": "https://attacker.example"},
    ]
    for params in invalid:
        resp = asyncio.run(oauth.register_claude_client(
            _make_http_request("POST", "/oauth/register", json_body=params)
        ))
        assert resp.status_code == 400
        assert json.loads(resp.body) == {"error": "invalid_client_metadata"}


def test_claude_dcr_valid_authorization_even_when_cimd_fetch_blocked(monkeypatch):
    import httpx
    client_id = "fcbdcr1_" + "A" * 32

    class BlockAllOutbound:
        def __init__(self, *args, **kwargs):
            raise AssertionError("DCR must not require an outbound CIMD request")

    monkeypatch.setattr(oauth.httpx, "AsyncClient", BlockAllOutbound)
    monkeypatch.setattr(
        oauth.oauth_store, "valid_claude_dcr_client",
        lambda supplied, redirect: supplied == client_id and redirect == CLAUDE_REDIRECT,
    )
    request = _make_http_request("GET", "/oauth/authorize", query={
        "client_id": client_id,
        "redirect_uri": CLAUDE_REDIRECT,
        "response_type": "code",
        "scope": "browser:use",
        "resource": oauth.resource_url(),
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "state": "state",
    })
    response = asyncio.run(oauth.authorize_get(request))
    assert response.status_code == 200
    assert b"Password di autorizzazione" in response.body


def test_claude_dcr_unknown_client_or_redirect_never_gets_consent(monkeypatch):
    client_id = "fcbdcr1_" + "z" * 32
    monkeypatch.setattr(oauth.oauth_store, "valid_claude_dcr_client", lambda *_args: False)
    base = {
        "client_id": client_id,
        "redirect_uri": CLAUDE_REDIRECT,
        "response_type": "code",
        "scope": "browser:use",
        "resource": oauth.resource_url(),
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "state": "state",
    }
    for redirect in (CLAUDE_REDIRECT, "https://attacker.example/cb"):
        params = dict(base, redirect_uri=redirect)
        response = asyncio.run(oauth.authorize_get(
            _make_http_request("GET", "/oauth/authorize", query=params)
        ))
        assert response.status_code == 400


def test_claude_dcr_code_and_pkce_remain_bound_after_consent(monkeypatch):
    # Test complete authorization through code exchange with in-memory storage,
    # not the production database or real OAuth password.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    client_id = "fcbdcr1_" + "q" * 32
    state = "test-state"
    code_rows = {}
    captured = {}
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "LOCAL_TEST_ONLY_PASSWORD")
    monkeypatch.setattr(oauth.oauth_store, "valid_claude_dcr_client", lambda cid, redirect: cid == client_id and redirect == CLAUDE_REDIRECT)

    def save_code(code, **kw):
        code_rows[code] = kw

    monkeypatch.setattr(oauth.oauth_store, "save_code", save_code)
    monkeypatch.setattr(oauth.oauth_store, "consume_code", lambda code: code_rows.pop(code, None))
    monkeypatch.setattr(oauth, "_token_payload", lambda cid, resource, scope: {"access_token": "TEST_ACCESS", "scope": scope, "token_type": "Bearer"})

    form = {
        "client_id": client_id,
        "redirect_uri": CLAUDE_REDIRECT,
        "response_type": "code",
        "scope": "browser:use",
        "resource": oauth.resource_url(),
        "code_challenge": oauth._pkce_s256(verifier),
        "code_challenge_method": "S256",
        "state": state,
        "password": "LOCAL_TEST_ONLY_PASSWORD",
    }
    res = asyncio.run(oauth.authorize_post(_make_http_request("POST", "/oauth/authorize", form_body=form)))
    assert res.status_code == 302
    from urllib.parse import urlparse, parse_qs
    redir = urlparse(res.headers["location"])
    assert redir.scheme == "https" and redir.netloc == "claude.ai"
    query = parse_qs(redir.query)
    assert query["state"] == [state]
    code = query["code"][0]
    bad = asyncio.run(oauth.token_endpoint(_make_http_request("POST", "/oauth/token", form_body={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": "https://attacker.example/callback",
        "code_verifier": verifier,
    })))
    assert bad.status_code == 400
    assert json.loads(bad.body) == {"error": "invalid_grant"}
    # A consumed code cannot be replayed.
    replay = asyncio.run(oauth.token_endpoint(_make_http_request("POST", "/oauth/token", form_body={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": CLAUDE_REDIRECT,
        "code_verifier": verifier,
    })))
    assert replay.status_code == 400
    # Separately prove successful exchange with a fresh authorization code.
    res2 = asyncio.run(oauth.authorize_post(_make_http_request("POST", "/oauth/authorize", form_body=form)))
    code2 = parse_qs(urlparse(res2.headers["location"]).query)["code"][0]
    good = asyncio.run(oauth.token_endpoint(_make_http_request("POST", "/oauth/token", form_body={
        "grant_type": "authorization_code", "client_id": client_id,
        "code": code2, "redirect_uri": CLAUDE_REDIRECT, "code_verifier": verifier,
    })))
    assert good.status_code == 200
    assert json.loads(good.body)["access_token"] == "TEST_ACCESS"


def test_dcr_storage_id_format_is_unforgeable_by_guesswork():
    from bridge import oauth_store
    assert oauth_store._DCR_ID_PATTERN.fullmatch("fcbdcr1_" + "q" * 32)
    assert not oauth_store._DCR_ID_PATTERN.fullmatch("fcbdcr1_" + "q" * 31)
    assert not oauth_store._DCR_ID_PATTERN.fullmatch("fcbdcr1_" + "!" * 32)



def test_dcr_postgres_registration_round_trip_when_database_available():
    import os
    import pytest
    from bridge import oauth_store

    if not os.getenv("DATABASE_URL"):
        pytest.skip("PostgreSQL test requires the dedicated CI DATABASE_URL")
    client_id = oauth_store.register_claude_dcr_client(CLAUDE_REDIRECT)
    assert client_id.startswith("fcbdcr1_")
    assert oauth_store.valid_claude_dcr_client(client_id, CLAUDE_REDIRECT)
    assert not oauth_store.valid_claude_dcr_client(client_id, "https://attacker.example/callback")
    assert not oauth_store.valid_claude_dcr_client("fcbdcr1_" + "z" * 32, CLAUDE_REDIRECT)
    with oauth_store._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT client_id_hash FROM oauth_claude_dcr_clients WHERE client_id_hash=%s",
            (oauth_store.digest(client_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["client_id_hash"] != client_id
        cur.execute(
            "DELETE FROM oauth_claude_dcr_clients WHERE client_id_hash=%s",
            (oauth_store.digest(client_id),),
        )
        conn.commit()
    assert not oauth_store.valid_claude_dcr_client(client_id, CLAUDE_REDIRECT)
