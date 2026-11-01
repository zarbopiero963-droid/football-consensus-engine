"""Order lifecycle state machine + append-only, hash-chained order ledger (governance §2.6).

The ledger is the single source of truth for: consumed approval nonces, the
idempotency key -> intent mapping, every order's state, and the exposure the
risk engine sees. State and exposure are ALWAYS rebuilt by replaying the
ledger; nothing is kept only in memory.

States and allowed transitions (anything else is ILLEGAL_TRANSITION):

    PREPARED ─► AUTHORIZED ─► SIGNED ─► UNKNOWN (write-ahead before a send, timeout, ambiguous reply)
                    │            │  ├─► SUBMITTED ─► OPEN ─► PARTIALLY_FILLED ─► FILLED ─► SETTLED
                    ▼            │  ├─► REJECTED (definitive exchange rejection)    │
                 REJECTED        │  └─► EXPIRED (exchange evidence only)            ├─► CANCELLED ─► SETTLED*
              (never signed)     ▼                                                  └─► EXPIRED   ─► SETTLED*
                       (any reconciled state)                    (* only when something was matched)

UNKNOWN ─► SIGNED only on "reconciled absent" exchange evidence (the only path
to a safe re-send, always with the same signed order and the same
clientOrderId), or to any state the exchange evidence proves.

Exposure accounting (base units, per order; `filled` = matched stake):
- AUTHORIZED, SIGNED, SUBMITTED, OPEN, UNKNOWN: reserved = stake - filled,
  matched = filled. UNKNOWN never releases anything until reconciled.
- PARTIALLY_FILLED: matched = filled; the unfilled part stays reserved until a
  terminal CANCELLED / EXPIRED / FILLED proven by exchange data.
- FILLED / CANCELLED / EXPIRED: reserved = 0, matched = filled (stays until SETTLED).
- SETTLED, REJECTED: 0. SETTLED records realized P&L for the loss limits.

Persistence: one JSON object per line, each with the sha256 of the previous
line, fsync'd. The first line (GENESIS) binds the file to one signer_id and
one signer address. A missing, empty, foreign or tampered ledger fails closed
(LedgerError) instead of silently starting from zero exposure; a new ledger is
created only by the explicit `OrderLedger.create`.

Residual (documented) limitation: a hash chain cannot detect truncation of the
TAIL of the file by someone with write access; anchoring the head hash outside
the signer host is an owner deployment decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

LEDGER_SCHEMA = "sxbet.order_ledger.v1"
GENESIS_PREV = "sha256:" + "0" * 64

PREPARED, AUTHORIZED, SIGNED, SUBMITTED, OPEN = "PREPARED", "AUTHORIZED", "SIGNED", "SUBMITTED", "OPEN"
PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED = "PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"
SETTLED, REJECTED, UNKNOWN = "SETTLED", "REJECTED", "UNKNOWN"
STATES = frozenset({PREPARED, AUTHORIZED, SIGNED, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED,
                    EXPIRED, SETTLED, REJECTED, UNKNOWN})

TRANSITIONS: dict[str, frozenset[str]] = {
    PREPARED: frozenset({AUTHORIZED, REJECTED}),
    AUTHORIZED: frozenset({SIGNED, REJECTED}),
    SIGNED: frozenset({UNKNOWN, SUBMITTED, REJECTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED}),
    SUBMITTED: frozenset({OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, UNKNOWN}),
    OPEN: frozenset({PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, UNKNOWN}),
    PARTIALLY_FILLED: frozenset({PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, UNKNOWN}),
    UNKNOWN: frozenset({SIGNED, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, REJECTED}),
    FILLED: frozenset({SETTLED}),
    CANCELLED: frozenset({SETTLED}),
    EXPIRED: frozenset({SETTLED}),
    SETTLED: frozenset(),
    REJECTED: frozenset(),
}
RESERVING = frozenset({AUTHORIZED, SIGNED, SUBMITTED, OPEN, UNKNOWN, PARTIALLY_FILLED})
MATCHED_HOLDING = frozenset({SIGNED, SUBMITTED, OPEN, UNKNOWN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED})
OPEN_ORDER_STATES = RESERVING
TERMINAL = frozenset({SETTLED, REJECTED})

# A 404 from GET /orders-v3/{orderId} proves nothing about an order in flight. A
# "reconciled absent" verdict (the only path to a re-send) needs: 404 by id AND by
# clientOrderId, a complete empty fills list, and at least this long since the
# last send attempt (POST maxWaitTime is capped at 15 s server-side; margin on top).
ABSENT_SETTLE_SECONDS = 60
# After expiry the exchange cannot match the order any more (docs: order-lifecycle EXPIRED).
EXPIRY_GRACE_SECONDS = 30

_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_REASON = re.compile(r"^[A-Z0-9_]{1,64}$")


class LedgerError(RuntimeError):
    """Fail-closed: the ledger cannot be trusted; the signer must refuse to sign."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OrderStateError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Order:
    client_order_id: str
    intent_hash: str
    order_id: str
    market_hash: str
    event_id: str
    stake: int
    expiry: int
    state: str
    filled: int = 0
    realized_pnl: int | None = None
    last_send_at: int | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reserved(self) -> int:
        return self.stake - self.filled if self.state in RESERVING else 0

    @property
    def matched(self) -> int:
        return self.filled if self.state in MATCHED_HOLDING else 0

    @property
    def exposure(self) -> int:
        return self.reserved + self.matched


