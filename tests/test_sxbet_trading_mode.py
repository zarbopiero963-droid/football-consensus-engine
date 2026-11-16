"""P5 (issue #187): SX Bet MCP dual mode READ_ONLY / LIVE_TRADING. Offline: fakes, the simulated exchange and the
in-memory queue; nothing reaches SX Bet, mainnet or testnet."""
import asyncio
import math
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from bridge.sxbet import trading_mode as tm  # noqa: E402
from bridge.sxbet.client import Settings  # noqa: E402
from bridge.sxbet.tools import ORDER_REQUEST_TOOLS, register_sxbet_tools  # noqa: E402
import test_sxbet_order_request_tools as s7  # noqa: E402  (S7 helpers: SimClient, MemoryWriter, place, ...)
from sxbet_e2e_kit import SIGNER_ID, Stack  # noqa: E402
from sxbet_queue_kit import MemoryQueue  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEALTHY = dict(s7.HEALTHY)


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------- shipped policy
def test_shipped_policy_refuses_mainnet_and_cancel_under_kill_switch():
    src = (ROOT / "bridge/sxbet/trading_mode.py").read_text(encoding="utf-8")
    assert "\nMAINNET_LIVE_TRADING_AUTHORIZED = False\n" in src
    assert "\nCANCEL_ALLOWED_WHILE_KILL_SWITCH_ENGAGED = False\n" in src
    assert tm.MAINNET_LIVE_TRADING_AUTHORIZED is False


# ---------------------------------------------------------------- configuration
@pytest.mark.parametrize("env, mode, error", [
    ({}, "READ_ONLY", None),
    ({"SXBET_MCP_MODE": "read_only"}, "READ_ONLY", None),
    ({"SXBET_MCP_MODE": "live"}, "READ_ONLY", "SXBET_MCP_MODE_INVALID"),
    ({"SXBET_MCP_MODE": "LIVE_TRADING", "SXBET_SIGNER_STATUS_MAX_AGE_SECONDS": "5"}, "READ_ONLY",
     "SXBET_SIGNER_STATUS_MAX_AGE_INVALID"),
    ({"SXBET_MCP_MODE": "LIVE_TRADING", "SXBET_SIGNER_STATUS_MAX_AGE_SECONDS": "60s"}, "READ_ONLY",
     "SXBET_SIGNER_STATUS_MAX_AGE_INVALID"),
])
def test_mode_from_env_fails_closed(env, mode, error):
    m = tm.TradingMode.from_env("testnet", True, None, env=env)
    assert (m.mode, m.error) == (mode, error)


def test_live_trading_on_mainnet_is_refused_by_the_shipped_code():
    m = tm.TradingMode.from_env("mainnet", True, None, env={"SXBET_MCP_MODE": "LIVE_TRADING"})
    assert (m.mode, m.error) == ("READ_ONLY", "LIVE_TRADING_MAINNET_NOT_AUTHORIZED")


def test_live_trading_needs_a_complete_order_request_configuration():
    env = {"SXBET_MCP_MODE": "LIVE_TRADING"}
    assert tm.TradingMode.from_env("testnet", False, "SXBET_MAKER_ADDRESS_INVALID", env=env).error == \
        "SXBET_MAKER_ADDRESS_INVALID"
    assert tm.TradingMode.from_env("testnet", False, None, env=env).error == "ORDER_REQUESTS_NOT_CONFIGURED"
    ok = tm.TradingMode.from_env("testnet", True, None, env={**env, "SXBET_SIGNER_STATUS_MAX_AGE_SECONDS": "60"})
    assert ok.live and ok.status_max_age_seconds == 60


# ---------------------------------------------------------------- registration
def _register(s, *, mode=None, network="mainnet", ors=True, writer=None):
    reg = s7._Registry()
    names = register_sxbet_tools(reg, Settings(enabled=True, network=network, api_key="k"),
                                 client=s7.SimClient(s.sim), order_requests=s7.ors_for(s) if ors else None,
                                 order_request_writer=writer or s7.MemoryWriter(MemoryQueue()), trading_mode=mode)
    return reg.tools, names


