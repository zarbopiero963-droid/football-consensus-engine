"""S7 (issue #187): MCP order-request tools -> signer queue -> isolated signer -> evidence-based status.

Offline only: an in-memory queue with the PostgreSQL semantics (tests/sxbet_queue_kit.py), the simulated exchange
and throwaway keys. Nothing reaches SX Bet, mainnet or testnet. The bridge never imports signer/ (the signer is
imported here by the TEST to run the other side of the queue).
"""
import asyncio
import inspect
import json
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from bridge.sxbet import order_requests as oreq  # noqa: E402
from bridge.sxbet.client import Settings, SXBetError  # noqa: E402
from bridge.sxbet.order_request_tools import TOOL_NAMES  # noqa: E402
from bridge.sxbet.tools import ORDER_REQUEST_TOOLS, register_sxbet_tools  # noqa: E402
from signer import request_queue as srq  # noqa: E402
from signer.intent import INTENT_KEYS, validate as signer_validate_intent  # noqa: E402
from signer.service import SignerService  # noqa: E402
from signer import rest_adapter as ra  # noqa: E402
from sxbet_e2e_kit import SIGNER_ID, USDC, Stack  # noqa: E402
from sxbet_queue_kit import MemoryQueue  # noqa: E402
from sxbet_signer_kit import H, META_RAW, command_text  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
P = 10 ** 18


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            assert fn.__name__ not in self.tools
            self.tools[fn.__name__] = fn
            return fn
        return attach


class SimClient:
    """The bridge's read client, answered by the simulated exchange (GET only)."""

    def __init__(self, sim):
        self.sim = sim
        self.calls = []

    async def get(self, path, params, *, auth):
        self.calls.append(path)
        if path == "/metadata/obv3":                     # the simulated exchange serves no metadata route
            return {"data": json.loads(json.dumps(META_RAW)), "http_status": 200, "rate_limit_remaining": None}
        resp = self.sim.send(ra.RestRequest("GET", path, tuple((k, str(v)) for k, v in params.items()), None))
        if resp.http_status != 200:
            raise SXBetError("SXBET_NOT_FOUND" if resp.http_status == 404 else "SXBET_HTTP_ERROR",
                             http_status=resp.http_status)
        return {"data": resp.body["data"], "http_status": 200, "rate_limit_remaining": None}


class MemoryWriter:
    """MCP-role view of the in-memory queue: insert a request, read requests and events. Nothing else."""

    def __init__(self, q):
        self.q = q
        self.fail_next = None

    def submit(self, payload):
        if self.fail_next == "before":
            self.fail_next = None
            raise ConnectionError("dsn=postgres://secret")
        rid = payload["request_id"]
        stored = self.request(rid)
        if stored is not None:
            return "EXISTS", stored
        self.q.insert(payload)
        if self.fail_next == "after_commit":
            self.fail_next = None
            raise ConnectionError("dropped after commit")
        return "INSERTED", None

    def request(self, rid):
        for r in self.q.requests:
            if r[0] == rid:
                return json.loads(json.dumps(r[2]))
        return None

    def events(self, rid):
        return self.q.events_of(rid)


def ors_for(s):
    return oreq.OrderRequestSettings(enabled=True, queue_dsn="postgresql://unused", maker=s.maker)


def tools(s, q, *, api_key="test-key", ors=None):
    reg = _Registry()
    writer = MemoryWriter(q)
    register_sxbet_tools(reg, Settings(enabled=True, network="mainnet", api_key=api_key), client=SimClient(s.sim),
                         order_requests=ors or ors_for(s), order_request_writer=writer)
    return reg.tools, writer


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def s(tmp_path):
    return Stack(tmp_path)


@pytest.fixture(autouse=True)
def _clock(monkeypatch, s):
    import bridge.sxbet.order_request_tools as ort
    monkeypatch.setattr(ort, "_wallclock", s.clock)


def owner_text(s, key, *, stake=5 * USDC, odds=40 * P, tif="GTC", outcome_one=True):
    i = s.intent(key, stake=stake, odds=odds, tif=tif, outcome_one=outcome_one)
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx      # what the signer's OWN reads will see (test provider)
    m = ctx["market"]
    label = m["outcomeOneName" if outcome_one else "outcomeTwoName"]
    return command_text(i, label=label, teams=(m["teamOneName"], m["teamTwoName"])), i


def place(t, s, key, text, *, stake=5 * USDC, odds=40 * P, tif="GTC", outcome_one=True, expiry=None, **kw):
    return run(t["sxbet_order_request_place"](
        owner_command=text, market_hash=H, betting_outcome_one=outcome_one, stake_base_units=stake,
        percentage_odds=str(odds), time_in_force=tif, expiry=expiry or int(s.clock()) + 3600, idempotency_key=key,
        **kw))


def status(t, rid):
    return run(t["sxbet_order_request_status"](request_id=rid))


def signer_service(s, q):
    return SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)


# ---------------------------------------------------------------- registration (OFF by default)
def test_disabled_by_default_registry_and_health_unchanged(s, monkeypatch):
    monkeypatch.delenv("SXBET_ORDER_REQUESTS_ENABLED", raising=False)
    reg = _Registry()
    names = register_sxbet_tools(reg, Settings(enabled=True, network="mainnet"), client=SimClient(s.sim))
    assert not set(ORDER_REQUEST_TOOLS) & set(names)
    health = run(reg.tools["sxbet_health"]())
    assert "order_requests" not in health and health["write_tools"] == []
    assert health["available_modes"]["LIVE_TRADING"] == "UNAVAILABLE"


