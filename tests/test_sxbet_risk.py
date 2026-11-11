"""Signer risk engine v2: every limit, missing/stale data, owner-signed config. Offline, throwaway keys.

Intentional change (risk config v2, owner decisions D1 / #187): schema `sxbet.risk_config.v2` with separate
mainnet / testnet blocks; v1 files are refused (all-zero). Removed v1 limits: `session_loss_limit` (replaced by
the daily loss limit that also counts the worst case of open positions) and `slippage_tolerance_bps` (the
owner's command now fixes the limit price itself: no slippage allowance can make the signed price worse).
"""
import json

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import risk  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.core import KEY_ENV, Signer, SignerConfig, SignerConfigError, SignerRefused  # noqa: E402
from signer.orders import OrderLedger  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, ESCROW, GENEROUS, SIGNER_ID, TOKEN, pub, NOW, Clock, context,  # noqa: E402
                              make_intent, make_signer, owner_command, signer_config,
                              write_risk_config)
from sxbet_signer_kit import sign_with_reads  # noqa: E402


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


def attempt(s, owner, i, ctx=None, **kw):
    return refused(lambda: sign_with_reads(s, i, owner_command(i, **kw), context(i) if ctx is None else ctx))


def load(path, owner, network="mainnet"):
    return risk.load_risk_config(path, owner.public_key(), network=network)


# ---------------------------------------------------------------- config loading (owner-signed only)
def test_default_and_unsigned_and_invalid_configs_are_all_zero(tmp_path, owner):
    assert load(None, owner).limits == risk.RiskLimits()
    assert not risk.RiskLimits().configured()  # safe default: zero live exposure
    assert load(str(tmp_path / "missing.json"), owner).source == "MISSING"
    unsigned = write_risk_config(tmp_path / "u.json", owner, signed=False)
    assert load(unsigned, owner) == risk.LoadedRiskConfig(risk.RiskLimits(), "UNSIGNED_IGNORED", "mainnet")
    wrong = write_risk_config(tmp_path / "w.json", owner, signer_key=Ed25519PrivateKey.generate())
    assert load(wrong, owner).source == "SIGNATURE_INVALID"
    p = tmp_path / "t.json"
    doc = json.loads(open(write_risk_config(p, owner)).read())
    doc["networks"]["mainnet"]["max_stake_per_order_base_units"] *= 100  # owner signed a smaller value
    p.write_text(json.dumps(doc))
    assert load(str(p), owner).limits == risk.RiskLimits()
    bads = [{**GENEROUS, "extra": 1}, {**GENEROUS, "max_open_orders": -1}, {**GENEROUS, "max_open_orders": "10"},
            {**GENEROUS, "allowed_sport_ids": "SOME"}, {**GENEROUS, "allowed_sport_ids": [1, "2"]},
            {**GENEROUS, "min_decimal_odds": 1.2}, {**GENEROUS, "pregame_only": 1},
            {**GENEROUS, "market_time_in_force": "GTC"}, {**GENEROUS, "limit_time_in_force": "IOC"},
            {**GENEROUS, "command_max_age_seconds": 301}]
    for bad in bads:
        write_risk_config(p, owner, networks={"mainnet": bad})
        assert load(str(p), owner).source == "MALFORMED", bad
    for bad in ([1], "x", {"limits": GENEROUS}):
        p.write_text(json.dumps(bad))
        assert load(str(p), owner).source == "MALFORMED"
    p.write_text("{not json")
    assert load(str(p), owner).source == "MALFORMED"
    ok = write_risk_config(tmp_path / "ok.json", owner)
    assert load(ok, owner).source == "OWNER_SIGNED" and load(ok, owner).limits.configured()


def test_v1_config_is_refused(tmp_path, owner):
    """Intentional non-backward-compatible change: a correctly signed v1 file yields all-zero limits."""
    v1 = {"max_stake_per_order_base_units": 1}
    msg = json.dumps({"domain": "sxbet.risk_config.v1", "limits": v1}, sort_keys=True, separators=(",", ":"))
    (tmp_path / "v1.json").write_text(json.dumps({"limits": v1, "signature": owner.sign(msg.encode()).hex()}))
    got = load(str(tmp_path / "v1.json"), owner)
    assert got.source == "MALFORMED" and got.limits == risk.RiskLimits()


