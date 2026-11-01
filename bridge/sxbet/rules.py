"""SX Bet rule engine: evaluates owner-supplied rules against position + market snapshots.

Pure and deterministic. The only outputs are NO_ACTION, ALERT and ORDER_PROPOSAL.
An ORDER_PROPOSAL is a description for the owner: ``executable``, ``signed`` and
``submitted`` are always False and it needs an out-of-band, signer-verified
approval of its exact parameters before anything real can happen (governance
§2.6). Rules arrive with each call; nothing is persisted and every rule is
disabled unless the call enables it. Thresholds are the caller's, not limits of
the exchange or of the owner's signer risk config (which the MCP cannot see).

Inputs that rules read and where they come from:
* ``mark_to_market``: positions.mark_to_market (HYBRID_MARKET_AWARE, top of book);
* ``worst_case_liability_base_units``: positions roll-up;
* market ``status`` (ACTIVE/INACTIVE) and ``gameTime`` (unix seconds) from GET /markets/find
  (api-reference Market schema); resting orders are swept at kickoff
  (developers/order-lifecycle, EVENT_LIFECYCLE);
* current taker prices per outcome derived from GET /orders-v3/odds/best
  (taker = 1e20 - best maker on the other outcome, developers/unit-conversions).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

NO_ACTION, ALERT, ORDER_PROPOSAL = "NO_ACTION", "ALERT", "ORDER_PROPOSAL"
SCHEMA = "sxbet.rules.v1"
_INT_MAX = 2 ** 128
RULES: dict[str, dict[str, str]] = {
    "stop_loss": {"max_loss_base_units": "pos_int"},
    "take_profit": {"min_profit_base_units": "pos_int"},
    "trailing_stop": {"trail_base_units": "pos_int", "peak_mark_to_market_base_units": "int"},
    "daily_loss_limit": {"max_daily_loss_base_units": "pos_int", "realized_pnl_today_base_units": "int"},
    "exposure_limit": {"max_worst_case_liability_base_units": "pos_int"},
    "close_on_odds_move": {"outcome": "outcome", "reference_taker_percentage_odds": "odds",
                           "max_move_percentage_odds": "pos_int"},
    "close_on_time_remaining": {"min_seconds_before_start": "pos_int"},
    "block_on_suspended_market": {},
    "block_on_stale_data": {"max_data_age_seconds": "pos_int"},
}
BLOCKERS = ("block_on_suspended_market", "block_on_stale_data")
DEFAULT_CONFIG: dict[str, dict[str, Any]] = {name: {"enabled": False} for name in RULES}


class RuleError(ValueError):
    """Fixed-code config error."""


def _check(kind: str, v: Any) -> bool:
    if kind == "pos_int":
        return type(v) is int and 0 < v < _INT_MAX
    if kind == "int":
        return type(v) is int and -_INT_MAX < v < _INT_MAX
    if kind == "outcome":
        return v in ("outcome_one", "outcome_two")
    if kind == "odds":
        return isinstance(v, str) and v.isdecimal() and v.isascii() and len(v) <= 21 and 0 < int(v) < 10 ** 20
    return False


def validate_config(config: Any) -> dict[str, dict[str, Any]]:
    """Return a full config (defaults = disabled). Unknown rule or field, or a bad value -> RuleError."""
    if config is None:
        return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    if not isinstance(config, dict):
        raise RuleError("INVALID_RULES_CONFIG")
    out = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    for name, rule in config.items():
        if name not in RULES:
            raise RuleError("UNKNOWN_RULE")
        if not isinstance(rule, dict) or not isinstance(rule.get("enabled", False), bool):
            raise RuleError("INVALID_RULE_" + name.upper())
        fields = RULES[name]
        if set(rule) - set(fields) - {"enabled"}:
            raise RuleError("UNKNOWN_FIELD_" + name.upper())
        if rule.get("enabled"):
            for f, kind in fields.items():
                if not _check(kind, rule.get(f)):
                    raise RuleError("INVALID_" + f.upper())
        out[name] = dict(rule, enabled=bool(rule.get("enabled", False)))
    return out


def proposal_hash(p: Mapping[str, Any]) -> str:
    raw = json.dumps(p, sort_keys=True, separators=(",", ":"), default=str).encode("ascii", "replace")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def evaluate(config: Any, *, market_position: Mapping[str, Any] | None, market: Mapping[str, Any] | None,
             now_unix: int, data_age_seconds: float | None, taker_percentage_odds: Mapping[str, Any] | None = None,
             account_worst_case_liability_base_units: int | None = None,
             hedge: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate every rule. `hedge` is a FULL hedge simulation used as the closing proposal, if any."""
    cfg = validate_config(config)
    mp = market_position or {}
    mtm = (mp.get("mark_to_market") or {}).get("value_base_units")
    liability = mp.get("worst_case_liability_base_units")
    open_stake = (mp.get("matched_stake_base_units") or {}).get("total")
    has_position = bool(open_stake) or bool(liability)
    status = (market or {}).get("status")
    game_time = (market or {}).get("gameTime")
    prices = taker_percentage_odds or {}
    results: list[dict] = []

    def res(name, result, reason, **inputs):
        results.append({"rule": name, "enabled": cfg[name]["enabled"], "result": result, "reason": reason,
                        "inputs": inputs})

    for name in RULES:
        r = cfg[name]
        if not r["enabled"]:
            res(name, NO_ACTION, "RULE_DISABLED")
            continue
        if name == "stop_loss":
            if mtm is None:
                res(name, ALERT, "MARK_TO_MARKET_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                hit = mtm <= -r["max_loss_base_units"]
                res(name, ORDER_PROPOSAL if hit else NO_ACTION, "STOP_LOSS_HIT" if hit else "WITHIN_THRESHOLD",
                    mark_to_market_base_units=mtm, max_loss_base_units=r["max_loss_base_units"])
        elif name == "take_profit":
            if mtm is None:
                res(name, ALERT, "MARK_TO_MARKET_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                hit = mtm >= r["min_profit_base_units"]
                res(name, ORDER_PROPOSAL if hit else NO_ACTION, "TAKE_PROFIT_HIT" if hit else "BELOW_TARGET",
                    mark_to_market_base_units=mtm, min_profit_base_units=r["min_profit_base_units"])
        elif name == "trailing_stop":
            if mtm is None:
                res(name, ALERT, "MARK_TO_MARKET_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                peak = max(r["peak_mark_to_market_base_units"], mtm)
                hit = mtm <= peak - r["trail_base_units"]
                res(name, ORDER_PROPOSAL if hit else NO_ACTION, "TRAILING_STOP_HIT" if hit else "WITHIN_TRAIL",
                    mark_to_market_base_units=mtm, peak_used_base_units=peak,
                    next_peak_to_pass_base_units=peak, trail_base_units=r["trail_base_units"])
        elif name == "daily_loss_limit":
            if mtm is None:
                res(name, ALERT, "MARK_TO_MARKET_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                day = r["realized_pnl_today_base_units"] + mtm
                hit = day <= -r["max_daily_loss_base_units"]
                res(name, ALERT if hit else NO_ACTION, "DAILY_LOSS_LIMIT_REACHED_NO_NEW_EXPOSURE" if hit else "WITHIN_LIMIT",
                    day_pnl_base_units=day, max_daily_loss_base_units=r["max_daily_loss_base_units"])
        elif name == "exposure_limit":
            exposure = account_worst_case_liability_base_units if account_worst_case_liability_base_units is not None else liability
            if exposure is None:
                res(name, ALERT, "EXPOSURE_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                hit = exposure > r["max_worst_case_liability_base_units"]
                res(name, ALERT if hit else NO_ACTION, "EXPOSURE_ABOVE_LIMIT" if hit else "WITHIN_LIMIT",
                    worst_case_liability_base_units=exposure,
                    max_worst_case_liability_base_units=r["max_worst_case_liability_base_units"])
        elif name == "close_on_odds_move":
            cur = prices.get(r["outcome"])
            if type(cur) is not int:
                res(name, ALERT, "CURRENT_PRICE_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                move = abs(cur - int(r["reference_taker_percentage_odds"]))
                hit = has_position and move >= r["max_move_percentage_odds"]
                res(name, ORDER_PROPOSAL if hit else NO_ACTION,
                    "ODDS_MOVED" if hit else ("NO_OPEN_POSITION" if not has_position else "WITHIN_MOVE"),
                    current_taker_percentage_odds=str(cur), move_percentage_odds=str(move))
        elif name == "close_on_time_remaining":
            if not isinstance(game_time, (int, float)) or isinstance(game_time, bool):
                res(name, ALERT, "GAME_TIME_UNAVAILABLE_CANNOT_EVALUATE")
            else:
                left = int(game_time) - now_unix
                hit = has_position and left <= r["min_seconds_before_start"]
                res(name, ORDER_PROPOSAL if hit else NO_ACTION,
                    "TIME_THRESHOLD_REACHED" if hit else ("NO_OPEN_POSITION" if not has_position else "TIME_REMAINING"),
                    seconds_to_game_time=left)
        elif name == "block_on_suspended_market":
            hit = status != "ACTIVE"
            res(name, ALERT if hit else NO_ACTION, "MARKET_NOT_ACTIVE_PROPOSALS_BLOCKED" if hit else "MARKET_ACTIVE",
                provider_market_status=status)
        elif name == "block_on_stale_data":
            hit = data_age_seconds is None or data_age_seconds > r["max_data_age_seconds"]
            res(name, ALERT if hit else NO_ACTION, "STALE_DATA_PROPOSALS_BLOCKED" if hit else "DATA_FRESH",
                data_age_seconds=data_age_seconds)

    blocked = [x["rule"] for x in results if x["rule"] in BLOCKERS and x["result"] == ALERT]
    triggers = [x["rule"] for x in results if x["result"] == ORDER_PROPOSAL]
    proposal = None
    if triggers:
        hstat = (hedge or {}).get("status")
        params = (hedge or {}).get("order_parameters_for_owner_review")
        reason = None
        if blocked:
            reason = "BLOCKED_BY_" + "_AND_".join(b.upper() for b in blocked)
        elif hstat not in ("HEDGE_AVAILABLE", "PARTIAL_HEDGE_ONLY") or not params:
            reason = "NO_EXECUTABLE_HEDGE_" + str(hstat or "NOT_SIMULATED")
        if reason:
            for x in results:
                if x["result"] == ORDER_PROPOSAL:
                    x["result"], x["reason"] = ALERT, x["reason"] + "_BUT_" + reason
        else:
            body = {"kind": "CLOSE_POSITION_PROPOSAL", "triggered_by": triggers,
                    "market_hash": mp.get("market_hash"), "hedge_status": hstat,
                    "order_parameters": {k: v for k, v in params.items() if k != "note"}}
            proposal = {**body, "proposal_hash": proposal_hash(body), "executable": False, "signed": False,
                        "submitted": False, "requires_owner_out_of_band_approval": True,
                        "note": "A proposal is not an order. Only an owner-approved, signer-verified order with these "
                                "exact parameters could ever be sent; any change needs a new approval."}
    decision = ORDER_PROPOSAL if proposal else (ALERT if any(x["result"] == ALERT for x in results) else NO_ACTION)
    return {"schema": SCHEMA, "label": "DERIVED", "decision": decision, "results": results,
            "proposal": proposal, "blocked_by": blocked,
            "enabled_rules": [k for k, v in cfg.items() if v["enabled"]],
            "persistence": "NONE: rules are evaluated per call and never stored"}