@pytest.mark.parametrize("env, code", [
    ({"SXBET_ORDER_REQUESTS_ENABLED": "true"}, "SXBET_SIGNER_QUEUE_DATABASE_URL_MISSING"),
    ({"SXBET_ORDER_REQUESTS_ENABLED": "true", "SXBET_SIGNER_QUEUE_DATABASE_URL": "postgresql://x/db",
      "DATABASE_URL": "postgresql://x/db", "SXBET_MAKER_ADDRESS": "0x" + "a" * 40},
     "SXBET_SIGNER_QUEUE_MUST_NOT_BE_THE_BRIDGE_DATABASE"),
    ({"SXBET_ORDER_REQUESTS_ENABLED": "true", "SXBET_SIGNER_QUEUE_DATABASE_URL": "postgresql://x/signer",
      "SXBET_MAKER_ADDRESS": "0x123"}, "SXBET_MAKER_ADDRESS_INVALID"),
])
def test_misconfiguration_fails_closed_without_tools_and_without_values(s, env, code):
    ors = oreq.OrderRequestSettings.from_env(env)
    assert not ors.enabled and ors.error == code
    reg = _Registry()
    names = register_sxbet_tools(reg, Settings(enabled=True, network="mainnet"), client=SimClient(s.sim),
                                 order_requests=ors)
    assert not set(ORDER_REQUEST_TOOLS) & set(names)
    h = run(reg.tools["sxbet_health"]())["order_requests"]
    assert h == {**h, "status": "DISABLED_MISCONFIGURED", "error": code, "tools": []}
    assert "postgresql" not in repr(ors) and "postgresql" not in json.dumps(h)


def test_enabled_registers_exactly_three_tools_with_no_dangerous_argument(s):
    t, _ = tools(s, MemoryQueue())
    assert set(TOOL_NAMES) == set(ORDER_REQUEST_TOOLS) <= set(t)
    for name in ORDER_REQUEST_TOOLS:
        params = set(inspect.signature(t[name]).parameters)
        assert not params & {"maker", "maker_address", "network", "live", "live_trading", "mode", "limits",
                             "kill_switch", "private_key", "signature", "chain_id"}, name
    for name in ("sxbet_order_request_place", "sxbet_order_request_cancel"):
        doc = t[name].__doc__
        assert "copied verbatim" in doc and "Never compose" in doc and "ask the owner" in doc


def test_bridge_never_imports_the_signer_package():
    import ast
    for f in ("order_requests.py", "order_request_tools.py", "tools.py"):
        tree = ast.parse((ROOT / "bridge" / "sxbet" / f).read_text())
        mods = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        mods += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not [m for m in mods if m == "signer" or m.startswith("signer.")], f


# ---------------------------------------------------------------- contract parity with the signer
def test_request_contract_matches_the_signer_byte_for_byte(s):
    assert oreq.REQUEST_SCHEMA == srq.REQUEST_SCHEMA and oreq.EVENT_STATES == srq.EVENT_STATES
    assert oreq.CANCEL_SCOPES == srq.CANCEL_SCOPES and oreq.KINDS == srq.KINDS
    q = MemoryQueue()
    t, w = tools(s, q)
    text, _ = owner_text(s, "order-key-S7001")
    res = place(t, s, "order-key-S7001", text)
    assert res["status"] == "QUEUED"
    stored = w.request(res["request_id"])
    assert srq.validate_request(stored) == stored
    assert srq.canonical_sha256(stored) == oreq.canonical_sha256(stored) == q.requests[0][3]
    assert set(stored["intent"]) == INTENT_KEYS
    signer_validate_intent(stored["intent"], odds_ladder_step_size=s.metadata["odds_ladder_step_size"])
    assert stored["intent"]["maker"] == s.maker and stored["command"]["command_text"] == text
    cancel = oreq.build_cancel_request(network="mainnet", scope="MARKET", key="cancel-key-1", command_text="x",
                                       now=1, market_hash="0x" + "AB" * 32)
    assert srq.validate_request(cancel)["market_hash"] == "0x" + "ab" * 32


