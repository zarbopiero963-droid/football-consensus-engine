"""PR-06 gate and wiring regression tests; never contact API-FOOTBALL."""
from __future__ import annotations

from bridge.api_football.config import Settings
from bridge.api_football.tools import register_tools
from bridge.api_football.wiring import build_client


class _Server:
    def __init__(self):
        self.names = []
    def tool(self):
        def deco(fn):
            self.names.append(fn.__name__)
            return fn
        return deco


def test_key_does_not_enable_live_requests_or_construct_client():
    settings = Settings(enabled=True, api_key="secret", live_authorized=False)
    assert settings.configured
    assert not settings.real_requests_authorized
    assert build_client(settings) is None


def test_explicit_live_gate_requires_key_and_enabled():
    assert not Settings(enabled=False, api_key="secret", live_authorized=True).real_requests_authorized
    assert not Settings(enabled=True, api_key="", live_authorized=True).real_requests_authorized
    assert Settings(enabled=True, api_key="secret", live_authorized=True).real_requests_authorized


def test_registered_tool_returns_gate_without_client():
    server = _Server()
    register_tools(server, Settings(enabled=True, api_key="secret", live_authorized=False))
    assert len(server.names) == 42