@dataclass(frozen=True)
class ExposureView:
    """Read-only snapshot handed to the risk engine (computed inside the ledger lock)."""
    total: int
    reserved_unmatched: int
    matched: int
    by_market: Mapping[str, int]
    by_event: Mapping[str, int]
    open_orders: int
    realized_pnl_since: Callable[[int], int]


@dataclass(frozen=True)
class Evidence:
    """A normalized exchange observation. `target` None = nothing learned."""
    target: str | None
    filled: int | None
    reason: str
    sha256: str
    settle_pnl: int | None = None


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _line_hash(line: str) -> str:
    return "sha256:" + hashlib.sha256(line.encode("ascii")).hexdigest()


def _dec(v: Any) -> int | None:
    return int(v) if isinstance(v, str) and _DECIMAL.fullmatch(v) else None


# ---------------------------------------------------------------- exchange adapter
def evidence_from_exchange(order: Order, *, lookup: Mapping[str, Any] | None,
                           client_lookup: Mapping[str, Any] | None = None,
                           fills: Mapping[str, Any] | None = None, now: int) -> Evidence:
    """Normalize exchange reads into one Evidence. Fail closed: anything unproven => target None.

    Inputs mirror the official read routes (field names from https://docs.sx.bet/openapi.json,
    verified 01-11-2026):
    - lookup: {"kind": "FOUND", "order": <OrderV3>} | {"kind": "NOT_FOUND"} | {"kind": "ERROR"} for
      GET /orders-v3/{orderId}; OrderV3 fields used: id, totalBetSize, remainingSize, status
      (PENDING|ACTIVE|INACTIVE), inactiveReason.
    - client_lookup: same shape for GET /orders-v3/client/{clientOrderId}.
    - fills: {"kind": "OK", "fills": [<FillV3>], "complete": bool} for GET /fills-v3?orderId=;
      FillV3 fields used: orderId, fillAmount, status (MATCHED|LOCKED|SETTLED|FAILED),
      settlement.settleReturnAmount.
    DOCS_UNVERIFIED: that, for a maker order, sum(fillAmount) over its fills equals
    totalBetSize - remainingSize. Settlement is only recorded when both agree.
    """
    sha = "sha256:" + hashlib.sha256(_canon({"lookup": lookup, "client_lookup": client_lookup,
                                             "fills": fills}).encode("ascii")).hexdigest()
    none = lambda reason: Evidence(None, None, reason, sha)  # noqa: E731
    kind = lookup.get("kind") if isinstance(lookup, Mapping) else None
    fills_ok = isinstance(fills, Mapping) and fills.get("kind") == "OK" and isinstance(fills.get("fills"), list)
    if kind == "FOUND":
        row = lookup.get("order")
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or row["id"].lower() != order.order_id:
            return none("EVIDENCE_OTHER_ORDER")
        total, remaining = _dec(row.get("totalBetSize")), _dec(row.get("remainingSize"))
        if total != order.stake or remaining is None or remaining > total:
            return none("EVIDENCE_SIZE_MISMATCH")
        filled = total - remaining
        status, reason = row.get("status"), row.get("inactiveReason")
        if status == "PENDING":
            target = PARTIALLY_FILLED if filled else SUBMITTED
        elif status == "ACTIVE":
            target = PARTIALLY_FILLED if filled else OPEN
        elif status == "INACTIVE":
            if reason == "FILLED":
                if not filled:
                    return none("EVIDENCE_FILLED_WITHOUT_FILL")
                target = FILLED
            elif reason == "EXPIRED":
                target = EXPIRED
            elif reason in ("USER_REQUESTED", "NO_LIQUIDITY", "INSUFFICIENT_BALANCE", "EVENT_LIFECYCLE",
                            "MARKET_HALTED", "HEARTBEAT_TIMEOUT", "POST_ONLY_WOULD_CROSS", "SYSTEM"):
                target = CANCELLED
            else:
                return none("EVIDENCE_INACTIVE_REASON_UNKNOWN")
        else:
            return none("EVIDENCE_STATUS_UNKNOWN")
        pnl = None
        if target in (FILLED, CANCELLED, EXPIRED) and filled and fills_ok and fills.get("complete") is True:
            pnl = _settlement_pnl(order, fills["fills"], filled)
        return Evidence(target, filled, "EXCHANGE_ORDER_" + status, sha, pnl)
    if kind == "NOT_FOUND":
        if order.filled or not fills_ok or fills.get("complete") is not True or fills["fills"]:
            return none("EVIDENCE_NOT_FOUND_INCONCLUSIVE")
        if now > order.expiry + EXPIRY_GRACE_SECONDS:
            # Past expiry nothing can match any more and no fill exists: the reservation can be released.
            return Evidence(EXPIRED, 0, "NOT_FOUND_AFTER_EXPIRY_NO_FILLS", sha)
        client_kind = client_lookup.get("kind") if isinstance(client_lookup, Mapping) else None
        if (order.state == UNKNOWN and client_kind == "NOT_FOUND" and order.last_send_at is not None
                and now - order.last_send_at >= ABSENT_SETTLE_SECONDS):
            # DOCS_UNVERIFIED: the docs do not state that a 404 after the settle window proves the
            # order was never accepted. It only re-opens the SAME signed order with the SAME
            # clientOrderId for re-send; exposure stays fully reserved.
            return Evidence(SIGNED, 0, "RECONCILED_ABSENT", sha)
        return none("EVIDENCE_NOT_FOUND_INCONCLUSIVE")
    return none("EVIDENCE_LOOKUP_ERROR")


