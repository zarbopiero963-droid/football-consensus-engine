"""Offline kit for the AF-05 campaign runner and the live snapshot: a persistent-like ledger, a quota
stub and a provider stub. No network, no database."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx

from bridge.api_football.config import Settings
from bridge.api_football.quota import QuotaLease

ROOT = Path(__file__).resolve().parents[1]
KEY = "LOCAL_FAKE_KEY_CAMPAIGN_KIT_0123456789"


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Store:
    """The 'database': survives every FakeLedger instance (= process restart / workflow re-run)."""

    def __init__(self):
        self.campaigns: dict[str, dict] = {}
        self.attempts: list[tuple[str, int, str]] = []


class FakeLedger:
    """Same semantics as PostgresCampaignLedger: write-ahead, budget fixed at open, append-only."""

    def __init__(self, store: Store, hard_cap: int = 78):
        self.store = store
        self.hard_cap = hard_cap

    async def open(self, campaign_id, budget, plan_sha):
        stored = self.store.campaigns.setdefault(campaign_id, {"budget": budget, "plan_sha": plan_sha})
        if stored["plan_sha"] != plan_sha or stored["budget"] > self.hard_cap:
            raise RuntimeError("CAMPAIGN_PLAN_MISMATCH")

    async def reserve(self, campaign_id, call_id):
        from_cap = min(self.store.campaigns[campaign_id]["budget"], self.hard_cap)
        used = sum(1 for c, _, _ in self.store.attempts if c == campaign_id)
        if used >= from_cap:
            raise BudgetError("CAMPAIGN_BUDGET_EXHAUSTED")
        self.store.attempts.append((campaign_id, used + 1, call_id))
        return used + 1

    async def used(self, campaign_id):
        return sum(1 for c, _, _ in self.store.attempts if c == campaign_id)

    async def attempted_calls(self, campaign_id):
        return {k for c, _, k in self.store.attempts if c == campaign_id}


class BudgetError(RuntimeError):
    pass


class Quota:
    async def acquire(self, secret):
        return QuotaLease("l", "h")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None

    async def snapshot(self, secret):
        return {}


def settings():
    return Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False)


def envelope(path: str, response, *, results=None, paging=None, errors=None) -> dict:
    return {"get": path.strip("/"), "parameters": {}, "errors": errors if errors is not None else [],
            "results": len(response) if results is None else results,
            "paging": paging or {"current": 1, "total": 1}, "response": response}


GENERIC_ROW = {"fixture": {"id": 101}, "teams": {"home": {"id": 33}, "away": {"id": 34}}, "player": {"id": 276}}


class Provider:
    """MockTransport handler. `script` maps a path to a callable(request) -> httpx.Response or raises."""

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.sent: list[str] = []
        self.default = default or (lambda request: httpx.Response(
            200, json=envelope(request.url.path, [GENERIC_ROW], paging={"current": 1, "total": 3})))

    def __call__(self, request):
        path = request.url.path.removeprefix("/v3") if request.url.path.startswith("/v3") else request.url.path
        self.sent.append(path)
        handler = self.script.get(path, self.default)
        return handler(request)

    def transport(self):
        return httpx.MockTransport(self)


def dumps(value) -> str:
    return json.dumps(value)