# ---------------------------------------------------------------- end to end, offline
def test_end_to_end_place_is_verified_signed_and_reported_from_evidence(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7002")
    res = place(t, s, "order-key-S7002", text)
    assert res["status"] == "QUEUED" and res["signed_by_mcp"] is False and res["sent_by_mcp"] is False
    assert status(t, res["request_id"])["state"] == "QUEUED"
    (ev,) = signer_service(s, q).process_once()
    assert ev["event"] == "SUBMITTED"
    st = status(t, res["request_id"])
    assert st["state"] == "ACCEPTED" and st["evidence"] == "EXCHANGE_READ" and st["exchange_state"] == "SUBMITTED"
    assert text not in json.dumps(st) and "signature" not in json.dumps(st["events"])
    oid = st["order_id"]
    s.sim.fill_maker_order(oid, 2 * USDC)
    assert status(t, res["request_id"])["state"] == "PARTIALLY_FILLED"   # exchange evidence, not the queue
    s.sim.fill_maker_order(oid, 3 * USDC)
    assert status(t, res["request_id"])["state"] == "FILLED"
    assert len([r for r in s.sim.writes() if r.method == "POST"]) == 1


def test_a_command_that_does_not_match_the_order_is_refused_by_the_signer(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7003", stake=5 * USDC)
    res = place(t, s, "order-key-S7003", text, stake=9 * USDC)          # the model changed the stake
    assert res["status"] == "QUEUED"                                    # the MCP cannot judge: the signer does
    signer_service(s, q).process_once()
    st = status(t, res["request_id"])
    assert st["state"] == "REFUSED" and st["code"].startswith("COMMAND_MISMATCH")
    assert "exchange_read" not in st and s.sim.writes() == []


def test_end_to_end_cancel_is_accepted_but_never_reported_as_proof(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7004")
    placed = place(t, s, "order-key-S7004", text)
    signer_service(s, q).process_once()
    oid = status(t, placed["request_id"])["order_id"]
    res = run(t["sxbet_order_request_cancel"](owner_command=f"Cancella l'ordine {oid}", scope="ORDER_IDS",
                                              idempotency_key="cancel-key-S7004", order_ids=[oid]))
    assert res["status"] == "QUEUED"
    signer_service(s, q).process_once()
    st = status(t, res["request_id"])
    assert st["state"] == "ACCEPTED" and "not proof" in st["warning"] and oid in st["orders"]
    assert status(t, placed["request_id"])["state"] == "CANCELLED"         # from the exchange read of the order


# ---------------------------------------------------------------- idempotency (owner requirement 3)
def test_identical_retry_is_already_queued_and_writes_nothing(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7005")
    expiry = int(s.clock()) + 3600
    a = place(t, s, "order-key-S7005", text, expiry=expiry)
    s.clock.t += 3                                      # a later call: new timestamps, new command id
    b = place(t, s, "order-key-S7005", text, expiry=expiry)
    assert b["status"] == "ALREADY_QUEUED" and b["request_id"] == a["request_id"] and len(q.requests) == 1


@pytest.mark.parametrize("change", ["stake", "text", "side", "post_only"])
def test_same_key_with_a_different_payload_is_refused_without_writing(s, change):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7006")
    place(t, s, "order-key-S7006", text)
    kw = {"stake": {"stake": 6 * USDC}, "side": {"outcome_one": False}, "post_only": {"post_only": True},
          "text": {}}[change]
    res = place(t, s, "order-key-S7006", text + " " if change == "text" else text, **kw)
    assert res["status"] == "REFUSED" and res["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
    assert len(q.requests) == 1


def test_place_and_cancel_keys_live_in_separate_namespaces(s):
    assert oreq.request_id_for("mainnet", "PLACE", "k" * 8) != oreq.request_id_for("mainnet", "CANCEL", "k" * 8)
    assert oreq.request_id_for("mainnet", "PLACE", "k" * 8) != oreq.request_id_for("testnet", "PLACE", "k" * 8)


# ---------------------------------------------------------------- ambiguous writes (owner requirement 2)
def test_write_error_is_outcome_unknown_and_a_retry_never_duplicates(s):
    q = MemoryQueue()
    t, w = tools(s, q)
    text, _ = owner_text(s, "order-key-S7007")
    w.fail_next = "after_commit"
    res = place(t, s, "order-key-S7007", text)
    assert res["status"] == "QUEUE_WRITE_OUTCOME_UNKNOWN" and "secret" not in json.dumps(res)
    assert res.get("state") is None                                     # never "not queued" / "not sent"
    again = place(t, s, "order-key-S7007", text)
    assert again["status"] == "ALREADY_QUEUED" and len(q.requests) == 1
    w.fail_next = "before"
    other_text, _ = owner_text(s, "order-key-S7008")
    res2 = place(t, s, "order-key-S7008", other_text)
    assert res2["status"] == "QUEUE_WRITE_OUTCOME_UNKNOWN" and "postgres://" not in json.dumps(res2)


def test_status_never_says_not_sent_while_the_send_is_ambiguous(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7009")
    res = place(t, s, "order-key-S7009", text)
    s.sim.inject("POST", "/orders-v3", "timeout_before")
    signer_service(s, q).process_once()
    st = status(t, res["request_id"])
    assert st["state"] in ("UNKNOWN", "ACCEPTED") and st["state"] != "REFUSED"
    assert "NOT_SENT" not in json.dumps({k: v for k, v in st.items() if k != "events"})


# ---------------------------------------------------------------- state mapping (owner requirements 1 and 4)
def ev(*states_details, signer="sig-1"):
    out = []
    for i, (st, d) in enumerate(states_details, start=1):
        out.append({"seq": i, "state": st, "detail": d, "signer_id": signer})
    return out


@pytest.mark.parametrize("events, state", [
    ([], "QUEUED"),
    (ev(("CLAIMED", {})), "PROCESSING"),
    (ev(("CLAIMED", {}), ("SIGNED", {"order_id": "0x" + "1" * 64})), "PROCESSING"),
    (ev(("CLAIMED", {}), ("REFUSED", {"code": "COMMAND_EXPIRED"})), "REFUSED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("UNKNOWN", {"note": "RECONCILE_BEFORE_ANY_RETRY"})), "UNKNOWN"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "SUBMITTED", "sent": True})), "ACCEPTED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "PARTIALLY_FILLED"})), "PARTIALLY_FILLED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "REJECTED", "sent": True})), "UNKNOWN"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN", "sent": False})), "UNKNOWN"),
    # Changed by the review of ce4a702 (P2-1): signed, never sent, signature outstanding -> UNKNOWN, not final.
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("DONE", {"sent": False, "code": "INTERRUPTED_AFTER_SIGNATURE"})),
     "UNKNOWN"),
    # Changed by the review of 47a770b (P2-1): ledger FILLED without its amount (and without the stake) is no
    # longer "fully filled" (declared behaviour change; FILLED with filled == stake: test_review_47a770b_*).
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}), ("DONE", {"state": "FILLED"})), "UNKNOWN"),
    # Changed by the review of ce4a702 (P1-1): a final CANCELLED / EXPIRED needs its fill amount.
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
        ("DONE", {"state": "EXPIRED", "filled": "0"})), "EXPIRED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}), ("DONE", {"state": "EXPIRED"})),
     "UNKNOWN"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
        ("DONE", {"state": "CANCELLED", "filled": "2000000"})), "PARTIALLY_FILLED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
        ("DONE", {"state": "SETTLED", "filled": "2000000"})), "SETTLED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("REFUSED", {"code": "DUPLICATE_REQUEST"})), "UNKNOWN"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}), ("DONE", {"state": "REJECTED"})),
     "UNKNOWN"),
])
def test_place_state_is_a_pure_function_of_the_signer_events(events, state):
    assert oreq.queue_view("PLACE", events)["state"] == state


@pytest.mark.parametrize("events, code", [
    (ev(("SIGNED", {})), "QUEUE_EVIDENCE_NOT_CLAIMED_FIRST"),
    (ev(("CLAIMED", {}), ("CLAIMED", {})), "QUEUE_EVIDENCE_DUPLICATE_CLAIM"),
    (ev(("CLAIMED", {}), ("REFUSED", {}), ("DONE", {"state": "FILLED"})), "QUEUE_EVIDENCE_EVENT_AFTER_TERMINAL"),
    ([{"seq": 2, "state": "CLAIMED", "detail": {}, "signer_id": "a"}], "QUEUE_EVIDENCE_MALFORMED"),
    (ev(("CLAIMED", {})) + [{"seq": 2, "state": "DONE", "detail": {"state": "FILLED"}, "signer_id": "other"}],
     "QUEUE_EVIDENCE_MIXED_WRITERS"),
])
def test_inconsistent_or_duplicate_terminal_evidence_is_unknown_never_a_false_success(events, code):
    v = oreq.queue_view("PLACE", events)
    assert v["state"] == "UNKNOWN" and v["code"] == code