def test_testnet_and_mainnet_values_are_separate(tmp_path, throwaway, owner):
    p = write_risk_config(tmp_path / "n.json", owner, networks={
        "testnet": {**GENEROUS, "max_stake_per_order_base_units": 3_000_000}})
    assert load(p, owner, "testnet").limits.max_stake_per_order_base_units == 3_000_000
    main = load(p, owner, "mainnet")
    assert main.source == "NETWORK_NOT_CONFIGURED" and main.limits == risk.RiskLimits()
    s = make_signer(tmp_path, throwaway, owner, risk_config=main)
    i = make_intent(throwaway[0].address)
    assert attempt(s, owner, i) == "EXPOSURE_LIMIT_ZERO"
    # Testnet limits never feed a mainnet signer.
    with pytest.raises(SignerConfigError, match="RISK_CONFIG_NETWORK_MISMATCH"):
        make_signer(tmp_path, throwaway, owner, risk_config=load(p, owner, "testnet"))
    assert load(p, owner, "devnet").source == "MALFORMED"


def test_config_loaded_from_env_path(tmp_path, throwaway, owner):
    path = write_risk_config(tmp_path / "env.json", owner)
    led = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    cfg = signer_config()
    s = Signer(cfg, owner_public_key=pub(owner), ledger=led, audit=AuditLog(str(tmp_path / "a")),
               env={KEY_ENV: throwaway[1], risk.RISK_CONFIG_ENV: path}, clock=Clock())
    assert s.risk_config_source == "OWNER_SIGNED"
    i = make_intent(throwaway[0].address)
    assert sign_with_reads(s, i, owner_command(i), context(i))["cached"] is False


