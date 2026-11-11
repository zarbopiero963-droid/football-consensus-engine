"""FIX-08: LOCAL_QUOTA_EXHAUSTED is replaced by two fixed codes that say which local limit
refused the request, with the wait computed by the shared coordinator. No network: the real
resilience client and MCP tools over a fake coordinator; the PostgreSQL computation itself is
covered in tests/integration/test_apifootball_quota_codes_postgres.py."""
from __future__ import annotations

import asyncio

import pytest

import _apifootball_fake_provider as fake
from bridge.api_football.config import Settings
from bridge.api_football.quota import QuotaExceeded
from bridge.api_football.tools import _local_quota_error, register_tools


def _tool(quota_error):
    transport = fake.FakeTransport()
    client = fake.build_client(transport, fake.FakeQuota(error=quota_error))
    server = fake._Server()
    register_tools(server, Settings(enabled=True, api_key=fake.KEY, live_authorized=True), client=client)
    return server.tools, transport


def test_rolling_window_refusal_is_local_rate_limited_with_retry_after():
    tools, transport = _tool(QuotaExceeded("rolling", window="minute", retry_after_seconds=23))
    out = asyncio.run(tools["apifootball_fixtures"](id=1))
    assert out["status"] == "PROVIDER_ERROR"
    assert out["error"] == {"code": "LOCAL_RATE_LIMITED", "window": "ROLLING_60_SECONDS", "retry_after_seconds": 23}
    assert out["network_calls_made"] == 0 and transport.requests == []


def test_daily_refusal_is_local_daily_quota_exhausted_with_utc_midnight_reset():
    tools, transport = _tool(QuotaExceeded("day", window="day", retry_after_seconds=4000,
                                           reset_at_utc="2026-11-11T00:00:00+00:00"))
    out = asyncio.run(tools["apifootball_fixtures"](id=1))
    assert out["error"] == {"code": "LOCAL_DAILY_QUOTA_EXHAUSTED", "window": "UTC_CALENDAR_DAY",
                            "reset_at_utc": "2026-11-11T00:00:00+00:00", "retry_after_seconds": 4000}
    assert out["network_calls_made"] == 0 and transport.requests == []


def test_the_old_code_is_gone():
    tools, _ = _tool(QuotaExceeded("x", window="minute", retry_after_seconds=5))
    assert "LOCAL_QUOTA_EXHAUSTED" not in str(asyncio.run(tools["apifootball_status"]()))


@pytest.mark.parametrize("exc,expected", [
    # Unknown window: conservative daily code, and no invented reset time or wait.
    (QuotaExceeded("x"), {"code": "LOCAL_DAILY_QUOTA_EXHAUSTED", "window": "UTC_CALENDAR_DAY", "reset_at_utc": None}),
    # Out-of-range waits are never reported.
    (QuotaExceeded("x", window="minute", retry_after_seconds=0), {"code": "LOCAL_RATE_LIMITED", "window": "ROLLING_60_SECONDS"}),
    (QuotaExceeded("x", window="minute", retry_after_seconds=61), {"code": "LOCAL_RATE_LIMITED", "window": "ROLLING_60_SECONDS"}),
    (QuotaExceeded("x", window="minute", retry_after_seconds=True), {"code": "LOCAL_RATE_LIMITED", "window": "ROLLING_60_SECONDS"}),
    (QuotaExceeded("x", window="day", retry_after_seconds=90_000, reset_at_utc="2026-11-11T00:00:00+00:00"),
     {"code": "LOCAL_DAILY_QUOTA_EXHAUSTED", "window": "UTC_CALENDAR_DAY", "reset_at_utc": "2026-11-11T00:00:00+00:00"}),
])
def test_mapping_edges(exc, expected):
    assert _local_quota_error(exc) == expected


def test_unknown_window_is_refused_at_construction():
    with pytest.raises(ValueError):
        QuotaExceeded("x", window="hour")


def test_message_is_never_exposed():
    secret = "postgresql://user:pw@host/db"
    out = _local_quota_error(QuotaExceeded(secret, window="minute", retry_after_seconds=3))
    assert secret not in str(out)


def test_limits_and_defaults_are_unchanged():
    # FIX-08 changes codes only: defaults and env bounds stay as they were.
    s = Settings()
    assert (s.max_rpm, s.max_daily_requests, s.max_concurrency) == (8, 80, 2)
    env = {"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_API_KEY": "k", "APIFOOTBALL_MAX_RPM": "60",
           "APIFOOTBALL_MAX_DAILY_REQUESTS": "7000", "APIFOOTBALL_MAX_CONCURRENCY": "1"}
    s = Settings.from_env(env)
    assert (s.max_rpm, s.max_daily_requests, s.max_concurrency) == (60, 7000, 1)