# Rewritten twice, both declared behaviour changes: review of ce4a702 (`final` field, P3-2 rule) and review of
# 8396424 P2-A (`ledger_final` used for conflicts, c9c32e7).
@pytest.mark.parametrize("queue, final, lookup, state", [
    ("UNKNOWN", False, "SUBMITTED", "ACCEPTED"),
    ("UNKNOWN", False, "NOT_ON_EXCHANGE", "UNKNOWN"),          # a 404 is never promoted to "not sent"
    ("UNKNOWN", False, "CANCELLED", "UNKNOWN"),                # review P3-2: a signature may be outstanding
    ("PROCESSING", False, "CANCELLED", "PROCESSING"),
    ("PROCESSING", False, None, "PROCESSING"),
    ("ACCEPTED", False, "PARTIALLY_FILLED", "PARTIALLY_FILLED"),
    ("ACCEPTED", False, "CANCELLED", "CANCELLED"),
    ("ACCEPTED", False, "EXPIRED", "EXPIRED"),
    ("PARTIALLY_FILLED", False, "SUBMITTED", "UNKNOWN"),       # earlier stage than the queue: conflict
    ("FILLED", False, "CANCELLED", "UNKNOWN"),                 # contradiction: never pick one silently
    ("FILLED", True, "SUBMITTED", "UNKNOWN"),
    ("FILLED", True, "FILLED", "FILLED"),
    ("CANCELLED", True, "FILLED", "UNKNOWN"),
    ("PARTIALLY_FILLED", True, "PARTIALLY_FILLED", "PARTIALLY_FILLED"),
    ("SETTLED", True, "CANCELLED", "SETTLED"),                 # settlement comes after the order lifecycle
    ("REFUSED", True, "FILLED", "REFUSED"),
])
def test_exchange_evidence_merge_is_deterministic(queue, final, lookup, state):
    out = oreq.merge_exchange({"state": queue, "final": final, "ledger_final": final, "evidence": "QUEUE_EVENTS"},
                              lookup)
    assert out["state"] == state


# ---------------------------------------------------------------- review of ce4a702 (NOT SAFE): regressions
def test_review_ce4a702_p1_1_partial_fill_then_cancel_then_settled_never_reads_as_full_or_empty(s):
    """Signer side: the final DONE carries the matched amount, so the MCP never says FILLED / nothing filled."""
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7101")
    res = place(t, s, "order-key-S7101", text)
    svc = signer_service(s, q)
    svc.process_once()
    oid = status(t, res["request_id"])["order_id"]
    s.sim.fill_maker_order(oid, 2 * USDC)
    s.gw.reconcile("order-key-S7101")
    s.ledger.transition("order-key-S7101", "CANCELLED", reason="TEST_REMAINDER_CANCELLED")
    (done,) = svc.sync_outcomes()
    assert done["detail"]["state"] == "CANCELLED" and done["detail"]["filled"] == str(2 * USDC)
    st = oreq.queue_view("PLACE", q.events_of(res["request_id"]))
    assert st["state"] == "PARTIALLY_FILLED" and st["resting"] is False and st["filled_base_units"] == str(2 * USDC)
    settled = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
                                          ("DONE", {"state": "SETTLED", "filled": str(2 * USDC)})))
    assert settled["state"] == "SETTLED" and settled["state"] != "FILLED"
    assert oreq.merge_exchange(settled, None)["state"] == "SETTLED"   # purged row: still not "FILLED"


def test_review_ce4a702_p2_1_signed_not_sent_is_not_final_and_the_exchange_is_still_read(s):
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {"order_id": "0x" + "1" * 64}),
                                    ("DONE", {"sent": False, "code": "INTERRUPTED_AFTER_SIGNATURE"})))
    assert v["state"] == "UNKNOWN" and v["final"] is False and v["code"] == "SIGNED_NOT_SENT_SIGNATURE_OUTSTANDING"
    assert oreq.merge_exchange(v, "FILLED")["state"] == "FILLED"   # it was live after all: the read says so


def test_review_ce4a702_p2_1_end_to_end_recovered_signed_not_sent_reads_the_exchange(s, monkeypatch):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7102")
    res = place(t, s, "order-key-S7102", text)
    monkeypatch.setattr(s.gw, "submit", lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        signer_service(s, q).process_once()
    monkeypatch.undo()
    signer_service(s, q).recover()
    st = status(t, res["request_id"])
    assert st["state"] == "UNKNOWN" and st["final"] is False and "exchange_read" in st
    assert st["state"] != "REFUSED"


@pytest.mark.parametrize("buckets, state", [
    ({"a": "CANCELLED", "b": "CANCELLED"}, "ACCEPTED"),
    ({"a": "NOT_CANCELLED"}, "NOT_CANCELLED"),
    ({"a": "CANCELLED", "b": "NOT_CANCELLED"}, "PARTIALLY_CANCELLED"),
    ({"a": "CANCELLED", "b": "UNKNOWN"}, "UNKNOWN"),
    ({"a": "UNCONFIRMED"}, "UNKNOWN"),
    ({"a": None}, "UNKNOWN"),
    ({}, "UNKNOWN"),
])
def test_review_ce4a702_p2_2_cancel_by_ids_state_follows_the_buckets(buckets, state):
    orders = {k: {"bucket": b} for k, b in buckets.items()}
    v = oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"scope": "ORDER_IDS", "sent": True,
                                                                 "orders": orders})))
    assert v["state"] == state


def test_review_ce4a702_p2_2_cancel_by_ids_timeout_is_unknown_end_to_end(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7103")
    placed = place(t, s, "order-key-S7103", text)
    signer_service(s, q).process_once()
    oid = status(t, placed["request_id"])["order_id"]
    s.sim.inject("DELETE", "/orders-v3", "timeout_before")    # cancel by ids: DELETE /orders-v3
    res = run(t["sxbet_order_request_cancel"](owner_command=f"Cancella l'ordine {oid}", scope="ORDER_IDS",
                                              idempotency_key="cancel-key-S7103", order_ids=[oid]))
    signer_service(s, q).process_once()
    st = status(t, res["request_id"])
    assert st["state"] == "UNKNOWN" and st["final"] is False


def test_review_ce4a702_p3_4_out_of_order_events_are_unknown():
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SUBMITTED", {"state": "OPEN"}), ("SIGNED", {})))
    assert v["state"] == "UNKNOWN" and v["code"] == "QUEUE_EVIDENCE_TRANSITION_NOT_ALLOWED"


def test_review_ce4a702_p3_5_cancel_not_sent_keeps_the_per_order_report():
    orders = {"0x" + "1" * 64: {"bucket": "NOT_CANCELLED", "reason": "ALREADY_TERMINAL"}}
    v = oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"scope": "ORDER_IDS", "sent": False,
                                                                 "orders": orders})))
    assert v["state"] == "REFUSED" and v["orders"] == orders


