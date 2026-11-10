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
    # The run goes on. Under the worst-case accounting (3 per provider call, budget 20) it ends on
    # the budget exactly where a control run without the plan error ends: same cases executed.
    assert _by_id(suite)["A06"]["outcome"] != "NOT_TESTED" and suite.stop_reason == "STOP_BUDGET_REACHED"
    control = _suite(Transport())
    control.run()
    executed = [r["id"] for r in suite.records if r["outcome"] != "NOT_TESTED"]
    assert executed == [r["id"] for r in control.records if r["outcome"] != "NOT_TESTED"]
    assert control.stop_reason == "STOP_BUDGET_REACHED" and suite.budget.committed == control.budget.committed
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


# ---- catalog rows: an explicit null name is published data, a missing name is a shape defect ----

def _catalog(rows):
    facts: dict = {}
    page = {"data": {"response": rows}, "pagination": {"current": 1, "total": 1}}
    return e2e._check_catalog_single_page(page, {}, facts), facts


def test_a_catalog_row_without_a_name_key_is_a_shape_defect():
    defects, facts = _catalog([{"id": 1, "name": "10Bet"}, {"id": 2}])
    assert defects == ["CATALOG_SHAPE_INVALID"]
    assert facts["rows_shape_invalid"] == 1 and facts["null_names_kept_null"] == 0


def test_only_an_explicit_null_name_counts_as_a_published_null():
    defects, facts = _catalog([{"id": 1, "name": "10Bet"}, {"id": 14, "name": None}])
    assert defects == [] and facts["null_names_kept_null"] == 1 and facts["rows_shape_invalid"] == 0


# ---- live odds selections (C08 real run on b8877fc: ODDS_SELECTION_NOT_DISTINCT_DECIMAL_FIELDS) ----

FID = 1602548


def _live(values, *, live=True):
    facts: dict = {}
    if live:
        item = {"fixture": {"id": FID, "status": {"elapsed": 60}}, "update": "x",
                "odds": [{"id": 36, "name": "Over/Under Line", "values": values}]}
    else:
        item = {"fixture": {"id": FID}, "bookmakers": [{"id": 8, "bets": [{"id": 5, "values": values}]}]}
    page = {"status": "REAL_API_RESPONSE", "data": {"response": [item]}}
    return e2e._check_odds(page, {"fixture_live": FID}, facts, live=live, fixture_key="fixture_live"), facts


PRICED = {"value": "Over", "odd": "1.90", "handicap": "2.5", "main": True, "suspended": False}


def test_live_suspended_null_odd_stays_null_and_is_counted():
    defects, facts = _live([PRICED, {"value": "Under", "odd": None, "handicap": "2.5", "main": None,
                                     "suspended": True}])
    assert defects == []
    assert facts == {"fixtures": 1, "selections": 2, "null_odds_kept_null": 1, "suspended": 1,
                     "suspended_zero_odds": 0}


def test_live_suspended_zero_odd_as_in_the_repo_fixture_is_counted_not_a_defect():
    # tests/test_api_football_five_point_evidence.py: a suspended live selection published odd "0".
    defects, facts = _live([{"value": "Over", "odd": "0", "suspended": True, "main": None}])
    assert defects == [] and facts["suspended_zero_odds"] == 1 and facts["suspended"] == 1


@pytest.mark.parametrize("bad", [
    {"value": "Over", "handicap": "2.5", "suspended": True},  # `odd` key missing
    {"odd": "1.90"},  # `value` key missing
    {"value": "Over", "odd": "N/A", "suspended": True},  # not a decimal string
    {"value": "Over", "odd": 1.9},  # a number, not the provider's string
    {"value": "Over", "odd": "nan"}, {"value": "Over", "odd": "1e3"}, {"value": "Over", "odd": " 1.9"},
    {"value": "Over", "odd": "0", "suspended": False},  # zero on an open selection
    {"value": "Over", "odd": "0.5", "suspended": True},  # below 1 and not zero
    "Over 1.90",
])
def test_live_missing_or_non_decimal_odd_stays_a_defect_with_facts_first(bad):
    defects, facts = _live([PRICED, bad])
    assert defects == ["ODDS_SELECTION_NOT_DISTINCT_DECIMAL_FIELDS"]
    # Facts are written before the defect: the next run shows what was counted.
    assert facts["fixtures"] == 1 and facts["selections"] == 1 and facts["null_odds_kept_null"] == 0


def test_prematch_strictness_is_unchanged():
    assert _live([{"value": "Over 2.5", "odd": "1.85"}], live=False)[0] == []
    for bad in ({"value": "Over 2.5", "odd": None}, {"value": "Over 2.5", "odd": "0"},
                {"value": "Over 2.5"}):
        defects, facts = _live([{"value": "Under 2.5", "odd": "1.95"}, bad], live=False)
        assert defects == ["ODDS_SELECTION_NOT_DISTINCT_DECIMAL_FIELDS"]
        assert facts == {"fixtures": 1, "selections": 1}  # no live-only counters on pre-match
