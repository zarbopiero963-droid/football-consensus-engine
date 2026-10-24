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
