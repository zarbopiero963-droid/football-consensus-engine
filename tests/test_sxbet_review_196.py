"""Regression tests for the adversarial review of PR #196 (verdict UNSAFE). Offline, throwaway keys.

The reviewer's eight probes (A-H) demonstrated bypasses; here each one is adopted as a regression test that
asserts the REFUSAL. RED before the fix (on c128134 the probes succeeded), GREEN after.
"""
import json
import threading
import time

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import cancellation as cx  # noqa: E402
from signer import command as cm  # noqa: E402
from signer import gateway as gw  # noqa: E402
from signer import risk as rk  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.core import Signer, SignerRefused  # noqa: E402
from signer.killswitch import KillSwitch  # noqa: E402
from signer.orders import OrderLedger  # noqa: E402
from sxbet_e2e_kit import P, USDC, Stack  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, EVENT_ID, H, KEY_ENV, NOW, SIGNER_ID, Clock, command_text,  # noqa: E402
                              context, event_of, load_limits, make_intent, make_signer, owner_command, pub,
                              sign_with_reads, signer_config)


@pytest.fixture
def throwaway():
    a = Account.create()
    return a, a.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def refused(fn, exc=SignerRefused):
    with pytest.raises(exc) as e:
        fn()
    return e.value.code


# ---------------------------------------------------------------- P1-1: the signer's OWN reads only
def test_B_caller_cannot_supply_the_market_row(tmp_path, throwaway, owner):
    """Probe B: a caller-supplied market row bound the command to any hash. There is no context argument any
    more, and the signer's own reads decide which market the command names."""
    s = make_signer(tmp_path, throwaway, owner)
    other = "0x" + "ee" * 32
    i = make_intent(throwaway[0].address.lower(), market_hash=other, time_in_force="FOK")
    with pytest.raises(TypeError):
        s.sign_order(i, owner_command(i), context(i))             # the old call shape is gone
    # The exchange (the signer's own provider) knows market H, not a disguised `other`.
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), context())) == "MARKET_MISMATCH"
    # `other` exists but is a different market (Over/Under) of the same event: the command names Home FC.
    world = context()
    ou = {**world["market"], "marketHash": other, "outcomeOneName": "Over 2.5", "outcomeTwoName": "Under 2.5",
          "line": 2.5, "type": 2}
    world["event_markets"] = [world["market"], ou]
    world["market"] = ou
    # The Over/Under row has a line the moneyline command does not state: refused (never signed on `other`).
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), world)) == "COMMAND_INCOMPLETE_LINE"


def test_no_signer_owned_provider_means_no_authorization(tmp_path, throwaway, owner):
    acct, key = throwaway
    led = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=acct.address)
    s = Signer(signer_config(market_data=None), owner_public_key=pub(owner), ledger=led,
               audit=AuditLog(str(tmp_path / "a")), risk_config=load_limits(tmp_path, owner),
               env={KEY_ENV: key}, clock=Clock())
    i = make_intent(acct.address)
    assert refused(lambda: s.sign_order(i, owner_command(i))) == "SIGNER_MARKET_READS_UNAVAILABLE"

    class Broken:
        def read(self, **kw):
            raise TimeoutError("exchange down")

    s.config.__dict__["market_data"] = Broken()   # frozen dataclass: test-only swap of the configured provider
    assert refused(lambda: s.sign_order(i, owner_command(i))) == "SIGNER_MARKET_READS_FAILED"
    assert [json.loads(x)["kind"] for x in (tmp_path / "l").read_text().splitlines()] == ["GENESIS"]


def test_provider_reads_are_keyed_by_the_order_being_signed(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, time_in_force="FOK")
    sign_with_reads(s, i, owner_command(i), context(i))
    (call,) = s.config.market_data.calls
    assert call == (H, True, int(i["percentage_odds"]), NOW)


