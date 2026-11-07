from __future__ import annotations

import os

import pytest

from app import db


def test_verify_full_policy_rejects_require(monkeypatch, tmp_path):
    ca = tmp_path / "aiven-ca.pem"
    ca.write_text("test-ca")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example:14274/defaultdb?sslmode=require")
    monkeypatch.setenv("FCB_DATABASE_TLS_VERIFY_FULL", "1")
    monkeypatch.setenv("PGSSLROOTCERT", str(ca))
    with pytest.raises(RuntimeError, match="DATABASE_TLS_VERIFY_FULL_REQUIRED"):
        db.database_url()


def test_verify_full_policy_requires_existing_ca(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example:14274/defaultdb?sslmode=verify-full")
    monkeypatch.setenv("FCB_DATABASE_TLS_VERIFY_FULL", "1")
    monkeypatch.setenv("PGSSLROOTCERT", str(tmp_path / "missing.pem"))
    with pytest.raises(RuntimeError, match="DATABASE_TLS_CA_UNAVAILABLE"):
        db.database_url()


def test_verify_full_policy_accepts_verified_dsn(monkeypatch, tmp_path):
    ca = tmp_path / "aiven-ca.pem"
    ca.write_text("test-ca")
    dsn = "postgresql://u:p@db.example:14274/defaultdb?sslmode=verify-full"
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("FCB_DATABASE_TLS_VERIFY_FULL", "1")
    monkeypatch.setenv("PGSSLROOTCERT", str(ca))
    assert db.database_url() == dsn


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE"])
def test_policy_off_preserves_local_development(monkeypatch, value):
    dsn = "postgresql://u:p@localhost:5432/test"
    monkeypatch.setenv("DATABASE_URL", dsn)
    if value:
        monkeypatch.setenv("FCB_DATABASE_TLS_VERIFY_FULL", value)
    else:
        monkeypatch.delenv("FCB_DATABASE_TLS_VERIFY_FULL", raising=False)
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    assert db.database_url() == dsn
