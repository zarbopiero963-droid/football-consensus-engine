import asyncio
import pytest
from bridge import oauth,oauth_store

def test_cert_registration_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FCB_CERT_OAUTH_REGISTRATION_ENABLED",raising=False)
    class FakeRequest:
        async def json(self):
            return {"redirect_uri":"http://127.0.0.1:8765/oauth/callback"}
    reply=asyncio.run(oauth.register_cert_client(FakeRequest()))
    assert reply.status_code==404

def test_cert_registration_requires_exact_loopback_callback():
    assert oauth._cert_redirect_allowed("http://127.0.0.1:8765/oauth/callback")
    assert not oauth._cert_redirect_allowed("https://evil.example/oauth/callback")
    assert not oauth._cert_redirect_allowed("http://127.0.0.1:8765/other")
    assert not oauth._cert_redirect_allowed("http://127.0.0.1:8765/oauth/callback?token=x")

def test_cert_registration_scoped_when_enabled(monkeypatch):
    monkeypatch.setenv("FCB_CERT_OAUTH_REGISTRATION_ENABLED","true")
    monkeypatch.setattr(oauth_store,"register_cert_client",lambda redirect_uri:"fcbe2e1_"+"a"*32)
    class FakeRequest:
        async def json(self):
            return {"redirect_uri":"http://127.0.0.1:8765/oauth/callback"}
    reply=asyncio.run(oauth.register_cert_client(FakeRequest()))
    assert reply.status_code==201
    assert b"fcbe2e1_" in reply.body
    assert b"secret" not in reply.body

class Cursor:
    def __init__(self): self.commands=[]
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execute(self,s,params=None): self.commands.append((s,params))
class Conn:
    def __init__(self):self.c=Cursor();self.did_commit=False
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self):return self.c
    def commit(self):self.did_commit=True

def test_offline_revoke_invalidates_access_refresh_and_registration(monkeypatch):
    conn=Conn()
    monkeypatch.setattr(oauth_store,"_conn",lambda:conn)
    client="fcbe2e1_"+"a"*32
    assert oauth_store.revoke_cert_client(client) is True
    assert len(conn.c.commands)==3
    assert "oauth_access_tokens" in conn.c.commands[0][0]
    assert "oauth_refresh_tokens" in conn.c.commands[1][0]
    assert "oauth_cert_dcr_clients" in conn.c.commands[2][0]
    assert conn.did_commit
    assert not oauth_store.revoke_cert_client("malformed")