@pytest.mark.parametrize("bridge_url, queue_url", [
    ("postgresql://u:p@db.example:5432/app", "postgres://other:x@DB.example/app"),
    ("postgresql://u:p@db.example/app?sslmode=require", "postgresql://u2:p2@db.example:5432/app"),
])
def test_review_ce4a702_p3_6_the_same_database_spelled_differently_is_refused(bridge_url, queue_url):
    ors = oreq.OrderRequestSettings.from_env({"SXBET_ORDER_REQUESTS_ENABLED": "true", "DATABASE_URL": bridge_url,
                                              "SXBET_SIGNER_QUEUE_DATABASE_URL": queue_url,
                                              "SXBET_MAKER_ADDRESS": "0x" + "a" * 40})
    assert ors.error == "SXBET_SIGNER_QUEUE_MUST_NOT_BE_THE_BRIDGE_DATABASE"
    ok = oreq.OrderRequestSettings.from_env({"SXBET_ORDER_REQUESTS_ENABLED": "true", "DATABASE_URL": bridge_url,
                                             "SXBET_SIGNER_QUEUE_DATABASE_URL": "postgresql://s@db.example/signer",
                                             "SXBET_MAKER_ADDRESS": "0x" + "a" * 40})
    assert ok.enabled


def test_review_ce4a702_p3_7_health_declares_the_cost_of_the_request_tools(s):
    reg = _Registry()
    register_sxbet_tools(reg, Settings(enabled=True, network="mainnet"), client=SimClient(s.sim),
                         order_requests=ors_for(s), order_request_writer=MemoryWriter(MemoryQueue()))
    costs = run(reg.tools["sxbet_health"]())["order_requests"]["tool_cost_catalog"]
    assert {k: v["max_provider_calls"] for k, v in costs.items()} == {
        "sxbet_order_request_place": 1, "sxbet_order_request_cancel": 0, "sxbet_order_request_status": 2}


def test_review_ce4a702_p3_8_retry_refused_after_validation_points_to_the_existing_request(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7104")
    expiry = int(s.clock()) + 60
    first = place(t, s, "order-key-S7104", text, expiry=expiry)
    assert first["status"] == "QUEUED"
    s.clock.t = expiry - 5                                      # the same retry now has an expiry too soon
    again = place(t, s, "order-key-S7104", text, expiry=expiry)
    assert again["status"] == "INVALID_INPUT" and again["request_id"] == first["request_id"]
    assert again["request_with_this_key_exists"] is True


# Changed by the review of ce4a702 (P2-2): `sent` with no buckets / no explicit acceptance is UNKNOWN, no longer
# ACCEPTED (declared behaviour change; the old `accepted: False` case is covered by the bucket/scope tests).
def test_cancel_states_never_claim_a_cancellation_as_proven():
    done = ev(("CLAIMED", {}), ("DONE", {"scope": "ALL", "sent": True, "accepted": True}))
    assert oreq.queue_view("CANCEL", done)["state"] == "ACCEPTED"
    assert oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"scope": "ALL", "sent": True})))["state"] \
        == "UNKNOWN"                                         # scope cancel without an explicit acceptance
    assert oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"scope": "ORDER_IDS", "sent": True,
                                                                     "orders": {}})))["state"] == "UNKNOWN"
    assert oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"sent": None})))["state"] == "UNKNOWN"
    assert oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("UNKNOWN", {})))["state"] == "UNKNOWN"


# ---------------------------------------------------------------- input validation before any read
@pytest.mark.parametrize("kw, code", [
    ({"owner_command": ""}, "OWNER_COMMAND_MISSING"),
    ({"owner_command": "x" * 501}, "OWNER_COMMAND_TOO_LONG"),
    ({"owner_command": "Piazza\x00 10"}, "OWNER_COMMAND_NOT_PRINTABLE"),
    ({"idempotency_key": "short"}, "INVALID_IDEMPOTENCY_KEY"),
    ({"percentage_odds": "1e19"}, "INVALID_PERCENTAGE_ODDS"),
    ({"post_only": True, "time_in_force": "IOC"}, "POST_ONLY_REQUIRES_GTC"),
])
def test_invalid_place_input_is_refused_before_any_read_or_write(s, kw, code):
    q = MemoryQueue()
    t, _ = tools(s, q)
    args = dict(owner_command="Piazza 5 USDC", market_hash=H, betting_outcome_one=True, stake_base_units=5 * USDC,
                percentage_odds=str(40 * P), time_in_force="GTC", expiry=int(s.clock()) + 3600,
                idempotency_key="order-key-S7010")
    res = run(t["sxbet_order_request_place"](**{**args, **kw}))
    assert res["status"] == "INVALID_INPUT" and res["error"]["code"] == code
    assert res["network_calls_made"] == 0 and q.requests == []


@pytest.mark.parametrize("kw, code", [
    ({"scope": "ALL", "order_ids": ["0x" + "1" * 64]}, "CANCEL_ARGUMENT_NOT_FOR_THIS_SCOPE"),
    ({"scope": "ORDER_IDS", "order_ids": ["0x" + "1" * 64, "0x" + "1" * 64]}, "INVALID_ORDER_IDS"),
    ({"scope": "EVENT", "event_id": "bad id"}, "INVALID_EVENT_ID"),
    ({"scope": "NONE"}, "INVALID_CANCEL_SCOPE"),
])
def test_invalid_cancel_input_is_refused(s, kw, code):
    q = MemoryQueue()
    t, _ = tools(s, q)
    res = run(t["sxbet_order_request_cancel"](owner_command="Cancel all orders", idempotency_key="cancel-key-1",
                                              **kw))
    assert res["status"] == "INVALID_INPUT" and res["error"]["code"] == code and q.requests == []


