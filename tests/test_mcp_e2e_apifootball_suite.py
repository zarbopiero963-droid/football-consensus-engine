"""Opt-in API-FOOTBALL suite of the protected MCP E2E client (owner decision, path B), offline.

The suite is driven through the REAL apifootball_* tool functions (tools.py) over the REAL
resilience client (retries, breaker, cache); only the provider HTTP transport is a fake that
counts every request it receives, like the provider's own daily counter. Proves:
- the hard budget: the worst case (3 attempts) is reserved before each call, a call that could
  exceed the budget is never made, and what reaches the provider never exceeds the budget, with
  real retries, missing counters and other consumers;
- every stop condition, with the remaining cases NOT_TESTED and no further request;
- the strict allowlist and the reservation guard on every tools/call path;
- deterministic id selection, outcome classification and envelope correspondence;
- no price, key or personal field in the public report.
"""
from __future__ import annotations

import json
import random

import pytest

import _apifootball_fake_provider as fake
from bridge.api_football.contracts import ENDPOINTS
from bridge.api_football.quota import QuotaExceeded
from bridge.api_football.resilience import RetryPolicy
from scripts import mcp_e2e_render as e2e


def _suite(transport=None, *, budget=20, full=False, quota=None, wrap=None, sleep=None, now=None):
    transport = transport or fake.FakeTransport()
    call, _ = fake.tool_caller(transport, quota)
    real = call
    if wrap is not None:
        def call(tool, args):  # noqa: F811 - a tampered server for RED controls
            return wrap(tool, args, real)
    suite = e2e.ApiFootballSuite(call, e2e.ProviderBudget(budget), full=full,
                                 now=now or (lambda: fake.NOW), sleep=sleep or (lambda s: None))
    suite.real_call = real
    return suite, transport


def _by_id(suite):
    return {r["id"]: r for r in suite.records}


def _after(suite, cid):
    ids = [r["id"] for r in suite.records]
    return suite.records[ids.index(cid) + 1:]


# ---- contracts the budget is derived from ---------------------------------------------------

def test_the_allowlist_is_exactly_the_read_only_provider_contracts():
    assert e2e.APIFOOTBALL_PROVIDER_TOOLS == {name: ep.path for name, ep in ENDPOINTS.items()}
    assert len(e2e.APIFOOTBALL_PROVIDER_TOOLS) == 40
    assert not any(n.startswith(("sxbet_", "browser_", "cloak_")) for n in e2e.APIFOOTBALL_PROVIDER_TOOLS)
    # The two local tools make no provider request and stay outside the budgeted list.
    assert {"apifootball_health", "apifootball_quota_report"}.isdisjoint(e2e.APIFOOTBALL_PROVIDER_TOOLS)


def test_the_worst_case_per_call_is_the_code_retry_bound():
    assert RetryPolicy().max_attempts == e2e.APIFOOTBALL_MAX_ATTEMPTS_PER_CALL == 3
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=4)  # the server cannot be configured above it
    assert e2e.APIFOOTBALL_WORST_CASE_PER_CALL == 3
    assert e2e.APIFOOTBALL_BUDGET_CEILING == 20  # the owner-approved number
    for bad in (0, 21, -1, True, "20", 20.0):
        with pytest.raises(ValueError):
            e2e.ProviderBudget(bad)


def test_the_plans_and_their_call_counts():
    p, f = e2e.plan_summary(False), e2e.plan_summary(True)
    assert p == {"cases": 16, "provider_calls_nominal": 15, "worst_case_if_every_call_retries": 45,
                 "min_budget_to_complete_without_retries": 19, "provider_tools_covered": 10}
    assert f == {"cases": 46, "provider_calls_nominal": 45, "worst_case_if_every_call_retries": 135,
                 "min_budget_to_complete_without_retries": 49, "provider_tools_covered": 40}
    for full in (False, True):
        assert all(c.tool in e2e.APIFOOTBALL_PROVIDER_TOOLS for c in e2e._plan(full))
        assert [c.cid for c in e2e._plan(full)][-1] == "A15"  # the negative id stays last


# ---- the happy path through the real tools --------------------------------------------------