def test_read_only_registers_no_trading_tool_even_with_order_requests_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("SXBET_MCP_MODE", raising=False)
    s = Stack(tmp_path)
    t, names = _register(s)
    assert not set(ORDER_REQUEST_TOOLS) & set(names) and "sxbet_trading_mode" in names
    h = run(t["sxbet_health"]())
    assert h["order_requests"]["status"] == "DISABLED_MCP_MODE_READ_ONLY" and h["order_requests"]["tools"] == []
    assert run(t["sxbet_trading_mode"]())["mode"] == "READ_ONLY"


def test_a_live_mode_object_passed_in_code_is_still_refused_on_mainnet(tmp_path):
    s = Stack(tmp_path)
    t, names = _register(s, mode=tm.TradingMode(mode=tm.LIVE_TRADING))
    assert not set(ORDER_REQUEST_TOOLS) & set(names)
    m = run(t["sxbet_trading_mode"]())
    assert m["mode"] == "READ_ONLY" and m["configuration_error"] == "LIVE_TRADING_MAINNET_NOT_AUTHORIZED"
    assert m["network_calls_made"] == 0


def test_live_trading_on_testnet_registers_the_three_request_tools(tmp_path):
    s = Stack(tmp_path)
    t, names = _register(s, network="testnet", mode=tm.TradingMode.live_for("testnet", True))
    assert set(ORDER_REQUEST_TOOLS) <= set(names)
    assert run(t["sxbet_trading_mode"]())["trading_tools_registered"] is True


def test_the_mode_tool_takes_no_argument():
    import inspect
    from bridge.sxbet import tools as tools_mod
    assert "SXBET_MCP_MODE" not in inspect.getsource(tools_mod).split("async def sxbet_trading_mode")[1][:600]


# ---------------------------------------------------------------- signer status precheck
def _row(**kw):
    return {**HEALTHY, **kw}


@pytest.mark.parametrize("rows, kind, code", [
    (None, "PLACE", "SIGNER_STATUS_UNAVAILABLE"),
    ([], "PLACE", "SIGNER_STATUS_MISSING"),
    ([_row(), _row(signer_id="other")], "PLACE", "SIGNER_STATUS_AMBIGUOUS"),
    ([_row(age_seconds=121.0)], "PLACE", "SIGNER_UNREACHABLE"),
    ([_row(age_seconds=-6.0)], "PLACE", "SIGNER_UNREACHABLE"),
    ([_row(age_seconds=math.nan)], "PLACE", "SIGNER_UNREACHABLE"),
    ([_row(age_seconds=None)], "PLACE", "SIGNER_UNREACHABLE"),
    ([_row(age_seconds="1")], "PLACE", "SIGNER_UNREACHABLE"),
    ([_row(kill_switch="ENGAGED")], "PLACE", "KILL_SWITCH_ENGAGED"),
    ([_row(kill_switch="ENGAGED")], "CANCEL", "KILL_SWITCH_ENGAGED"),
    ([_row(kill_switch="UNKNOWN")], "CANCEL", "KILL_SWITCH_STATE_UNKNOWN"),
    ([_row(kill_switch=None)], "PLACE", "KILL_SWITCH_STATE_UNKNOWN"),
    ([_row(live_trading="NOT_AUTHORIZED", live_reason="LIVE_TRADING_FLAG_EXPIRED")], "PLACE",
     "LIVE_TRADING_NOT_AUTHORIZED"),
    ([_row(live_trading=None)], "CANCEL", "LIVE_TRADING_NOT_AUTHORIZED"),
    ([_row(last_cycle_ok=False)], "PLACE", "SIGNER_LAST_CYCLE_FAILED"),
    ([_row(last_cycle_ok=1)], "PLACE", "SIGNER_LAST_CYCLE_FAILED"),
    ([_row()], "PLACE", None),
    ([_row(age_seconds=120)], "CANCEL", None),
])
def test_precheck_fails_closed(rows, kind, code):
    assert tm.precheck(rows, max_age_seconds=120, kind=kind) == code


