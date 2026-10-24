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