def test_limits_cannot_be_changed_through_intent_or_approval(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_stake_per_order_base_units": 1_000_000})
    i = make_intent(throwaway[0].address)
    with pytest.raises(Exception):
        s._risk.limits.max_stake_per_order_base_units = 10 ** 12  # frozen
    # Extra intent / context fields never reach the limits.
    assert refused(lambda: sign_with_reads(s, {**i, "max_stake": "9" * 20}, owner_command(i), context(i))) \
        == "INTENT_KEYS_MISMATCH"
    ctx = {**context(i), "limits": {"max_stake_per_order_base_units": 10 ** 12}}
    assert refused(lambda: sign_with_reads(s, i, owner_command(i), ctx)) == "STAKE_ABOVE_LIMIT"


_POSITIVE = sorted(f for f in risk.LIMIT_FIELDS if f in risk._INT_FIELDS and f != "min_seconds_between_orders")


@pytest.mark.parametrize("field", _POSITIVE)
def test_any_single_zero_limit_disables_everything(tmp_path, throwaway, owner, field):
    s = make_signer(tmp_path, throwaway, owner, limits={field: 0})
    i = make_intent(throwaway[0].address)
    assert attempt(s, owner, i) == "EXPOSURE_LIMIT_ZERO"


@pytest.mark.parametrize("lo,hi", [("0", "10"), ("1", "10"), ("3", "2")])
def test_invalid_odds_band_disables_everything(tmp_path, throwaway, owner, lo, hi):
    s = make_signer(tmp_path, throwaway, owner, limits={"min_decimal_odds": lo, "max_decimal_odds": hi})
    assert attempt(s, owner, make_intent(throwaway[0].address)) == "EXPOSURE_LIMIT_ZERO"


# ---------------------------------------------------------------- every limit
def test_max_stake_per_order(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_stake_per_order_base_units": 4_999_999})
    assert attempt(s, owner, make_intent(throwaway[0].address)) == "STAKE_ABOVE_LIMIT"


def _sign(s, owner, i, **kw):
    return sign_with_reads(s, i, owner_command(i, **kw), context(i, now=kw.get("received_at") or NOW))


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
    """v2: no session limit; the daily loss limit counts realised loss AND the worst case of open orders."""
    s = make_signer(tmp_path, throwaway, owner, limits={"max_daily_loss_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-S001"))  # 5 open
    assert attempt(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-S002")) \
        == "DAILY_LOSS_LIMIT"  # 0 realised + 5 open worst case + 5 > 9
    _settle_loss(s, "order-key-S001", 5_000_000)
    i = make_intent(throwaway[0].address, idempotency_key="order-key-S003", stake_base_units=4_000_000)
    assert _sign(s, owner, i)["cached"] is False  # 5 realised + 0 open + 4 = 9 <= 9


def test_daily_loss_limit_counts_today_only(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock, limits={"max_daily_loss_base_units": 9_000_000})
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-D001"))
    _settle_loss(s, "order-key-D001", 5_000_000)
    # A restarted signer still sees today's loss (rebuilt from the ledger).
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits={"max_daily_loss_base_units": 9_000_000})
    assert attempt(r, owner, make_intent(throwaway[0].address, idempotency_key="order-key-D002")) == "DAILY_LOSS_LIMIT"
    # Next UTC day: yesterday's loss no longer counts.
    clock.t = NOW + 86_400
    nxt = make_signer(tmp_path, throwaway, owner, clock=clock, limits={"max_daily_loss_base_units": 9_000_000})
    i = make_intent(throwaway[0].address, idempotency_key="order-key-D003", now=clock.t, expiry=clock.t + 3600)
    assert sign_with_reads(nxt, i, owner_command(i, received_at=clock.t), context(i, now=clock.t))["cached"] is False


def test_max_stake_per_utc_day_rebuilt_after_restart(tmp_path, throwaway, owner):
    clock = Clock()
    lim = {"max_stake_per_utc_day_base_units": 12_000_000}
    s = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-Y001"))
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-Y002"))
    s._ledger.transition("order-key-Y001", "REJECTED", reason="EXCHANGE_REJECTED")  # still counts (conservative)
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)  # counters rebuilt from the ledger
    assert attempt(r, owner, make_intent(throwaway[0].address, idempotency_key="order-key-Y003")) \
        == "DAILY_STAKE_ABOVE_LIMIT"
    clock.t = NOW + 86_400
    r2 = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    i = make_intent(throwaway[0].address, idempotency_key="order-key-Y004", now=clock.t, expiry=clock.t + 3600)
    assert sign_with_reads(r2, i, owner_command(i, received_at=clock.t), context(i, now=clock.t))["cached"] is False


def test_max_orders_per_hour_and_day_rebuilt_after_restart(tmp_path, throwaway, owner):
    clock = Clock()
    lim = {"max_orders_per_hour": 2, "max_orders_per_utc_day": 3}
    s = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    for n in (1, 2):
        _sign(s, owner, make_intent(throwaway[0].address, idempotency_key=f"order-key-H00{n}",
                                    stake_base_units=1_000_000))
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    assert attempt(r, owner, make_intent(throwaway[0].address, idempotency_key="order-key-H003",
                                         stake_base_units=1_000_000)) == "ORDERS_PER_HOUR_ABOVE_LIMIT"
    clock.t = NOW + 3601
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    i = make_intent(throwaway[0].address, idempotency_key="order-key-H004", stake_base_units=1_000_000,
                    now=clock.t, expiry=clock.t + 3600)
    sign_with_reads(r, i, owner_command(i, received_at=clock.t), context(i, now=clock.t))
    clock.t = NOW + 7300
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    j = make_intent(throwaway[0].address, idempotency_key="order-key-H005", stake_base_units=1_000_000,
                    now=clock.t, expiry=clock.t + 3600)
    assert refused(lambda: sign_with_reads(r, j, owner_command(j, received_at=clock.t), context(j, now=clock.t))) \
        == "ORDERS_PER_DAY_ABOVE_LIMIT"


def test_min_seconds_between_orders_rebuilt_after_restart(tmp_path, throwaway, owner):
    clock = Clock()
    lim = {"min_seconds_between_orders": 30}
    s = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    _sign(s, owner, make_intent(throwaway[0].address, idempotency_key="order-key-P001"))
    clock.t = NOW + 29
    r = make_signer(tmp_path, throwaway, owner, clock=clock, limits=lim)
    i = make_intent(throwaway[0].address, idempotency_key="order-key-P002")
    assert refused(lambda: sign_with_reads(r, i, owner_command(i, received_at=clock.t), context(i, now=clock.t))) \
        == "ORDER_PACE_TOO_FAST"
    clock.t = NOW + 30
    assert sign_with_reads(r, i, owner_command(i, received_at=clock.t), context(i, now=clock.t))["cached"] is False


@pytest.mark.parametrize("lo,hi,ok", [("1.01", "1000", True), ("2.6", "1000", False), ("1.01", "2.4", False),
                                      ("2.5", "2.5", True)])
def test_min_max_acceptable_odds(tmp_path, throwaway, owner, lo, hi, ok):
    s = make_signer(tmp_path, throwaway, owner, limits={"min_decimal_odds": lo, "max_decimal_odds": hi})
    i = make_intent(throwaway[0].address)  # signed price 40% = decimal odds 2.5
    if ok:
        _sign(s, owner, i)
    else:
        assert attempt(s, owner, i) == "ODDS_OUTSIDE_OWNER_BAND"


def test_odds_moved_beyond_slippage(tmp_path, throwaway, owner):
    """v2: no slippage allowance exists. The signed price is exactly the owner's limit rounded on the ladder
    in the owner's favour; one tick worse is a command mismatch, a better book price is irrelevant."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    worse = make_intent(throwaway[0].address, requested_percentage_odds=40 * 10 ** 18 + 125 * 10 ** 15)
    assert refused(lambda: sign_with_reads(s, worse, owner_command(i), context(worse))) == "COMMAND_MISMATCH_ODDS"
    c = context(i)
    c["quote"] = {**c["quote"], "best_percentage_odds": str(38 * 10 ** 18)}  # the book moved in our favour
    out = sign_with_reads(s, i, owner_command(i), c)
    assert out["order"]["percentageOdds"] == str(40 * 10 ** 18)


def test_zero_slippage_tolerance_refuses_any_adverse_tick(tmp_path, throwaway, owner):
    """v2: the smallest adverse change of the signed price (one ladder tick) is refused."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, requested_percentage_odds=40 * 10 ** 18 + 125 * 10 ** 15)
    assert refused(lambda: sign_with_reads(s, i, owner_command(i, odds_text="2,5"), context(i))) == "COMMAND_MISMATCH_ODDS"


# ---------------------------------------------------------------- owner policy: allowlists, pre-match
@pytest.mark.parametrize("field,row_key,code", [("allowed_sport_ids", "sportId", "SPORT"),
                                                ("allowed_league_ids", "leagueId", "LEAGUE"),
                                                ("allowed_market_types", "type", "MARKET_TYPE")])
def test_allowlists_are_owner_policy(tmp_path, throwaway, owner, field, row_key, code):
    i = make_intent(throwaway[0].address)
    value = context(i)["market"][row_key]
    s = make_signer(tmp_path, throwaway, owner, limits={field: [value + 1]})
    assert attempt(s, owner, i) == code + "_NOT_ALLOWED"
    s = make_signer(tmp_path, throwaway, owner, limits={field: []})  # empty list allows nothing
    assert attempt(s, owner, i) == code + "_NOT_ALLOWED"
    s = make_signer(tmp_path, throwaway, owner, limits={field: [value]})
    c = context(i)
    del c["market"][row_key]
    assert attempt(s, owner, i, c) == code + "_MISSING"
    assert sign_with_reads(s, i, owner_command(i), context(i))["cached"] is False
    # "ANY" (the default here) does not even need the field.
    s2 = make_signer(tmp_path, throwaway, owner)
    j = make_intent(throwaway[0].address, idempotency_key="order-key-AL02")
    c2 = context(j)
    del c2["market"][row_key]
    assert sign_with_reads(s2, j, owner_command(j), c2)["cached"] is False


def test_pregame_only(tmp_path, throwaway, owner):
    i = make_intent(throwaway[0].address)
    s = make_signer(tmp_path, throwaway, owner)
    c = context(i)
    c["market"]["gameTime"] = NOW
    assert attempt(s, owner, i, c) == "PREGAME_ONLY_EVENT_STARTED"
    c["market"]["gameTime"] = None
    assert attempt(s, owner, i, c) == "GAME_TIME_MISSING"
    live = make_signer(tmp_path, throwaway, owner, limits={"pregame_only": False})
    c["market"]["gameTime"] = NOW - 600
    assert sign_with_reads(live, i, owner_command(i), c)["cached"] is False


# ---------------------------------------------------------------- runtime exchange metadata (never hardcoded)
@pytest.mark.parametrize("tif,minimum,resting,code", [
    ("GTC", "5000001", "1", "STAKE_BELOW_EXCHANGE_MINIMUM"),
    ("FOK", "5000001", "1", "STAKE_BELOW_EXCHANGE_MINIMUM"),
    ("GTC", "1", "5000001", "STAKE_BELOW_MIN_RESTING_SIZE"),
    ("FOK", "1", "5000001", None),                     # min resting size applies to resting (GTC) orders only
])
def test_min_order_size_from_runtime_metadata(tmp_path, throwaway, owner, tif, minimum, resting, code):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, time_in_force=tif)
    c = context(i)
    c["exchange_limits"] = {**c["exchange_limits"], "order_size_minimum_base_units": minimum,
                            "min_resting_order_size_base_units": resting}
    if code is None:
        assert sign_with_reads(s, i, owner_command(i), c)["cached"] is False
    else:
        assert attempt(s, owner, i, c) == code


