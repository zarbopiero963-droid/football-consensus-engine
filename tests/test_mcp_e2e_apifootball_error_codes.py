"""E2E runner mapping of the distinct API-FOOTBALL error codes (G05/G06), offline.

Driven through the REAL tools.py and resilience client, with the shared fake transport; only
new behaviours are added here (by subclassing), so the shared fake stays untouched.
"""
from __future__ import annotations

import json

import pytest

import _apifootball_fake_provider as fake
from bridge.api_football.errors import ProviderDeclaredErrors
from scripts import mcp_e2e_render as e2e

SECRET_VALUE = "Free plans do not have access SECRET-PROVIDER-TEXT https://dashboard.example.invalid"


class Transport(fake.FakeTransport):
    """Adds `declared:<key>[,<key>]` (HTTP 200 with provider errors) to the shared fake."""

    async def get(self, path, params=None):
        queue = self.by_path.get(path) or []
        if queue and queue[0].startswith("declared:"):
            behaviour = queue.pop(0)
            self.requests.append((path, dict(params or {})))
            self.day -= 1
            raise ProviderDeclaredErrors(tuple(behaviour[9:].split(",")), quota_remaining_day=max(self.day, 0),
                                         quota_remaining_minute=299)
        return await super().get(path, params)


def _suite(transport):
    call, _ = fake.tool_caller(transport)
    return e2e.ApiFootballSuite(call, e2e.ProviderBudget(20), now=lambda: fake.NOW, sleep=lambda s: None)


def _by_id(suite):
    return {r["id"]: r for r in suite.records}


def test_plan_not_included_is_provider_unavailable_with_evidence_and_the_run_goes_on():
    t = Transport(by_path={"/fixtures/events": ["declared:plan"]})
    suite = _suite(t)
    suite.run()
    rec = _by_id(suite)["A05"]
    assert rec["verdict"] == "PROVIDER_UNAVAILABLE" and "stop" not in rec and rec["defects"] == []
    assert rec["error_code"] == "PROVIDER_DECLARED_ERRORS"
    assert rec["facts"] == {"unavailable_evidence": {"source": "PROVIDER_DECLARED_ERRORS", "keys": ["plan"],
                                                     "category": "PLAN_NOT_INCLUDED"}}
    assert rec["charged"] == rec["reserved"] == 3  # no settlement down on an error
    # The run goes on (it ends later only on the per-run budget, as without the plan error).
    assert _by_id(suite)["A06"]["outcome"] != "NOT_TESTED" and suite.stop_reason == "STOP_BUDGET_REACHED"
    rep = suite.report()
    assert rep["verdict"] == "BLOCKED" and rep["verdicts"]["PROVIDER_UNAVAILABLE"] == 1
    assert "SECRET-PROVIDER-TEXT" not in json.dumps(rep) and "example.invalid" not in json.dumps(rep)


@pytest.mark.parametrize("keys", ["plan,token", "OTHER", "season"])
def test_other_declared_errors_are_never_evidence_of_unavailability(keys):
    suite = _suite(Transport(by_path={"/leagues": ["declared:" + keys]}))
    suite.run()
    rec = _by_id(suite)["A02"]
    assert rec["verdict"] == "FAIL" and rec["stop"] == "STOP_UNEXPECTED_PROVIDER_ERROR"


@pytest.mark.parametrize("keys", ["token", "rateLimit", "requests"])
def test_declared_auth_or_quota_errors_stop_as_environment(keys):
    suite = _suite(Transport(by_path={"/leagues": ["declared:" + keys]}))
    suite.run()
    rec = _by_id(suite)["A02"]
    assert rec["stop"] == "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT" and rec["verdict"] == "NOT_TESTED"


@pytest.mark.parametrize("behaviour,code", [(["http_400"], "PROVIDER_REQUEST_REJECTED"),
                                           (["http_404"], "PROVIDER_REQUEST_REJECTED")])
def test_a_rejected_request_is_a_fail_never_masked_as_environment(behaviour, code):
    suite = _suite(Transport(by_path={"/leagues": behaviour}))
    suite.run()
    rec = _by_id(suite)["A02"]
    assert rec["error_code"] == code and rec["verdict"] == "FAIL"
    assert rec["stop"] == "STOP_UNEXPECTED_PROVIDER_ERROR" and suite.report()["verdict"] == "FAIL"


@pytest.mark.parametrize("code,reason", [
    ("PROVIDER_AUTH_FAILED", "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT"),
    ("PROVIDER_RATE_LIMITED", "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT"),
    ("PROVIDER_SERVER_ERROR", "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT"),
    ("CIRCUIT_OPEN", "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT"),
    # MCP-side or request defects: never masked as an environment stop (FAIL, not BLOCKED).
    ("PROVIDER_REQUEST_FAILED", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("QUEUE_TIMEOUT", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("PROVIDER_REQUEST_REJECTED", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("PROVIDER_REPLY_CONTRACT_INVALID", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("INVALID_STATUS_RESPONSE", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("UNSUPPORTED_ENDPOINT", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("REQUEST_VALIDATION_FAILED", "STOP_UNEXPECTED_PROVIDER_ERROR"),
    ("PROVIDER_RESPONSE_TOO_LARGE", None),
])
def test_stop_reason_for_each_fixed_code(code, reason):
    assert e2e.stop_reason_for({"status": "PROVIDER_ERROR", "error": {"code": code}}) == reason
    # Only the provider-side stop is NOT_TESTED; every MCP/request defect is FAIL.
    assert e2e.STOP_VERDICT.get(reason, "FAIL") == ("NOT_TESTED" if reason == "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT" else "FAIL")


def test_plan_evidence_needs_the_exact_keys_and_category():
    base = {"status": "PROVIDER_ERROR", "error": {"code": "PROVIDER_DECLARED_ERRORS", "keys": ["plan"],
                                                  "category": "PLAN_NOT_INCLUDED"}}
    assert e2e.plan_not_included(base)
    for change in ({"keys": ["plan", "token"]}, {"keys": []}, {"category": "UNKNOWN"},
                   {"code": "PROVIDER_REQUEST_FAILED"}):
        p = {**base, "error": {**base["error"], **change}}
        assert not e2e.plan_not_included(p)
    assert not e2e.plan_not_included({**base, "status": "REAL_API_RESPONSE"})


@pytest.mark.parametrize("status", ["PROVIDER_ERROR", "PAYLOAD_TOO_LARGE", "SOMETHING_ELSE"])
def test_a_cache_hit_flag_on_a_non_reply_is_charged_in_full(status):
    b = e2e.ProviderBudget(20)
    b.reserve("apifootball_countries")
    charged, basis = b.settle({"status": status, "cache_status": "HIT"})
    assert (charged, basis) != (0, "CACHE_HIT") and charged == 3


def test_a_real_cache_hit_is_still_free():
    b = e2e.ProviderBudget(20)
    b.reserve("apifootball_countries")
    assert b.settle({"status": "REAL_API_RESPONSE", "cache_status": "HIT"}) == (0, "CACHE_HIT")


def test_server_error_after_retries_reports_the_real_attempts():
    t = Transport(by_path={"/leagues": ["http_503"] * 3})
    suite = _suite(t)
    suite.run()
    rec = _by_id(suite)["A02"]
    assert rec["error_code"] == "PROVIDER_SERVER_ERROR" and rec["network_calls_made_reported"] == 3
