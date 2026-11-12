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
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("DONE", {"sent": False, "code": "INTERRUPTED_AFTER_SIGNATURE"})),
     "REFUSED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}), ("DONE", {"state": "FILLED"})), "FILLED"),
    (ev(("CLAIMED", {}), ("SIGNED", {}), ("SUBMITTED", {"state": "OPEN"}), ("DONE", {"state": "EXPIRED"})),
     "EXPIRED"),
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


@pytest.mark.parametrize("queue, lookup, state", [
    ("UNKNOWN", "SUBMITTED", "ACCEPTED"),
    ("UNKNOWN", "NOT_ON_EXCHANGE", "UNKNOWN"),                 # a 404 is never promoted to "not sent"
    ("PROCESSING", None, "PROCESSING"),
    ("ACCEPTED", "PARTIALLY_FILLED", "PARTIALLY_FILLED"),
    ("ACCEPTED", "CANCELLED", "CANCELLED"),
    ("FILLED", "SUBMITTED", "UNKNOWN"),                        # contradiction: never pick one silently
    ("FILLED", "CANCELLED", "UNKNOWN"),
    ("CANCELLED", "FILLED", "UNKNOWN"),
    ("REFUSED", "FILLED", "REFUSED"),
])
def test_exchange_evidence_merge_is_deterministic(queue, lookup, state):
    out = oreq.merge_exchange({"state": queue, "evidence": "QUEUE_EVENTS"}, lookup)
    assert out["state"] == state


def test_cancel_states_never_claim_a_cancellation_as_proven():
    done = ev(("CLAIMED", {}), ("DONE", {"scope": "ALL", "sent": True, "accepted": True}))
    assert oreq.queue_view("CANCEL", done)["state"] == "ACCEPTED"
    assert oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"sent": True, "accepted": False})))["state"] \
        == "UNKNOWN"
    assert oreq.queue_view("CANCEL", ev(("CLAIMED", {}), ("DONE", {"sent": True, "orders": {}})))["state"] \
        == "ACCEPTED"
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
