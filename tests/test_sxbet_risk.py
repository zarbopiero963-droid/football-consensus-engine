"""Signer risk engine: every limit, missing/stale data, owner-signed config. Offline, throwaway keys."""
import json

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import risk  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.core import KEY_ENV, Signer, SignerConfig, SignerRefused  # noqa: E402
from signer.orders import OrderLedger  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, ESCROW, GENEROUS, SIGNER_ID, TOKEN, pub, NOW, Clock, approve, context, make_intent, make_signer,  # noqa: E402
                              write_risk_config)


@pytest.fixture
def throwaway():
    acct = Account.create()
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def refused(fn):
    with pytest.raises(SignerRefused) as e:
        fn()
    return e.value.code


def attempt(s, owner, i, ctx=None, **binding):
    return refused(lambda: s.sign_order(i, approve(owner, i, **binding), context(i) if ctx is None else ctx))


# ---------------------------------------------------------------- config loading (owner-signed only)
def test_default_and_unsigned_and_invalid_configs_are_all_zero(tmp_path, owner):
    pk = owner.public_key()
    assert risk.load_risk_config(None, pk).limits == risk.RiskLimits()
    assert risk.load_risk_config(str(tmp_path / "missing.json"), pk).source == "MISSING"
    unsigned = write_risk_config(tmp_path / "u.json", owner, signed=False)
    assert risk.load_risk_config(unsigned, pk) == risk.LoadedRiskConfig(risk.RiskLimits(), "UNSIGNED_IGNORED")
    wrong = write_risk_config(tmp_path / "w.json", owner, signer_key=Ed25519PrivateKey.generate())
    assert risk.load_risk_config(wrong, pk).source == "SIGNATURE_INVALID"
    p = tmp_path / "t.json"
    doc = json.loads(open(write_risk_config(p, owner)).read())
    doc["limits"]["max_stake_per_order_base_units"] *= 100  # owner signed a smaller value
    p.write_text(json.dumps(doc))
    assert risk.load_risk_config(str(p), pk).limits == risk.RiskLimits()
    for bad in ({"limits": {**GENEROUS, "extra": 1}}, {"limits": {**GENEROUS, "max_open_orders": -1}},
                {"limits": {**GENEROUS, "max_open_orders": "10"}}, [1], "x"):
        p.write_text(json.dumps(bad))
        assert risk.load_risk_config(str(p), pk).source == "MALFORMED"
    p.write_text("{not json")
    assert risk.load_risk_config(str(p), pk).source == "MALFORMED"
    ok = write_risk_config(tmp_path / "ok.json", owner)
    assert risk.load_risk_config(ok, pk).source == "OWNER_SIGNED"
    assert risk.load_risk_config(ok, pk).limits.configured()


def test_config_loaded_from_env_path(tmp_path, throwaway, owner):
    path = write_risk_config(tmp_path / "env.json", owner)
    led = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    cfg = SignerConfig(SIGNER_ID, "mainnet", 4162, ESCROW, TOKEN, 125, account_id=ACCOUNT_ID)
    s = Signer(cfg, owner_public_key=pub(owner), ledger=led, audit=AuditLog(str(tmp_path / "a")),
               env={KEY_ENV: throwaway[1], risk.RISK_CONFIG_ENV: path}, clock=Clock())
    assert s.risk_config_source == "OWNER_SIGNED"
    i = make_intent(throwaway[0].address)
    assert s.sign_order(i, approve(owner, i), context(i))["cached"] is False


