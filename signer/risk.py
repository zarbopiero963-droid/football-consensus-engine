"""Authoritative pre-signature risk engine v2, enforced by the signer (governance §2.6, owner decisions D1/#187).

Limits come ONLY from an owner-controlled JSON file (path in env SXBET_SIGNER_RISK_CONFIG) signed with the
owner's Ed25519 key. A missing, unsigned, malformed, v1 or invalidly signed file yields the all-zero
default, and all-zero refuses every order (EXPOSURE_LIMIT_ZERO): the safe default is zero live exposure.
Nothing in an intent, a command, a tool argument or the risk context can change a limit: `RiskLimits` is
frozen, loaded once at signer start, and `evaluate` only reads it.

Config file (schema `sxbet.risk_config.v2`, intentional non-backward-compatible change from v1):
    {"schema": "sxbet.risk_config.v2", "config_version": <int >= 1>,
     "networks": {"mainnet": {<every RiskLimits field>}, "testnet": {<every RiskLimits field>}},
     "signature": "<hex Ed25519>"}
signed message = canonical JSON {"domain": "sxbet.risk_config.v2",
                                 "body": {"schema", "config_version", "networks"}}.
Testnet and mainnet values are separate; a network absent from the file has the all-zero limits.

Fields of one network block (all required; base units of the base token):
    max_stake_per_order_base_units, max_stake_per_utc_day_base_units,
    max_exposure_per_market_base_units, max_exposure_per_event_base_units, max_total_exposure_base_units,
    max_daily_loss_base_units          (realised loss of the UTC day + worst case of every open position
                                        + this order, i.e. the whole reserved / matched stake),
    max_open_orders, max_orders_per_hour, max_orders_per_utc_day,
    min_seconds_between_orders         (0 = no spacing),
    max_quote_age_seconds, command_max_age_seconds (<= 300),
    min_decimal_odds, max_decimal_odds (decimal strings, e.g. "1.20" and "10.0"),
    allowed_sport_ids, allowed_league_ids, allowed_market_types ("ANY" or a list of provider ints;
                                        an empty list allows nothing),
    pregame_only (bool), market_time_in_force ("FOK" | "IOC"), limit_time_in_force ("GTC").

Counters (stake per UTC day, orders per hour / per UTC day, time since the last order, realised loss) are
NEVER kept in memory only: `ExposureView` is computed inside the ledger lock by replaying the append-only
ledger (signer/orders.py), so a restart rebuilds them exactly. Every AUTHORIZE counts, also one later
rejected or abandoned (conservative).

Risk context (evidence the caller read from the exchange; every item is required, missing or stale =>
refuse):
    market:  GET /markets/find row {marketHash, status, sportXeventId, outcomeOneName, outcomeTwoName,
             teamOneName, teamTwoName, line, type, sportId, leagueId, gameTime}
    market_observed_at: unix seconds
    quote:   {outcome: 1|2, best_percentage_odds: str, available_liquidity_base_units: str, observed_at}
    balance: {available_base_units: str, observed_at}   from GET /user/balance-v3 availableAmount
    fees:    GET /user/fees-v3 `data` snapshot (well formed; null stays null)
    exchange_limits: {order_size_minimum_base_units: str, min_resting_order_size_base_units: str,
             base_token_symbol: str, base_token_decimals: int, observed_at} from GET /metadata/obv3
             (`limits` and `activeAsset`: runtime metadata, never hardcoded; the symbol and decimals are what
             the owner command's stake is converted with, Decimal-exact, in signer/command.py)
The signer cannot authenticate this evidence itself yet (its own reads come with the sender PR); freshness,
completeness and cross-checks are enforced here, provenance is a documented remaining gap.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields
from decimal import Decimal
from fractions import Fraction
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .orders import ExposureView, utc_day_start

RISK_CONFIG_ENV = "SXBET_SIGNER_RISK_CONFIG"
RISK_CONFIG_SCHEMA = "sxbet.risk_config.v2"
RISK_CONFIG_DOMAIN = "sxbet.risk_config.v2"
NETWORKS = ("mainnet", "testnet")
ANY = "ANY"
CLOCK_SKEW_SECONDS = 5
COMMAND_MAX_AGE_CAP_SECONDS = 300
ODDS_PRECISION = 10 ** 20
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_ODDS_TEXT = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,6})?$")
_HEX_SIG = re.compile(r"^[0-9a-f]{128}$")
_FEE = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,18})?$")


class RiskRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RiskLimits:
    """Owner-set, per network. The defaults are the safe all-zero configuration: every order refused."""
    max_stake_per_order_base_units: int = 0
    max_stake_per_utc_day_base_units: int = 0
    max_exposure_per_market_base_units: int = 0
    max_exposure_per_event_base_units: int = 0
    max_total_exposure_base_units: int = 0
    max_daily_loss_base_units: int = 0
    max_open_orders: int = 0
    max_orders_per_hour: int = 0
    max_orders_per_utc_day: int = 0
    min_seconds_between_orders: int = 0
    max_quote_age_seconds: int = 0
    command_max_age_seconds: int = 0
    min_decimal_odds: str = "0"
    max_decimal_odds: str = "0"
    allowed_sport_ids: Any = ()
    allowed_league_ids: Any = ()
    allowed_market_types: Any = ()
    pregame_only: bool = True
    market_time_in_force: str = "FOK"
    limit_time_in_force: str = "GTC"

    @property
    def odds_band(self) -> tuple[Fraction, Fraction]:
        return Fraction(Decimal(self.min_decimal_odds)), Fraction(Decimal(self.max_decimal_odds))

    def configured(self) -> bool:
        positive = [f.name for f in fields(self) if f.type in ("int", int) and f.name != "min_seconds_between_orders"]
        if not all(getattr(self, n) > 0 for n in positive):
            return False
        lo, hi = self.odds_band
        return 1 < lo <= hi and self.command_max_age_seconds <= COMMAND_MAX_AGE_CAP_SECONDS


LIMIT_FIELDS = frozenset(f.name for f in fields(RiskLimits))
_INT_FIELDS = frozenset(f.name for f in fields(RiskLimits) if f.type in ("int", int))
_LIST_FIELDS = frozenset({"allowed_sport_ids", "allowed_league_ids", "allowed_market_types"})


@dataclass(frozen=True)
class LoadedRiskConfig:
    limits: RiskLimits
    source: str  # OWNER_SIGNED | NETWORK_NOT_CONFIGURED | MISSING | UNSIGNED_IGNORED | SIGNATURE_INVALID | MALFORMED
    network: str | None = None
    config_version: int | None = None


def risk_config_message(body: Mapping[str, Any]) -> bytes:
    return json.dumps({"domain": RISK_CONFIG_DOMAIN, "body": dict(body)}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _limits_from(raw: Any) -> RiskLimits | None:
    if not isinstance(raw, dict) or set(raw) != LIMIT_FIELDS:
        return None
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _INT_FIELDS:
            if type(v) is not int or v < 0:
                return None
            out[k] = v
        elif k in _LIST_FIELDS:
            if v == ANY:
                out[k] = ANY
            elif isinstance(v, list) and len(v) <= 1000 and all(type(x) is int and x >= 0 for x in v):
                out[k] = tuple(sorted(set(v)))
            else:
                return None
        elif k in ("min_decimal_odds", "max_decimal_odds"):
            if not isinstance(v, str) or not _ODDS_TEXT.fullmatch(v):
                return None
            out[k] = v
        elif k == "pregame_only":
            if type(v) is not bool:
                return None
            out[k] = v
        elif k == "market_time_in_force":
            if v not in ("FOK", "IOC"):
                return None
            out[k] = v
        elif k == "limit_time_in_force":
            if v != "GTC":
                return None
            out[k] = v
    if out["command_max_age_seconds"] > COMMAND_MAX_AGE_CAP_SECONDS:
        return None
    return RiskLimits(**out)


def load_risk_config(path: str | None, owner_public_key: Ed25519PublicKey, *, network: str) -> LoadedRiskConfig:
    """Never raises: any problem yields the all-zero default with a fixed `source` code."""
    zero = RiskLimits()

    def fail(source: str) -> LoadedRiskConfig:
        return LoadedRiskConfig(zero, source, network)

    if network not in NETWORKS:
        return fail("MALFORMED")
    if not path:
        return fail("MISSING")
    try:
        with open(path, encoding="ascii") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return fail("MISSING")
    except (OSError, ValueError):
        return fail("MALFORMED")
    if not isinstance(doc, dict) or doc.get("schema") != RISK_CONFIG_SCHEMA:
        return fail("MALFORMED")  # includes every v1 file: intentional, fail closed
    body = {k: doc.get(k) for k in ("schema", "config_version", "networks")}
    nets = body["networks"]
    if type(body["config_version"]) is not int or body["config_version"] < 1 or not isinstance(nets, dict) \
            or not set(nets) <= set(NETWORKS):
        return fail("MALFORMED")
    parsed = {n: _limits_from(v) for n, v in nets.items()}
    if any(v is None for v in parsed.values()):
        return fail("MALFORMED")
    sig = doc.get("signature")
    if sig is None:
        return fail("UNSIGNED_IGNORED")
    if set(doc) != {"schema", "config_version", "networks", "signature"} or not isinstance(sig, str) \
            or not _HEX_SIG.fullmatch(sig):
        return fail("MALFORMED")
    try:
        owner_public_key.verify(bytes.fromhex(sig), risk_config_message(body))
    except InvalidSignature:
        return fail("SIGNATURE_INVALID")
    if network not in parsed:
        return LoadedRiskConfig(zero, "NETWORK_NOT_CONFIGURED", network, body["config_version"])
    return LoadedRiskConfig(parsed[network], "OWNER_SIGNED", network, body["config_version"])


def load_from_env(env: Mapping[str, str] | None, owner_public_key: Ed25519PublicKey, *,
                  network: str) -> LoadedRiskConfig:
    return load_risk_config((os.environ if env is None else env).get(RISK_CONFIG_ENV), owner_public_key,
                            network=network)


def fees_well_formed(fees: Any) -> bool:
    """GET /user/fees-v3 `data`: decimal-fraction strings or null (null stays null, never 0)."""
    if not isinstance(fees, Mapping) or not fees:
        return False
    return all(isinstance(k, str) and (v is None or (isinstance(v, str) and _FEE.fullmatch(v)))
               for k, v in fees.items())


def _dec(v: Any) -> int | None:
    return int(v) if isinstance(v, str) and _DECIMAL.fullmatch(v) else None


def _fresh(observed_at: Any, now: int, max_age: int, code: str) -> None:
    if type(observed_at) is not int:
        raise RiskRefused(code + "_MISSING")
    if observed_at > now + CLOCK_SKEW_SECONDS:
        raise RiskRefused(code + "_FROM_FUTURE")
    if now - observed_at > max_age:
        raise RiskRefused(code + "_STALE")


def _allowed(allow: Any, value: Any, name: str) -> None:
    if allow == ANY:
        return
    if type(value) is not int:
        raise RiskRefused(name + "_MISSING")
    if value not in allow:
        raise RiskRefused(name + "_NOT_ALLOWED")


def odds_inside_band(limits: RiskLimits, percentage_odds: int) -> bool:
    lo, hi = limits.odds_band
    if not 0 < percentage_odds < ODDS_PRECISION:
        return False
    return lo <= Fraction(ODDS_PRECISION, percentage_odds) <= hi


def evaluate(limits: RiskLimits, *, intent: Mapping[str, Any], event_id: str, context: Any,
             exposure: ExposureView, now: int) -> None:
    """Raise RiskRefused(code) unless every limit and every data check passes. Pure read-only."""
    if not isinstance(limits, RiskLimits) or not limits.configured():
        raise RiskRefused("EXPOSURE_LIMIT_ZERO")
    if not isinstance(context, Mapping):
        raise RiskRefused("RISK_CONTEXT_MISSING")
    stake = int(intent["total_bet_size"])
    odds = int(intent["percentage_odds"])
    outcome = 1 if intent["is_maker_betting_outcome_one"] else 2
    tif = intent["time_in_force"]

    # -- market: exact hash, ACTIVE, fresh, the event the command named
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
    if m.get("sportXeventId") is None or str(m.get("sportXeventId")) != event_id:
        raise RiskRefused("MARKET_EVENT_MISMATCH")
    # -- owner policy allowlists ("ANY" = every sport / league / market type the exchange offers)
    _allowed(limits.allowed_sport_ids, m.get("sportId"), "SPORT")
    _allowed(limits.allowed_league_ids, m.get("leagueId"), "LEAGUE")
    _allowed(limits.allowed_market_types, m.get("type"), "MARKET_TYPE")
    if limits.pregame_only:
        gt = m.get("gameTime")
        if type(gt) is not int:
            raise RiskRefused("GAME_TIME_MISSING")
        if now >= gt:
            raise RiskRefused("PREGAME_ONLY_EVENT_STARTED")

    # -- fees: present and well formed (never defaulted to 0)
    if not fees_well_formed(context.get("fees")):
        raise RiskRefused("FEES_MISSING_OR_MALFORMED")

    # -- runtime exchange limits (GET /metadata/obv3 `limits`): minimum order size, never hardcoded
    xl = context.get("exchange_limits")
    if not isinstance(xl, Mapping):
        raise RiskRefused("EXCHANGE_LIMITS_MISSING")
    _fresh(xl.get("observed_at"), now, limits.max_quote_age_seconds, "EXCHANGE_LIMITS")
    min_order = _dec(xl.get("order_size_minimum_base_units"))
    min_resting = _dec(xl.get("min_resting_order_size_base_units"))
    if min_order is None or min_resting is None:
        raise RiskRefused("EXCHANGE_LIMITS_MISSING")
    if stake < min_order:
        raise RiskRefused("STAKE_BELOW_EXCHANGE_MINIMUM")
    if tif == "GTC" and stake < min_resting:
        raise RiskRefused("STAKE_BELOW_MIN_RESTING_SIZE")

    # -- quote: fresh, same side, priced; owner odds band on the SIGNED price
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
    if not odds_inside_band(limits, odds):
        raise RiskRefused("ODDS_OUTSIDE_OWNER_BAND")
    if tif in ("IOC", "FOK") and liquidity < stake:
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

    # -- stake, exposure and counts (UNKNOWN / unreconciled orders count at full stake in the ledger)
    day = utc_day_start(now)
    if stake > limits.max_stake_per_order_base_units:
        raise RiskRefused("STAKE_ABOVE_LIMIT")
    if exposure.stake_since(day) + stake > limits.max_stake_per_utc_day_base_units:
        raise RiskRefused("DAILY_STAKE_ABOVE_LIMIT")
    if exposure.by_market.get(intent["market_hash"], 0) + stake > limits.max_exposure_per_market_base_units:
        raise RiskRefused("MARKET_EXPOSURE_ABOVE_LIMIT")
    if exposure.by_event.get(event_id, 0) + stake > limits.max_exposure_per_event_base_units:
        raise RiskRefused("EVENT_EXPOSURE_ABOVE_LIMIT")
    if exposure.total + stake > limits.max_total_exposure_base_units:
        raise RiskRefused("EXPOSURE_ABOVE_LIMIT")
    if exposure.open_orders + 1 > limits.max_open_orders:
        raise RiskRefused("OPEN_ORDERS_ABOVE_LIMIT")
    # -- pace (rebuilt from the ledger on restart)
    if exposure.orders_since(now - 3600) + 1 > limits.max_orders_per_hour:
        raise RiskRefused("ORDERS_PER_HOUR_ABOVE_LIMIT")
    if exposure.orders_since(day) + 1 > limits.max_orders_per_utc_day:
        raise RiskRefused("ORDERS_PER_DAY_ABOVE_LIMIT")
    last = exposure.last_authorized_at
    if last is not None and now - last < limits.min_seconds_between_orders:
        raise RiskRefused("ORDER_PACE_TOO_FAST")

    # -- daily loss: realised loss of the UTC day + worst case of everything open + this order
    realised_loss = max(0, -exposure.realized_pnl_since(day))
    if realised_loss + exposure.total + stake > limits.max_daily_loss_base_units:
        raise RiskRefused("DAILY_LOSS_LIMIT")