def test_prioritized_plan_passes_within_the_budget_on_real_tool_outputs():
    suite, t = _suite()
    suite.run()
    rep = suite.report()
    assert rep["verdict"] == "PASS" and rep["stop_reason"] is None, rep
    assert len(t.requests) == 15 and rep["provider_requests_charged"] == 17 <= 20
    r = _by_id(suite)
    assert r["N00"]["charged"] == 0 and r["N00"]["settlement"] == "CODE_PATH_NO_REQUEST"
    assert r["A01"]["outcome"] == "HTTP_200" and r["A01"]["charged"] == 3  # no earlier counter reading
    assert all(r[c]["settlement"] == "PROVIDER_COUNTER_DELTA" and r[c]["charged"] == 1
               for c in r if c not in ("N00", "A01"))
    assert all(x["reserved"] == 3 for x in suite.records)
    assert r["A15"]["outcome"] == "EMPTY_VALID" and r["A15"]["verdict"] == "PASS"
    assert r["A09"]["facts"] == {"fixtures": 1, "selections": 3, "over_published": 2, "under_published": 1}
    assert r["A06"]["facts"]["null_values_kept_null"] == 2
    assert r["A04"]["facts"]["halftime_as_published"] == [1, 1]
    assert r["A11"]["arguments"]["page"] == 2 and r["A11"]["paging"] == {"current": 2, "total": 2}
    text = json.dumps(rep)
    assert fake.PRICE_SENTINEL not in text and fake.KEY not in text and "PRIVATE" not in text


def test_full_plan_covers_all_40_provider_tools_with_a_raised_ceiling(monkeypatch):
    """What full coverage costs: 45 requests, 49 of budget without retries (ceiling stays 20)."""
    monkeypatch.setattr(e2e, "APIFOOTBALL_BUDGET_CEILING", 49)
    suite, t = _suite(budget=49, full=True)
    suite.run()
    rep = suite.report()
    assert rep["verdict"] == "PASS", [(c["id"], c.get("defects"), c.get("reason")) for c in rep["cases"]]
    assert len(t.requests) == 45 and rep["provider_requests_charged"] == 47
    assert {c["tool"] for c in rep["cases"]} == set(e2e.APIFOOTBALL_PROVIDER_TOOLS)


def test_full_plan_under_the_owner_ceiling_stops_at_the_budget():
    suite, t = _suite(full=True)
    suite.run()
    rep = suite.report()
    assert rep["stop_reason"] == "STOP_BUDGET_REACHED" and rep["verdict"] == "BLOCKED"
    assert len(t.requests) <= 20 and rep["provider_requests_charged"] <= 20


# ---- the hard budget --------------------------------------------------------------------------

class RetryTwice(fake.FakeTransport):
    """Every logical call costs 3 real requests: two 503s, then the reply (worst case)."""

    async def get(self, path, params=None):
        if len(self.requests) % 3 != 2:
            self.requests.append((path, dict(params or {})))
            self.day -= 1
            raise e2e_errors().ProviderHTTPError(503)
        return await super().get(path, params)


def e2e_errors():
    from bridge.api_football import errors
    return errors


@pytest.mark.parametrize("budget", range(1, 21))
def test_worst_case_retries_never_exceed_the_budget(budget):
    suite, t = _suite(RetryTwice(), budget=budget)
    suite.run()
    assert len(t.requests) <= budget and suite.budget.committed <= budget
    assert len(t.requests) == suite.budget.committed  # every attempt charged
    assert suite.stop_reason == "STOP_BUDGET_REACHED"
    if budget < 3:
        assert t.requests == [] and all(r["outcome"] == "NOT_TESTED" for r in suite.records)


@pytest.mark.parametrize("seed", range(40))
def test_random_failures_missing_counters_and_other_consumers_never_exceed_the_budget(seed):
    rng = random.Random(seed)

    class Chaos(fake.FakeTransport):
        async def get(self, path, params=None):
            roll = rng.random()
            if roll < 0.15:
                self.requests.append((path, dict(params or {})))
                self.day -= 1
                raise e2e_errors().ProviderUnavailable("PROVIDER_TIMEOUT")  # retryable
            if roll < 0.25:
                self.by_path.setdefault(path, []).insert(0, "no_headers")
            return await super().get(path, params)

    t = Chaos(other_consumer=lambda i: rng.choice((0, 0, 0, 1, 4)))
    suite, _ = _suite(t, budget=rng.randint(1, 20))
    suite.run()
    assert len(t.requests) <= suite.budget.budget and suite.budget.committed <= suite.budget.budget
    assert len(t.requests) <= suite.budget.committed  # the charge is an upper bound of the real cost