# ---------------------------------------------------------------- P1-2: token decimals / symbol from owner config
def test_A_caller_decimals_cannot_inflate_the_stake(tmp_path, throwaway, owner):
    """Probe A: the owner wrote 1 USDC; decimals from the exchange data (7) made it 10 USDC at 6 decimals."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address.lower(), stake_base_units=10_000_000, time_in_force="FOK")
    txt = command_text(i).replace("10 USDC", "1 USDC")
    ctx = context(i)
    ctx["exchange_limits"] = {**ctx["exchange_limits"], "base_token_decimals": 7}
    assert refused(lambda: sign_with_reads(s, i, owner_command(text=txt), ctx)) == \
        "TOKEN_DECIMALS_DIFFER_FROM_EXCHANGE"
    del ctx["exchange_limits"]["base_token_decimals"]   # without the cross-check value: config decimals (6)
    assert refused(lambda: sign_with_reads(s, i, owner_command(text=txt), ctx)) == "COMMAND_MISMATCH_STAKE"


def test_token_config_is_required(tmp_path, throwaway, owner):
    acct, key = throwaway
    led = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=acct.address)
    import dataclasses
    s = Signer(dataclasses.replace(signer_config(), base_token_decimals=None), owner_public_key=pub(owner), ledger=led, audit=AuditLog(str(tmp_path / "a")),
               risk_config=load_limits(tmp_path, owner), env={KEY_ENV: key}, clock=Clock())
    i = make_intent(acct.address)
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), context(i))) == "SIGNER_TOKEN_CONFIG_MISSING"


# ---------------------------------------------------------------- P1-3: cross-event ambiguity
def test_C_two_active_events_with_the_same_teams_are_ambiguous(tmp_path, throwaway, owner):
    """Probe C: another active event with the same team names (second leg, U21 / Women listed with identical
    names): the command's teams do not identify one event => refused unless it names the event id."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address.lower(), time_in_force="FOK")
    ctx = context(i)
    ctx["same_team_events"] = [event_of(ctx["market"]),
                               {**event_of(ctx["market"]), "sportXeventId": "L_OTHER_EVENT_SAME_TEAMS",
                                "gameTime": NOW + 7 * 86_400}]
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), ctx)) == "COMMAND_AMBIGUOUS_EVENT"
    by_id = f"Piazza 5 USDC su Home FC in evento {EVENT_ID} a quota minima 2,5 FOK"
    assert sign_with_reads(s, i, owner_command(text=by_id), ctx)["cached"] is False


def test_cross_event_matching_is_exact_never_containment(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address.lower(), time_in_force="FOK")
    ctx = context(i)
    ctx["same_team_events"] = [event_of(ctx["market"]),
                               {**event_of(ctx["market"]), "sportXeventId": "L_WOMEN",
                                "teamOneName": "Home FC Women", "teamTwoName": "Away FC Women"}]
    assert sign_with_reads(s, i, owner_command(i), ctx)["cached"] is False   # "Home FC" != "Home FC Women"


@pytest.mark.parametrize("events,code", [(None, "SAME_TEAM_EVENTS_MISSING"), ([], "SAME_TEAM_EVENTS_INCOMPLETE"),
                                         ([{"sportXeventId": "L_X", "teamOneName": None}],
                                          "SAME_TEAM_EVENTS_INCOMPLETE")])
def test_same_team_event_reads_fail_closed(tmp_path, throwaway, owner, events, code):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    ctx = context(i)
    ctx["same_team_events"] = events
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), ctx)) == code


def test_unreadable_same_team_event_fails_closed(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    ctx = context(i)
    ctx["same_team_events"] = [event_of(ctx["market"]), {"sportXeventId": "L_X", "teamOneName": None}]
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), ctx)) == "COMMAND_AMBIGUOUS_EVENT"


# ---------------------------------------------------------------- P2-1: kill switch race
def test_F_kill_switch_engaged_during_verification_refuses(tmp_path, throwaway, owner):
    """Probe F: engaged after the signer's first check but before AUTHORIZE: the order was still signed."""
    s = make_signer(tmp_path, throwaway, owner)
    ks = KillSwitch(s._ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=owner.public_key(),
                    network="mainnet", clock=Clock())
    i = make_intent(throwaway[0].address.lower(), time_in_force="FOK")
    orig = s._verify_command

    def hook(*a, **k):
        r = orig(*a, **k)
        ks.engage("MANUAL_STOP")
        return r

    s._verify_command = hook
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), context(i))) == "KILL_SWITCH_ENGAGED"
    assert s._ledger.get(i["idempotency_key"]) is None


def test_kill_switch_engaged_after_authorize_never_signs(tmp_path, throwaway, owner, monkeypatch):
    s = make_signer(tmp_path, throwaway, owner)
    ks = KillSwitch(s._ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=owner.public_key(),
                    network="mainnet", clock=Clock())
    i = make_intent(throwaway[0].address.lower(), time_in_force="FOK")
    real = s._ledger.authorize
    seen = {}

    def authorize_then_engage(**kw):
        o = real(**kw)
        seen["intents"] = ks.engage("MANUAL_STOP")["cancel_intents"]
        return o

    monkeypatch.setattr(s._ledger, "authorize", authorize_then_engage)
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), context(i))) == "KILL_SWITCH_ENGAGED"
    o = s._ledger.get(i["idempotency_key"])
    assert o.state == "REJECTED" and not o.signature_released
    assert o.order_id in seen["intents"]                     # the snapshot covers the AUTHORIZED order