def test_status_input_and_missing_request(s):
    t, _ = tools(s, MemoryQueue())
    assert status(t, "XYZ")["error"]["code"] == "INVALID_REQUEST_ID"
    assert status(t, "0" * 32)["status"] == "NOT_FOUND"


# ---------------------------------------------------------------- review of 8396424 (NOT SAFE): regressions
def test_review_8396424_p2_a_cancelled_is_never_final_and_a_late_fill_is_a_conflict(s):
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
                                    ("DONE", {"state": "CANCELLED", "filled": "0"})))
    assert v["state"] == "CANCELLED" and v["final"] is False and "late fill" in v["warning"]
    assert oreq.merge_exchange(v, None, fills_seen=True)["state"] == "UNKNOWN"         # row purged, fills remain
    assert oreq.merge_exchange(v, None, fills_seen=True)["code"] == "FILLS_SEEN_ON_EXCHANGE"
    assert oreq.merge_exchange(v, "PARTIALLY_FILLED")["state"] == "UNKNOWN"            # ledger final contradicted
    acc = {"state": "ACCEPTED", "final": False, "evidence": "QUEUE_EVENTS"}
    assert oreq.merge_exchange(acc, None, fills_seen=True)["state"] == "UNKNOWN"


def test_review_8396424_p2_a_end_to_end_late_fill_after_purge_is_not_cancelled(s):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7201")
    res = place(t, s, "order-key-S7201", text)
    svc = signer_service(s, q)
    svc.process_once()
    oid = status(t, res["request_id"])["order_id"]
    run(t["sxbet_order_request_cancel"](owner_command=f"Cancella l'ordine {oid}", scope="ORDER_IDS",
                                        idempotency_key="cancel-key-S7201", order_ids=[oid]))
    svc.process_once()                                  # cancelled on the exchange, nothing filled
    s.gw.reconcile("order-key-S7201")
    (done,) = svc.sync_outcomes()
    assert done["detail"] == {**done["detail"], "state": "CANCELLED", "filled": "0"}
    first = status(t, res["request_id"])
    assert first["state"] == "CANCELLED" and first["final"] is False
    s.sim.late_fill(oid, 2 * USDC)
    s.clock.t += 1000                                   # past the ~900 s retention: the row is purged (404)
    st = status(t, res["request_id"])
    assert st["exchange_read"]["status"] == "NOT_FOUND" and st["exchange_read"]["fills_count"] == 1
    assert st["state"] == "UNKNOWN" and st["code"] == "FILLS_SEEN_ON_EXCHANGE" and st["final"] is False


def test_review_8396424_p2_b_without_a_read_accepted_is_labelled_a_snapshot(s):
    q = MemoryQueue()
    t, _ = tools(s, q, api_key="")
    text, _ = owner_text(s, "order-key-S7202")
    res = place(t, s, "order-key-S7202", text)
    signer_service(s, q).process_once()
    s.sim.fill_maker_order(status(t, res["request_id"])["order_id"], 2 * USDC)
    st = status(t, res["request_id"])
    assert st["state"] == "ACCEPTED" and st["fill_evidence"] == "SIGNER_EVENT_SNAPSHOT_ONLY"
    assert st["exchange_read"] == {"status": "KEY_NOT_CONFIGURED"}
    doc = t["sxbet_order_request_status"].__doc__
    assert "proves no fill" not in doc and "snapshot" in doc


def test_review_8396424_p3_3_one_cost_catalog():
    from bridge.sxbet import order_request_tools as ort, tools as tl
    assert tl.ORDER_REQUEST_TOOL_COSTS is ort.TOOL_COSTS and tl.ORDER_REQUEST_TOOLS is ort.TOOL_NAMES


# ---------------------------------------------------------------- review of c9c32e7 (NOT SAFE): regressions
@pytest.mark.parametrize("queue_state", ["PROCESSING", "UNKNOWN", "ACCEPTED", "QUEUED_BUT_SIGNED"])
def test_review_c9c32e7_p2_1_fill_rows_never_coexist_with_a_no_fill_result(queue_state):
    view = {"state": "PROCESSING" if queue_state == "QUEUED_BUT_SIGNED" else queue_state, "final": False,
            "evidence": "QUEUE_EVENTS"}
    out = oreq.merge_exchange(view, "SUBMITTED", fills_seen=True)      # ACTIVE row, nothing filled, but fill rows
    assert out["state"] == "UNKNOWN" and out["code"] == "FILLS_SEEN_ON_EXCHANGE"
    assert oreq.merge_exchange(view, "PARTIALLY_FILLED", fills_seen=True)["state"] == "PARTIALLY_FILLED"
    assert oreq.merge_exchange(view, "SUBMITTED", fills_seen=False)["state"] == "ACCEPTED"


def test_review_c9c32e7_p2_1_end_to_end_timeout_then_fill_rows_is_not_accepted(s, monkeypatch):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7301")
    res = place(t, s, "order-key-S7301", text)
    s.sim.inject("POST", "/orders-v3", "timeout_after")
    signer_service(s, q).process_once()
    v = status(t, res["request_id"])
    assert v["last_event"] == "UNKNOWN"
    import bridge.sxbet.order_request_tools as ort
    real = SimClient.get

    async def fills_but_unfilled_row(self, path, params, *, auth):
        r = await real(self, path, params, auth=auth)
        if path.startswith("/orders-v3/"):
            r["data"]["order"]["remainingSize"] = r["data"]["order"]["totalBetSize"]
        if path == "/fills-v3":
            r["data"] = {"fills": [{"orderId": params["orderId"], "fillAmount": "1000000", "status": "MATCHED"}]}
        return r
    monkeypatch.setattr(SimClient, "get", fills_but_unfilled_row)
    st = status(t, res["request_id"])
    assert st["state"] == "UNKNOWN" and st["code"] == "FILLS_SEEN_ON_EXCHANGE"
    assert ort  # module imported for the monkeypatch scope only


