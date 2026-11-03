"""FCB-WP-006: schema readiness codes, non-blocking /healthz, no secret in the output."""
from __future__ import annotations

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from bridge import schema_readiness as sr


def test_not_configured_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = sr.SchemaReadiness()
    assert r.snapshot()["state"] == "NOT_CONFIGURED"
    assert asyncio.run(r.refresh())["state"] == "NOT_CONFIGURED"


def test_connection_failure_is_a_fixed_code_without_text(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:SECRET_PW@db.internal/x")

    def boom():
        raise RuntimeError("could not connect to postgresql://u:SECRET_PW@db.internal/x")
    result = asyncio.run(sr.SchemaReadiness(boom).refresh())
    assert result["state"] == "DB_UNAVAILABLE" and result["missing_tables"] is None
    assert "SECRET_PW" not in repr(result) and "db.internal" not in repr(result)


def test_slow_database_times_out(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(sr, "CHECK_TIMEOUT_SECONDS", 0.05)

    def slow():
        time.sleep(0.5)
        raise RuntimeError("late")
    async def go():
        started = time.monotonic()
        result = await sr.SchemaReadiness(slow).refresh()
        return result, time.monotonic() - started
    result, elapsed = asyncio.run(go())
    assert result["state"] == "DB_UNAVAILABLE" and elapsed < 0.4


def test_snapshot_never_waits_and_schedules_one_refresh(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        raise RuntimeError("x")

    async def go():
        r = sr.SchemaReadiness(slow)
        started = time.monotonic()
        first = r.snapshot()
        second = r.snapshot()  # refresh already in flight: no second one
        assert time.monotonic() - started < 0.05
        await asyncio.sleep(0.4)
        return first, second, r.snapshot()
    first, second, third = asyncio.run(go())
    assert first["state"] == second["state"] == "UNKNOWN"
    assert third["state"] == "DB_UNAVAILABLE" and len(calls) == 1


def test_healthz_reports_schema_state_without_secrets(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from bridge.server import app
    data = TestClient(app).get("/healthz").json()
    assert data["schema_state"] == "NOT_CONFIGURED" and data["schema_missing_tables"] is None