# ---------------------------------------------------------------- missing / stale / unverifiable data
@pytest.mark.parametrize("key,code", [("market", "MARKET_DATA_MISSING"), ("quote", "QUOTE_MISSING"),
                                      ("balance", "BALANCE_MISSING"), ("fees", "FEES_MISSING_OR_MALFORMED"),
                                      ("market_observed_at", "MARKET_DATA_MISSING"),
                                      ("exchange_limits", "EXCHANGE_LIMITS_MISSING"),
                                      ("event_markets", "EVENT_MARKETS_MISSING")])
def test_missing_context_items_fail_closed(tmp_path, throwaway, owner, key, code):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    c = context(i)
    del c[key]
    assert attempt(s, owner, i, c) == code


def test_no_context_at_all_fails_closed(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    assert refused(lambda: sign_with_reads(s, i, owner_command(i))) == "RISK_CONTEXT_MISSING"


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c["quote"].update(observed_at=NOW - 31), "QUOTE_STALE"),
    (lambda c: c["quote"].update(observed_at=NOW + 60), "QUOTE_FROM_FUTURE"),
    (lambda c: c["quote"].pop("observed_at"), "QUOTE_MISSING"),
    (lambda c: c.update(market_observed_at=NOW - 31), "MARKET_DATA_STALE"),
    (lambda c: c["balance"].update(observed_at=NOW - 31), "BALANCE_STALE"),
    (lambda c: c["exchange_limits"].update(observed_at=NOW - 31), "EXCHANGE_LIMITS_STALE"),
    (lambda c: c["exchange_limits"].update(order_size_minimum_base_units=None), "EXCHANGE_LIMITS_MISSING"),
    # intentional (review #196 P1-2): decimals come from the signer's owner config; an exchange value that
    # differs is a refusal (was EXCHANGE_LIMITS_MISSING when the decimals came from the caller)
    (lambda c: c["exchange_limits"].update(base_token_decimals="6"), "TOKEN_DECIMALS_DIFFER_FROM_EXCHANGE"),
    (lambda c: c["quote"].update(best_percentage_odds=None), "QUOTE_PRICE_MISSING"),
    (lambda c: c["quote"].update(available_liquidity_base_units=None), "LIQUIDITY_MISSING"),
    (lambda c: c["quote"].update(outcome=2), "QUOTE_SIDE_MISMATCH"),
    (lambda c: c["balance"].update(available_base_units="-1"), "BALANCE_MISSING"),
    (lambda c: c["market"].update(status=None), "MARKET_STATUS_MISSING"),
    (lambda c: c["market"].update(marketHash="0x" + "cd" * 32), "MARKET_MISMATCH"),
    # intentional: was APPROVAL_BINDING_MISMATCH_EVENT_ID (no approval binding any more)
    (lambda c: c["market"].update(sportXeventId=None), "COMMAND_EVENT_UNVERIFIABLE"),
    (lambda c: c["fees"].update(takerPayoutFee=0.03), "FEES_MISSING_OR_MALFORMED"),  # number, not a string
    (lambda c: c.update(event_markets=[]), "EVENT_MARKETS_INCOMPLETE"),
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
    assert sign_with_reads(s, nxt, owner_command(nxt), c)["cached"] is False


@pytest.mark.parametrize("tif,liquidity,ok", [("FOK", "4999999", False), ("IOC", "4999999", False),
                                              ("FOK", "5000000", True), ("GTC", "0", True)])
def test_insufficient_liquidity_for_taking_orders(tmp_path, throwaway, owner, tif, liquidity, ok):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, time_in_force=tif)
    c = context(i)
    c["quote"] = {**c["quote"], "available_liquidity_base_units": liquidity}
    if ok:
        assert sign_with_reads(s, i, owner_command(i), c)["cached"] is False
    else:
        assert attempt(s, owner, i, c) == "INSUFFICIENT_LIQUIDITY"


def test_risk_refusal_never_writes_the_ledger(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    c = context(i)
    c["market"] = {**c["market"], "status": "INACTIVE"}
    attempt(s, owner, i, c)
    assert [json.loads(l)["kind"] for l in (tmp_path / "ledger.jsonl").read_text().splitlines()] == ["GENESIS"]
