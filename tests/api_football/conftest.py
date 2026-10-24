from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from bridge.api_football import registry
from bridge.api_football.client import ApiFootballClient
from bridge.api_football.config import DEFAULT_BASE_URL, Settings

# Obviously fake credential: tests never use a real key or the real provider.
FAKE_KEY = "fake-test-key-not-real-0000"

BASE_SETTINGS = Settings(
    enabled=True, api_key=FAKE_KEY, base_url=DEFAULT_BASE_URL, timeout_seconds=2.0, max_rpm=1000,
    max_daily_requests=10000, max_concurrency=4, cache_enabled=True, circuit_breaker_enabled=True,
    quota_reserve_percent=10, max_retries=1, queue_timeout_seconds=1.0, max_response_bytes=1_000_000,
    max_output_bytes=1_000_000,
)

SAMPLES = {"id": 33, "season": 2024, "int": 1, "bool": True, "date": "2026-10-24", "text": "Total Shots",
           "round": "Regular Season - 1", "timezone": "Europe/Rome", "status": "NS-FT", "ids": "1-2", "live": "all"}
WIRE_SAMPLES = {kind: ("true" if value is True else str(value)) for kind, value in SAMPLES.items()}


def endpoint(tool_name: str) -> registry.Endpoint:
    return next(e for e in registry.implemented() if e.tool_name == tool_name)


def envelope(response=None, errors=None, paging=None, results=None, **extra) -> dict:
    response = [{"fixture": {"id": 1}}] if response is None else response
    body = {"get": "fixtures", "parameters": {}, "errors": [] if errors is None else errors,
            "results": len(response) if results is None else results,
            "paging": {"current": 1, "total": 1} if paging is None else paging, "response": response}
    body.update(extra)
    return body


def json_response(body, status=200, headers=None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item(request)
        return item


@pytest.fixture
def make_client():
    def build(handler, **overrides):
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        settings = replace(BASE_SETTINGS, **overrides)
        client = ApiFootballClient(transport=httpx.MockTransport(handler), settings_loader=lambda: settings,
                                   sleep=fake_sleep)
        client.sleeps = sleeps
        return client
    return build
