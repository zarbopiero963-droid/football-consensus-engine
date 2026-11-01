"""SX Bet rule engine (bridge/sxbet/rules.py): pure, offline; proposals are never executable."""
import pytest

from bridge.sxbet import rules as rl

M = "0x" + "11" * 32
NOW = 1_800_000_000
P = 10 ** 18
HEDGE_OK = {"status": "HEDGE_AVAILABLE", "order_parameters_for_owner_review": {
    "executable": False, "signed": False, "submitted": False, "requires_owner_out_of_band_approval": True,
    "market_hash": M, "betting_outcome_one": False, "stake_base_units": "212", "percentage_odds_limit": str(45 * P),
    "note": "x"}}


def pos(mtm=-500, liability=200, stake=200):
    return {"market_hash": M, "mark_to_market": {"value_base_units": mtm}, "worst_case_liability_base_units": liability,
            "matched_stake_base_units": {"total": stake}}


def ev(config, *, mtm=-500, market=None, age=1.0, prices=None, hedge=HEDGE_OK, account=None, liability=200, stake=200):
    return rl.evaluate(config, market_position=pos(mtm, liability, stake),
                       market=market if market is not None else {"status": "ACTIVE", "gameTime": NOW + 3600},
                       now_unix=NOW, data_age_seconds=age, taker_percentage_odds=prices or {},
                       account_worst_case_liability_base_units=account, hedge=hedge)


def test_all_rules_disabled_by_default():
    out = ev(None)
    assert out["decision"] == "NO_ACTION" and out["enabled_rules"] == [] and out["proposal"] is None
    assert {r["reason"] for r in out["results"]} == {"RULE_DISABLED"} and len(out["results"]) == len(rl.RULES)
    assert all(not v["enabled"] for v in rl.DEFAULT_CONFIG.values())
    assert out["persistence"].startswith("NONE")


def test_stop_loss_produces_a_non_executable_proposal():
    out = ev({"stop_loss": {"enabled": True, "max_loss_base_units": 400}})
    assert out["decision"] == "ORDER_PROPOSAL"
    p = out["proposal"]
    assert p["executable"] is False and p["signed"] is False and p["submitted"] is False
    assert p["requires_owner_out_of_band_approval"] is True and p["triggered_by"] == ["stop_loss"]
    assert p["order_parameters"]["stake_base_units"] == "212" and p["proposal_hash"].startswith("sha256:")
    assert ev({"stop_loss": {"enabled": True, "max_loss_base_units": 600}})["decision"] == "NO_ACTION"


def test_take_profit_and_trailing_stop():
    assert ev({"take_profit": {"enabled": True, "min_profit_base_units": 100}}, mtm=150)["decision"] == "ORDER_PROPOSAL"
    assert ev({"take_profit": {"enabled": True, "min_profit_base_units": 100}}, mtm=50)["decision"] == "NO_ACTION"
    cfg = {"trailing_stop": {"enabled": True, "trail_base_units": 100, "peak_mark_to_market_base_units": 400}}
    hit = ev(cfg, mtm=290)
    assert hit["decision"] == "ORDER_PROPOSAL"
    assert ev(cfg, mtm=350)["decision"] == "NO_ACTION"
    higher = ev(cfg, mtm=500)  # a new peak is reported for the caller to pass next time (not persisted)
    assert higher["results"][2]["inputs"]["next_peak_to_pass_base_units"] == 500


def test_daily_loss_and_exposure_limits_alert_only():
    d = ev({"daily_loss_limit": {"enabled": True, "max_daily_loss_base_units": 600,
                                 "realized_pnl_today_base_units": -200}}, mtm=-500)
    assert d["decision"] == "ALERT" and d["proposal"] is None
    e = ev({"exposure_limit": {"enabled": True, "max_worst_case_liability_base_units": 1000}}, account=5000)
    assert e["decision"] == "ALERT" and "EXPOSURE_ABOVE_LIMIT" in [r["reason"] for r in e["results"]]
    assert ev({"exposure_limit": {"enabled": True, "max_worst_case_liability_base_units": 1000}})["decision"] == "NO_ACTION"