def test_limits_cannot_be_changed_through_intent_or_approval(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_stake_per_order_base_units": 1_000_000})
    i = make_intent(throwaway[0].address)
    with pytest.raises(Exception):
        s._risk.limits.max_stake_per_order_base_units = 10 ** 12  # frozen
    # Extra intent / context fields never reach the limits.
    assert refused(lambda: s.sign_order({**i, "max_stake": "9" * 20}, approve(owner, i), context(i))) \
        == "INTENT_KEYS_MISMATCH"
    ctx = {**context(i), "limits": {"max_stake_per_order_base_units": 10 ** 12}}
    assert refused(lambda: s.sign_order(i, approve(owner, i), ctx)) == "STAKE_ABOVE_LIMIT"


@pytest.mark.parametrize("field", sorted(risk.LIMIT_FIELDS - {"slippage_tolerance_bps"}))
def test_any_single_zero_limit_disables_everything(tmp_path, throwaway, owner, field):
    s = make_signer(tmp_path, throwaway, owner, limits={field: 0})
    i = make_intent(throwaway[0].address)
    assert attempt(s, owner, i) == "EXPOSURE_LIMIT_ZERO"


# ---------------------------------------------------------------- every limit
def test_max_stake_per_order(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_stake_per_order_base_units": 4_999_999})
    assert attempt(s, owner, make_intent(throwaway[0].address)) == "STAKE_ABOVE_LIMIT"


def _sign(s, owner, i):
    return s.sign_order(i, approve(owner, i), context(i))


def test_max_exposure_per_market(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_exposure_per_market_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-M001"))
    assert attempt(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-M002")) \
        == "MARKET_EXPOSURE_ABOVE_LIMIT"
    # Another market (same event) is fine.
    other = make_intent(throwaway[0].address, idempotency_key="order-key-M003", market_hash="0x" + "cd" * 32)
    _sign(s, owner, other)


def test_max_exposure_per_event(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_exposure_per_event_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-E001"))
    other = make_intent(throwaway[0].address, idempotency_key="order-key-E002", market_hash="0x" + "cd" * 32)
    assert attempt(s, owner, other) == "EVENT_EXPOSURE_ABOVE_LIMIT"


def test_max_total_exposure(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_total_exposure_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-X001"))
    assert attempt(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-X002")) \
        == "EXPOSURE_ABOVE_LIMIT"


def test_max_open_orders(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_open_orders": 1})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-O001"))
    assert attempt(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-O002")) \
        == "OPEN_ORDERS_ABOVE_LIMIT"


def _settle_loss(s, key, loss):
    led = s._ledger
    led.transition(key, "UNKNOWN", reason="SEND_ATTEMPT")
    led.transition(key, "SUBMITTED", reason="EXCHANGE_SUBMITTED")
    led.transition(key, "FILLED", reason="EXCHANGE_ORDER_INACTIVE", filled=loss)
    led.transition(key, "SETTLED", reason="EXCHANGE_FILLS_SETTLED", realized_pnl=-loss)


def test_session_loss_limit(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"session_loss_limit_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-S001"))
    _settle_loss(s, "order-key-S001", 5_000_000)
    assert attempt(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-S002")) \
        == "SESSION_LOSS_LIMIT"


def test_daily_loss_limit_counts_today_only(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock, limits={"daily_loss_limit_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-D001"))
    _settle_loss(s, "order-key-D001", 5_000_000)
    # A restarted signer (new session) still sees today's loss.
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits={"daily_loss_limit_base_units": 9_000_000})
    assert attempt(r, owner, make_intent(throwaway[0].address, idempotency_key="order-key-D002")) == "DAILY_LOSS_LIMIT"
    # Next UTC day: yesterday's loss no longer counts (the fresh signer has a fresh session too).
    clock.t = NOW + 86_400
    nxt = make_signer(tmp_path, throwaway, owner, clock=clock, limits={"daily_loss_limit_base_units": 9_000_000})
    i = make_intent(throwaway[0].address, idempotency_key="order-key-D003", now=clock.t, expiry=clock.t + 3600)
    a = approve(owner, i, issued=clock.t)
    assert nxt.sign_order(i, a, context(i, now=clock.t))["cached"] is False


@pytest.mark.parametrize("lo,hi,ok", [(10 ** 17, 99 * 10 ** 18, True), (41 * 10 ** 18, 99 * 10 ** 18, False),
                                      (10 ** 17, 39 * 10 ** 18, False)])
def test_min_max_acceptable_odds(tmp_path, throwaway, owner, lo, hi, ok):
    s = make_signer(tmp_path, throwaway, owner, limits={"min_percentage_odds": lo, "max_percentage_odds": hi})
    i = make_intent(throwaway[0].address)
    if ok:
        _sign(s, owner, i)
    else:
        assert attempt(s, owner, i) == "ODDS_OUTSIDE_OWNER_BAND"


def test_odds_moved_beyond_slippage(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"slippage_tolerance_bps": 100})  # 1 %
    i = make_intent(throwaway[0].address)
    quoted = 40 * 10 ** 18

    def ctx(best):
        c = context(i)
        c["quote"] = {**c["quote"], "best_percentage_odds": str(best)}
        return c

    assert attempt(s, owner, i, ctx(quoted * 102 // 100)) == "ODDS_MOVED_BEYOND_SLIPPAGE"  # 2 % worse
    # A favourable move or one inside tolerance is accepted.
    assert s.sign_order(i, approve(owner, i), ctx(quoted * 1005 // 1000))["cached"] is False


def test_zero_slippage_tolerance_refuses_any_adverse_tick(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"slippage_tolerance_bps": 0})
    i = make_intent(throwaway[0].address)
    c = context(i)
    c["quote"] = {**c["quote"], "best_percentage_odds": str(40 * 10 ** 18 + 1)}
    assert attempt(s, owner, i, c) == "ODDS_MOVED_BEYOND_SLIPPAGE"


# ---------------------------------------------------------------- missing / stale / unverifiable data
@pytest.mark.parametrize("key,code", [("market", "MARKET_DATA_MISSING"), ("quote", "QUOTE_MISSING"),
                                      ("balance", "BALANCE_MISSING"), ("fees", "FEES_MISSING_OR_MALFORMED"),
                                      ("market_observed_at", "MARKET_DATA_MISSING")])
def test_missing_context_items_fail_closed(tmp_path, throwaway, owner, key, code):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    c = context(i)
    del c[key]
    assert attempt(s, owner, i, c) == code


def test_no_context_at_all_fails_closed(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    assert refused(lambda: s.sign_order(i, approve(owner, i))) == "RISK_CONTEXT_MISSING"


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c["quote"].update(observed_at=NOW - 31), "QUOTE_STALE"),
    (lambda c: c["quote"].update(observed_at=NOW + 60), "QUOTE_FROM_FUTURE"),
    (lambda c: c["quote"].pop("observed_at"), "QUOTE_MISSING"),
    (lambda c: c.update(market_observed_at=NOW - 31), "MARKET_DATA_STALE"),
    (lambda c: c["balance"].update(observed_at=NOW - 31), "BALANCE_STALE"),
    (lambda c: c["quote"].update(best_percentage_odds=None), "QUOTE_PRICE_MISSING"),
    (lambda c: c["quote"].update(available_liquidity_base_units=None), "LIQUIDITY_MISSING"),
    (lambda c: c["quote"].update(outcome=2), "QUOTE_SIDE_MISMATCH"),
    (lambda c: c["balance"].update(available_base_units="-1"), "BALANCE_MISSING"),
    (lambda c: c["market"].update(status=None), "MARKET_STATUS_MISSING"),
    (lambda c: c["market"].update(marketHash="0x" + "cd" * 32), "MARKET_MISMATCH"),
    (lambda c: c["market"].update(sportXeventId=None), "APPROVAL_BINDING_MISMATCH_EVENT_ID"),
    (lambda c: c["fees"].update(takerPayoutFee=0.03), "FEES_MISSING_OR_MALFORMED"),  # number, not a string
    (lambda c: c["fees"].update(takerPayoutFee="0.04"), "FEES_CHANGED_SINCE_APPROVAL"),
])
def test_stale_or_unverifiable_data(tmp_path, throwaway, owner, mutate, code):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    c = json.loads(json.dumps(context(i)))
    mutate(c)
    assert attempt(s, owner, i, c) == code


@pytest.mark.parametrize("status", ["INACTIVE", "SUSPENDED", "PAUSED", "SETTLED"])
def test_suspended_or_inactive_market(tmp_path, throwaway, owner, status):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    c = context(i)
    c["market"] = {**c["market"], "status": status}
    assert attempt(s, owner, i, c) == "MARKET_NOT_ACTIVE"


def test_insufficient_balance_counts_other_unmatched_reservations(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    first = make_intent(throwaway[0].address, idempotency_key="order-key-B001")
    _sign(s, owner, first)
    nxt = make_intent(throwaway[0].address, idempotency_key="order-key-B002")
    c = context(nxt)
    c["balance"] = {"available_base_units": "9999999", "observed_at": NOW}  # < 5M reserved + 5M new
    assert attempt(s, owner, nxt, c) == "INSUFFICIENT_BALANCE"
    c["balance"]["available_base_units"] = "10000000"
    assert s.sign_order(nxt, approve(owner, nxt), c)["cached"] is False


@pytest.mark.parametrize("tif,liquidity,ok", [("FOK", "4999999", False), ("IOC", "4999999", False),
                                              ("FOK", "5000000", True), ("GTC", "0", True)])
def test_insufficient_liquidity_for_taking_orders(tmp_path, throwaway, owner, tif, liquidity, ok):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, time_in_force=tif)
    c = context(i)
    c["quote"] = {**c["quote"], "available_liquidity_base_units": liquidity}
    if ok:
        assert s.sign_order(i, approve(owner, i), c)["cached"] is False
    else:
        assert attempt(s, owner, i, c) == "INSUFFICIENT_LIQUIDITY"


def test_risk_refusal_never_writes_the_ledger(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    c = context(i)
    c["market"] = {**c["market"], "status": "INACTIVE"}
    attempt(s, owner, i, c)
    assert [json.loads(l)["kind"] for l in (tmp_path / "ledger.jsonl").read_text().splitlines()] == ["GENESIS"]
