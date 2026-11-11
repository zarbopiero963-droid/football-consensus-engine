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
    """The 'database': survives every FakeLedger instance (= process restart / workflow re-run).

    AF-15: `plans` is the approved-plan register (plan_sha256 -> budget). With auto_approve (the
    default for offline tests) the first open() of a plan approves it with the requested budget;
    with auto_approve=False an unknown plan is refused, like PostgresCampaignLedger."""

    def __init__(self, auto_approve: bool = True):
        self.auto_approve = auto_approve
        self.plans: dict[str, int] = {}
        self.campaigns: dict[str, dict] = {}
        self.attempts: list[tuple[str, int, str]] = []
        self.outcomes: dict[tuple[str, int], tuple[str, int | None]] = {}  # append-only, first wins


class FakeLedger:
    """Same semantics as PostgresCampaignLedger: write-ahead, PLAN budget shared by every campaign
    of the plan, append-only."""

    def __init__(self, store: Store, hard_cap: int = 78):
        self.store = store
        self.hard_cap = hard_cap

    async def open(self, campaign_id, budget, plan_sha):
        if plan_sha not in self.store.plans:
            if not self.store.auto_approve:
                raise PlanNotApproved("PLAN_NOT_APPROVED")
            self.store.plans[plan_sha] = budget
        stored = self.store.campaigns.setdefault(campaign_id, {"plan_sha": plan_sha})
        if stored["plan_sha"] != plan_sha:
            raise RuntimeError("CAMPAIGN_PLAN_MISMATCH")

    def _plan_used(self, plan):
        return sum(1 for c, _, _ in self.store.attempts if self.store.campaigns[c]["plan_sha"] == plan)

    async def reserve(self, campaign_id, call_id):
        plan = self.store.campaigns[campaign_id]["plan_sha"]
        if self._plan_used(plan) >= min(self.store.plans[plan], self.hard_cap):
            raise BudgetError("CAMPAIGN_BUDGET_EXHAUSTED")
        used = sum(1 for c, _, _ in self.store.attempts if c == campaign_id)
        self.store.attempts.append((campaign_id, used + 1, call_id))
        return used + 1

    async def used(self, campaign_id):
        return sum(1 for c, _, _ in self.store.attempts if c == campaign_id)

    async def plan_usage(self, campaign_id):
        plan = self.store.campaigns[campaign_id]["plan_sha"]
        return {"plan_sha256": plan, "budget": self.store.plans[plan], "used": self._plan_used(plan)}

    async def attempted_calls(self, campaign_id):
        return {k for c, _, k in self.store.attempts if c == campaign_id}

    async def record_outcome(self, campaign_id, seq, outcome, http_status):
        if outcome not in ("HTTP_RESPONSE", "TRANSPORT_ERROR"):
            raise RuntimeError("CAMPAIGN_OUTCOME_INVALID")
        self.store.outcomes.setdefault((campaign_id, seq), (outcome, http_status))

    async def attempt_states(self, campaign_id):
        names = {"HTTP_RESPONSE": "RESPONSE_RECEIVED", "TRANSPORT_ERROR": "TRANSPORT_ERROR"}
        out = []
        for c, seq, call_id in self.store.attempts:
            if c == campaign_id:
                outcome, status = self.store.outcomes.get((c, seq), (None, None))
                out.append({"seq": seq, "call_id": call_id, "state": names.get(outcome, "RESERVED_UNKNOWN"),
                            "http_status": status})
        return out


class BudgetError(RuntimeError):
    pass


class PlanNotApproved(RuntimeError):
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
