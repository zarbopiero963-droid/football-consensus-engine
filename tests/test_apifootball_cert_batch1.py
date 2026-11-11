"""AF-05 first real batch: plan bounds and the persistent, write-ahead campaign budget (offline only).

Owner conditions (2026-11-11): at most 78 attempts per campaign, retries/timeouts/ambiguous replies
included; the counter is persistent and shared (a restart or re-run with the same campaign_id never
gets a new budget); no secret in logs or artefacts. Provider = httpx.MockTransport, ledger = an
in-memory store that outlives every runner instance (as the database would).
"""
from __future__ import annotations

import asyncio
import re
import json

import httpx
import pytest

from apifootball_campaign_kit import (KEY, FakeLedger, Provider, Quota, Store, envelope, load,
                                            settings)

batch = load("apifootball_cert_batch1_t", "apifootball_cert_batch1.py")
PLAN_TEXT = batch.render_plan(batch.build_plan())
PLAN = json.loads(PLAN_TEXT)


@pytest.fixture(autouse=True)
def _owner_authorized(monkeypatch):
    """The runner sends nothing without the owner authorization variable (tested separately)."""
    monkeypatch.setenv(batch.AUTH_VAR, "true")


def _run(store, provider, campaign="cert-batch1-test-0001"):
    return asyncio.run(batch.execute(PLAN, campaign_id=campaign, settings=settings(), ledger=FakeLedger(store),
                                     inner_transport=provider.transport(), quota=Quota(), plan_text=PLAN_TEXT,
                                     source_sha="f666cc1"))


def test_plan_is_committed_deterministic_and_within_the_hard_cap():
    assert batch.main(["--check-plan"]) == 0
    assert batch.HARD_CAP == 78
    assert PLAN["max_attempts_total"] <= 78 and all(c["max_attempts"] == 1 for c in PLAN["calls"])
    assert PLAN["status"] == "PREPARED_NOT_AUTHORIZED"
    assert batch.render_plan(batch.build_plan()) == PLAN_TEXT


def test_every_plan_call_passes_the_contract_and_covers_existing_rows():
    from bridge.api_football.contracts import validate
    for call in PLAN["calls"]:
        validate(call["tool"], batch._contract_args(call["arguments"]))
        assert call["covers_rows"] or "AF-02_SEARCH_MIN_LENGTH_PROBE" in call["tags"]


def test_full_run_never_exceeds_78_and_counts_before_sending():
    store, provider = Store(), Provider()
    run = _run(store, provider)
    assert len(provider.sent) == len(store.attempts) <= 78
    assert run["ledger_attempts_used"] == len(provider.sent)


def test_restart_mid_campaign_does_not_get_a_new_budget():
    store = Store()

    class Crash(BaseException):
        pass

    count = {"n": 0}

    def crashing(request):
        count["n"] += 1
        if count["n"] == 30:
            raise Crash()  # the worker dies while the 30th request is in flight
        return httpx.Response(200, json=envelope(request.url.path, [{"fixture": {"id": 101}}]))

    with pytest.raises(Crash):
        _run(store, Provider(default=crashing))
    assert len(store.attempts) == 30  # the in-flight attempt was reserved before the send
    second = Provider()
    run = _run(store, second)
    assert len(store.attempts) <= 78
    assert len(second.sent) + 30 <= 78
    attempted = {c for _, _, c in store.attempts}
    assert len(attempted) == len(store.attempts)  # no call attempted twice
    redone = [c for c in run["calls"] if c["outcome"] == "ALREADY_ATTEMPTED_IN_CAMPAIGN_NOT_RETRIED"]
    assert len(redone) == 30


def test_rerun_with_same_campaign_id_makes_zero_requests():
    store = Store()
    _run(store, Provider())
    used = len(store.attempts)
    again = Provider()
    _run(store, again)
    assert again.sent == [] and len(store.attempts) == used


def test_timeouts_and_ambiguous_replies_are_counted():
    store = Store()
    count = {"n": 0}

    def flaky(request):
        count["n"] += 1
        if count["n"] % 3 == 0:
            raise httpx.ReadTimeout("t", request=request)
        if count["n"] % 3 == 1:
            return httpx.Response(200, content=b'{"truncated": ')  # ambiguous: did it count upstream?
        return httpx.Response(200, json=envelope(request.url.path, [{"fixture": {"id": 101}}]))

    provider = Provider(default=flaky)
    _run(store, provider)
    assert len(provider.sent) == len(store.attempts) <= 78


