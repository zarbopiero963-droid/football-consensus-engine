"""FCB-WP-009 (R2): bridge/db_identity.py returns role names only, never connection details."""
from __future__ import annotations

import asyncio

from bridge import db_identity


class _Conn:
    def __init__(self, row=None, exc=None):
        self.row, self.exc = row, exc

    def __enter__(self):
        if self.exc:
            raise self.exc
        return self

    def __exit__(self, *a):
        return False

    def execute(self, _sql):
        return self

    def fetchone(self):
        return self.row


def test_role_names_only():
    assert db_identity.database_identity(lambda: _Conn(("fcb_app", "fcb_app"))) == {
        "status": "OK", "current_user": "fcb_app", "session_user": "fcb_app"}
    assert db_identity.database_identity(lambda: _Conn({"a": "fcb_app", "b": "avnadmin"}))["session_user"] == "avnadmin"


def test_errors_are_fixed_codes_without_details(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db_identity.database_identity() == {"status": "NOT_CONFIGURED"}
    boom = RuntimeError('connection to "db.example.invalid" port 14274 failed: password "x" for user "avnadmin"')
    out = db_identity.database_identity(lambda: _Conn(exc=boom))
    assert out == {"status": "UNAVAILABLE"}
    for weird in (("fcb_app", "host=db.example.invalid"), ("A" * 70, "fcb_app"), ("fcb_app",), None):
        assert db_identity.database_identity(lambda w=weird: _Conn(w)) == {"status": "UNEXPECTED"}


def test_async_wrapper_is_bounded():
    assert asyncio.run(db_identity.database_identity_async(lambda: _Conn(("fcb_app", "fcb_app"))))["status"] == "OK"
