"""SX Bet SIMULATION: pure order-intent validation, fill simulation and reconciliation.

Nothing in this module signs, holds a key or talks to the network. It turns a
requested order into a validated, canonical *intent* (whose sha256 is what an
out-of-band owner approval binds to), simulates the fill against an order-book
snapshot the caller already read, and models the reconciliation that a future
submission path must perform. LIVE_TRADING stays UNAVAILABLE (governance §2.6).

Official contract (docs.sx.bet, verified 01-11-2026):
- https://docs.sx.bet/developers/posting-orders and
  https://docs.sx.bet/api-reference/eip712-order-signing : 8-field signed struct
  Order(marketHash, baseToken, totalBetSize, percentageOdds, salt, expiry,
  maker, isMakerBettingOutcomeOne); timeInForce is NOT signed; amounts in USDC
  base units (6 decimals); percentageOdds = implied probability x 1e20.
- https://docs.sx.bet/developers/odds-rounding : 0 < p < 1e20 and
  (p % (oddsLadderStepSize * 1e15) == 0 or p in {1e17, 999e17}); round DOWN.
- https://docs.sx.bet/developers/time-in-force : GTC rests the remainder, IOC
  discards it, FOK executes nothing unless it fills completely.
- https://docs.sx.bet/developers/taking-liquidity : the submitted percentageOdds
  is the highest implied probability the bettor accepts; read the book with
  showTakerPerspective=true, where each level's size is the taker stake that
  fully consumes the level.
- https://docs.sx.bet/developers/order-lifecycle : PENDING/ACTIVE/INACTIVE and
  inactiveReason values; FILLED is an inactiveReason, not a status.
- https://docs.sx.bet/developers/cancelling-orders : cancel by id reports
  cancelled / notCancelled / unconfirmed; a 200 alone never proves a cancel.
- GET /orders-v3/{orderId} (https://docs.sx.bet/openapi.json): inactive orders
  are returned for about 10-20 minutes, then 404; a 404 is not proof of absence
  outside that window.

Every value produced here from provider data is a simulation (`DERIVED`), never
a provider fact; fees and the betting delay are not modelled.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

INTENT_SCHEMA = "sxbet.order_intent.v1"
ODDS_PRECISION = 10 ** 20
ODDS_EXCEPTIONS = frozenset({10 ** 17, 999 * 10 ** 17})
TIME_IN_FORCE = ("GTC", "IOC", "FOK")
EXPECTED_CHAIN_IDS = {"mainnet": 4162, "testnet": 79479957}  # openapi.json servers
EXPECTED_DOMAIN = ("OBv3 Escrow", "1")
EXPIRY_SAFETY_SECONDS = 10       # local margin on top of betting delay + 2 s (docs)
DEFAULT_MAX_EXPIRY_SECONDS = 86_400  # local policy, not a provider limit
# GET /orders-v3/{orderId} keeps inactive orders "about 10-20 minutes": use the lower bound.
ORDER_LOOKUP_GRACE_SECONDS = 600

_HEX32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")  # same charset as clientOrderId
_DECIMAL = re.compile(r"^[0-9]{1,40}$")


class IntentError(ValueError):
    """Fixed-code validation failure."""


@dataclass(frozen=True)
class ExposureLimits:
    """Owner-set limits. Default zero: nothing is eligible for live (governance §2.6)."""
    max_stake_base_units: int = 0
    max_exposure_base_units: int = 0


# ---------------------------------------------------------------- helpers
def _dec(value: Any, code: str) -> int:
    """Provider decimal string (or int) -> int; anything else fails closed."""
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        return int(value)
    raise IntentError(code)


def on_ladder(odds: int, step_size: int) -> bool:
    if not 0 < odds < ODDS_PRECISION:
        return False
    if odds in ODDS_EXCEPTIONS:
        return True
    return odds % (step_size * 10 ** 15) == 0


def round_down_to_ladder(odds: int, step_size: int) -> int:
    if odds in ODDS_EXCEPTIONS:
        return odds
    step = step_size * 10 ** 15
    return (odds // step) * step


def canonical_json(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def intent_hash(intent: Mapping[str, Any]) -> str:
    """sha256 over canonical JSON of the exact parameters: what an approval binds to."""
    return "sha256:" + hashlib.sha256(canonical_json(intent)).hexdigest()


# ---------------------------------------------------------------- metadata
def parse_metadata(meta: Any, network: str) -> dict[str, Any]:
    """Extract and cross-check the GET /metadata/obv3 fields the order needs."""
    if not isinstance(meta, dict):
        raise IntentError("METADATA_MISSING")
    try:
        domain = meta["domain"]
        asset = meta["activeAsset"]
        limits = meta["limits"]
        chain_id = meta["chainId"]
        step = meta["oddsLadderStepSize"]
    except (KeyError, TypeError):
        raise IntentError("METADATA_INCOMPLETE") from None
    if not all(isinstance(x, dict) for x in (domain, asset, limits)):
        raise IntentError("METADATA_INCOMPLETE")
    if type(chain_id) is not int or domain.get("chainId") != chain_id:
        raise IntentError("METADATA_CHAIN_INCONSISTENT")
    if EXPECTED_CHAIN_IDS.get(network) != chain_id:
        raise IntentError("METADATA_CHAIN_NOT_EXPECTED_FOR_NETWORK")
    if (domain.get("name"), domain.get("version")) != EXPECTED_DOMAIN:
        raise IntentError("METADATA_DOMAIN_UNEXPECTED")
    escrow, verifying, base = asset.get("escrowAddress"), domain.get("verifyingContract"), asset.get("baseToken")
    if not all(isinstance(a, str) and _ADDRESS.fullmatch(a) for a in (escrow, verifying, base)):
        raise IntentError("METADATA_ADDRESS_INVALID")
    if escrow.lower() != verifying.lower():
        raise IntentError("METADATA_ESCROW_MISMATCH")
    if asset.get("decimals") != 6:
        raise IntentError("METADATA_DECIMALS_UNEXPECTED")
    if type(step) is not int or not 1 <= step <= 1000:
        raise IntentError("METADATA_LADDER_STEP_INVALID")
    delay = meta.get("bettingDelay") if isinstance(meta.get("bettingDelay"), dict) else {}
    delays = [delay.get("liveMsDefault"), delay.get("pregameMsDefault")]
    for k in ("sportIdToMs", "leagueIdToMs"):
        if isinstance(delay.get(k), dict):
            delays.extend(delay[k].values())
    known = [d for d in delays if isinstance(d, (int, float)) and not isinstance(d, bool) and d >= 0]
    if not known:
        raise IntentError("METADATA_BETTING_DELAY_MISSING")
    return {
        "chain_id": chain_id,
        "domain_name": domain["name"],
        "domain_version": domain["version"],
        "verifying_contract": verifying.lower(),
        "base_token": base.lower(),
        "odds_ladder_step_size": step,
        "min_order_base_units": _dec(limits.get("orderSizeMinimumBaseUnits"), "METADATA_MIN_ORDER_INVALID"),
        "min_resting_base_units": _dec(limits.get("minRestingOrderSizeBaseUnits"), "METADATA_MIN_RESTING_INVALID"),
        "max_create_orders": limits.get("maxCreateOrders"),
        "max_cancel_orders": limits.get("maxCancelOrders"),
        # Conservative: the largest delay any market can have (live vs pregame is not proven here).
        "max_betting_delay_ms": max(known),
    }


# ---------------------------------------------------------------- intent
def build_intent(*, network: str, metadata: Mapping[str, Any], market_hash: Any, betting_outcome_one: Any,
                 stake_base_units: Any, requested_percentage_odds: Any, time_in_force: Any, expiry: Any,
                 idempotency_key: Any, now: int, maker: Any = None, salt: Any = None,
                 max_expiry_seconds: int = DEFAULT_MAX_EXPIRY_SECONDS) -> dict[str, Any]:
    """Validate a requested order and return its canonical intent.

    The odds are rounded DOWN onto the ladder explicitly; the raw requested value
    is preserved beside the rounded one and both are part of the hash. `maker`
    and `salt` may be None for a pure simulation: the intent is then not
    signable and `signing_ready` is False.
    """
    if network not in EXPECTED_CHAIN_IDS:
        raise IntentError("INVALID_NETWORK")
    if not isinstance(market_hash, str) or not _HEX32.fullmatch(market_hash):
        raise IntentError("INVALID_MARKET_HASH")
    if not isinstance(betting_outcome_one, bool):
        raise IntentError("INVALID_SIDE")
    if type(stake_base_units) is not int or stake_base_units <= 0:
        raise IntentError("INVALID_STAKE_BASE_UNITS")
    if stake_base_units < metadata["min_order_base_units"]:
        raise IntentError("STAKE_BELOW_MINIMUM")
    if stake_base_units >= 2 ** 256:
        raise IntentError("INVALID_STAKE_BASE_UNITS")
    if type(requested_percentage_odds) is not int:
        raise IntentError("INVALID_PERCENTAGE_ODDS")
    if not 0 < requested_percentage_odds < ODDS_PRECISION:
        raise IntentError("ODDS_OUT_OF_BOUNDS")
    odds = round_down_to_ladder(requested_percentage_odds, metadata["odds_ladder_step_size"])
    if not on_ladder(odds, metadata["odds_ladder_step_size"]):
        raise IntentError("ODDS_NOT_ON_LADDER_AFTER_ROUND_DOWN")
    if time_in_force not in TIME_IN_FORCE:
        raise IntentError("INVALID_TIME_IN_FORCE")
    if type(expiry) is not int:
        raise IntentError("INVALID_EXPIRY")
    min_expiry = now + -(-metadata["max_betting_delay_ms"] // 1000) + 2 + EXPIRY_SAFETY_SECONDS
    if expiry < min_expiry:
        raise IntentError("EXPIRY_TOO_SOON")
    if expiry > now + max_expiry_seconds:
        raise IntentError("EXPIRY_TOO_FAR")
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(idempotency_key):
        raise IntentError("INVALID_IDEMPOTENCY_KEY")
    if maker is not None and (not isinstance(maker, str) or not _ADDRESS.fullmatch(maker)):
        raise IntentError("INVALID_MAKER")
    if salt is not None and (type(salt) is not int or not 0 <= salt < 2 ** 256):
        raise IntentError("INVALID_SALT")
    return {
        "schema": INTENT_SCHEMA,
        "network": network,
        "chain_id": metadata["chain_id"],
        "domain_name": metadata["domain_name"],
        "domain_version": metadata["domain_version"],
        "verifying_contract": metadata["verifying_contract"],
        "base_token": metadata["base_token"],
        "market_hash": market_hash.lower(),
        "maker": None if maker is None else maker.lower(),
        "is_maker_betting_outcome_one": betting_outcome_one,
        "total_bet_size": str(stake_base_units),
        "percentage_odds": str(odds),
        "requested_percentage_odds": str(requested_percentage_odds),
        "salt": None if salt is None else str(salt),
        "expiry": expiry,
        "time_in_force": time_in_force,
        "idempotency_key": idempotency_key,
    }


def signing_ready(intent: Mapping[str, Any]) -> bool:
    return intent.get("maker") is not None and intent.get("salt") is not None


def live_eligibility(intent: Mapping[str, Any], limits: ExposureLimits, *,
                     current_exposure_base_units: int = 0) -> dict[str, Any]:
    """Would the owner limits allow this for live? Default zero limits: always no."""
    stake = int(intent["total_bet_size"])
    reasons = []
    if limits.max_stake_base_units <= 0 or limits.max_exposure_base_units <= 0:
        reasons.append("EXPOSURE_LIMIT_ZERO")
    if stake > limits.max_stake_base_units:
        reasons.append("STAKE_ABOVE_LIMIT")
    if current_exposure_base_units + stake > limits.max_exposure_base_units:
        reasons.append("EXPOSURE_ABOVE_LIMIT")
    return {"eligible": not reasons, "reasons": reasons,
            "exposure_after_base_units": current_exposure_base_units + stake,
            "note": "LIVE_TRADING is UNAVAILABLE regardless of limits (governance 2.6)."}


# ---------------------------------------------------------------- market and book
def check_market(markets: Any, market_hash: str) -> dict[str, Any]:
    """Exact hash match on GET /markets/find output; never fuzzy (governance 2.3bis)."""
    if not isinstance(markets, list):
        return {"status": "MARKET_DATA_MISSING"}
    found = [m for m in markets if isinstance(m, dict) and isinstance(m.get("marketHash"), str)
             and m["marketHash"].lower() == market_hash.lower()]
    if not found:
        return {"status": "MARKET_NOT_FOUND"}
    if len(found) > 1:
        return {"status": "MARKET_AMBIGUOUS"}
    m = found[0]
    keys = ("marketHash", "status", "outcomeOneName", "outcomeTwoName", "teamOneName", "teamTwoName",
            "sportXeventId", "leagueLabel", "gameTime", "line", "type", "liveEnabled")
    raw = {k: m.get(k) for k in keys}
    if m.get("status") != "ACTIVE":
        return {"status": "MARKET_INACTIVE", "market": raw}
    return {"status": "MARKET_ACTIVE", "market": raw}


def _levels(raw: Any) -> list[tuple[int, int]]:
    if not isinstance(raw, list):
        raise IntentError("BOOK_MALFORMED")
    out = []
    for lv in raw:
        if not isinstance(lv, dict):
            raise IntentError("BOOK_MALFORMED")
        odds = _dec(lv.get("percentageOdds"), "BOOK_MALFORMED")
        size = _dec(lv.get("size"), "BOOK_MALFORMED")
        if not 0 < odds < ODDS_PRECISION:
            raise IntentError("BOOK_MALFORMED")
        out.append((odds, size))
    return out


def simulate_fill(intent: Mapping[str, Any], book: Any, *, min_resting_base_units: int,
                  taker_perspective: bool = True) -> dict[str, Any]:
    """Walk a taker-perspective snapshot. Never invents liquidity.

    Levels at or below the order's percentageOdds are consumed cheapest first,
    each up to its `size` (taker stake that consumes it). Missing or malformed
    book => NOT_SIMULATABLE. Fees and betting delay are not modelled.
    """
    base = {"status": "NOT_SIMULATABLE", "label": "DERIVED_SIMULATION",
            "fees_modelled": False, "betting_delay_modelled": False}
    if not taker_perspective:
        return {**base, "reason": "BOOK_NOT_TAKER_PERSPECTIVE"}
    if not isinstance(book, dict):
        return {**base, "reason": "ORDER_BOOK_MISSING"}
    if isinstance(book.get("marketHash"), str) and book["marketHash"].lower() != intent["market_hash"]:
        return {**base, "reason": "BOOK_MARKET_MISMATCH"}
    side = "outcomeOne" if intent["is_maker_betting_outcome_one"] else "outcomeTwo"
    if side not in book:
        return {**base, "reason": "ORDER_BOOK_SIDE_MISSING"}
    try:
        levels = _levels(book[side])
    except IntentError as exc:
        return {**base, "reason": str(exc)}
    limit = int(intent["percentage_odds"])
    stake = int(intent["total_bet_size"])
    eligible = sorted(lv for lv in levels if lv[0] <= limit)
    fillable = sum(s for _, s in eligible)
    tif = intent["time_in_force"]
    remaining, legs = stake, []
    if not (tif == "FOK" and fillable < stake):
        for odds, size in eligible:
            if remaining == 0:
                break
            take = min(size, remaining)
            if take:
                legs.append({"percentage_odds": str(odds), "stake_base_units": str(take)})
                remaining -= take
    filled = stake - remaining
    payout = sum(int(l["stake_base_units"]) * ODDS_PRECISION // int(l["percentage_odds"]) for l in legs)
    resting = remaining if tif == "GTC" and remaining >= min_resting_base_units else 0
    discarded = remaining - resting
    if filled == stake:
        fill = "FULL"
    elif filled:
        fill = "PARTIAL"
    else:
        fill = "NONE"
    return {**{k: v for k, v in base.items() if k != "status"},
            "status": "SIMULATED", "fill": fill, "time_in_force": tif,
            "side_read": side, "levels_seen": len(levels), "levels_eligible": len(eligible),
            "fillable_at_or_better_base_units": str(fillable),
            "filled_base_units": str(filled), "unfilled_base_units": str(remaining),
            "resting_base_units": str(resting), "discarded_base_units": str(discarded),
            "legs": legs,
            # Effective implied probability = total stake / total potential return (1e20 scale).
            "effective_percentage_odds": str(filled * ODDS_PRECISION // payout) if payout else None,
            "potential_return_base_units": str(payout) if legs else None,
            "fok_killed": tif == "FOK" and fillable < stake}


def check_balance(balances: Any, *, base_token: str, escrow: str, stake: int) -> dict[str, Any]:
    """Compare availableAmount of the exact (token, escrow) row; missing stays missing."""
    rows = balances.get("balances") if isinstance(balances, dict) else None
    if not isinstance(rows, list):
        return {"status": "BALANCE_UNRESOLVED", "reason": "BALANCE_DATA_MISSING"}
    match = [r for r in rows if isinstance(r, dict) and isinstance(r.get("tokenAddress"), str)
             and isinstance(r.get("escrowAddress"), str) and r["tokenAddress"].lower() == base_token
             and r["escrowAddress"].lower() == escrow]
    if len(match) != 1:
        return {"status": "BALANCE_UNRESOLVED", "reason": "NO_UNIQUE_BALANCE_ROW"}
    try:
        available = _dec(match[0].get("availableAmount"), "BALANCE_MALFORMED")
    except IntentError:
        return {"status": "BALANCE_UNRESOLVED", "reason": "BALANCE_MALFORMED"}
    return {"status": "SUFFICIENT" if available >= stake else "INSUFFICIENT_BALANCE",
            "available_base_units": str(available), "required_base_units": str(stake)}


# ---------------------------------------------------------------- cancel
def simulate_cancel(order_ids: Any, open_orders: Any, *, max_cancel_orders: int) -> dict[str, Any]:
    """Simulated DELETE-by-id: reports what WOULD be cancelled; never claims a real cancel."""
    if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= max_cancel_orders:
        raise IntentError("INVALID_CANCEL_COUNT")
    if len(set(i.lower() for i in order_ids if isinstance(i, str))) != len(order_ids):
        raise IntentError("DUPLICATE_ORDER_ID")
    for i in order_ids:
        if not isinstance(i, str) or not _HEX32.fullmatch(i):
            raise IntentError("INVALID_ORDER_ID")
    if not isinstance(open_orders, list):
        return {"status": "NOT_SIMULATABLE", "reason": "OPEN_ORDERS_MISSING"}
    active = {o["id"].lower(): o for o in open_orders
              if isinstance(o, dict) and isinstance(o.get("id"), str) and o.get("status") == "ACTIVE"}
    would, not_found = [], []
    for i in order_ids:
        (would if i.lower() in active else not_found).append(i.lower())
    return {"status": "SIMULATED", "label": "DERIVED_SIMULATION", "would_cancel": would,
            "not_open": not_found, "real_cancel_sent": False,
            "note": "A real cancel by id can also return 'unconfirmed'; only a reconciled INACTIVE/USER_REQUESTED proves it."}


# ---------------------------------------------------------------- reconciliation
SUBMITTED = "SUBMITTED"
UNKNOWN_AFTER_TIMEOUT = "UNKNOWN_AFTER_TIMEOUT"
AMBIGUOUS_RESPONSE = "AMBIGUOUS_RESPONSE"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
NOT_ON_EXCHANGE = "NOT_ON_EXCHANGE"  # 404 inside the lookup window and no fills
UNRESOLVED_STATES = frozenset({UNKNOWN_AFTER_TIMEOUT, AMBIGUOUS_RESPONSE})
RETRYABLE_STATES = frozenset({REJECTED, NOT_ON_EXCHANGE})


class ReconcileError(RuntimeError):
    pass


def classify_submission(outcome: Mapping[str, Any], order_id: str) -> str:
    """Map a (hypothetical) submission outcome to a state. No transport exists here."""
    kind = outcome.get("kind")
    if kind == "TIMEOUT":
        return UNKNOWN_AFTER_TIMEOUT
    if kind != "HTTP":
        return AMBIGUOUS_RESPONSE
    status = outcome.get("http_status")
    body = outcome.get("body")
    if status == 400 and isinstance(body, dict) and isinstance(body.get("message"), str):
        return REJECTED
    if status != 200 or not isinstance(body, dict) or body.get("status") != "success":
        return AMBIGUOUS_RESPONSE  # 5xx/503/401/other: not proof either way
    rows = (body.get("data") or {}).get("orders") if isinstance(body.get("data"), dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return AMBIGUOUS_RESPONSE
    row = rows[0]
    if not isinstance(row.get("orderId"), str) or row["orderId"].lower() != order_id.lower():
        return AMBIGUOUS_RESPONSE
    return SUBMITTED if row.get("status") == "SUBMITTED" else AMBIGUOUS_RESPONSE


def state_from_lookup(lookup: Mapping[str, Any], *, order_id: str, total_bet_size: int,
                      seconds_since_submit: int) -> str | None:
    """Reconcile from GET /orders-v3/{orderId} (+ GET /fills-v3?orderId=). None = still unresolved."""
    kind = lookup.get("kind")
    fills = lookup.get("fills")
    if kind == "FOUND":
        o = lookup.get("order")
        if not isinstance(o, dict) or not isinstance(o.get("id"), str) or o["id"].lower() != order_id.lower():
            return None
        try:
            remaining = _dec(o.get("remainingSize"), "LOOKUP_MALFORMED")
        except IntentError:
            return None
        status, reason = o.get("status"), o.get("inactiveReason")
        if status in ("PENDING", "ACTIVE"):
            return PARTIALLY_FILLED if remaining < total_bet_size else SUBMITTED
        if status == "INACTIVE":
            if reason == "FILLED":
                return FILLED
            return PARTIALLY_FILLED if remaining < total_bet_size else CANCELLED
        return None
    if kind == "NOT_FOUND":
        # A 404 proves absence only inside the documented retention window and with no fills.
        if seconds_since_submit <= ORDER_LOOKUP_GRACE_SECONDS and isinstance(fills, list) and not fills:
            return NOT_ON_EXCHANGE
        return None
    return None  # lookup error: nothing learned


@dataclass
class _Record:
    intent_hash: str
    order_id: str
    total_bet_size: int
    state: str
    history: list[dict[str, Any]] = field(default_factory=list)


class SubmissionTracker:
    """Idempotency-keyed state machine. History is append-only."""

    def __init__(self):
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    def _get(self, key: str, ihash: str) -> _Record:
        rec = self._records.get(key)
        if rec is None:
            raise ReconcileError("UNKNOWN_IDEMPOTENCY_KEY")
        if rec.intent_hash != ihash:
            raise ReconcileError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT")
        return rec

    def record_submission(self, key: str, ihash: str, order_id: str, total_bet_size: int,
                          outcome: Mapping[str, Any]) -> str:
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                rec = self._records[key] = _Record(ihash, order_id.lower(), total_bet_size, "NEW")
            elif (rec.intent_hash != ihash or rec.order_id != order_id.lower()
                  or rec.total_bet_size != total_bet_size):
                raise ReconcileError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT")
            elif rec.state not in RETRYABLE_STATES:
                raise ReconcileError("RETRY_NOT_ALLOWED_IN_STATE_" + rec.state)
            new = classify_submission(outcome, rec.order_id)
            rec.history.append({"event": "SUBMISSION_OUTCOME", "from": rec.state, "to": new})
            rec.state = new
            return new

    def reconcile(self, key: str, ihash: str, lookup: Mapping[str, Any], *, seconds_since_submit: int) -> str:
        with self._lock:
            rec = self._get(key, ihash)
            new = state_from_lookup(lookup, order_id=rec.order_id, total_bet_size=rec.total_bet_size,
                                    seconds_since_submit=seconds_since_submit)
            rec.history.append({"event": "RECONCILE", "from": rec.state, "to": new or rec.state,
                                "lookup": lookup.get("kind")})
            if new is not None:
                rec.state = new
            return rec.state

    def may_retry(self, key: str, ihash: str) -> bool:
        """Retry only with the same key and only after reconciliation proved non-execution."""
        with self._lock:
            return self._get(key, ihash).state in RETRYABLE_STATES

    def state(self, key: str) -> str:
        return self._records[key].state

    def history(self, key: str) -> list[dict[str, Any]]:
        return [dict(h) for h in self._records[key].history]
