"""Regression tests for the MCP status live gate; zero external API calls."""
import asyncio
from bridge.api_football.config import Settings
from bridge.api_football.tools import register_tools


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(func):
            self.tools[func.__name__] = func
            return func
        return decorate


class FakeClient:
    def __init__(self, data=None, exc=None, cache_status="MISS"):
        self.calls = []
        self.data = data
        self.exc = exc
        self.cache_status = cache_status

    async def get(self, path, params):
        self.calls.append((path, dict(params)))
        if self.exc:
            raise self.exc
        return {
            "http_status": 200,
            "cache_status": self.cache_status,
            "quota_remaining_provider_day": 7499,
            "quota_remaining_provider_minute": 10,
            "data": self.data,
        }


def registry(settings, client=None):
    r = Registry()
    names = register_tools(r, settings, client=client)
    assert len(names) == 42
    return r.tools


def run(tools, name):
    return asyncio.run(tools[name]())


def test_live_status_redacts_personal_account_fields():
    client = FakeClient({"response": {
        "account": {"firstname": "SECRET", "lastname": "PRIVATE", "email": "secret@example.com"},
        "subscription": {"plan": "Pro", "active": True, "end": "2026-12-31"},
        "requests": {"current": 1, "limit_day": 7500},
    }})
    s = Settings(enabled=True, api_key="TEST_ONLY", live_authorized=True)
    result = run(registry(s, client), "apifootball_status")
    assert result["status"] == "REAL_API_RESPONSE"
    assert result["data"]["subscription"]["plan"] == "Pro"
    assert result["data"]["requests"] == {"current": 1, "limit_day": 7500}
    assert result["quota_remaining"]["day"] == 7499
    assert result["network_calls_made"] == 1
    assert client.calls == [("/status", {})]
    assert "secret@example.com" not in str(result)
    assert "SECRET" not in str(result)


def test_live_gate_denies_even_if_client_injected():
    client = FakeClient({})
    s = Settings(enabled=True, api_key="TEST_ONLY", live_authorized=False)
    result = run(registry(s, client), "apifootball_status")
    assert result["status"] == "LIVE_NOT_AUTHORIZED"
    assert result["network_calls_made"] == 0
    assert client.calls == []


def test_missing_key_denies_calls():
    client = FakeClient({})
    s = Settings(enabled=True, api_key="", live_authorized=True)
    result = run(registry(s, client), "apifootball_status")
    assert result["status"] == "KEY_NOT_CONFIGURED"
    assert client.calls == []


def test_live_provider_error_not_misreported_as_mock():
    client = FakeClient(exc=RuntimeError("confidential-provider-message"))
    s = Settings(enabled=True, api_key="TEST_ONLY", live_authorized=True)
    result = run(registry(s, client), "apifootball_status")
    assert result["status"] == "PROVIDER_ERROR"
    assert result["error"]["code"] == "PROVIDER_REQUEST_FAILED"
    assert "confidential-provider-message" not in str(result)
    assert client.calls == [("/status", {})]


def test_invalid_real_status_shape_fails_closed_without_data_leak():
    client = FakeClient({"response": {"account": {"email": "secret@example.com"}}})
    s = Settings(enabled=True, api_key="TEST_ONLY", live_authorized=True)
    result = run(registry(s, client), "apifootball_status")
    assert result["status"] == "PROVIDER_ERROR"
    assert result["data"] is None
    assert "secret@example.com" not in str(result)


def test_cache_hit_not_counted_as_new_provider_request():
    client = FakeClient({"response": {
        "subscription": {"plan": "Pro", "active": True},
        "requests": {"current": 5, "limit_day": 7500},
    }}, cache_status="HIT")
    s = Settings(enabled=True, api_key="TEST_ONLY", live_authorized=True)
    result = run(registry(s, client), "apifootball_status")
    assert result["status"] == "REAL_API_RESPONSE"
    assert result["network_calls_made"] == 0