def test_close_on_odds_move_and_time_remaining():
    cfg = {"close_on_odds_move": {"enabled": True, "outcome": "outcome_one",
                                  "reference_taker_percentage_odds": str(50 * P), "max_move_percentage_odds": 5 * P}}
    assert ev(cfg, prices={"outcome_one": 56 * P})["decision"] == "ORDER_PROPOSAL"
    assert ev(cfg, prices={"outcome_one": 52 * P})["decision"] == "NO_ACTION"
    assert ev(cfg, prices={"outcome_one": None})["decision"] == "ALERT"
    t = {"close_on_time_remaining": {"enabled": True, "min_seconds_before_start": 7200}}
    assert ev(t)["decision"] == "ORDER_PROPOSAL"
    assert ev({"close_on_time_remaining": {"enabled": True, "min_seconds_before_start": 60}})["decision"] == "NO_ACTION"
    assert ev(t, liability=0, stake=0)["results"][6]["reason"] == "NO_OPEN_POSITION"


def test_blockers_turn_proposals_into_alerts():
    cfg = {"stop_loss": {"enabled": True, "max_loss_base_units": 1}, "block_on_suspended_market": {"enabled": True}}
    out = ev(cfg, market={"status": "INACTIVE", "gameTime": NOW})
    assert out["decision"] == "ALERT" and out["proposal"] is None and out["blocked_by"] == ["block_on_suspended_market"]
    assert "BLOCKED_BY_BLOCK_ON_SUSPENDED_MARKET" in out["results"][0]["reason"]
    stale = {"stop_loss": {"enabled": True, "max_loss_base_units": 1},
             "block_on_stale_data": {"enabled": True, "max_data_age_seconds": 5}}
    assert ev(stale, age=30)["decision"] == "ALERT" and ev(stale, age=None)["proposal"] is None
    assert ev(stale, age=1)["decision"] == "ORDER_PROPOSAL"


def test_no_proposal_without_an_executable_hedge_simulation():
    cfg = {"stop_loss": {"enabled": True, "max_loss_base_units": 1}}
    for hedge in (None, {"status": "HEDGE_UNAVAILABLE"}, {"status": "HEDGE_UNCERTAIN",
                                                           "order_parameters_for_owner_review": {"stake_base_units": "1"}}):
        out = ev(cfg, hedge=hedge)
        assert out["decision"] == "ALERT" and out["proposal"] is None


def test_missing_mark_to_market_alerts_instead_of_guessing():
    out = ev({"stop_loss": {"enabled": True, "max_loss_base_units": 1}}, mtm=None)
    assert out["decision"] == "ALERT" and out["results"][0]["reason"] == "MARK_TO_MARKET_UNAVAILABLE_CANNOT_EVALUATE"


@pytest.mark.parametrize("config,code", [
    ("x", "INVALID_RULES_CONFIG"),
    ({"martingale": {"enabled": True}}, "UNKNOWN_RULE"),
    ({"stop_loss": {"enabled": "yes"}}, "INVALID_RULE_STOP_LOSS"),
    ({"stop_loss": {"enabled": True}}, "INVALID_MAX_LOSS_BASE_UNITS"),
    ({"stop_loss": {"enabled": True, "max_loss_base_units": -5}}, "INVALID_MAX_LOSS_BASE_UNITS"),
    ({"stop_loss": {"enabled": True, "max_loss_base_units": 5, "auto_submit": True}}, "UNKNOWN_FIELD_STOP_LOSS"),
    ({"close_on_odds_move": {"enabled": True, "outcome": "draw", "reference_taker_percentage_odds": "1",
                             "max_move_percentage_odds": 1}}, "INVALID_OUTCOME"),
])
def test_invalid_config_is_rejected(config, code):
    with pytest.raises(rl.RuleError, match=code):
        rl.validate_config(config)


def test_proposal_hash_is_deterministic():
    cfg = {"stop_loss": {"enabled": True, "max_loss_base_units": 1}}
    assert ev(cfg)["proposal"]["proposal_hash"] == ev(cfg)["proposal"]["proposal_hash"]
