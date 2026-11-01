"""Authoritative pre-signature risk engine, enforced by the signer (governance §2.6).

Limits come ONLY from an owner-controlled JSON file (path in env
SXBET_SIGNER_RISK_CONFIG) signed with the owner's Ed25519 key. A missing,
unsigned, malformed or invalidly signed file yields the all-zero default, and
all-zero refuses every order (EXPOSURE_LIMIT_ZERO). Nothing in an intent, an
approval or a tool argument can change a limit: `RiskLimits` is frozen, loaded
once at signer start, and `evaluate` only reads it.

Config file:
    {"limits": {<every RiskLimits field>: int, ...}, "signature": "<hex Ed25519>"}
signed message = canonical JSON {"domain": "sxbet.risk_config.v1", "limits": {...}}.

Risk context (evidence the caller read from the exchange; every item is required,
missing or stale => refuse):
    market:  GET /markets/find row  {marketHash, status, sportXeventId, outcomeOneName, outcomeTwoName}
    market_observed_at: unix seconds
    quote:   {outcome: 1|2, best_percentage_odds: str, available_liquidity_base_units: str, observed_at}
             best taker-perspective price for the order's side (GET /orderbook-v3 ... showTakerPerspective)
    balance: {available_base_units: str, observed_at}   from GET /user/balance-v3 availableAmount
    fees:    GET /user/fees-v3 `data` snapshot (must equal the approval's expected_fees)
The signer cannot authenticate this evidence itself (it has no network access by
design); freshness, completeness and cross-checks are enforced here, provenance
is a documented remaining gap.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import approval as appr
from .orders import ExposureView, utc_day_start

RISK_CONFIG_ENV = "SXBET_SIGNER_RISK_CONFIG"
RISK_CONFIG_DOMAIN = "sxbet.risk_config.v1"
CLOCK_SKEW_SECONDS = 5
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_HEX_SIG = re.compile(r"^[0-9a-f]{128}$")


class RiskRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RiskLimits:
    """Owner-set. All zero = disabled = every order refused."""
    max_stake_per_order_base_units: int = 0
    max_exposure_per_market_base_units: int = 0
    max_exposure_per_event_base_units: int = 0
    max_total_exposure_base_units: int = 0
    session_loss_limit_base_units: int = 0
    daily_loss_limit_base_units: int = 0
    max_open_orders: int = 0
    slippage_tolerance_bps: int = 0          # 0 = no adverse move tolerated
    min_percentage_odds: int = 0
    max_percentage_odds: int = 0
    max_quote_age_seconds: int = 0

    def configured(self) -> bool:
        return all(getattr(self, f.name) > 0 for f in fields(self) if f.name != "slippage_tolerance_bps")


LIMIT_FIELDS = frozenset(f.name for f in fields(RiskLimits))


@dataclass(frozen=True)
class LoadedRiskConfig:
    limits: RiskLimits
    source: str  # OWNER_SIGNED | MISSING | UNSIGNED_IGNORED | SIGNATURE_INVALID | MALFORMED


def risk_config_message(limits: Mapping[str, Any]) -> bytes:
    return json.dumps({"domain": RISK_CONFIG_DOMAIN, "limits": dict(limits)}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def load_risk_config(path: str | None, owner_public_key: Ed25519PublicKey) -> LoadedRiskConfig:
    """Never raises: any problem yields the all-zero default with a fixed `source` code."""
    zero = RiskLimits()
    if not path:
        return LoadedRiskConfig(zero, "MISSING")
    try:
        with open(path, encoding="ascii") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return LoadedRiskConfig(zero, "MISSING")
    except (OSError, ValueError):
        return LoadedRiskConfig(zero, "MALFORMED")
    if not isinstance(doc, dict) or not isinstance(doc.get("limits"), dict):
        return LoadedRiskConfig(zero, "MALFORMED")
    raw = doc["limits"]
    if set(raw) != LIMIT_FIELDS or any(type(v) is not int or v < 0 for v in raw.values()):
        return LoadedRiskConfig(zero, "MALFORMED")
    sig = doc.get("signature")
    if sig is None:
        return LoadedRiskConfig(zero, "UNSIGNED_IGNORED")
    if set(doc) != {"limits", "signature"} or not isinstance(sig, str) or not _HEX_SIG.fullmatch(sig):
        return LoadedRiskConfig(zero, "MALFORMED")
    try:
        owner_public_key.verify(bytes.fromhex(sig), risk_config_message(raw))
    except InvalidSignature:
        return LoadedRiskConfig(zero, "SIGNATURE_INVALID")
    return LoadedRiskConfig(RiskLimits(**raw), "OWNER_SIGNED")


def load_from_env(env: Mapping[str, str] | None, owner_public_key: Ed25519PublicKey) -> LoadedRiskConfig:
    return load_risk_config((os.environ if env is None else env).get(RISK_CONFIG_ENV), owner_public_key)


def _dec(v: Any) -> int | None:
    return int(v) if isinstance(v, str) and _DECIMAL.fullmatch(v) else None


def _fresh(observed_at: Any, now: int, max_age: int, code: str) -> None:
    if type(observed_at) is not int:
        raise RiskRefused(code + "_MISSING")
    if observed_at > now + CLOCK_SKEW_SECONDS:
        raise RiskRefused(code + "_FROM_FUTURE")
    if now - observed_at > max_age:
        raise RiskRefused(code + "_STALE")


def evaluate(limits: RiskLimits, *, intent: Mapping[str, Any], binding: Mapping[str, Any], context: Any,
             exposure: ExposureView, now: int, session_started_at: int) -> None:
    """Raise RiskRefused(code) unless every limit and every data check passes. Pure read-only."""
    if not limits.configured():
        raise RiskRefused("EXPOSURE_LIMIT_ZERO")
    if not isinstance(context, Mapping):
        raise RiskRefused("RISK_CONTEXT_MISSING")
    stake = int(intent["total_bet_size"])
    odds = int(intent["percentage_odds"])
    outcome = 1 if intent["is_maker_betting_outcome_one"] else 2

    # -- market: exact hash, ACTIVE, event + selection label equal to what the owner approved
    m = context.get("market")
    if not isinstance(m, Mapping):
        raise RiskRefused("MARKET_DATA_MISSING")
    if not isinstance(m.get("marketHash"), str) or m["marketHash"].lower() != intent["market_hash"]:
        raise RiskRefused("MARKET_MISMATCH")
    if m.get("status") is None:
        raise RiskRefused("MARKET_STATUS_MISSING")
    if m.get("status") != "ACTIVE":
        raise RiskRefused("MARKET_NOT_ACTIVE")
    _fresh(context.get("market_observed_at"), now, limits.max_quote_age_seconds, "MARKET_DATA")
    if m.get("sportXeventId") is None or str(m.get("sportXeventId")) != binding["event_id"]:
        raise RiskRefused("APPROVAL_BINDING_MISMATCH_EVENT_ID")
    label = m.get("outcomeOneName" if outcome == 1 else "outcomeTwoName")
    if not isinstance(label, str) or label != binding["selection"]["label"]:
        raise RiskRefused("APPROVAL_BINDING_MISMATCH_SELECTION_LABEL")

    # -- fees: the snapshot must be present, well formed and exactly what the owner approved
    fees = context.get("fees")
    if not appr.fees_well_formed(fees):
        raise RiskRefused("FEES_MISSING_OR_MALFORMED")
    if dict(fees) != dict(binding["expected_fees"]):
        raise RiskRefused("FEES_CHANGED_SINCE_APPROVAL")

    # -- quote: fresh, same side, price inside the owner's odds band and slippage tolerance
    q = context.get("quote")
    if not isinstance(q, Mapping):
        raise RiskRefused("QUOTE_MISSING")
    _fresh(q.get("observed_at"), now, limits.max_quote_age_seconds, "QUOTE")
    if q.get("outcome") != outcome or type(q.get("outcome")) is not int:
        raise RiskRefused("QUOTE_SIDE_MISMATCH")
    best = _dec(q.get("best_percentage_odds"))
    liquidity = _dec(q.get("available_liquidity_base_units"))
    if best is None or best <= 0:
        raise RiskRefused("QUOTE_PRICE_MISSING")
    if liquidity is None:
        raise RiskRefused("LIQUIDITY_MISSING")
    if not limits.min_percentage_odds <= odds <= limits.max_percentage_odds:
        raise RiskRefused("ODDS_OUTSIDE_OWNER_BAND")
    quoted = int(binding["quoted_percentage_odds"])
    if quoted <= 0:
        raise RiskRefused("QUOTE_PRICE_MISSING")
    # Adverse move = the implied probability to pay went UP since the owner saw the quote.
    if (best - quoted) * 10_000 > quoted * limits.slippage_tolerance_bps:
        raise RiskRefused("ODDS_MOVED_BEYOND_SLIPPAGE")
    if intent["time_in_force"] in ("IOC", "FOK") and liquidity < stake:
        raise RiskRefused("INSUFFICIENT_LIQUIDITY")

    # -- balance: available must cover every unmatched reservation plus this stake
    b = context.get("balance")
    if not isinstance(b, Mapping):
        raise RiskRefused("BALANCE_MISSING")
    _fresh(b.get("observed_at"), now, limits.max_quote_age_seconds, "BALANCE")
    available = _dec(b.get("available_base_units"))
    if available is None:
        raise RiskRefused("BALANCE_MISSING")
    if available < exposure.reserved_unmatched + stake:
        raise RiskRefused("INSUFFICIENT_BALANCE")

    # -- exposure and counts (UNKNOWN / unreconciled orders are counted at full stake by the ledger)
    if int(binding["max_exposure_base_units"]) < stake:
        raise RiskRefused("ORDER_EXPOSURE_ABOVE_APPROVED_MAX")
    if stake > limits.max_stake_per_order_base_units:
        raise RiskRefused("STAKE_ABOVE_LIMIT")
    if exposure.by_market.get(intent["market_hash"], 0) + stake > limits.max_exposure_per_market_base_units:
        raise RiskRefused("MARKET_EXPOSURE_ABOVE_LIMIT")
    if exposure.by_event.get(binding["event_id"], 0) + stake > limits.max_exposure_per_event_base_units:
        raise RiskRefused("EVENT_EXPOSURE_ABOVE_LIMIT")
    if exposure.total + stake > limits.max_total_exposure_base_units:
        raise RiskRefused("EXPOSURE_ABOVE_LIMIT")
    if exposure.open_orders + 1 > limits.max_open_orders:
        raise RiskRefused("OPEN_ORDERS_ABOVE_LIMIT")

    # -- loss limits: realized loss so far plus this order's worst case (its whole stake)
    session_loss = max(0, -exposure.realized_pnl_since(session_started_at))
    if session_loss + stake > limits.session_loss_limit_base_units:
        raise RiskRefused("SESSION_LOSS_LIMIT")
    daily_loss = max(0, -exposure.realized_pnl_since(utc_day_start(now)))
    if daily_loss + stake > limits.daily_loss_limit_base_units:
        raise RiskRefused("DAILY_LOSS_LIMIT")