# ---------------------------------------------------------------- tools: refused before anything is written
@pytest.fixture
def live(tmp_path, monkeypatch):
    """S7 environment (simulated mainnet, mainnet block lifted FOR THE TEST ONLY, as in the S7 tests)."""
    import bridge.sxbet.order_request_tools as ort
    monkeypatch.setattr(tm, "MAINNET_LIVE_TRADING_AUTHORIZED", True)
    s = Stack(tmp_path)
    monkeypatch.setattr(ort, "_wallclock", s.clock)
    q = MemoryQueue()
    t, w = s7.tools(s, q)
    return s, q, t, w


@pytest.mark.parametrize("rows, code", [
    ([_row(kill_switch="ENGAGED")], "KILL_SWITCH_ENGAGED"),
    ([_row(age_seconds=10_000.0)], "SIGNER_UNREACHABLE"),
    ([_row(live_trading="NOT_AUTHORIZED", live_reason="LIVE_TRADING_DISABLED")], "LIVE_TRADING_NOT_AUTHORIZED"),
    ([], "SIGNER_STATUS_MISSING"),
])
def test_place_and_cancel_are_refused_before_queue(live, rows, code):
    s, q, t, w = live
    w.signer_rows = rows
    text, _ = s7.owner_text(s, "order-key-P5A")
    out = s7.place(t, s, "order-key-P5A", text)
    assert out["status"] == "REFUSED_BEFORE_QUEUE" and out["error"]["code"] == code and out["queued"] is False
    assert out["request_with_this_key_exists"] is False and q.requests == []
    c = run(t["sxbet_order_request_cancel"](owner_command="Cancel all my open orders", scope="ALL",
                                            idempotency_key="cancel-key-P5A"))
    assert c["status"] == "REFUSED_BEFORE_QUEUE" and c["error"]["code"] == code and q.requests == []


def test_status_read_error_refuses_without_leaking(live):
    s, q, t, w = live

    def boom(network):
        raise ConnectionError("postgresql://user:secret@host/db")
    w.signer_status = boom
    text, _ = s7.owner_text(s, "order-key-P5B")
    out = s7.place(t, s, "order-key-P5B", text)
    assert out["error"]["code"] == "SIGNER_STATUS_UNAVAILABLE" and "secret" not in str(out) and q.requests == []


def test_healthy_signer_status_queues_as_before(live):
    s, q, t, w = live
    text, _ = s7.owner_text(s, "order-key-P5C")
    out = s7.place(t, s, "order-key-P5C", text)
    assert out["status"] == "QUEUED" and out["mcp_mode"] == "LIVE_TRADING" and len(q.requests) == 1


# ---------------------------------------------------------------- signer side: published status
def test_status_from_cycle_maps_fail_closed():
    from signer.status import status_from_cycle
    assert status_from_cycle({"kill_switch_engaged": False, "consecutive_failures": 0}, None) == {
        "kill_switch": "DISENGAGED", "live_trading": "AUTHORIZED", "live_reason": None, "last_cycle_ok": True}
    r = status_from_cycle({"kill_switch_engaged": "UNKNOWN", "consecutive_failures": 2}, "LIVE_TRADING_FLAG_EXPIRED")
    assert r == {"kill_switch": "UNKNOWN", "live_trading": "NOT_AUTHORIZED",
                 "live_reason": "LIVE_TRADING_FLAG_EXPIRED", "last_cycle_ok": False}
    assert status_from_cycle({}, None)["kill_switch"] == "UNKNOWN"


class _Store:
    def __init__(self, fail=False):
        self.rows, self.fail = [], fail

    def publish(self, signer_id, network, row):
        if self.fail:
            raise OSError("db down secret")
        self.rows.append((signer_id, network, row))