def test_review_c9c32e7_p3_1_and_p3_2_fill_evidence_labels(s, monkeypatch):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7302")
    res = place(t, s, "order-key-S7302", text)
    signer_service(s, q).process_once()
    assert status(t, res["request_id"])["fill_evidence"] == "EXCHANGE_READ_NOW"
    real = SimClient.get

    async def fills_error(self, path, params, *, auth):
        if path == "/fills-v3":
            raise SXBetError("SXBET_HTTP_ERROR", http_status=500)
        return await real(self, path, params, auth=auth)
    monkeypatch.setattr(SimClient, "get", fills_error)
    st = status(t, res["request_id"])
    assert st["state"] == "ACCEPTED" and st["fill_evidence"] == "EXCHANGE_ORDER_ROW_ONLY"
    monkeypatch.undo()
    oid = st["order_id"]
    c = run(t["sxbet_order_request_cancel"](owner_command=f"Cancella l'ordine {oid}", scope="ORDER_IDS",
                                            idempotency_key="cancel-key-S7302", order_ids=[oid]))
    signer_service(s, q).process_once()
    cs = status(t, c["request_id"])
    assert cs["state"] == "ACCEPTED" and "fill_evidence" not in cs


def test_review_c9c32e7_p3_3_filled_amount_is_labelled_a_ledger_snapshot():
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
                                    ("DONE", {"state": "CANCELLED", "filled": "2000000"})))
    assert v["filled_source"] == "SIGNER_LEDGER_SNAPSHOT"


# ---------------------------------------------------------------- review of 21082c0 (NOT SAFE): regressions
OID = "0x" + "c" * 64


def row(**kw):
    r = {"id": OID, "totalBetSize": "5000000", "remainingSize": "5000000", "status": "ACTIVE",
         "inactiveReason": None}
    r.update(kw)
    return r


@pytest.mark.parametrize("r, state, reason", [
    (row(status="INACTIVE", inactiveReason="FILLED"), None, "EVIDENCE_FILLED_WITHOUT_FILL"),
    (row(totalBetSize="3000000", remainingSize="3000000"), None, "EVIDENCE_SIZE_MISMATCH"),
    (row(remainingSize="6000000"), None, "EVIDENCE_SIZE_MISMATCH"),
    (row(remainingSize="-1"), None, "EVIDENCE_SIZE_MISMATCH"),
    (row(status="INACTIVE", inactiveReason="SOMETHING_NEW"), None, "EVIDENCE_INACTIVE_REASON_UNKNOWN"),
    (row(status="INACTIVE", inactiveReason=None), None, "EVIDENCE_INACTIVE_REASON_UNKNOWN"),
    (row(status="WEIRD"), None, "EVIDENCE_STATUS_UNKNOWN"),
    (row(id="0x" + "d" * 64), None, "EVIDENCE_OTHER_ORDER"),
    (row(), "SUBMITTED", "EXCHANGE_ORDER_ACTIVE"),
    (row(remainingSize="3000000"), "PARTIALLY_FILLED", "EXCHANGE_ORDER_ACTIVE"),
    (row(status="INACTIVE", inactiveReason="FILLED", remainingSize="0"), "FILLED", "EXCHANGE_ORDER_INACTIVE_FILLED"),
    (row(status="INACTIVE", inactiveReason="FILLED", remainingSize="1"), "PARTIALLY_FILLED",
     "EXCHANGE_ORDER_INACTIVE_FILLED"),
    (row(status="INACTIVE", inactiveReason="EXPIRED"), "EXPIRED", "EXCHANGE_ORDER_INACTIVE_EXPIRED"),
    (row(status="INACTIVE", inactiveReason="USER_REQUESTED"), "CANCELLED", "EXCHANGE_ORDER_INACTIVE_USER_REQUESTED"),
    (row(status="INACTIVE", inactiveReason="USER_REQUESTED", remainingSize="2000000"), "PARTIALLY_FILLED",
     "EXCHANGE_ORDER_INACTIVE_USER_REQUESTED"),
])
def test_review_21082c0_p2_1_row_evidence_has_the_signer_guards(r, state, reason):
    out = oreq.exchange_row_state(r, order_id=OID, intent_total="5000000")
    assert out["state"] == state and out["reason"] == reason


def _status_with_row(s, monkeypatch, key, mutate_row, *, fills=None, timeout=True):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, key)
    res = place(t, s, key, text)
    if timeout:
        s.sim.inject("POST", "/orders-v3", "timeout_after")
    signer_service(s, q).process_once()
    real = SimClient.get

    async def altered(self, path, params, *, auth):
        r = await real(self, path, params, auth=auth)
        if path.startswith("/orders-v3/") and mutate_row is not None:
            r["data"]["order"].update(mutate_row)
        if path == "/fills-v3" and fills is not None:
            r["data"] = {"fills": fills}
        return r
    monkeypatch.setattr(SimClient, "get", altered)
    return status(t, res["request_id"])


def test_review_21082c0_p2_1_filled_without_fill_is_never_reported_filled(s, monkeypatch):
    st = _status_with_row(s, monkeypatch, "order-key-S7401", {"status": "INACTIVE", "inactiveReason": "FILLED"})
    assert st["state"] == "UNKNOWN" and st["exchange_read"]["row_evidence"] == "EVIDENCE_FILLED_WITHOUT_FILL"


def test_review_21082c0_p2_1_size_mismatch_is_never_a_partial_fill(s, monkeypatch):
    st = _status_with_row(s, monkeypatch, "order-key-S7402",
                          {"totalBetSize": "3000000", "remainingSize": "3000000"})
    assert st["state"] == "UNKNOWN" and st["exchange_read"]["row_evidence"] == "EVIDENCE_SIZE_MISMATCH"


def test_review_21082c0_p3_1_404_is_labelled_inconclusive_not_a_fresh_read(s, monkeypatch):
    q = MemoryQueue()
    t, _ = tools(s, q)
    text, _ = owner_text(s, "order-key-S7403")
    res = place(t, s, "order-key-S7403", text)
    signer_service(s, q).process_once()
    real = SimClient.get

    async def gone(self, path, params, *, auth):
        if path.startswith("/orders-v3/"):
            raise SXBetError("SXBET_NOT_FOUND", http_status=404)
        return await real(self, path, params, auth=auth)
    monkeypatch.setattr(SimClient, "get", gone)
    st = status(t, res["request_id"])
    assert st["state"] == "ACCEPTED" and st["fill_evidence"] == "EXCHANGE_READ_INCONCLUSIVE"
    assert st["exchange_read"]["status"] == "NOT_FOUND"