def test_retries_are_counted_one_by_one_by_the_transport():
    """Even with a retrying client, every attempt is reserved before it is sent."""
    from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
    from bridge.api_football.transport import InjectedHTTPXTransport

    store = Store()
    ledger = FakeLedger(store)
    asyncio.run(ledger.open("cert-retry-test-01", 2, "sha"))
    provider = Provider(default=lambda request: httpx.Response(503))
    http = httpx.AsyncClient(base_url=settings().base_url,
                             transport=batch.budget_transport(provider.transport(), ledger, "cert-retry-test-01"))
    client = ResilientFootballClient(settings(), InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     retry=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0))
    with pytest.raises(Exception):
        asyncio.run(client.get("/countries", {}))
    assert len(store.attempts) == 2 and len(provider.sent) == 2  # 3rd attempt refused before sending


def test_a_campaign_with_a_different_plan_is_refused():
    store = Store()
    asyncio.run(FakeLedger(store).open("cert-batch1-test-0001", 78, "other-plan"))
    with pytest.raises(RuntimeError):
        _run(store, Provider())


def test_postgres_ledger_fails_closed_without_the_owner_migration():
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            self.executed = a

        def fetchone(self):
            return {"t": None}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cursor()

    ledger = batch.PostgresCampaignLedger(connect=lambda: Conn())
    with pytest.raises(batch.LedgerUnavailable) as exc:
        asyncio.run(ledger.open("cert-batch1-test-0001", 78, "a" * 64))  # a well-formed plan sha (AF-15)
    assert str(exc.value) == "CAMPAIGN_LEDGER_MIGRATION_REQUIRED"


def test_artifacts_of_a_simulated_run_hold_no_secret(tmp_path, monkeypatch):
    dsn = "postgresql://fcb_user:S3cretPassw0rd@db.example.invalid:5432/fcb?sslmode=verify-full"
    monkeypatch.setenv("DATABASE_URL", dsn)
    run = _run(Store(), Provider())
    paths = batch.write_artifacts(run, tmp_path)
    assert len(paths) > 1
    assert batch.scan_for_secrets(paths, [KEY, dsn, "S3cretPassw0rd"]) == []
    planted = tmp_path / "planted.json"
    planted.write_text(json.dumps({"x": dsn}))
    assert batch.scan_for_secrets([planted], [KEY, dsn]) == ["planted.json"]


def test_execute_refuses_without_authorization(capsys):
    assert batch.main(["--execute"]) == 2
    assert "LIVE_CAMPAIGN_NOT_AUTHORIZED" in capsys.readouterr().out


def test_plan_exclusion_is_a_distinct_verdict():
    out = {"status": "PROVIDER_ERROR", "error": {"code": "PROVIDER_DECLARED_ERRORS", "category": "PLAN_NOT_INCLUDED"}}
    assert batch.classify(out) == "PLAN_EXCLUDED"
    assert batch.classify({"status": "PROVIDER_ERROR", "error": {"code": "PROVIDER_SERVER_ERROR"}}) == "ERROR"
    assert batch.classify({"status": "REAL_API_RESPONSE", "data": {"results": 0, "response": []}}) == "EMPTY_VALID"
    assert batch.classify({"status": "PARTIAL_ID_MANIFEST"}) == "PARTIAL"


def test_workflow_is_manual_owner_gated_and_masks_secrets():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "api-football-cert-campaign.yml").read_text()
    assert "workflow_dispatch:" in text and "pull_request" not in text and "schedule:" not in text
    assert "environment: api-football-live" in text
    assert "require_owner_main_dispatch.sh" in text and "require_protected_environment.sh" in text
    assert "::add-mask::" in text
    assert "--authorize-live-campaign" in text


def test_without_owner_authorization_nothing_is_reserved_or_sent(monkeypatch):
    monkeypatch.delenv(batch.AUTH_VAR, raising=False)
    store, provider = Store(), Provider()
    _run(store, provider)
    assert provider.sent == [] and store.attempts == []


# ---- AF-07 extra: a RESERVED attempt is not a SENT request (owner decision 2026-11-11) ----------

class _Crash(BaseException):
    """Stands for the process dying (kill, OOM, runner lost): nothing after it runs."""


class _CrashingLedger(FakeLedger):
    async def record_outcome(self, campaign_id, seq, outcome, http_status):
        raise _Crash()  # dies AFTER the provider answered, BEFORE the outcome is written


def _crash_before_send(request):
    raise _Crash()  # dies after the reservation, before anything leaves


