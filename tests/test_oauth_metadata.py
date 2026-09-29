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
