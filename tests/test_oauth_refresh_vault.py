import asyncio
import os
from cryptography.fernet import Fernet
from bridge import oauth_refresh_vault as vault

class Cursor:
    def __init__(self, initial):
        self.initial=initial
        self.calls=[]
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execute(self,sql,params=None): self.calls.append((sql,params))
    def fetchone(self): return (self.initial,)

class Connection:
    def __init__(self, initial):
        self.c=Cursor(initial)
        self.commits=0
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def cursor(self): return self.c
    def commit(self): self.commits+=1

def test_vault_client_lock_and_encrypted_rotation(monkeypatch):
    key=Fernet.generate_key().decode()
    monkeypatch.setenv("FCB_CERT_VAULT_FERNET_KEY",key)
    monkeypatch.setenv("FCB_CERT_VAULT_DATABASE_URL","postgresql://example.invalid/db")
    initial=Fernet(key.encode()).encrypt(b"previous-secret")
    conn=Connection(initial)
    async def exchange(*,client_id,refresh_token):
        assert client_id=="test-client"
        assert refresh_token=="previous-secret"
        return {"access_token":"temporary-access","refresh_token":"next-secret"}
    result=asyncio.run(vault.rotate_in_vault("test-client",exchange,connection=conn))
    assert result=="temporary-access"
    assert "FOR UPDATE" in conn.c.calls[0][0]
    assert conn.c.calls[0][1]==("test-client",)
    encrypted=conn.c.calls[1][1][0]
    assert b"next-secret" not in encrypted
    assert Fernet(key.encode()).decrypt(encrypted)==b"next-secret"
    assert conn.commits>=1

def test_missing_vault_key_fail_closed(monkeypatch):
    import pytest
    monkeypatch.delenv("FCB_CERT_VAULT_FERNET_KEY",raising=False)
    monkeypatch.setenv("FCB_CERT_VAULT_DATABASE_URL","dummy")
    with pytest.raises(ValueError,match="CERT_VAULT_NOT_CONFIGURED"):
        vault._settings()

def test_isolation_per_client(monkeypatch):
    import pytest
    key=Fernet.generate_key().decode()
    monkeypatch.setenv("FCB_CERT_VAULT_FERNET_KEY",key)
    monkeypatch.setenv("FCB_CERT_VAULT_DATABASE_URL","dummy")
    conn=Connection(Fernet(key.encode()).encrypt(b"valid"))
    async def rejected(*,client_id,refresh_token):
        raise ValueError("OAUTH_REFRESH_REJECTED")
    with pytest.raises(ValueError):
        asyncio.run(vault.rotate_in_vault("client-A",rejected,connection=conn))
    assert not any("UPDATE fcb_cert_oauth_vault" in sql for sql,_ in conn.c.calls)