@pytest.mark.parametrize("where", ["between_reservation_and_send", "between_send_and_outcome"])
def test_a_crash_leaves_reserved_unknown_counted_and_never_retried(where):
    store, provider = Store(), Provider()
    if where == "between_reservation_and_send":
        ledger, inner = FakeLedger(store), httpx.MockTransport(_crash_before_send)
    else:
        ledger, inner = _CrashingLedger(store), provider.transport()
    with pytest.raises(_Crash):
        asyncio.run(batch.execute(PLAN, campaign_id="cert-crash-test-01", settings=settings(), ledger=ledger,
                                  inner_transport=inner, quota=Quota(), plan_text=PLAN_TEXT, source_sha="x"))
    # before the send: nothing left; after the send: the provider DID get it, still no outcome
    assert len(provider.sent) == (0 if where == "between_reservation_and_send" else 1)
    first = PLAN["calls"][0]["call_id"]
    states = asyncio.run(FakeLedger(store).attempt_states("cert-crash-test-01"))
    assert states == [{"seq": 1, "call_id": first, "state": "RESERVED_UNKNOWN", "http_status": None}]
    # Re-run with the same campaign_id: the reserved number stays consumed, the call is not retried
    # and the evidence carries the explicit state.
    again = Provider()
    run = asyncio.run(batch.execute(PLAN, campaign_id="cert-crash-test-01", settings=settings(),
                                    ledger=FakeLedger(store), inner_transport=again.transport(), quota=Quota(),
                                    plan_text=PLAN_TEXT, source_sha="x"))
    entry = next(c for c in run["calls"] if c["call_id"] == first)
    assert entry["outcome"] == "ALREADY_ATTEMPTED_IN_CAMPAIGN_NOT_RETRIED" and entry["ledger_state"] == "RESERVED_UNKNOWN"
    assert run["ledger_attempts_used"] == 1 + len(again.sent) <= 78
    assert run["ledger_attempt_states"]["RESERVED_UNKNOWN"] == 1
    assert run["ledger_attempts"][0] == {"seq": 1, "call_id": first, "state": "RESERVED_UNKNOWN", "http_status": None}


def test_each_answered_attempt_records_its_outcome_and_transport_errors_are_explicit():
    calls = iter(range(10_000))

    def flaky(request):
        if next(calls) == 0:
            raise httpx.ConnectTimeout("boom", request=request)  # the first send fails ambiguously
        return Provider().default(request)
    store = Store()
    run = asyncio.run(batch.execute(PLAN, campaign_id="cert-outcomes-001", settings=settings(),
                                    ledger=FakeLedger(store), inner_transport=httpx.MockTransport(flaky),
                                    quota=Quota(), plan_text=PLAN_TEXT, source_sha="x"))
    counts = run["ledger_attempt_states"]
    assert counts["RESERVED_UNKNOWN"] == 0 and counts["TRANSPORT_ERROR"] == 1
    assert sum(counts.values()) == run["ledger_attempts_used"]
    assert run["ledger_attempts"][0]["state"] == "TRANSPORT_ERROR" and run["ledger_attempts"][0]["http_status"] is None
    assert all(s["http_status"] == 200 for s in run["ledger_attempts"][1:])


# ---- review #199 P3-4: a call refused BEFORE the send is NOT_TESTED, never ERROR ---------------

class _RefusingLedger(FakeLedger):
    def __init__(self, store, exc):
        super().__init__(store)
        self.exc = exc

    async def reserve(self, campaign_id, call_id):
        raise self.exc


@pytest.mark.parametrize("cause,code", [
    ("no_owner_authorization", "CAMPAIGN_NOT_AUTHORIZED_BY_OWNER"),
    ("ledger_unavailable", "CAMPAIGN_LEDGER_UNAVAILABLE"),
    ("budget_exhausted", "CAMPAIGN_BUDGET_EXHAUSTED"),
])
def test_a_call_refused_before_the_send_is_not_tested_with_the_refusal_code(monkeypatch, cause, code):
    store, provider = Store(), Provider()
    ledger = FakeLedger(store)
    if cause == "no_owner_authorization":
        monkeypatch.delenv(batch.AUTH_VAR, raising=False)
    elif cause == "ledger_unavailable":
        ledger = _RefusingLedger(store, batch.LedgerUnavailable(code))
    else:
        ledger = _RefusingLedger(store, batch.BudgetExhausted(code))
    run = asyncio.run(batch.execute(PLAN, campaign_id="cert-refused-0001", settings=settings(), ledger=ledger,
                                    inner_transport=provider.transport(), quota=Quota(), plan_text=PLAN_TEXT,
                                    source_sha="x"))
    assert provider.sent == [] and store.attempts == []
    attempted = [c for c in run["calls"] if c["outcome"] != "BINDING_UNRESOLVED"]
    assert attempted and all(c["verdict"] == "NOT_TESTED" for c in run["calls"])
    assert all(c["outcome"] == "REFUSED_BEFORE_SEND" for c in attempted)
    # the first call carries the transport refusal; later ones may be stopped by the client first
    # (e.g. CIRCUIT_OPEN): still nothing sent, still NOT_TESTED with a fixed code
    assert attempted[0]["refusal_code"] == code
    assert all(re.fullmatch(r"[A-Z0-9_]+", c["refusal_code"]) for c in attempted)
    assert not any(c["verdict"] == "ERROR" for c in run["calls"])