def _settlement_pnl(order: Order, rows: list, filled: int) -> int | None:
    mine = [f for f in rows if isinstance(f, Mapping) and isinstance(f.get("orderId"), str)
            and f["orderId"].lower() == order.order_id]
    if not mine or len(mine) != len(rows):
        return None
    total, paid = 0, 0
    for f in mine:
        amount = _dec(f.get("fillAmount"))
        st = f.get("settlement")
        ret = _dec(st.get("settleReturnAmount")) if isinstance(st, Mapping) else None
        if f.get("status") != "SETTLED" or amount is None or ret is None:
            return None  # FAILED / not yet settled / malformed: exposure stays
        total += amount
        paid += ret
    if total != filled:
        return None
    return paid - total


# ---------------------------------------------------------------- ledger
class OrderLedger:
    def __init__(self, path: str, *, signer_id: str, signer_address: str, clock: Callable[[], float],
                 _create: bool = False):
        if not isinstance(signer_address, str) or not _ADDRESS.fullmatch(signer_address.lower()):
            raise LedgerError("LEDGER_SIGNER_ADDRESS_INVALID")
        self._path = path
        self.signer_id = signer_id
        self.signer_address = signer_address.lower()
        self._clock = clock
        self._lock = threading.RLock()
        self._orders: dict[str, Order] = {}
        self._by_order_id: dict[str, str] = {}
        self._nonces: set[str] = set()
        self._settlements: list[tuple[int, int]] = []  # (ts, pnl)
        self._prev = GENESIS_PREV
        if _create:
            if os.path.exists(path):
                raise LedgerError("LEDGER_ALREADY_EXISTS")
            self._append({"kind": "GENESIS", "schema": LEDGER_SCHEMA, "signer_id": signer_id,
                          "signer_address": self.signer_address}, exclusive=True)
        else:
            self._replay()

    # ---- construction (explicit: a missing ledger never means "start from zero") ----
    @classmethod
    def create(cls, path: str, *, signer_id: str, signer_address: str,
               clock: Callable[[], float] = time.time) -> "OrderLedger":
        return cls(path, signer_id=signer_id, signer_address=signer_address, clock=clock, _create=True)

    @classmethod
    def open(cls, path: str, *, signer_id: str, signer_address: str,
             clock: Callable[[], float] = time.time) -> "OrderLedger":
        return cls(path, signer_id=signer_id, signer_address=signer_address, clock=clock)

    # ---- persistence ----
    def _append(self, rec: dict[str, Any], *, exclusive: bool = False) -> dict[str, Any]:
        rec = {"ts": int(self._clock()), "prev_sha256": self._prev, **rec}
        line = _canon(rec)
        with open(self._path, "x" if exclusive else "a", encoding="ascii") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev = _line_hash(line)
        return rec

    def _lines(self) -> Iterator[tuple[str, dict[str, Any]]]:
        if not os.path.exists(self._path):
            raise LedgerError("LEDGER_MISSING")
        with open(self._path, encoding="ascii") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    raise LedgerError("LEDGER_CORRUPT")
                try:
                    rec = json.loads(line)
                except ValueError:
                    raise LedgerError("LEDGER_CORRUPT") from None
                if not isinstance(rec, dict):
                    raise LedgerError("LEDGER_CORRUPT")
                yield line, rec

    def _replay(self) -> None:
        prev, first = GENESIS_PREV, True
        try:
            for line, rec in self._lines():
                if rec.get("prev_sha256") != prev:
                    raise LedgerError("LEDGER_CHAIN_BROKEN")
                prev = _line_hash(line)
                if first:
                    first = False
                    if rec.get("kind") != "GENESIS" or rec.get("schema") != LEDGER_SCHEMA:
                        raise LedgerError("LEDGER_CORRUPT")
                    if rec.get("signer_id") != self.signer_id or rec.get("signer_address") != self.signer_address:
                        raise LedgerError("LEDGER_FOREIGN")
                    continue
                self._apply(rec)
        except (KeyError, TypeError, ValueError, OrderStateError):
            raise LedgerError("LEDGER_CORRUPT") from None
        if first:
            raise LedgerError("LEDGER_EMPTY")
        self._prev = prev

    def _apply(self, rec: Mapping[str, Any]) -> None:
        kind = rec["kind"]
        if kind == "AUTHORIZE":
            o = Order(client_order_id=rec["client_order_id"], intent_hash=rec["intent_hash"],
                      order_id=rec["order_id"], market_hash=rec["market_hash"], event_id=rec["event_id"],
                      stake=int(rec["stake"]), expiry=int(rec["expiry"]), state=AUTHORIZED)
            if o.client_order_id in self._orders or rec["approval_nonce"] in self._nonces:
                raise OrderStateError("LEDGER_DUPLICATE_AUTHORIZE")
            self._orders[o.client_order_id] = o
            self._by_order_id[o.order_id] = o.client_order_id
            self._nonces.add(rec["approval_nonce"])
        elif kind == "TRANSITION":
            o = self._orders[rec["client_order_id"]]
            new = self._next(o, rec["to"], filled=rec.get("filled"), pnl=rec.get("realized_pnl"))
            if rec.get("from") != o.state:
                raise OrderStateError("LEDGER_TRANSITION_FROM_MISMATCH")
            ev = rec.get("evidence_sha256")
            new = replace(new, evidence=o.evidence + ((ev,) if ev else ()),
                          last_send_at=rec["ts"] if rec.get("reason") == "SEND_ATTEMPT" else new.last_send_at)
            self._orders[o.client_order_id] = new
            if new.state == SETTLED:
                self._settlements.append((int(rec["ts"]), int(new.realized_pnl)))
        else:
            raise OrderStateError("LEDGER_UNKNOWN_RECORD")

    @staticmethod
    def _next(o: Order, to: str, *, filled: Any, pnl: Any) -> Order:
        if to not in TRANSITIONS.get(o.state, frozenset()):
            raise OrderStateError(f"ILLEGAL_TRANSITION_{o.state}_TO_{to}")
        f = o.filled if filled is None else filled
        if type(f) is not int or not o.filled <= f <= o.stake:
            raise OrderStateError("FILLED_AMOUNT_INVALID")  # never decreases, never above stake
        if to == SETTLED:
            if not o.filled or type(pnl) is not int or f != o.filled:
                raise OrderStateError("SETTLEMENT_INVALID")
            return replace(o, state=to, realized_pnl=pnl)
        if to == REJECTED and f:
            raise OrderStateError("REJECTED_WITH_FILL")
        if to == FILLED and not f:
            raise OrderStateError("FILLED_WITHOUT_FILL")
        if to == PARTIALLY_FILLED and not 0 < f < o.stake:
            raise OrderStateError("PARTIAL_FILL_INVALID")
        if to in (SIGNED, SUBMITTED, OPEN) and f:
            raise OrderStateError("UNFILLED_STATE_WITH_FILL")
        if to == o.state and f == o.filled:
            raise OrderStateError("NO_OP_TRANSITION")
        return replace(o, state=to, filled=f)

    # ---- reads ----
    def get(self, client_order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(client_order_id)

    def by_order_id(self, order_id: str) -> Order | None:
        with self._lock:
            key = self._by_order_id.get(order_id.lower() if isinstance(order_id, str) else order_id)
            return None if key is None else self._orders[key]

    def nonce_consumed(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._nonces

    def orders(self) -> list[Order]:
        with self._lock:
            return list(self._orders.values())

    def realized_pnl_since(self, ts: int) -> int:
        with self._lock:
            return sum(p for t, p in self._settlements if t >= ts)

    def exposure(self) -> ExposureView:
        with self._lock:
            by_market: dict[str, int] = {}
            by_event: dict[str, int] = {}
            for o in self._orders.values():
                if o.exposure:
                    by_market[o.market_hash] = by_market.get(o.market_hash, 0) + o.exposure
                    by_event[o.event_id] = by_event.get(o.event_id, 0) + o.exposure
            os_ = self._orders.values()
            return ExposureView(total=sum(o.exposure for o in os_), reserved_unmatched=sum(o.reserved for o in os_),
                                matched=sum(o.matched for o in os_), by_market=by_market, by_event=by_event,
                                open_orders=sum(1 for o in os_ if o.state in OPEN_ORDER_STATES),
                                realized_pnl_since=self.realized_pnl_since)

    # ---- writes ----
    def authorize(self, *, client_order_id: str, intent_hash: str, order_id: str, market_hash: str, event_id: str,
                  stake: int, expiry: int, approval_nonce: str,
                  precheck: Callable[[ExposureView], None] | None = None) -> Order:
        """Atomically: re-check nonce + key unused, run `precheck` on the live exposure, then persist
        ONE fsync'd AUTHORIZE line that consumes the nonce and reserves the full stake."""
        if not (_KEY.fullmatch(client_order_id or "") and _NONCE.fullmatch(approval_nonce or "")
                and _HEX32.fullmatch(order_id or "") and _HEX32.fullmatch(market_hash or "")
                and isinstance(event_id, str) and event_id and type(stake) is int and stake > 0
                and type(expiry) is int):
            raise OrderStateError("AUTHORIZE_FIELDS_INVALID")
        with self._lock:
            if approval_nonce in self._nonces:
                raise OrderStateError("APPROVAL_NONCE_REPLAYED")
            if client_order_id in self._orders:
                raise OrderStateError("IDEMPOTENCY_KEY_ALREADY_AUTHORIZED")
            if order_id in self._by_order_id:
                raise OrderStateError("ORDER_ID_ALREADY_AUTHORIZED")
            if precheck is not None:
                precheck(self.exposure())
            rec = {"kind": "AUTHORIZE", "client_order_id": client_order_id, "intent_hash": intent_hash,
                   "order_id": order_id, "market_hash": market_hash, "event_id": event_id, "stake": str(stake),
                   "expiry": expiry, "approval_nonce": approval_nonce}
            self._apply(self._append(rec))
            return self._orders[client_order_id]

    def transition(self, client_order_id: str, to: str, *, reason: str, filled: int | None = None,
                   realized_pnl: int | None = None, evidence_sha256: str | None = None) -> Order:
        if not _REASON.fullmatch(reason or ""):
            raise OrderStateError("REASON_INVALID")
        with self._lock:
            o = self._orders.get(client_order_id)
            if o is None:
                raise OrderStateError("ORDER_NOT_FOUND")
            new = self._next(o, to, filled=filled, pnl=realized_pnl)  # raises before anything is written
            rec = {"kind": "TRANSITION", "client_order_id": client_order_id, "from": o.state, "to": to,
                   "filled": new.filled, "reason": reason}
            if realized_pnl is not None:
                rec["realized_pnl"] = realized_pnl
            if evidence_sha256 is not None:
                rec["evidence_sha256"] = evidence_sha256
            self._apply(self._append(rec))
            return self._orders[client_order_id]

    def apply_evidence(self, client_order_id: str, evidence: Evidence) -> Order:
        """Idempotent: the same evidence twice, or evidence that changes nothing, writes nothing."""
        with self._lock:
            o = self._orders.get(client_order_id)
            if o is None:
                raise OrderStateError("ORDER_NOT_FOUND")
            if evidence.sha256 in o.evidence or evidence.target is None:
                return o
            if o.state == SETTLED:
                return o
            if not (evidence.target == o.state and evidence.filled == o.filled):
                o = self.transition(client_order_id, evidence.target, reason=evidence.reason,
                                    filled=evidence.filled, evidence_sha256=evidence.sha256)
            if evidence.settle_pnl is not None and o.state in (FILLED, CANCELLED, EXPIRED):
                o = self.transition(client_order_id, SETTLED, reason="EXCHANGE_FILLS_SETTLED",
                                    realized_pnl=evidence.settle_pnl, evidence_sha256=evidence.sha256)
            return o


def utc_day_start(now: int) -> int:
    d = datetime.fromtimestamp(now, tz=timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
