import asyncio
import logging

import httpx
import pytest

from bridge.api_football import errors
from bridge.api_football.client import ApiFootballClient
from bridge.api_football.config import load_settings
from bridge.api_football.resilience import CircuitBreaker, DailyBudget, SlidingWindowLimiter, TTLCache
from conftest import BASE_SETTINGS, FAKE_KEY, Recorder, endpoint, envelope, json_response

FIXTURES = endpoint("apifootball_fixtures")
ODDS = endpoint("apifootball_odds")
ENV = ["APIFOOTBALL_ENABLED", "APIFOOTBALL_API_KEY", "APIFOOTBALL_BASE_URL", "APIFOOTBALL_TIMEOUT_SECONDS",
       "APIFOOTBALL_MAX_RPM", "APIFOOTBALL_MAX_DAILY_REQUESTS", "APIFOOTBALL_MAX_CONCURRENCY",
       "APIFOOTBALL_CACHE_ENABLED", "APIFOOTBALL_CIRCUIT_BREAKER_ENABLED", "APIFOOTBALL_QUOTA_RESERVE_PERCENT",
       "APIFOOTBALL_MAX_RETRIES", "APIFOOTBALL_QUEUE_TIMEOUT_SECONDS"]


def run(coro):
    return asyncio.run(coro)


def fixtures(client, **arguments):
    return run(client.call(FIXTURES, arguments or {"id": 1}))


# ------------------------------------------------------------------ configuration

@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_disabled_and_prudent(clean_env):
    settings = load_settings()
    assert settings.enabled is False and settings.key_configured is False
    assert settings.base_url == "https://v3.football.api-sports.io"
    assert settings.max_rpm <= 10 and settings.max_daily_requests <= 100
    assert settings.max_retries <= 1 and settings.max_concurrency <= 2 and settings.quota_reserve_percent > 0
    assert settings.errors == ()


def test_key_never_appears_in_repr_or_public_config(clean_env, monkeypatch):
    monkeypatch.setenv("APIFOOTBALL_API_KEY", FAKE_KEY)
    settings = load_settings()
    assert FAKE_KEY not in repr(settings) and FAKE_KEY not in str(settings.public())
    assert settings.public()["api_key_configured"] is True