def test_answered_calls_are_attempted_not_refused():
    run = _run(Store(), Provider())
    assert {c["outcome"] for c in run["calls"]} <= {"ATTEMPTED", "BINDING_UNRESOLVED"}
    assert not any("refusal_code" in c for c in run["calls"])


def test_snapshot_refused_before_the_send_stops_with_the_code_and_sends_nothing(monkeypatch):
    monkeypatch.delenv(batch.AUTH_VAR, raising=False)
    store, provider = Store(), Provider()
    report = asyncio.run(batch.execute_snapshot(snapshot_id="snap-refused-001", fixture_id=None, settings=settings(),
                                                ledger=FakeLedger(store), inner_transport=provider.transport(),
                                                quota=Quota()))
    assert provider.sent == [] and store.attempts == []
    assert report["incomplete_reasons"] == ["STOPPED:REFUSED_BEFORE_SEND:CAMPAIGN_NOT_AUTHORIZED_BY_OWNER"]


# ---- AF-15: the budget belongs to the owner-approved PLAN, shared by all its campaign_ids --------

def test_a_new_campaign_id_of_the_same_plan_gets_no_new_budget():
    store = Store()
    store.plans[batch.plan_sha256(PLAN_TEXT)] = 10  # approved plan budget
    first, second = Provider(), Provider()
    run1 = _run(store, first, campaign="cert-plan-shared-01")
    run2 = _run(store, second, campaign="cert-plan-shared-02")
    assert len(first.sent) == 10 and second.sent == []
    assert run2["plan_budget"] == {"plan_sha256": batch.plan_sha256(PLAN_TEXT), "budget": 10, "used": 10}
    assert all(c["verdict"] == "NOT_TESTED" for c in run2["calls"]) and run1["plan_budget"]["used"] == 10


def test_an_unapproved_plan_stops_the_runner_before_any_request():
    from apifootball_campaign_kit import PlanNotApproved
    store, provider = Store(auto_approve=False), Provider()
    with pytest.raises(PlanNotApproved, match="PLAN_NOT_APPROVED"):
        _run(store, provider)
    assert provider.sent == [] and store.attempts == [] and store.campaigns == {}


def test_snapshot_plan_is_fixed_committed_and_shared_by_every_snapshot_id():
    text = batch.render_snapshot_plan()
    assert batch.SNAPSHOT_PLAN_PATH.read_text(encoding="utf-8") == text
    assert "fixture" not in json.loads(text) and json.loads(text)["cap"] == batch.SNAPSHOT_CAP == 8
    store = Store()
    store.plans[batch.plan_sha256(text)] = 8
    sent = []
    for sid in ("snap-shared-0001", "snap-shared-0002"):
        provider = Provider()
        asyncio.run(batch.execute_snapshot(snapshot_id=sid, fixture_id=101, settings=settings(),
                                           ledger=FakeLedger(store), inner_transport=provider.transport(),
                                           quota=Quota()))
        sent.append(len(provider.sent))
    assert sum(sent) <= 8 and {c["plan_sha"] for c in store.campaigns.values()} == {batch.plan_sha256(text)}


def test_plan_approval_request_prints_the_committed_plan_sha_budget_and_kind(capsys):
    import hashlib
    assert batch.main(["--plan-approval-request", "batch1"]) == 0
    sha, budget, kind = capsys.readouterr().out.split()
    assert sha == hashlib.sha256(batch.PLAN_PATH.read_bytes()).hexdigest() and budget == "78" and kind == "BATCH1"
    assert batch.main(["--plan-approval-request", "live_full_match_snapshot"]) == 0
    sha, budget, kind = capsys.readouterr().out.split()
    assert sha == hashlib.sha256(batch.SNAPSHOT_PLAN_PATH.read_bytes()).hexdigest()
    assert (budget, kind) == ("8", "LIVE_FULL_MATCH_SNAPSHOT")


def test_execute_refuses_when_the_committed_plan_file_differs_from_the_code(monkeypatch, tmp_path, capsys):
    """INVARIANT 1 (runner side): the sha that is looked up among approved plans is the committed
    file's; a file that is not exactly what the code renders stops everything before any request."""
    edited = tmp_path / "batch1_plan.json"
    edited.write_text(PLAN_TEXT.replace("PREPARED_NOT_AUTHORIZED", "EDITED"), encoding="utf-8")
    monkeypatch.setattr(batch, "PLAN_PATH", edited)
    assert batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "cert-edited-0001",
                       "--out", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().out.strip() == "PLAN_OUT_OF_DATE"
    assert batch.main(["--plan-approval-request", "batch1"]) == 2  # nothing to approve either