def test_engage_and_intent_snapshot_are_atomic_with_authorize(tmp_path, throwaway, owner):
    """ENGAGE + CANCEL_INTENT happen under one hold of the ledger lock: a concurrent AUTHORIZE is either fully
    before (and in the intents) or refused."""
    s = make_signer(tmp_path, throwaway, owner)
    ks = KillSwitch(s._ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=owner.public_key(),
                    network="mainnet", clock=Clock())
    intents = [make_intent(throwaway[0].address, idempotency_key=f"order-key-RACE{n:02d}",
                           stake_base_units=1_000_000) for n in range(6)]
    barrier = threading.Barrier(len(intents) + 1)
    results = {}

    def signer_worker(i):
        barrier.wait()
        try:
            results[i["idempotency_key"]] = sign_with_reads(s, i, owner_command(i), context(i))["order_id"]
        except SignerRefused as e:
            results[i["idempotency_key"]] = e.code

    out = {}

    def engager():
        barrier.wait()
        time.sleep(0.001)
        out["rep"] = ks.engage("MANUAL_STOP")

    ts = [threading.Thread(target=signer_worker, args=(i,)) for i in intents] + [threading.Thread(target=engager)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    signed = {v for v in results.values() if v.startswith("0x")}
    assert signed <= set(out["rep"]["cancel_intents"])       # every signature that exists is a cancel intent
    assert all(v == "KILL_SWITCH_ENGAGED" for v in results.values() if not v.startswith("0x"))


# ---------------------------------------------------------------- P2-2: replay fingerprint of the parsed command
def test_D_trivial_variants_of_a_consumed_command_are_replays(tmp_path, throwaway, owner):
    """Probe D: a trailing comma, or dropping an explicit FOK that equals the owner-signed default, gave a
    'new' command. The fingerprint is over the parsed, resolved operation."""
    s = make_signer(tmp_path, throwaway, owner)
    i1 = make_intent(throwaway[0].address.lower(), time_in_force="FOK", idempotency_key="k-000000001")
    t1 = command_text(i1, ref=False)
    sign_with_reads(s, i1, owner_command(text=t1), context(i1))
    i2 = make_intent(throwaway[0].address.lower(), time_in_force="FOK", idempotency_key="k-000000002")
    for variant in (t1 + ",", t1.replace(" FOK", ""), t1.replace("Piazza", "piazza").replace("2,5", "2,50"),
                    t1.replace("Home FC–Away FC", "home fc vs away fc")):
        assert refused(lambda: sign_with_reads(s, i2, owner_command(text=variant), context(i2))) == \
            "COMMAND_REPLAYED", variant
    # A deliberate repetition carries a NEW explicit reference token (owner decision 2).
    assert sign_with_reads(s, i2, owner_command(text=t1 + " (rif. 2)"), context(i2))["cached"] is False
    i3 = make_intent(throwaway[0].address.lower(), time_in_force="FOK", idempotency_key="k-000000003")
    assert refused(lambda: sign_with_reads(s, i3, owner_command(text=t1 + " (RIF. 2)"), context(i3))) == \
        "COMMAND_REPLAYED"


def test_E_cancel_all_repeatable_only_with_a_new_reference(tmp_path):
    """Probe E: the same cancel-all text was consumed for ever. A repeated risk-reducing cancel-all needs a new
    reference token; the identical command (however spelled) is a replay."""
    s = Stack(tmp_path)
    s.place("order-key-E001")

    def cancel_all(text):
        auth = cm.OwnerCommandAuthorization(f"cancel-all-{abs(hash(text)) % 10 ** 12:012d}-x", text,
                                            cm.text_sha256(text), int(s.clock()), cm.text_sha256("c"))
        return s.cx.cancel_all(auth)

    assert cancel_all("Cancella tutti gli ordini")["sent"] is True
    assert refused(lambda: cancel_all("cancella tutti gli ordini."), cx.CancelRefused) == "COMMAND_REPLAYED"
    assert refused(lambda: cancel_all("Cancel all orders"), cx.CancelRefused) == "COMMAND_REPLAYED"
    assert cancel_all("Cancella tutti gli ordini (rif. 2)")["sent"] is True


# ---------------------------------------------------------------- P3: grammar numbers
def test_G_non_ascii_digits_refused():
    for t in ("Piazza ١٠ USDC su Over 2,5 in A vs B a quota minima 1,90",
              "Piazza １０ USDC su Over 2,5 in A vs B a quota minima 1,90"):
        with pytest.raises(cm.CommandRefused) as e:
            cm.parse(t)
        assert e.value.code == "COMMAND_NON_ASCII_DIGITS"


@pytest.mark.parametrize("text,code", [
    ("Piazza 10 USDC su Over 2,5 in A vs B a quota minima 2,000", "COMMAND_AMBIGUOUS_ODDS"),
    ("Metti un Limit a quota 1.125 per 10 USDC su Over 2,5 in A vs B", "COMMAND_AMBIGUOUS_ODDS"),
    ("Piazza 10 USDC su Over 2,500 in A vs B a quota minima 1,9", "COMMAND_AMBIGUOUS_LINE"),
    ("Piazza 10 USDC su A -1,250 in A vs B a quota minima 1,9", "COMMAND_AMBIGUOUS_LINE"),
])
def test_H_three_decimal_numbers_are_ambiguous(text, code):
    with pytest.raises(cm.CommandRefused) as e:
        cm.parse(text)
    assert e.value.code == code
    assert cm.parse(text.replace("2,000", "2,0000").replace("1.125", "1.1250").replace("2,500", "2,5")
                    .replace("-1,250", "-1,25")).operation == "PLACE"


# ---------------------------------------------------------------- P2-3: post-only per order
def test_post_only_is_bound_per_order_in_the_submitted_request(tmp_path):
    s = Stack(tmp_path)
    s.place("order-key-PO01", stake=2 * USDC, odds=30 * P, post_only=True)
    s.place("order-key-PO02", stake=2 * USDC, odds=30 * P)
    posts = [r for r in s.sim.writes() if r.method == "POST"]
    flags = {o["clientOrderId"]: o.get("postOnly", False) for r in posts for o in r.body["orders"]}
    assert flags == {"order-key-PO01": True, "order-key-PO02": False}
    assert s.ledger.get("order-key-PO01").post_only and not s.ledger.get("order-key-PO02").post_only
    rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [r.get("post_only") for r in rows if r["kind"] == "AUTHORIZE"] == [True, False]


# ---------------------------------------------------------------- P2-5: a new ledger file is never live
def test_live_flag_is_bound_to_the_ledger_file(tmp_path):
    s = Stack(tmp_path)
    old_flag = s.live_flag
    s.ledger.close()
    (tmp_path / "ledger.jsonl").rename(tmp_path / "ledger.old")
    fresh = OrderLedger.create(str(tmp_path / "ledger.jsonl"), signer_id=SIGNER_ID, signer_address=s.acct.address,
                               clock=s.clock)                 # consumed ids, kill switch, counters: all empty
    assert fresh.genesis_sha256 != s.ledger.genesis_sha256
    with pytest.raises(gw.GatewayDisabled, match="LIVE_TRADING_FLAG_NOT_FOR_THIS_LEDGER"):
        gw.SubmissionGateway(old_flag, owner_public_key=s.owner.public_key(), transport=s.adapter, ledger=fresh,
                             audit=AuditLog(str(tmp_path / "g.jsonl")), odds_ladder_step_size=125,
                             network="mainnet", clock=s.clock)
    fresh.close()


def test_genesis_identity_survives_restart(tmp_path):
    s = Stack(tmp_path)
    g = s.ledger.genesis_sha256
    s.restart()
    assert s.ledger.genesis_sha256 == g and s.gw.submit is not None


# ---------------------------------------------------------------- P3: cancellation window, volatile, clock
def test_cancellation_window_comes_from_the_owner_signed_config(tmp_path):
    s = Stack(tmp_path)
    with pytest.raises(cx.CancelRefused, match="COMMAND_WINDOW_NOT_CONFIGURED"):
        cx.CancellationService(s.gw, account_id=ACCOUNT_ID, risk_limits=rk.RiskLimits())
    with pytest.raises(TypeError):
        cx.CancellationService(s.gw, account_id=ACCOUNT_ID, command_max_age_seconds=60)
    assert s.cx._max_age == s.signer._risk.limits.command_max_age_seconds


def test_volatile_kill_switch_blocks_the_gateway(tmp_path):
    s = Stack(tmp_path)
    i = s.intent("order-key-V001")
    sg = s.sign(i)
    s.ledger.engage_volatile_kill("LEDGER_ANCHOR_FORK")
    assert refused(lambda: s.gw.submit(sg, i), gw.GatewayRefused) == "KILL_SWITCH_ENGAGED"
    assert refused(lambda: s.sign(s.intent("order-key-V002"))) == "KILL_SWITCH_ENGAGED"


def test_host_clock_jump_stops_signing(tmp_path, throwaway, owner):
    acct, key = throwaway
    led = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=acct.address)
    s = Signer(signer_config(), owner_public_key=pub(owner), ledger=led, audit=AuditLog(str(tmp_path / "a")),
               risk_config=load_limits(tmp_path, owner), env={KEY_ENV: key})   # real clock
    now = int(time.time())
    i = make_intent(acct.address, now=now, expiry=now + 3600)
    s._wall0 -= 3600                                          # the wall clock moved one hour vs monotonic
    assert refused(lambda: sign_with_reads(s, i, owner_command(i, received_at=now), context(i, now=now))) == \
        "SIGNER_CLOCK_JUMP_DETECTED"