def test_settlement_rules():
    b = e2e.ProviderBudget(20)
    def settle(payload):
        b.committed = 0  # the rules only, not the budget, are under test here
        b.reserve("apifootball_fixtures")
        return b.settle(payload)
    real = {"status": "REAL_API_RESPONSE", "cache_status": "MISS"}
    assert settle({"status": "INVALID_INPUT"}) == (0, "CODE_PATH_NO_REQUEST", False)
    assert settle({"status": "LIVE_NOT_AUTHORIZED"}) == (0, "CODE_PATH_NO_REQUEST", False)
    assert settle({**real, "cache_status": "HIT", "quota_remaining": {"day": 1}}) == (0, "CACHE_HIT", False)
    assert b.last_day_remaining is None  # a HIT replays an old counter: never a reading
    assert settle(None) == (3, "WORST_CASE_NO_REPLY", False)
    assert settle({**real, "quota_remaining": {"day": 100}}) == (3, "WORST_CASE_NO_BASELINE", False)
    assert settle({**real, "quota_remaining": {"day": 99}}) == (1, "PROVIDER_COUNTER_DELTA", False)
    assert settle({**real, "quota_remaining": {"day": 97}}) == (2, "PROVIDER_COUNTER_DELTA", False)
    assert settle({**real, "quota_remaining": {"day": 97}}) == (3, "WORST_CASE_COUNTER_UNCHANGED", False)
    assert settle({**real, "quota_remaining": {"day": None}}) == (3, "WORST_CASE_NO_EVIDENCE", False)
    assert settle({"status": "PAYLOAD_TOO_LARGE"}) == (3, "WORST_CASE_NO_EVIDENCE", False)
    # 97 -> 95 with one request provably made by the oversized call in between: 1 for this one.
    assert settle({**real, "quota_remaining": {"day": 95}}) == (1, "PROVIDER_COUNTER_DELTA", False)
    assert settle({**real, "quota_remaining": {"day": 90}}) == (3, "WORST_CASE_COUNTER_ANOMALY", True)
    assert settle({**real, "quota_remaining": {"day": 500}}) == (3, "WORST_CASE_COUNTER_ANOMALY", True)


def test_a_reservation_that_would_exceed_the_budget_is_refused():
    b = e2e.ProviderBudget(5)
    b.reserve("apifootball_status")
    with pytest.raises(e2e.BudgetExceeded):
        b.reserve("apifootball_status")  # one open reservation at a time
    b.settle(None)
    assert b.committed == 3 and not b.can_reserve()
    with pytest.raises(e2e.BudgetExceeded):
        b.reserve("apifootball_leagues")
    with pytest.raises(RuntimeError):
        b.settle(None)


def test_calls_are_paced_under_the_runtime_per_minute_limit():
    clock = {"t": float(fake.NOW)}
    sleeps = []
    suite, t = _suite(now=lambda: clock["t"], sleep=lambda s: (sleeps.append(s), clock.__setitem__("t", clock["t"] + s)))
    suite.run()
    assert len(sleeps) == len([r for r in suite.records if r["reserved"]]) - 1
    assert all(0 < s <= e2e.APIFOOTBALL_CALL_SPACING_SECONDS for s in sleeps)
    assert 60 / e2e.APIFOOTBALL_CALL_SPACING_SECONDS < 8  # runtime default APIFOOTBALL_MAX_RPM


# ---- stop conditions --------------------------------------------------------------------------

def _assert_stopped_at(suite, t, cid, reason, requests):
    rep = suite.report()
    assert suite.stop_reason == reason and rep["verdict"] in ("BLOCKED", "FAIL"), rep
    assert _by_id(suite)[cid].get("stop") == reason
    rest = _after(suite, cid)
    assert rest and all(r["outcome"] == "NOT_TESTED" and r["reason"] == reason and r["reserved"] == 0 for r in rest)
    assert len(t.requests) == requests