def test_publisher_without_gateway_or_with_a_broken_one_is_not_authorized():
    from signer.status import StatusPublisher

    class Broken:
        def live_status(self):
            raise RuntimeError("x")
    st = _Store()
    StatusPublisher(st, signer_id="sig", network="testnet")({"kill_switch_engaged": False, "consecutive_failures": 0})
    StatusPublisher(st, signer_id="sig", network="testnet", gateway=Broken())(
        {"kill_switch_engaged": False, "consecutive_failures": 0})
    assert [r[2]["live_reason"] for r in st.rows] == ["LIVE_TRADING_DISABLED", "LIVE_STATUS_UNREADABLE"]


def test_worker_publishes_status_and_survives_a_publish_failure(tmp_path):
    from signer.worker import SignerWorker
    import test_sxbet_signer_worker as wk
    sink_calls = []
    w = SignerWorker(wk.FakeService(), wk.FakeMonitor(), wk.FakeKill(), lock_path=str(tmp_path / "a"),
                     status_sink=sink_calls.append)
    assert w.run_cycle()["status_published"] is True and len(sink_calls) == 1

    def bad(report):
        raise OSError("secret dsn")
    w2 = SignerWorker(wk.FakeService(), wk.FakeMonitor(), wk.FakeKill(), lock_path=str(tmp_path / "b"),
                      status_sink=bad)
    r = w2.run_cycle()
    assert r["status_published"] == {"ok": False, "error_type": "OSError"} and r["consecutive_failures"] == 0
    assert "secret" not in str(r)


def test_end_to_end_signer_status_drives_the_mcp_refusal(tmp_path, monkeypatch):
    """Real gateway + kill switch publish the status; the MCP refuses once the owner engages the kill switch."""
    from signer import killswitch as ks
    from signer.audit import AuditLog
    from signer.monitor import Monitor, MonitorState
    from signer.service import SignerService
    from signer.status import StatusPublisher
    from signer.worker import SignerWorker
    import bridge.sxbet.order_request_tools as ort
    monkeypatch.setattr(tm, "MAINNET_LIVE_TRADING_AUTHORIZED", True)
    s = Stack(tmp_path)
    monkeypatch.setattr(ort, "_wallclock", s.clock)
    q = MemoryQueue()
    t, writer = s7.tools(s, q)
    store = _Store()
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    mon = Monitor(s.ledger, s.adapter, MonitorState.create(str(tmp_path / "m.json"), ledger_id=s.ledger.ledger_id),
                  clock=s.clock)
    kill = ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "k.jsonl")), owner_public_key=s.owner.public_key(),
                         network="mainnet", clock=s.clock)
    pub = StatusPublisher(store, signer_id=SIGNER_ID, network="mainnet", gateway=s.gw)
    with SignerWorker(svc, mon, kill, lock_path=str(tmp_path / "w.lock"), status_sink=pub) as w:
        w.run_cycle()
        assert store.rows[-1][2]["live_trading"] == "AUTHORIZED" and store.rows[-1][2]["kill_switch"] == "DISENGAGED"
        kill.engage("OWNER_TEST", source="MANUAL")
        w.run_cycle()
    published = store.rows[-1][2]
    assert published["kill_switch"] == "ENGAGED"
    writer.signer_rows = [{**HEALTHY, **published, "age_seconds": 1.0}]
    text, _ = s7.owner_text(s, "order-key-P5E")
    out = s7.place(t, s, "order-key-P5E", text)
    assert out["error"]["code"] == "KILL_SWITCH_ENGAGED" and q.requests == []


def test_gateway_live_status_is_none_only_for_a_valid_flag(tmp_path):
    s = Stack(tmp_path)
    assert s.gw.live_status() is None
    s.clock.t += 10 ** 8                                   # far past the flag's expiry
    assert s.gw.live_status() == "LIVE_TRADING_FLAG_EXPIRED"
