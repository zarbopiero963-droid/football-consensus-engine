"""Network-free client contract and fixture-backed mock for PR-01.

Production transport is intentionally NOT implemented or registered.
A future PR may supply a hardened HTTPX transport after authorization.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .catalog import documented_path


class ProviderNotAuthorized(RuntimeError):
    """No real football API calls have been authorized in PR-01."""


class UnsupportedEndpoint(ValueError):
    """Only paths present in the documented football catalog are permitted."""


class FootballClient(Protocol):
    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        ...


@dataclass
class MockFootballClient:
    """Test-only deterministic fixture adapter; no sockets, secrets or HTTP."""

    fixtures: dict[str, dict[str, Any]]
    calls: list[tuple[str, dict[str, str | int]]] = field(default_factory=list)

    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        if not documented_path(path):
            raise UnsupportedEndpoint("API-FOOTBALL path not allowed")
        # Validate the mock boundary too; never silently interpolate caller URLs.
        checked: dict[str, str | int] = {}
        for key, value in (params or {}).items():
            if not isinstance(key, str) or not key.isidentifier() or not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError("Invalid mock request parameter")
            checked[key] = value
        self.calls.append((path, checked))
        if path not in self.fixtures:
            raise ProviderNotAuthorized("No mock fixture for this endpoint")
        return deepcopy(self.fixtures[path])


class RealFootballClient:
    """Explicitly disabled until independent security/resilience PR approval."""

    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        if not documented_path(path):
            raise UnsupportedEndpoint("API-FOOTBALL path not allowed")
        raise ProviderNotAuthorized("Real API requests are not authorized in PR-01")