@pytest.mark.parametrize("name,value", [
    ("APIFOOTBALL_ENABLED", "maybe"), ("APIFOOTBALL_MAX_RPM", "abc"), ("APIFOOTBALL_MAX_RPM", "0"),
    ("APIFOOTBALL_MAX_RETRIES", "50"), ("APIFOOTBALL_TIMEOUT_SECONDS", "600"),
    ("APIFOOTBALL_BASE_URL", "https://evil.example.com"), ("APIFOOTBALL_BASE_URL", "http://v3.football.api-sports.io"),
])
def test_invalid_configuration_is_reported_and_fails_closed(clean_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("APIFOOTBALL_API_KEY", FAKE_KEY)
    if name != "APIFOOTBALL_ENABLED":
        monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    settings = load_settings()
    assert settings.errors and settings.base_url == "https://v3.football.api-sports.io"
    recorder = Recorder(json_response(envelope()))
    client = ApiFootballClient(transport=httpx.MockTransport(recorder))
    result = fixtures(client)
    assert result["status"] in {"CONFIG_INVALID", "DISABLED"}
    assert recorder.requests == []


def test_disabled_module_makes_no_call(make_client):
    recorder = Recorder(json_response(envelope()))
    result = fixtures(make_client(recorder, enabled=False))
    assert result["status"] == "DISABLED" and result["ok"] is False
    assert recorder.requests == []


def test_missing_key_makes_no_call(make_client):
    recorder = Recorder(json_response(envelope()))
    result = fixtures(make_client(recorder, api_key=""))
    assert result["status"] == "API_KEY_MISSING" and recorder.requests == []


# ---------------------------------------------------------------- happy path/cache

def test_success_has_provenance_quota_and_cache_miss(make_client):
    headers = {"x-ratelimit-requests-limit": "7500", "x-ratelimit-requests-remaining": "7400",
               "X-RateLimit-Limit": "300", "X-RateLimit-Remaining": "299", "date": "Sat, 24 Oct 2026 10:00:00 GMT"}
    result = fixtures(make_client(Recorder(json_response(envelope(), headers=headers))))
    assert result["status"] == "OK" and result["ok"] is True and result["http_status"] == 200
    assert result["cache"]["status"] == "MISS" and result["attempts"] == 1
    assert result["quota"]["daily_remaining"] == 7400 and result["quota"]["minute_limit"] == 300
    assert result["provider_date_header"] == "Sat, 24 Oct 2026 10:00:00 GMT"
    assert result["provider_timestamp_utc"] is None  # an HTTP Date header is not a data timestamp
    assert result["error"] is None


def test_missing_quota_headers_stay_unknown_not_zero(make_client):
    result = fixtures(make_client(Recorder(json_response(envelope()))))
    assert result["quota"]["daily_remaining"] is None and result["quota"]["minute_remaining"] is None


def test_cache_hit_and_bypass(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder)
    first, second = fixtures(client), fixtures(client)
    assert first["cache"]["status"] == "MISS" and second["cache"]["status"] == "HIT"
    assert second["acquired_at_utc"] == first["acquired_at_utc"]  # acquisition time is the original one
    run(client.call(FIXTURES, {"id": 1}, use_cache=False))
    assert len(recorder.requests) == 2


def test_cache_can_be_disabled(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder, cache_enabled=False)
    fixtures(client), fixtures(client)
    assert len(recorder.requests) == 2


def test_errors_are_never_cached(make_client):
    recorder = Recorder(json_response({}, status=500), json_response(envelope()))
    client = make_client(recorder, max_retries=0)
    assert fixtures(client)["status"] == "UPSTREAM_ERROR"
    assert fixtures(client)["status"] == "OK"


def test_empty_response_is_ok_but_flagged(make_client):
    result = fixtures(make_client(Recorder(json_response(envelope(response=[])))))
    assert result["status"] == "EMPTY" and result["ok"] is True
    assert any("EMPTY_RESPONSE" in w for w in result["warnings"])


# ------------------------------------------------------------------ failure matrix

@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_trips_breaker_without_retry(make_client, status):
    recorder = Recorder(json_response({"message": "nope"}, status=status))
    client = make_client(recorder)
    result = fixtures(client)
    assert result["status"] == "AUTH_FAILED" and result["error"]["retryable"] is False
    assert len(recorder.requests) == 1
    assert fixtures(client, id=2)["status"] == "CIRCUIT_OPEN"
    assert len(recorder.requests) == 1


def test_429_is_retried_once_with_bounded_backoff(make_client):
    recorder = Recorder(json_response({}, status=429))
    client = make_client(recorder, max_retries=1)
    result = fixtures(client)
    assert result["status"] == "RATE_LIMITED_PROVIDER" and result["attempts"] == 2
    assert len(recorder.requests) == 2
    assert client.sleeps and max(client.sleeps) <= 10.0


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_retries_are_bounded(make_client, status):
    recorder = Recorder(json_response({}, status=status))
    client = make_client(recorder, max_retries=1)
    assert fixtures(client)["status"] == "UPSTREAM_ERROR"
    assert len(recorder.requests) == 2


def test_retry_recovers_after_transient_failure(make_client):
    recorder = Recorder(json_response({}, status=503), json_response(envelope()))
    result = fixtures(make_client(recorder, max_retries=1))
    assert result["status"] == "OK" and result["attempts"] == 2


def test_timeout_is_structured(make_client):
    recorder = Recorder(httpx.ReadTimeout("slow"))
    result = fixtures(make_client(recorder, max_retries=0))
    assert result["status"] == "TIMEOUT" and result["error"]["retryable"] is True


def test_provider_unreachable_is_structured_and_sanitized(make_client):
    recorder = Recorder(httpx.ConnectError(f"failed for https://v3.football.api-sports.io?key={FAKE_KEY}"))
    result = fixtures(make_client(recorder, max_retries=0))
    assert result["status"] == "NETWORK_ERROR" and FAKE_KEY not in str(result)


def test_overall_deadline_bounds_a_hanging_provider(make_client):
    async def hang(_request):
        await asyncio.sleep(30)

    client = make_client(Recorder(hang), timeout_seconds=30.0, max_retries=0)
    client._deadline = lambda _settings: 0.1
    result = fixtures(client)
    assert result["status"] == "TIMEOUT" and client._inflight == {}


@pytest.mark.parametrize("body,code", [
    (b"<html>oops</html>", "INVALID_JSON"),
    (b"{\"response\": NaN}", "INVALID_JSON"),
    (b"\xff\xfe", "INVALID_JSON"),
    (b"[]", "INCOMPLETE_RESPONSE"),
    (b"{\"errors\": []}", "INCOMPLETE_RESPONSE"),
])
def test_malformed_bodies_are_structured(make_client, body, code):
    result = fixtures(make_client(Recorder(httpx.Response(200, content=body)), max_retries=0))
    assert result["status"] == code and result["ok"] is False


def test_redirects_are_never_followed(make_client):
    recorder = Recorder(httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    result = fixtures(make_client(recorder))
    assert result["status"] == "HTTP_ERROR" and len(recorder.requests) == 1


def test_response_size_limit(make_client):
    big = envelope(response=[{"x": "y" * 5000}])
    result = fixtures(make_client(Recorder(json_response(big)), max_response_bytes=1024))
    assert result["status"] == "RESPONSE_TOO_LARGE"


def test_output_size_limit_drops_raw_but_keeps_metadata(make_client):
    big = envelope(response=[{"x": "y" * 5000}])
    result = fixtures(make_client(Recorder(json_response(big)), max_output_bytes=2048))
    assert result["status"] == "RESPONSE_TOO_LARGE" and result["raw"] is None and result["bytes_utf8"] > 2048


@pytest.mark.parametrize("provider_errors,code", [
    ({"token": "Error/Missing application key."}, "AUTH_FAILED"),
    ({"requests": "You have reached the request limit for the day."}, "QUOTA_EXHAUSTED"),
    ({"rateLimit": "Too many requests."}, "RATE_LIMITED_PROVIDER"),
    ({"plan": "Free plans do not have access to this season."}, "PLAN_RESTRICTED"),
    ({"date": "The Date field must contain a valid date."}, "PROVIDER_ERROR"),
    (["something unexpected"], "PROVIDER_ERROR"),
])
def test_provider_errors_in_200_body_are_classified_and_kept_verbatim(make_client, provider_errors, code):
    result = fixtures(make_client(Recorder(json_response(envelope(errors=provider_errors))), max_retries=0))
    assert result["status"] == code and result["provider_errors"] == provider_errors


def test_daily_quota_exhausted_stops_further_calls(make_client):
    recorder = Recorder(json_response(envelope(errors={"requests": "limit"})), json_response(envelope()))
    client = make_client(recorder, max_retries=0)
    assert fixtures(client)["status"] == "QUOTA_EXHAUSTED"
    assert fixtures(client, id=2)["status"] == "QUOTA_RESERVE_REACHED"
    assert len(recorder.requests) == 1


def test_quota_reserve_from_provider_headers(make_client):
    headers = {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "10"}
    recorder = Recorder(json_response(envelope(), headers=headers))
    client = make_client(recorder, quota_reserve_percent=10)
    assert fixtures(client)["status"] == "OK"
    result = fixtures(client, id=2)
    assert result["status"] == "QUOTA_RESERVE_REACHED" and len(recorder.requests) == 1


def test_provider_minute_exhaustion_pauses_calls(make_client):
    headers = {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "0"}
    recorder = Recorder(json_response(envelope(), headers=headers))
    client = make_client(recorder)
    fixtures(client)
    assert fixtures(client, id=2)["status"] == "RATE_LIMITED_PROVIDER" and len(recorder.requests) == 1


def test_local_daily_budget(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder, max_daily_requests=2, cache_enabled=False)
    assert [fixtures(client)["status"] for _ in range(3)] == ["OK", "OK", "DAILY_BUDGET_EXHAUSTED"]
    assert len(recorder.requests) == 2


def test_local_rate_limit_without_waiting(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder, max_rpm=2, cache_enabled=False, queue_timeout_seconds=0.0)
    statuses = [fixtures(client)["status"] for _ in range(3)]
    assert statuses == ["OK", "OK", "RATE_LIMITED_LOCAL"] and len(recorder.requests) == 2


def test_circuit_breaker_opens_and_recovers_after_cooldown(make_client):
    now = [0.0]
    recorder = Recorder(json_response({}, status=503))
    client = make_client(recorder, max_retries=0, cache_enabled=False)
    client.breaker = CircuitBreaker(threshold=3, cooldown=60, clock=lambda: now[0])
    assert [fixtures(client)["status"] for _ in range(3)] == ["UPSTREAM_ERROR"] * 3
    opened = fixtures(client)
    assert opened["status"] == "CIRCUIT_OPEN" and opened["error"]["retry_after_seconds"] == 60
    assert len(recorder.requests) == 3
    now[0] = 61.0
    recorder.responses = [json_response(envelope())]
    assert fixtures(client)["status"] == "OK"  # half-open probe succeeds
    assert client.breaker.state == "CLOSED"


def test_failed_half_open_probe_reopens(make_client):
    now = [0.0]
    client = make_client(Recorder(json_response({}, status=503)), max_retries=0, cache_enabled=False)
    client.breaker = CircuitBreaker(threshold=1, cooldown=60, clock=lambda: now[0])
    fixtures(client)
    now[0] = 61.0
    assert fixtures(client)["status"] == "UPSTREAM_ERROR"
    assert client.breaker.state == "OPEN"


def test_breaker_can_be_disabled(make_client):
    recorder = Recorder(json_response({}, status=401))
    client = make_client(recorder, circuit_breaker_enabled=False)
    fixtures(client), fixtures(client, id=2)
    assert len(recorder.requests) == 2


def test_restart_after_error_starts_clean(make_client):
    failing = make_client(Recorder(json_response({}, status=401)))
    fixtures(failing)
    assert failing.health()["state"] == "DEGRADED"
    fresh = make_client(Recorder(json_response(envelope())))
    assert fresh.health()["state"] == "READY" and fixtures(fresh)["status"] == "OK"


# --------------------------------------------------------------------- concurrency

def test_identical_concurrent_requests_are_deduplicated(make_client):
    async def slow(_request):
        await asyncio.sleep(0.05)
        return json_response(envelope())

    recorder = Recorder(slow)
    client = make_client(recorder)

    async def main():
        return await asyncio.gather(*(client.call(FIXTURES, {"id": 1}) for _ in range(5)))

    results = run(main())
    assert len(recorder.requests) == 1
    assert sorted(r["cache"]["status"] for r in results) == ["DEDUPLICATED"] * 4 + ["MISS"]
    assert all(r["ok"] for r in results) and client._inflight == {}


def test_concurrency_is_capped(make_client):
    active = [0, 0]

    async def slow(_request):
        active[0] += 1
        active[1] = max(active[1], active[0])
        await asyncio.sleep(0.02)
        active[0] -= 1
        return json_response(envelope())

    client = make_client(Recorder(slow), max_concurrency=2, cache_enabled=False)

    async def main():
        return await asyncio.gather(*(client.call(FIXTURES, {"id": n}) for n in range(1, 9)))

    assert all(r["ok"] for r in run(main()))
    assert active[1] == 2


def test_queue_timeout_when_slots_are_busy(make_client):
    async def slow(_request):
        await asyncio.sleep(0.3)
        return json_response(envelope())

    client = make_client(Recorder(slow), max_concurrency=1, queue_timeout_seconds=0.05, cache_enabled=False)

    async def main():
        return await asyncio.gather(client.call(FIXTURES, {"id": 1}), client.call(FIXTURES, {"id": 2}))

    statuses = sorted(r["status"] for r in run(main()))
    assert statuses == ["OK", "QUEUE_TIMEOUT"]


def test_cancelled_caller_does_not_break_shared_request(make_client):
    async def slow(_request):
        await asyncio.sleep(0.05)
        return json_response(envelope())

    recorder = Recorder(slow)
    client = make_client(recorder)

    async def main():
        first = asyncio.create_task(client.call(FIXTURES, {"id": 1}))
        second = asyncio.create_task(client.call(FIXTURES, {"id": 1}))
        await asyncio.sleep(0.01)
        first.cancel()
        result = await second
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0.01)
        return result

    result = run(main())
    assert result["ok"] and len(recorder.requests) == 1 and client._inflight == {}


def test_no_retry_storm_under_concurrent_failures(make_client):
    recorder = Recorder(json_response({}, status=503))
    client = make_client(recorder, max_retries=1, cache_enabled=False)
    client.breaker = CircuitBreaker(threshold=3, cooldown=60)

    async def main():
        return await asyncio.gather(*(client.call(FIXTURES, {"id": n}) for n in range(1, 21)))

    statuses = [r["status"] for r in run(main())]
    assert len(recorder.requests) <= 8  # bounded by breaker threshold plus in-flight attempts
    assert statuses.count("CIRCUIT_OPEN") >= 12


# --------------------------------------------------------------- secrets/logging

def test_key_never_appears_in_results_or_logs(make_client, caplog):
    caplog.set_level(logging.DEBUG)
    echo = envelope(errors={"token": f"invalid key {FAKE_KEY}"})
    client = make_client(Recorder(json_response(echo), json_response({}, status=500)), max_retries=0)
    results = [fixtures(client), fixtures(client, id=2), client.health(), client.quota_report()]
    for item in results:
        assert FAKE_KEY not in str(item)
    assert "[REDACTED]" in str(results[0])
    assert FAKE_KEY not in caplog.text


def test_health_and_quota_report_make_no_network_call(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder)
    health, quota = client.health(), client.quota_report()
    assert recorder.requests == []
    assert health["network_call_made"] is False and quota["network_call_made"] is False
    assert health["state"] == "READY" and health["process"]["limits_scope"] == "PER_PROCESS"


def test_every_returned_status_is_documented(make_client):
    for status in ("OK", "EMPTY", "DISABLED", "CIRCUIT_OPEN", "QUOTA_RESERVE_REACHED", "RESPONSE_TOO_LARGE"):
        assert status in errors.CODES


def test_paging_warnings(make_client):
    warn = fixtures(make_client(Recorder(json_response(envelope(paging={"current": 1, "total": 3})))))
    assert any("MORE_PAGES" in w for w in warn["warnings"])
    odd = run(make_client(Recorder(json_response(envelope(paging={"current": 2, "total": 1}))))
              .call(ODDS, {"fixture": 1, "page": 1}))
    assert sum("PAGINATION_UNEXPECTED" in w for w in odd["warnings"]) == 2
    absent = fixtures(make_client(Recorder(json_response({"response": [1]}))))
    assert any("PAGING_ABSENT" in w for w in absent["warnings"])


# ------------------------------------------------------------------ primitives

def test_primitives_are_bounded():
    now = [0.0]
    cache = TTLCache(max_entries=2, max_total_bytes=1000, clock=lambda: now[0])
    cache.put("a", 1, 10, 10), cache.put("b", 2, 10, 10), cache.put("c", 3, 10, 10)
    assert cache.get("a") is None and len(cache) == 2
    now[0] = 11
    assert cache.get("b") is None
    cache.put("huge", 4, 10, 600)
    assert cache.get("huge") is None
    day = ["2026-10-24"]
    budget = DailyBudget(today=lambda: day[0])
    budget.consume()
    assert not budget.available(1)
    day[0] = "2026-10-25"
    assert budget.available(1)
    limiter = SlidingWindowLimiter(clock=lambda: now[0])
    assert run(limiter.acquire(1, 0)) and not run(limiter.acquire(1, 0))
    now[0] += 61
    assert run(limiter.acquire(1, 0))


def test_base_settings_fixture_is_fake():
    assert BASE_SETTINGS.api_key.startswith("fake-")