def test_review_21082c0_p3_2_ledger_not_resting_against_a_resting_row_is_a_conflict():
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}),
                                    ("DONE", {"state": "CANCELLED", "filled": "2000000"})))
    assert v["resting"] is False
    out = oreq.merge_exchange(v, "PARTIALLY_FILLED", ex_resting=True)
    assert out["state"] == "UNKNOWN" and "resting" not in out
    assert oreq.merge_exchange(v, "PARTIALLY_FILLED", ex_resting=False)["resting"] is False


def test_review_21082c0_p3_3_queue_only_code_moves_when_the_exchange_decides():
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {"order_id": OID}),
                                    ("DONE", {"sent": False, "code": "INTERRUPTED_AFTER_SIGNATURE"})))
    out = oreq.merge_exchange(v, "SUBMITTED", ex_resting=True)
    assert out["state"] == "ACCEPTED" and out.get("code") is None and out.get("note") is None
    assert out["queue_code"] == "SIGNED_NOT_SENT_SIGNATURE_OUTSTANDING" and out["queue_state"] == "UNKNOWN"
    assert out["resting"] is True


# ---------------------------------------------------------------- review of 47a770b (NOT SAFE): regressions
def _done_filled(filled):
    d = {"state": "FILLED"}
    if filled is not None:
        d["filled"] = filled
    return ev(("CLAIMED", {}), ("SIGNED", {"order_id": OID}), ("SUBMITTED", {"state": "OPEN"}), ("DONE", d))


@pytest.mark.parametrize("filled, total, state, final, code", [
    ("5000000", "5000000", "FILLED", True, None),
    ("1000000", "5000000", "PARTIALLY_FILLED", False, "FILLED_AMOUNT_BELOW_STAKE"),
    (None, "5000000", "UNKNOWN", False, "FILL_AMOUNT_NOT_PROVEN"),
    ("5000000", None, "UNKNOWN", False, "FILL_AMOUNT_NOT_PROVEN"),
    ("0", "5000000", "UNKNOWN", False, "FILL_AMOUNT_INCONSISTENT"),
    ("6000000", "5000000", "UNKNOWN", False, "FILL_AMOUNT_INCONSISTENT"),
    ("05000000", "5000000", "UNKNOWN", False, "FILL_AMOUNT_NOT_PROVEN"),
])
def test_review_47a770b_p2_1_ledger_filled_is_full_only_when_it_matches_the_signed_stake(filled, total, state, final,
                                                                                         code):
    v = oreq.queue_view("PLACE", _done_filled(filled), intent_total=total)
    assert (v["state"], v["final"], v.get("code")) == (state, final, code)


def test_review_47a770b_p2_1_partial_ledger_filled_after_purge_or_without_key_is_never_filled():
    v = oreq.queue_view("PLACE", _done_filled("1000000"), intent_total="5000000")
    for lookup in (None, "NOT_ON_EXCHANGE"):
        out = oreq.merge_exchange(v, lookup)
        assert out["state"] == "PARTIALLY_FILLED" and out["final"] is False and out["resting"] is False


def test_review_47a770b_p3_1_partial_ledger_filled_agrees_with_the_same_row():
    v = oreq.queue_view("PLACE", _done_filled("1000000"), intent_total="5000000")
    r = oreq.exchange_row_state(row(status="INACTIVE", inactiveReason="FILLED", remainingSize="4000000"),
                                order_id=OID, intent_total="5000000")
    assert r["state"] == "PARTIALLY_FILLED" and r["resting"] is False
    out = oreq.merge_exchange(v, r["state"], ex_resting=r["resting"])
    assert out["state"] == "PARTIALLY_FILLED" and out.get("evidence") != "CONFLICT"


def test_review_47a770b_p2_1_send_time_filled_without_amount_is_unknown():
    v = oreq.queue_view("PLACE", ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "FILLED"})),
                        intent_total="5000000")
    assert v["state"] == "UNKNOWN" and v["code"] == "FILL_AMOUNT_NOT_PROVEN"
    assert oreq.merge_exchange(v, "FILLED")["state"] == "FILLED"                     # a fresh row can settle it


@pytest.mark.parametrize("r", [row(totalBetSize="05000000"), row(remainingSize="0001"), row(remainingSize="+1"),
                               row(remainingSize="1_000"), row(remainingSize="1.0")])
def test_review_47a770b_p3_2_number_format_follows_the_signer(r):
    assert oreq.exchange_row_state(r, order_id=OID, intent_total="5000000")["reason"] == "EVIDENCE_SIZE_MISMATCH"


def test_review_47a770b_p3_3_no_fill_amount_from_a_refused_row(s, monkeypatch):
    st = _status_with_row(s, monkeypatch, "order-key-S7501", {"id": "0x" + "e" * 64, "remainingSize": "1000"})
    assert st["exchange_read"]["row_evidence"] == "EVIDENCE_OTHER_ORDER"
    assert st["exchange_read"]["filled_base_units_from_order_row"] is None


def test_review_47a770b_p3_4_no_resting_flag_on_a_fills_conflict():
    v = {"state": "PROCESSING", "final": False, "evidence": "QUEUE_EVENTS"}
    out = oreq.merge_exchange(v, "SUBMITTED", fills_seen=True, ex_resting=True)
    assert out["code"] == "FILLS_SEEN_ON_EXCHANGE" and "resting" not in out


@pytest.mark.parametrize("r", [row(remainingSize="0"), row(status="PENDING", remainingSize="0"),
                               row(status="INACTIVE", inactiveReason="EXPIRED", remainingSize="0"),
                               row(status="INACTIVE", inactiveReason="USER_REQUESTED", remainingSize="0")])
def test_review_47a770b_p3_5_whole_stake_matched_but_not_marked_filled_is_not_evidence(r):
    out = oreq.exchange_row_state(r, order_id=OID, intent_total="5000000")
    assert out["state"] is None and out["reason"] == "EVIDENCE_FULL_FILL_NOT_MARKED_FILLED"
