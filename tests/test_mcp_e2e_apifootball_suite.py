"""Opt-in API-FOOTBALL suites of the protected MCP E2E client, offline.

The suites are driven through the REAL apifootball_* tool functions (tools.py) over the REAL
resilience client (retries, breaker, cache); only the provider HTTP transport is a fake that
counts every request it receives, like the provider's own daily counter. Proves (owner decision
"API-FOOTBALL REAL E2E COMPLETION"):
- conservative counting: every call that can reach the provider is charged its worst case (3);
  0 only on code paths that provably make no request; the charge bounds the real HTTP attempts,
  retries included, for every budget from 1 to 45; a call is refused when charged + 3 > budget;
- the provider counter is information only: a non-monotonic counter never stops the run;
- the ceilings: 45 for apifootball_completion, 20 for the earlier suites;
- the completion plan: what is still unproven only, its derivations, NOT_TESTED when U is absent;
- per-case verdicts PASS / FAIL / PARTIAL / NOT_TESTED / PROVIDER_UNAVAILABLE, the last only with
  explicit evidence in the facts (an empty reply alone never is);
- the bookmakers catalog check keeps null names null (regression of production run 4, A07);
- every stop condition, the strict allowlist and the reservation guard, and a leak-free report.
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

COMPLETION = "apifootball_completion"


def _suite(transport=None, *, budget=45, plan="apifootball", quota=None, wrap=None, sleep=None, now=None):
    """Plan logic under a 45 budget (the apifootball plan needs exactly 15 x 3 = 45)."""
    transport = transport or fake.FakeTransport()
    call, _ = fake.tool_caller(transport, quota)
    real = call
    if wrap is not None:
        def call(tool, args):  # noqa: F811 - a tampered server for RED controls
            return wrap(tool, args, real)
    suite = e2e.ApiFootballSuite(call, e2e.ProviderBudget(budget, max(budget, 45)),
                                 plan=plan, now=now or (lambda: fake.NOW), sleep=sleep or (lambda s: None))
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


def test_the_worst_case_per_call_is_the_code_retry_bound_and_the_ceilings_are_the_owner_numbers():
    assert RetryPolicy().max_attempts == e2e.APIFOOTBALL_MAX_ATTEMPTS_PER_CALL == 3
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=4)  # the server cannot be configured above it
    assert e2e.APIFOOTBALL_WORST_CASE_PER_CALL == 3
    assert e2e.APIFOOTBALL_BUDGET_CEILING == 45 and e2e.APIFOOTBALL_DEFAULT_BUDGET == 45
    # 45 was approved for the completion campaign only; the earlier suites keep 20.
    assert e2e.SUITE_BUDGET_CEILING == {"apifootball": 20, "apifootball_full": 20, COMPLETION: 45}
    for bad in (0, 46, -1, True, "45", 45.0):
        with pytest.raises(ValueError):
            e2e.ProviderBudget(bad)
    with pytest.raises(ValueError):
        e2e.ProviderBudget(21, 20)
    with pytest.raises(ValueError):
        e2e.ProviderBudget(46, 46)  # no ceiling above the owner maximum
    assert e2e.ProviderBudget(45).ceiling == 45


def test_the_plans_and_their_call_counts():
    p, f, c = (e2e.plan_summary(n) for n in ("apifootball", "apifootball_full", COMPLETION))
    assert p == {"cases": 16, "provider_calls_nominal": 15, "worst_case_http_attempts": 45,
                 "min_budget_to_complete": 45, "provider_tools_covered": 10}
    assert f == {"cases": 46, "provider_calls_nominal": 45, "worst_case_http_attempts": 135,
                 "min_budget_to_complete": 135, "provider_tools_covered": 40}
    assert c == {"cases": 12, "provider_calls_nominal": 12, "worst_case_http_attempts": 36,
                 "min_budget_to_complete": 36, "provider_tools_covered": 8}
    # Completion: at most 15 provider calls, count x 3 <= 45.
    assert c["provider_calls_nominal"] <= 15 and c["worst_case_http_attempts"] <= e2e.SUITE_BUDGET_CEILING[COMPLETION]
    for name in e2e.APIFOOTBALL_SUITES:
        assert all(x.tool in e2e.APIFOOTBALL_PROVIDER_TOOLS for x in e2e._plan(name))
    assert [x.cid for x in e2e._plan("apifootball")][-1] == "A15"  # the negative id stays last
    assert [x.cid for x in e2e._plan("apifootball_full")][-1] == "A15"
    comp = e2e._plan(COMPLETION)
    assert [x.cid for x in comp] == [f"C{i:02d}" for i in range(1, 13)]
    # Reused evidence, no repeat: no status, no leagues, no finished-fixture detail/events/statistics.
    assert {x.tool for x in comp}.isdisjoint({"apifootball_status", "apifootball_leagues",
                                              "apifootball_fixture_events", "apifootball_fixture_statistics"})
    with pytest.raises(ValueError):
        e2e._plan("health")


# ---- the happy paths through the real tools -------------------------------------------------

def test_prioritized_plan_passes_with_every_call_charged_its_worst_case():
    suite, t = _suite()
    suite.run()
    rep = suite.report()
    assert rep["verdict"] == "PASS" and rep["stop_reason"] is None, rep
    assert len(t.requests) == 15 and rep["provider_requests_charged"] == 45 <= 45
    r = _by_id(suite)
    assert r["N00"]["charged"] == 0 and r["N00"]["settlement"] == "CODE_PATH_NO_REQUEST"
    assert all(r[c]["settlement"] == "WORST_CASE" and r[c]["charged"] == 3 for c in r if c != "N00")
    assert all(x["reserved"] == 3 for x in suite.records)
    assert r["A15"]["outcome"] == "EMPTY_VALID" and r["A15"]["verdict"] == "PASS"
    assert r["A09"]["facts"] == {"fixtures": 1, "selections": 3, "over_published": 2, "under_published": 1}
    assert r["A06"]["facts"]["null_values_kept_null"] == 2
    assert r["A04"]["facts"]["halftime_as_published"] == [1, 1]
    assert r["A11"]["arguments"]["page"] == 2 and r["A11"]["paging"] == {"current": 2, "total": 2}
    assert rep["accounting"] == "WORST_CASE_EVERY_PROVIDER_CALL"
    assert rep["provider_counter"]["use"] == "INFORMATIONAL_ONLY_NEVER_USED_FOR_ACCOUNTING"
    text = json.dumps(rep)
    assert fake.PRICE_SENTINEL not in text and fake.KEY not in text and "PRIVATE" not in text


def test_completion_plan_passes_and_derives_u_bet_and_z_without_repeating_evidence():
    suite, t = _suite(plan=COMPLETION)
    suite.run()
    rep = suite.report()
    assert rep["verdict"] == "PASS" and rep["stop_reason"] is None, [(c["id"], c["verdict"]) for c in rep["cases"]]
    assert rep["suite"] == COMPLETION and rep["budget_ceiling"] == 45
    assert len(t.requests) == 12 and rep["provider_requests_charged"] == 36 <= 45
    assert rep["reused_evidence"] == {"commit": "37e74fd", "league": 39, "season": 2026, "finished_fixture": 1557464}
    paths = [p for p, _ in t.requests]
    assert "/status" not in paths and "/leagues" not in paths
    assert not any(q.get("id") == e2e.EVIDENCE_FINISHED_FIXTURE for _, q in t.requests)
    r = _by_id(suite)
    # Upcoming window: league 39, season 2026, today .. today + 28, UTC; contract parameters only.
    assert r["C01"]["arguments"] == {"league": 39, "season": 2026, "from_date": "2025-11-10",
                                     "to_date": "2025-12-08", "timezone": "UTC"}
    assert t.requests[0] == ("/fixtures", {"league": 39, "season": 2026, "timezone": "UTC",
                                           "from": "2025-11-10", "to": "2025-12-08"})
    assert r["C01"]["facts"]["U"]["fixture"] == 2001 and r["C01"]["facts"]["not_started"] == 3
    assert r["C03"]["facts"]["over_under_bet_ids"] == [5]
    assert r["C04"]["arguments"] == {"fixture": 2001, "bet": 5}
    assert r["C04"]["facts"] == {"fixtures": 1, "selections": 3, "over_published": 2, "under_published": 1}
    assert r["C05"]["arguments"] == {"league": 39, "season": 2026, "bet": 5}
    assert r["C06"]["arguments"]["page"] == 2 and r["C06"]["paging"] == {"current": 2, "total": 2}
    assert r["C07"]["facts"]["Z"] == 9001 and r["C08"]["arguments"] == {"fixture": 9001}
    assert r["C12"]["arguments"] == {"id": e2e.NONEXISTENT_FIXTURE_ID} and r["C12"]["verdict"] == "PASS"
    assert all(x["charged"] == 3 and x["settlement"] == "WORST_CASE" for x in suite.records)
    text = json.dumps(rep)
    assert fake.PRICE_SENTINEL not in text and fake.KEY not in text


def test_completion_without_a_not_started_fixture_leaves_the_u_odds_not_tested():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_fixtures" and "from_date" in args:
            rows = [f for f in p["data"]["response"] if f["fixture"]["status"]["short"] != "NS"]
            p["data"]["response"], p["data"]["results"] = rows, len(rows)
        return p
    suite, t = _suite(plan=COMPLETION, wrap=wrap)
    suite.run()
    r = _by_id(suite)
    assert r["C01"]["verdict"] == "PASS" and "U" not in r["C01"]["facts"] and r["C01"]["facts"]["not_started"] == 0
    assert r["C04"]["outcome"] == "NOT_TESTED" and r["C04"]["verdict"] == "NOT_TESTED"
    assert r["C04"]["reason"] == "NOT_DERIVABLE_FIXTURE_U" and r["C04"]["charged"] == 0
    assert len(t.requests) == 11 and suite.report()["verdict"] == "PARTIAL"


def test_completion_never_guesses_the_over_under_bet():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_bets":
            p["data"]["response"].append({"id": 77, "name": "Goals Over/Under"})  # two exact names
            p["data"]["results"] += 1
        return p
    suite, _ = _suite(plan=COMPLETION, wrap=wrap)
    suite.run()
    r = _by_id(suite)
    assert r["C03"]["facts"]["over_under_bet_ids"] == [5, 77] and "bet_ou" not in suite.ctx
    assert r["C04"]["reason"] == "NOT_DERIVABLE_BET_OU"
    assert "bet" not in r["C05"]["arguments"]  # an optional filter is dropped, never guessed


def test_full_plan_covers_all_40_provider_tools_with_a_raised_ceiling(monkeypatch):
    """What full coverage costs under worst-case counting: 45 calls, 135 of budget."""
    monkeypatch.setattr(e2e, "APIFOOTBALL_BUDGET_CEILING", 135)
    def wrap(tool, args, real):  # a provider plan large enough for 135 attempts
        p = real(tool, args)
        if tool == "apifootball_status":
            p["data"]["requests"] = {"current": 5, "limit_day": 7500}
        return p
    suite, t = _suite(budget=135, plan="apifootball_full", wrap=wrap)
    suite.run()
    rep = suite.report()
    assert rep["verdict"] == "PASS", [(c["id"], c.get("defects"), c.get("reason")) for c in rep["cases"]]
    assert len(t.requests) == 45 and rep["provider_requests_charged"] == 135
    assert {c["tool"] for c in rep["cases"]} == set(e2e.APIFOOTBALL_PROVIDER_TOOLS)


@pytest.mark.parametrize("plan", ["apifootball", "apifootball_full"])
def test_the_earlier_suites_under_their_owner_ceiling_stop_at_the_budget(plan):
    suite, t = _suite(budget=20, plan=plan)
    suite.run()
    rep = suite.report()
    assert rep["stop_reason"] == "STOP_BUDGET_REACHED" and rep["verdict"] == "BLOCKED"
    assert len(t.requests) == 6 and rep["provider_requests_charged"] == 18 <= 20
    assert rep["verdicts"] == {"NOT_TESTED": len(rep["cases"]) - 7, "PASS": 7}


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


@pytest.mark.parametrize("plan", ["apifootball", COMPLETION])
@pytest.mark.parametrize("budget", range(1, 46))
def test_worst_case_charging_bounds_real_attempts_with_retries_for_every_budget(budget, plan):
    suite, t = _suite(RetryTwice(), budget=budget, plan=plan)
    suite.run()
    assert len(t.requests) <= suite.budget.committed <= budget
    assert len(t.requests) == suite.budget.committed  # every attempt charged, nothing more
    assert all(r["reserved"] in (0, 3) for r in suite.records)
    calls = sum(1 for r in suite.records if r["reserved"])
    assert suite.budget.committed <= 3 * calls
    need = e2e.plan_summary(plan)["min_budget_to_complete"]  # 45 (apifootball), 36 (completion)
    if budget >= need:
        assert suite.stop_reason is None and len(t.requests) == need
    else:
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

    # Other consumers, and a counter that jumps back up (eventually consistent).
    t = Chaos(other_consumer=lambda i: rng.choice((0, 0, 0, 1, 4, -3)))
    suite, _ = _suite(t, budget=rng.randint(1, 45), plan=rng.choice(e2e.APIFOOTBALL_SUITES))
    suite.run()
    assert len(t.requests) <= suite.budget.committed <= suite.budget.budget  # the charge bounds the real cost


@pytest.mark.parametrize("pattern", [
    [0, 1, -3],                  # production run 3: 7500 -> 7498 -> 7497 -> 7499
    [0, -2, 2, 0, 0, -5],        # production run 4: 7497 -> 7498 -> 7495 -> ... -> 7496
])
def test_a_non_monotonic_provider_counter_no_longer_stops_the_run(pattern):
    seq = list(pattern)
    t = fake.FakeTransport(day_remaining=7501, other_consumer=lambda i: seq[i - 1] if i <= len(seq) else 0)
    suite, _ = _suite(t, plan=COMPLETION)
    suite.run()
    rep = suite.report()
    assert rep["stop_reason"] is None and rep["verdict"] == "PASS"
    assert rep["provider_counter"]["monotonic_non_increasing"] is False  # recorded, informational
    assert all(r["charged"] == 3 for r in suite.records) and suite.budget.committed == 36
    assert "STOP_QUOTA_ACCOUNTING_ANOMALY" not in json.dumps(rep)


def test_settlement_rules():
    b = e2e.ProviderBudget(45)

    def settle(payload):
        b.committed = 0  # the rules only, not the budget, are under test here
        b.reserve("apifootball_fixtures")
        return b.settle(payload)
    real = {"status": "REAL_API_RESPONSE", "cache_status": "MISS"}
    for status in ("INVALID_INPUT", "KEY_NOT_CONFIGURED", "LIVE_NOT_AUTHORIZED", "PROVIDER_CLIENT_UNAVAILABLE"):
        assert settle({"status": status}) == (0, "CODE_PATH_NO_REQUEST")
    assert settle({**real, "cache_status": "HIT", "quota_remaining": {"day": 1}}) == (0, "CACHE_HIT")
    assert b.day_readings == []  # a HIT replays an old counter: never a reading
    assert settle(None) == (3, "WORST_CASE_NO_REPLY")
    # Any counter value, rising, falling or missing: the full worst case, always.
    for day in (100, 99, 97, 97, 500, 0, None):
        assert settle({**real, "quota_remaining": {"day": day}}) == (3, "WORST_CASE")
    assert b.day_readings == [100, 99, 97, 97, 500, 0]
    for p in ({"status": "PAYLOAD_TOO_LARGE"}, {"status": "PROVIDER_ERROR", "error": {"code": "X"}},
              {"status": "PARTIAL_ODDS_MANIFEST", "cache_status": "MISS"}, {"status": "SOMETHING_NEW"}, {}):
        assert settle(p) == (3, "WORST_CASE")


def test_a_reservation_that_would_exceed_the_budget_is_refused():
    b = e2e.ProviderBudget(5)
    b.reserve("apifootball_status")
    with pytest.raises(e2e.BudgetExceeded):
        b.reserve("apifootball_status")  # one open reservation at a time
    b.settle(None)
    assert b.committed == 3 and not b.can_reserve()  # 3 + 3 > 5: refused, fail-closed
    with pytest.raises(e2e.BudgetExceeded):
        b.reserve("apifootball_leagues")
    with pytest.raises(RuntimeError):
        b.settle(None)
    b = e2e.ProviderBudget(6)
    b.reserve("x")
    b.settle({"status": "REAL_API_RESPONSE"})
    assert b.can_reserve()  # 3 + 3 == 6: allowed, the boundary is inclusive


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
    assert rest and all(r["outcome"] == "NOT_TESTED" and r["verdict"] == "NOT_TESTED" and r["reason"] == reason
                        and r["reserved"] == 0 for r in rest)
    assert len(t.requests) == requests


@pytest.mark.parametrize("behaviour,reason,requests,verdict", [
    (["http_401"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 2, "NOT_TESTED"),
    (["http_403"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 2, "NOT_TESTED"),
    (["http_429"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 2, "NOT_TESTED"),
    (["http_503", "http_503", "http_503"], "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 4, "NOT_TESTED"),
    # Declared provider errors reach the runner only as a fixed code without their text: no
    # evidence of a plan restriction can be read, so it stays FAIL (never PROVIDER_UNAVAILABLE).
    (["declared_errors"], "STOP_UNEXPECTED_PROVIDER_ERROR", 2, "FAIL"),
    (["minute_zero"], "STOP_PROVIDER_QUOTA_ZERO", 2, "PASS"),
])
def test_provider_failures_stop_the_run(behaviour, reason, requests, verdict):
    suite, t = _suite(fake.FakeTransport(by_path={"/leagues": behaviour}))
    suite.run()
    _assert_stopped_at(suite, t, "A02", reason, requests)
    assert _by_id(suite)["A02"]["verdict"] == verdict
    assert _by_id(suite)["A02"]["charged"] == 3


def test_daily_quota_zero_stops_the_run():
    suite, t = _suite(fake.FakeTransport(day_remaining=2))
    suite.run()
    _assert_stopped_at(suite, t, "A02", "STOP_PROVIDER_QUOTA_ZERO", 2)


def test_an_open_circuit_stops_the_run():
    suite, t = _suite()
    suite.real_call.client.breaker._open_until = float("inf")
    suite.run()
    _assert_stopped_at(suite, t, "A01", "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT", 0)
    assert _by_id(suite)["A01"]["charged"] == 3  # no proof that no request was made


@pytest.mark.parametrize("error,reason,verdict", [
    (RuntimeError("db down"), "STOP_COORDINATOR_UNAVAILABLE", "FAIL"),  # the MCP's own infrastructure
    # FIX-08 (declared behaviour change): LOCAL_QUOTA_EXHAUSTED is split in two codes. A refusal
    # without a known window maps to the conservative daily stop.
    (QuotaExceeded("local"), "STOP_LOCAL_DAILY_QUOTA_EXHAUSTED", "NOT_TESTED"),
    (QuotaExceeded("local", window="day", retry_after_seconds=3600,
                   reset_at_utc="2026-11-11T00:00:00+00:00"), "STOP_LOCAL_DAILY_QUOTA_EXHAUSTED", "NOT_TESTED"),
    (QuotaExceeded("local", window="minute", retry_after_seconds=17), "STOP_LOCAL_RATE_LIMITED", "NOT_TESTED"),
])
def test_coordinator_and_local_quota_failures_stop_before_any_request(error, reason, verdict):
    suite, t = _suite(quota=fake.FakeQuota(error=error))
    suite.run()
    _assert_stopped_at(suite, t, "A01", reason, 0)
    assert _by_id(suite)["A01"]["verdict"] == verdict


def test_a_failed_mcp_call_is_a_fail_with_the_worst_case_charged():
    suite, t = _suite(wrap=lambda tool, args, real: None if tool == "apifootball_leagues" else real(tool, args))
    suite.run()
    _assert_stopped_at(suite, t, "A02", "STOP_MCP_CALL_FAILED", 1)
    r = _by_id(suite)["A02"]
    assert r["charged"] == 3 and r["settlement"] == "WORST_CASE_NO_REPLY" and r["verdict"] == "FAIL"


def test_a_server_without_live_authorization_stops_at_no_cost():
    def wrap(tool, args, real):
        return {"status": "LIVE_NOT_AUTHORIZED", "network_calls_made": 0} if tool == "apifootball_status" else real(tool, args)
    suite, t = _suite(wrap=wrap)
    suite.run()
    _assert_stopped_at(suite, t, "A01", "STOP_PROVIDER_NOT_ENABLED", 0)
    assert suite.budget.committed == 0 and _by_id(suite)["A01"]["verdict"] == "NOT_TESTED"


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
    assert r["A05"]["charged"] == 3 and r["A06"]["charged"] == 3
    assert r["A15"]["verdict"] == "PASS" and suite.stop_reason is None
    assert len(t.requests) == 15 and suite.budget.committed == 45


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


@pytest.mark.parametrize("plan,live,odds", [("apifootball", "A12", "A13"), (COMPLETION, "C07", "C08")])
def test_no_live_fixture_is_empty_valid_partial_and_live_odds_not_tested(plan, live, odds):
    def wrap(tool, args, real):
        p = real(tool, args)
        if args.get("live") == "all":
            p["data"]["response"], p["data"]["results"] = [], 0
        return p
    suite, t = _suite(wrap=wrap, plan=plan)
    suite.run()
    r = _by_id(suite)
    # Empty alone is no evidence of unavailability: PARTIAL, never PROVIDER_UNAVAILABLE.
    assert r[live]["outcome"] == "EMPTY_VALID" and r[live]["verdict"] == "PARTIAL"
    assert r[odds]["outcome"] == "NOT_TESTED" and r[odds]["verdict"] == "NOT_TESTED"
    assert r[odds]["reason"] == "NO_LIVE_FIXTURE"
    assert suite.report()["verdict"] == "PARTIAL" and suite.stop_reason is None


def test_single_odds_page_skips_page_two_without_a_call():
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_odds" and "league" in args:
            p["data"]["paging"] = p["pagination"] = {"current": 1, "total": 1}
        return p
    for plan, cid, n in (("apifootball", "A11", 14), (COMPLETION, "C06", 11)):
        suite, t = _suite(wrap=wrap, plan=plan)
        suite.run()
        assert _by_id(suite)[cid]["reason"] == "NOT_APPLICABLE_SINGLE_PAGE" and len(t.requests) == n


# ---- PROVIDER_UNAVAILABLE needs explicit evidence -----------------------------------------------

def _empty_odds_for_u(tool, args, real):
    p = real(tool, args)
    if tool == "apifootball_odds" and "fixture" in args:
        p["data"]["response"], p["data"]["results"] = [], 0
    return p


def test_provider_unavailable_only_with_a_false_coverage_flag_recorded_in_the_facts():
    def wrap(tool, args, real):
        p = _empty_odds_for_u(tool, args, real)
        if tool == "apifootball_leagues":
            p["data"]["response"][0]["seasons"][1]["coverage"]["odds"] = False
        return p
    suite, _ = _suite(wrap=wrap)
    suite.run()
    r = _by_id(suite)["A09"]
    assert r["outcome"] == "EMPTY_VALID" and r["verdict"] == "PROVIDER_UNAVAILABLE"
    assert r["facts"]["unavailable_evidence"] == {"source": "/leagues coverage", "flag": "odds", "value": False}
    # The same flag never hides data that is present, nor a defect.
    assert _by_id(suite)["A10"]["verdict"] == "PASS"


@pytest.mark.parametrize("plan,cid", [("apifootball", "A09"), (COMPLETION, "C04")])
def test_an_empty_reply_alone_is_never_provider_unavailable(plan, cid):
    suite, _ = _suite(wrap=_empty_odds_for_u, plan=plan)  # coverage true (apifootball) or unknown (completion)
    suite.run()
    r = _by_id(suite)[cid]
    assert r["outcome"] == "EMPTY_VALID" and r["verdict"] == "PARTIAL"
    assert "unavailable_evidence" not in (r.get("facts") or {})
    assert "PROVIDER_UNAVAILABLE" not in suite.report()["verdicts"]


def test_a_false_coverage_flag_never_masks_an_mcp_defect():
    def wrap(tool, args, real):
        p = _empty_odds_for_u(tool, args, real)
        if tool == "apifootball_leagues":
            p["data"]["response"][0]["seasons"][1]["coverage"]["odds"] = False
        if tool == "apifootball_odds" and "fixture" in args:
            p["data"]["results"] = 3  # the envelope no longer matches its rows
        return p
    suite, _ = _suite(wrap=wrap)
    suite.run()
    r = _by_id(suite)["A09"]
    assert r["verdict"] == "FAIL" and "RESULTS_NOT_EQUAL_LEN_RESPONSE" in r["defects"]


def test_case_verdicts_are_the_five_owner_values():
    assert e2e.CASE_VERDICTS == ("PASS", "FAIL", "PARTIAL", "NOT_TESTED", "PROVIDER_UNAVAILABLE")
    suite, _ = _suite(fake.FakeTransport(by_path={"/fixtures/events": ["too_large"]}))
    suite.run()
    assert {r["verdict"] for r in suite.records} <= set(e2e.CASE_VERDICTS)


# ---- bookmakers catalog (production run 4, A07: CATALOG_SHAPE_INVALID with empty facts) ---------

@pytest.mark.parametrize("plan,cid", [("apifootball", "A07"), (COMPLETION, "C02")])
def test_bookmakers_with_a_null_published_name_pass_and_keep_the_null(plan, cid):
    """Regression: the real /odds/bookmakers publishes a row whose name is null (the fake does
    too, like run 41242779991). The old check demanded a string name: CATALOG_SHAPE_INVALID."""
    suite, _ = _suite(plan=plan)
    suite.run()
    r = _by_id(suite)[cid]
    assert r["verdict"] == "PASS" and r["defects"] == []
    assert r["facts"] == {"entries": 3, "paging_total": 1, "null_names_kept_null": 1, "rows_shape_invalid": 0}


@pytest.mark.parametrize("row", [{"id": "14", "name": "x"}, {"id": None, "name": "x"}, {"id": 3, "name": 7}, "row"])
def test_a_really_malformed_catalog_row_still_fails_with_its_counts(row):
    def wrap(tool, args, real):
        p = real(tool, args)
        if tool == "apifootball_bookmakers":
            p["data"]["response"].append(row)
            p["data"]["results"] += 1
        return p
    suite, _ = _suite(wrap=wrap)
    suite.run()
    r = _by_id(suite)["A07"]
    assert r["verdict"] == "FAIL" and r["defects"] == ["CATALOG_SHAPE_INVALID"]
    assert r["facts"]["rows_shape_invalid"] == 1 and r["facts"]["entries"] == 4  # facts never empty


# ---- classification and structure -------------------------------------------------------------

def test_oversized_odds_become_an_explicit_price_free_manifest():
    suite, t = _suite(fake.FakeTransport(huge_odds=True))
    suite.run()
    r = _by_id(suite)["A10"]
    assert r["tool_status"] == "PARTIAL_ODDS_MANIFEST" and r["outcome"] == "PARTIAL" and r["verdict"] == "PARTIAL"
    assert r["facts"]["manifest"] is True and r["paging"] == {"current": 1, "total": 2}
    assert _by_id(suite)["A11"]["verdict"] == "PASS"  # page 2 still reached from the manifest paging
    assert suite.report()["verdict"] == "PARTIAL" and fake.PRICE_SENTINEL not in json.dumps(suite.report())


def test_oversized_league_odds_in_the_completion_plan_are_an_explicit_manifest():
    suite, _ = _suite(fake.FakeTransport(huge_odds=True), plan=COMPLETION)
    suite.run()
    r = _by_id(suite)
    assert r["C05"]["tool_status"] == "PARTIAL_ODDS_MANIFEST" and r["C05"]["verdict"] == "PARTIAL"
    assert r["C05"]["facts"]["manifest"] is True and r["C06"]["verdict"] == "PASS"
    assert fake.PRICE_SENTINEL not in json.dumps(suite.report())


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
        # FIX-06 (declared change): the negative zone is now one outside the IANA catalog.
        if args.get("timezone") == e2e.NEGATIVE_TIMEZONE:
            return real(tool, {**args, "timezone": "UTC"})  # a server that ignores the policy
        return real(tool, args)
    suite, _ = _suite(wrap=wrap)
    suite.run()
    assert _by_id(suite)["N00"]["verdict"] == "FAIL"


# ---- allowlist and reservation guard on every path --------------------------------------------

def _run(suite="apifootball", budget=20):
    return e2e.Run(client=None, base=e2e.PINNED_BASE, password="LOCAL-ONLY-pw-31f9", expected_commit=None,
                   mask=lambda v: None, suite=suite, apifootball_budget=budget)


def test_each_suite_gets_its_own_owner_ceiling():
    assert _run(COMPLETION, 45).af_budget.ceiling == 45
    with pytest.raises(ValueError):
        _run("apifootball", 21)
    with pytest.raises(ValueError):
        _run("apifootball_full", 45)
    with pytest.raises(ValueError):
        _run(COMPLETION, 46)


def test_the_opt_in_suite_refuses_anything_outside_the_read_only_provider_tools():
    run = _run()
    for name in ("sxbet_balance", "sxbet_place_order", "browser_create", "apifootball_quota_report", None):
        with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
            run.rpc("tools/call", {"name": name, "arguments": {}}, "t")
        with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
            run.call_tool_payload(name, {}, "t")
    assert run.tools_called == []


def test_a_provider_tool_needs_its_own_open_reservation():
    run = _run(COMPLETION, 45)
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
    run = _run(COMPLETION, 45)
    suite, _ = _suite(plan=COMPLETION)
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
