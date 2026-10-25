"""Server-side OAuth refresh isolation regression without production database calls."""
from bridge import oauth_store

class Cursor:
    def __init__(self):
        self.executed=[]
        self.row=None
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execute(self,sql,params):
        self.executed.append((sql,params))
        # Simulated revoked or client-mismatch: never return a row.
        self.row=None
    def fetchone(self): return self.row

class Connection:
    def __init__(self):
        self.cursor_obj=Cursor()
        self.committed=False
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def cursor(self): return self.cursor_obj
    def commit(self): self.committed=True

def test_refresh_rotation_uses_atomic_client_match(monkeypatch):
    connection=Connection()
    monkeypatch.setattr(oauth_store,"_conn",lambda:connection)
    assert oauth_store.rotate_refresh_token("fake-rotating-token",client_id="client-A") is None
    statement,arguments=connection.cursor_obj.executed[0]
    assert "client_id=%s" in statement
    assert "revoked_at IS NULL" in statement
    assert arguments==(oauth_store.digest("fake-rotating-token"),"client-A")
    assert connection.committed

def test_rotation_requires_client_id():
    import pytest
    with pytest.raises(TypeError):
        oauth_store.rotate_refresh_token("token")
