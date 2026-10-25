import asyncio
import hashlib
import json
import os
from urllib.parse import parse_qs,urlparse
import httpx
import pytest
from bridge.oauth_test_client import pkce_pair,authorization_url,validate_token_response,refresh,exchange_code,write_private_json

def test_pkce_s256_and_authorization_request():
    verifier, challenge=pkce_pair()
    assert len(verifier)>40
    assert challenge
    params=parse_qs(urlparse(authorization_url(client_id="test",redirect_uri="http://127.0.0.1/callback",state="random",challenge=challenge)).query)
    assert params["code_challenge_method"]==["S256"]
    assert params["code_challenge"]==[challenge]
    assert params["response_type"]==["code"]
    assert "password" not in params

def test_refresh_rotation_is_client_bound():
    seen=[]
    def mock(request):
        body=request.content.decode()
        seen.append(body)
        assert "grant_type=refresh_token" in body
        assert "client_id=client-A" in body
        if "refresh_token=expired" in body:
            return httpx.Response(400,json={"error":"invalid_grant"})
        return httpx.Response(200,json={"token_type":"Bearer","access_token":"new-access",
            "refresh_token":"new-refresh","expires_in":3600})
    async def run():
        result=await refresh(client_id="client-A",refresh_token="old",transport=httpx.MockTransport(mock))
        assert result["refresh_token"]=="new-refresh"
        with pytest.raises(ValueError,match="OAUTH_REFRESH_REJECTED"):
            await refresh(client_id="client-A",refresh_token="expired",transport=httpx.MockTransport(mock))
    asyncio.run(run())

def test_pkce_code_exchange():
    def mock(request):
        body=request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code_verifier=verifier" in body
        return httpx.Response(200,json={"token_type":"Bearer","access_token":"a","refresh_token":"r","expires_in":3600})
    result=asyncio.run(exchange_code(client_id="c",code="code",redirect_uri="http://127.0.0.1/cb",
                                    verifier="verifier",transport=httpx.MockTransport(mock)))
    assert result["access_token"]=="a"

def test_expiry_and_missing_tokens_rejected():
    for payload in ({"token_type":"Bearer","access_token":"x","refresh_token":"r","expires_in":0},
                    {"token_type":"Bearer","access_token":"x","expires_in":3600}):
        with pytest.raises(ValueError):
            validate_token_response(payload)

def test_private_rotated_store(tmp_path):
    target=tmp_path/"rotated.json"
    write_private_json(target,{"refresh_token":"secret"})
    assert json.loads(target.read_text())["refresh_token"]=="secret"
    assert os.stat(target).st_mode&0o077==0