@pytest.mark.parametrize("behaviour,reason,requests", [
    (["http_401"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 2),
    (["http_403"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 2),
    (["http_429"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 2),
    (["http_503", "http_503", "http_503"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 4),
    (["declared_errors"], "STOP_UNEXPECTED_PROVIDER_ERROR", 2),
    (["minute_zero"], "STOP_PROVIDER_QUOTA_ZERO", 2),
])
def test_provider_failures_stop_the_run(behaviour, reason, requests):
    suite, t = _suite(fake.FakeTransport(by_path={"/leagues": behaviour}))
    suite.run()
    _assert_stopped_at(suite, t, "A02", reason, requests)
    # A reply that reports a zero minute budget is itself valid data; every failure is not.
    assert _by_id(suite)["A02"]["verdict"] == ("PASS" if behaviour == ["minute_zero"] else
                                               "FAIL" if behaviour == ["declared_errors"] else "BLOCKED")


def test_daily_quota_zero_stops_the_run():
    suite, t = _suite(fake.FakeTransport(day_remaining=2))
    suite.run()
    _assert_stopped_at(suite, t, "A02", "STOP_PROVIDER_QUOTA_ZERO", 2)


def test_an_open_circuit_stops_the_run():
    suite, t = _suite()
    suite.real_call.client.breaker._open_until = float("inf")
    suite.run()
    _assert_stopped_at(suite, t, "A01", "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 0)


@pytest.mark.parametrize("error,reason", [
    (RuntimeError("db down"), "STOP_COORDINATOR_UNAVAILABLE"),
    (QuotaExceeded("local"), "STOP_LOCAL_QUOTA_EXHAUSTED"),
])
def test_coordinator_and_local_quota_failures_stop_before_any_request(error, reason):
    suite, t = _suite(quota=fake.FakeQuota(error=error))
    suite.run()
    _assert_stopped_at(suite, t, "A01", reason, 0)
    assert _by_id(suite)["A01"]["verdict"] == "BLOCKED"


def test_an_accounting_anomaly_stops_the_run():
    t = fake.FakeTransport(other_consumer=lambda i: 10 if i == 3 else 0)
    suite, _ = _suite(t)
    suite.run()
    _assert_stopped_at(suite, t, "A03", "STOP_QUOTA_ACCOUNTING_ANOMALY", 3)
    assert _by_id(suite)["A03"]["charged"] == 3


def test_a_failed_mcp_call_stops_with_the_worst_case_charged():
    suite, t = _suite(wrap=lambda tool, args, real: None if tool == "apifootball_leagues" else real(tool, args))
    suite.run()
    _assert_stopped_at(suite, t, "A02", "STOP_MCP_CALL_FAILED", 1)
    assert _by_id(suite)["A02"]["charged"] == 3 and _by_id(suite)["A02"]["settlement"] == "WORST_CASE_NO_REPLY"


def test_a_server_without_live_authorization_stops_at_no_cost():
    def wrap(tool, args, real):
        return {"status": "LIVE_NOT_AUTHORIZED", "network_calls_made": 0} if tool == "apifootball_status" else real(tool, args)
    suite, t = _suite(wrap=wrap)
    suite.run()
    _assert_stopped_at(suite, t, "A01", "STOP_PROVIDER_NOT_ENABLED", 0)
    assert suite.budget.committed == 0


def test_a_provider_plan_too_small_for_the_budget_stops_after_status():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_status":
            p["data"]["requests"] = {"current": 95, "limit_day": 100}
        return p
    suite, t = _suite(wrap=wrap)
    suite.run()
    _assert_stopped_at(suite, t, "A01", "STOP_PROVIDER_QUOTA_LOW", 1)


def test_a_known_oversized_body_is_an_error_but_not_a_stop():
    suite, t = _suite(fake.FakeTransport(by_path={"/fixtures/events": ["too_large"]}))
    suite.run()
    r = _by_id(suite)
    assert r["A05"]["outcome"] == "ERROR" and r["A05"]["defects"] == ["F03_PROVIDER_BODY_TOO_LARGE"]
    assert r["A05"]["verdict"] == "FAIL" and "stop" not in r["A05"] and suite.report()["verdict"] == "FAIL"
    assert r["A05"]["charged"] == 3  # no counter reading: the worst case stays charged
    # The next reading covers two requests; one is provably the oversized call's.
    assert r["A06"]["charged"] == 1 and r["A06"]["settlement"] == "PROVIDER_COUNTER_DELTA"
    # Two more charged units leave no room for the last case: the budget, not the error, stops.
    assert r["A14"]["verdict"] == "PASS" and suite.stop_reason == "STOP_BUDGET_REACHED"
    assert len(t.requests) == 14 and suite.budget.committed == 18


# ---- deterministic selection ------------------------------------------------------------------

def test_fixture_selection_is_deterministic_and_order_independent():
    for seed in range(20):
        rows = list(fake.WINDOW)
        random.Random(seed).shuffle(rows)
        x, u = e2e.select_fixtures(rows, fake.NOW)
        assert x["fixture"]["id"] == 1002  # latest finished kickoff, tie -> lowest id (AET counts)
        assert u["fixture"]["id"] == 2001  # nearest NS at least 2 h ahead (2000 is too close), tie -> lowest
    assert e2e.select_fixtures([], fake.NOW) == (None, None)


def test_ambiguous_or_missing_ids_are_never_guessed():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_leagues":
            p["data"]["response"][0]["seasons"][0]["current"] = True  # two current seasons
        if tool == "apifootball_bets":
            p["data"]["response"].append({"id": 77, "name": "Goals Over/Under"})  # two exact names
            p["data"]["results"] += 1
        return p
    suite, t = _suite(wrap=wrap)
    suite.run()
    r = _by_id(suite)
    assert r["A02"]["verdict"] == "FAIL" and r["A02"]["defects"] == ["CURRENT_SEASON_NOT_UNIQUE"]
    assert r["A03"]["reason"] == "NOT_DERIVABLE_SEASON" and r["A04"]["reason"] == "NOT_DERIVABLE_FIXTURE_X"
    assert r["A09"]["reason"] == "NOT_DERIVABLE_FIXTURE_U" and r["A14"]["reason"] == "NOT_DERIVABLE_SEASON"
    assert r["A08"]["facts"]["over_under_bet_ids"] == [5, 77] and "bet_ou" not in suite.ctx
    assert r["A07"]["verdict"] == "PASS" and r["A12"]["verdict"] == "PASS"  # independent cases still run


def test_no_live_fixture_is_empty_valid_and_live_odds_not_tested():
    def wrap(tool, args, real):
        p = real(tool, args)
        if args.get("live") == "all":
            p["data"]["response"], p["data"]["results"] = [], 0
        return p
    suite, t = _suite(wrap=wrap)
    suite.run()
    r = _by_id(suite)
    assert r["A12"]["outcome"] == "EMPTY_VALID" and r["A12"]["verdict"] == "PASS"
    assert r["A13"]["outcome"] == "NOT_TESTED" and r["A13"]["reason"] == "NO_LIVE_FIXTURE"
    assert suite.report()["verdict"] == "PARTIAL" and suite.stop_reason is None


def test_single_odds_page_skips_page_two_without_a_call():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_odds" and "league" in args:
            p["data"]["paging"] = p["pagination"] = {"current": 1, "total": 1}
        return p
    suite, t = _suite(wrap=wrap)
    suite.run()
    assert _by_id(suite)["A11"]["reason"] == "NOT_APPLICABLE_SINGLE_PAGE" and len(t.requests) == 14


# ---- classification and structure -------------------------------------------------------------

def test_oversized_odds_become_an_explicit_price_free_manifest():
    suite, t = _suite(fake.FakeTransport(huge_odds=True))
    suite.run()
    r = _by_id(suite)["A10"]
    assert r["tool_status"] == "PARTIAL_ODDS_MANIFEST" and r["outcome"] == "PARTIAL" and r["verdict"] == "PARTIAL"
    assert r["facts"]["manifest"] is True and r["paging"] == {"current": 1, "total": 2}
    assert _by_id(suite)["A11"]["verdict"] == "PASS"  # page 2 still reached from the manifest paging
    assert suite.report()["verdict"] == "PARTIAL" and fake.PRICE_SENTINEL not in json.dumps(suite.report())


@pytest.mark.parametrize("tool,mutate,defect", [
    ("apifootball_standings", lambda p: p["data"].update(results=99), "RESULTS_NOT_EQUAL_LEN_RESPONSE"),
    ("apifootball_standings", lambda p: p["data"].update(parameters={}), "PARAMETERS_NOT_ECHOED_EXACTLY"),
    ("apifootball_standings", lambda p: p["data"].pop("paging"), "ENVELOPE_FIELD_MISSING_PAGING"),
    ("apifootball_standings", lambda p: p["data"].update(errors={"x": "y"}), "PROVIDER_ERRORS_NOT_SURFACED"),
    ("apifootball_standings", lambda p: p.update(pagination={"current": 1, "total": 9}),
     "MCP_PAGINATION_NOT_PROVIDER_PAGING"),
    ("apifootball_standings", lambda p: p["data"].update(paging={"current": 3, "total": 2}), "PAGING_INCONSISTENT"),
    ("apifootball_standings", lambda p: p["data"].update(get="teams"), "ENVELOPE_GET_MISMATCH"),
    ("apifootball_fixture_statistics",
     lambda p: p["data"]["response"][0]["statistics"][0].update(value=True), "STATISTIC_VALUE_TYPE_INVALID"),
    ("apifootball_fixture_statistics",
     lambda p: p["data"]["response"][0]["statistics"][0].pop("value"), "STATISTIC_FIELD_MISSING"),
    ("apifootball_odds", lambda p: p["data"]["response"][0]["bookmakers"][0]["bets"][0]["values"][0].update(odd=1.85),
     "ODDS_SELECTION_NOT_DISTINCT_DECIMAL_FIELDS"),
    ("apifootball_fixtures", lambda p: p["data"]["response"] and p["data"]["response"][0]["score"]["halftime"]
     .update(home="0"), "SCORE_VALUE_TYPE_INVALID"),
])
def test_structural_defects_fail_the_case(tool, mutate, defect):
    seen = {"n": 0}
    def wrap(t_, args, real):
        p = real(t_, args)
        if t_ == tool and (tool != "apifootball_fixtures" or "id" in args and args["id"] != e2e.NONEXISTENT_FIXTURE_ID):
            if seen["n"] == 0:
                mutate(p)
            seen["n"] += 1
        return p
    suite, _ = _suite(wrap=wrap)
    suite.run()
    failed = [r for r in suite.records if r["verdict"] == "FAIL"]
    assert failed and defect in failed[0]["defects"], [(r["id"], r.get("defects")) for r in suite.records]
    assert suite.report()["verdict"] == "FAIL"


def test_a_manifest_without_explicit_partial_flags_fails():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_odds" and "league" in args and "page" not in args:
            p["status"] = "PARTIAL_ODDS_MANIFEST"
            p["data"] = {"partial": False, "paging": p["pagination"], "fixtures_in_page": 1}
        return p
    suite, _ = _suite(wrap=wrap)
    suite.run()
    assert _by_id(suite)["A10"]["defects"] == ["ODDS_MANIFEST_NOT_EXPLICIT"]


def test_null_halftime_stays_null_and_the_negative_id_with_data_fails():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_fixtures" and args.get("id") == 1002:
            p["data"]["response"][0]["score"]["halftime"] = {"home": None, "away": None}
        if tool == "apifootball_fixtures" and args.get("id") == e2e.NONEXISTENT_FIXTURE_ID:
            p["data"]["response"], p["data"]["results"] = [{"fixture": {"id": 1}}], 1
        return p
    suite, _ = _suite(wrap=wrap)
    suite.run()
    r = _by_id(suite)
    assert r["A04"]["verdict"] == "PASS" and r["A04"]["facts"]["halftime_as_published"] == [None, None]
    assert r["A15"]["outcome"] == "DATA_PRESENT" and r["A15"]["verdict"] == "FAIL"


def test_classification_of_tool_statuses():
    assert e2e._classify({"status": "REAL_API_RESPONSE", "data": {"response": [1]}}) == "DATA_PRESENT"
    assert e2e._classify({"status": "REAL_API_RESPONSE", "data": {"response": []}}) == "EMPTY_VALID"
    assert e2e._classify({"status": "REAL_API_RESPONSE", "data": {"subscription": {}}}) == "HTTP_200"
    assert e2e._classify({"status": "PARTIAL_ODDS_MANIFEST"}) == "PARTIAL"
    for s in ("PROVIDER_ERROR", "PAYLOAD_TOO_LARGE", "INVALID_INPUT", None):
        assert e2e._classify({"status": s}) == "ERROR"


def test_a_validator_that_stopped_refusing_the_negative_input_fails():
    def wrap(tool, args, real):
        if args.get("timezone") == "Europe/Rome":
            return real(tool, {**args, "timezone": "UTC"})  # a server that ignores the policy
        return real(tool, args)
    suite, _ = _suite(wrap=wrap)
    suite.run()
    assert _by_id(suite)["N00"]["verdict"] == "FAIL"


# ---- allowlist and reservation guard on every path --------------------------------------------

def _run(suite="apifootball"):
    return e2e.Run(client=None, base=e2e.PINNED_BASE, password="LOCAL-ONLY-pw-31f9", expected_commit=None,
                   mask=lambda v: None, suite=suite, apifootball_budget=20)


def test_the_opt_in_suite_refuses_anything_outside_the_read_only_provider_tools():
    run = _run()
    for name in ("sxbet_balance", "sxbet_place_order", "browser_create", "apifootball_quota_report", None):
        with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
            run.rpc("tools/call", {"name": name, "arguments": {}}, "t")
        with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
            run.call_tool_payload(name, {}, "t")
    assert run.tools_called == []


def test_a_provider_tool_needs_its_own_open_reservation():
    run = _run()
    with pytest.raises(e2e.Fail, match="provider_call_without_reservation"):
        run.rpc("tools/call", {"name": "apifootball_odds", "arguments": {}}, "t")
    run.af_budget.reserve("apifootball_status")
    with pytest.raises(e2e.Fail, match="provider_call_without_reservation"):
        run.rpc("tools/call", {"name": "apifootball_odds", "arguments": {}}, "t")
    assert run.tools_called == []
    with pytest.raises(ValueError):
        e2e.Case("X", "sxbet_balance", lambda c: {}, "data")


def test_the_default_suite_is_unchanged():
    run = e2e.Run(client=None, base=e2e.PINNED_BASE, password="p", expected_commit=None, mask=lambda v: None)
    assert run.suite == "health" and run.allowed_tools == e2e.ALLOWED_TOOL_CALLS and run.af_budget is None
    with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
        run.rpc("tools/call", {"name": "apifootball_status", "arguments": {}}, "t")
    rep = run.report(None)
    assert "apifootball_suite" not in rep and rep["provider_or_order_calls_attempted"] == 0


# ---- public report ---------------------------------------------------------------------------

def test_the_suite_section_is_leak_checked_and_size_bounded():
    run = _run()
    suite, _ = _suite()
    suite.run()
    run.af_suite, run.af_budget = suite, suite.budget
    report, text = e2e.finalize(run, None)
    assert report["apifootball_suite"]["verdict"] == "PASS" and fake.PRICE_SENTINEL not in text
    tok = run.secret("tok-" + "y" * 40)
    suite.records[0]["arguments"] = {"x": tok}  # a secret that would reach the public report
    report, text = e2e.finalize(run, None)
    assert report == {"verdict": "FAIL", "failed_check": "report_leak_check"} and tok not in text
    suite.records[0]["arguments"] = {"x": "z" * e2e.SUITE_REPORT_MAX_BYTES}
    assert run.apifootball_report()["stop_reason"] == "SUITE_REPORT_TOO_LARGE"


def test_untrusted_provider_text_is_neutralised_for_the_summary():
    assert e2e._safe_name("Team | <b>x</b>`$(id)`\n") == "Team  bx/b(id)"  # no |, <, >, `, $, newline
    assert e2e._code("PROVIDER_REQUEST_FAILED") == "PROVIDER_REQUEST_FAILED"
    assert e2e._code("x\n::warning::y") is None and e2e._code("A" * 65) is None
