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

EXPIRED_UNCONFIRMED (404 after expiry is NOT proof). A 404 by order id after
expiry + EXPIRY_GRACE_SECONDS with a complete, empty fills list moves a live
order to EXPIRED_UNCONFIRMED, which still RESERVES the full unfilled stake. It
is released (-> EXPIRED) only on positive terminal evidence: the exchange row
INACTIVE with a documented inactiveReason, or the same 404 + complete fills
whose sum equals the recorded fill observed again at least
EXPIRY_CONFIRM_SECONDS after the first observation. A late fill is never
dropped: fills seen while the order is EXPIRED_UNCONFIRMED / EXPIRED /
CANCELLED raise `filled` (self-transition), and EXPIRED -> FILLED is allowed,
so the matched exposure is recorded and counted again.

Exposure accounting (base units, per order; `filled` = matched stake):
- AUTHORIZED, SIGNED, SUBMITTED, OPEN, UNKNOWN, EXPIRED_UNCONFIRMED:
  reserved = stake - filled, matched = filled. UNKNOWN never releases anything
  until reconciled.
- PARTIALLY_FILLED: matched = filled; the unfilled part stays reserved until a
  terminal CANCELLED / EXPIRED / FILLED proven by exchange data.
- FILLED / CANCELLED / EXPIRED: reserved = 0, matched = filled (stays until SETTLED).
- REJECTED after the signature was released (SIGNED reached, e.g. HTTP 400 on
  submit or a manual SIGNED -> REJECTED): the signed order stays postable by
  whoever holds it, so the whole stake stays reserved (and counted as an open
  order) until now > expiry + EXPIRY_GRACE_SECONDS; only then 0.
- REJECTED never signed (signing failed / abandoned), SETTLED: 0. SETTLED
  records realized P&L for the loss limits.

Persistence: one JSON object per line, each with the sha256 of the previous
line, fsync'd. The first line (GENESIS) binds the file to one signer_id and
one signer address. A missing, empty, foreign or tampered ledger fails closed
(LedgerError) instead of silently starting from zero exposure; a new ledger is
created only by the explicit `OrderLedger.create`.

Single writer: an OrderLedger holds an exclusive, non-blocking flock on the
file for its whole lifetime (until `close()`); a second instance, in this or
any other process, fails with LEDGER_LOCKED. Before every append the instance
re-checks that the file is still the one it locked (same inode), has exactly
the size it wrote, and ends with the line whose hash is its in-memory head;
anything else (an external writer ignoring the advisory lock, a replaced file)
is LEDGER_HEAD_MISMATCH. Any failed or partial append, or a head mismatch,
POISONS the instance: every later write raises LEDGER_POISONED until the
process restarts and replays the file (a torn last line then fails closed as
LEDGER_CORRUPT and needs an owner decision).

External anchor (optional, signer/anchor.py): with an `AnchorStore` the
ledger (a) takes a writer LEASE from the store instead of flock (LEDGER_LOCKED
for a second writer, on any host), (b) after EVERY fsync'd append advances the
anchored head (seq = number of lines, head hash, fencing token) -- write-ahead
order: local line first, anchor second, so a crash in between leaves the local
file exactly one step AHEAD of the anchor, never behind -- and (c) refuses to
start when the local tail is behind the anchor (truncation / rollback:
LEDGER_ANCHOR_AHEAD_OF_LOCAL), different at the anchored seq (fork / rewrite:
LEDGER_ANCHOR_HASH_MISMATCH), ahead of it (LEDGER_LOCAL_AHEAD_OF_ANCHOR), or
not anchored at all (LEDGER_ANCHOR_MISSING). Local-ahead and missing are
accepted only with `owner_ack_head` equal to the exact local head hash, i.e. an
explicit owner decision after reviewing the extra lines. An unreachable store
fails closed (LEDGER_ANCHOR_UNREACHABLE at start; LEDGER_ANCHOR_WRITE_FAILED +
poison on append); a stale writer whose lease was taken over is LEDGER_FENCED.

Residual (documented) limitations: without an anchor, a hash chain cannot detect
truncation of the TAIL of the file by someone with write access. The pre-append
check reads only the size and the last line: an in-place rewrite of earlier
lines that keeps the size is detected only at the next replay. In local-only
mode flock is advisory and local: the ledger must live on a local filesystem of
the signer host.

REJECTED is not a dead end once the signature was released (H3): exchange
evidence that the order rested or executed after all (a fill, a live row) moves
it to SUBMITTED / OPEN / PARTIALLY_FILLED / FILLED (or CANCELLED / EXPIRED with
a fill), so no financial event is lost. A never-signed REJECTED stays final.

CANCEL_REQUEST lines (signer/cancellation.py) consume a cancel-approval nonce and
record what was requested; they never change any order state or exposure.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from . import anchor as an

LEDGER_SCHEMA = "sxbet.order_ledger.v1"
GENESIS_PREV = "sha256:" + "0" * 64

PREPARED, AUTHORIZED, SIGNED, SUBMITTED, OPEN = "PREPARED", "AUTHORIZED", "SIGNED", "SUBMITTED", "OPEN"
PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED = "PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"
SETTLED, REJECTED, UNKNOWN = "SETTLED", "REJECTED", "UNKNOWN"
EXPIRED_UNCONFIRMED = "EXPIRED_UNCONFIRMED"
STATES = frozenset({PREPARED, AUTHORIZED, SIGNED, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED,
                    EXPIRED, EXPIRED_UNCONFIRMED, SETTLED, REJECTED, UNKNOWN})

TRANSITIONS: dict[str, frozenset[str]] = {
    PREPARED: frozenset({AUTHORIZED, REJECTED}),
    AUTHORIZED: frozenset({SIGNED, REJECTED}),
    SIGNED: frozenset({UNKNOWN, SUBMITTED, REJECTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED,
                       EXPIRED_UNCONFIRMED}),
    SUBMITTED: frozenset({OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, EXPIRED_UNCONFIRMED, UNKNOWN}),
    OPEN: frozenset({PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, EXPIRED_UNCONFIRMED, UNKNOWN}),
    PARTIALLY_FILLED: frozenset({PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, UNKNOWN}),
    UNKNOWN: frozenset({SIGNED, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, EXPIRED_UNCONFIRMED,
                        REJECTED}),
    EXPIRED_UNCONFIRMED: frozenset({EXPIRED_UNCONFIRMED, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED,
                                    EXPIRED}),
    FILLED: frozenset({SETTLED}),
    CANCELLED: frozenset({CANCELLED, SETTLED}),   # CANCELLED -> CANCELLED only to record a late fill
    EXPIRED: frozenset({EXPIRED, FILLED, SETTLED}),  # EXPIRED -> EXPIRED / FILLED only to record a late fill
    SETTLED: frozenset(),
    # H3: only on exchange evidence, only after the signature was released, CANCELLED/EXPIRED only with a fill.
    REJECTED: frozenset({SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED}),
}
RESERVING = frozenset({AUTHORIZED, SIGNED, SUBMITTED, OPEN, UNKNOWN, PARTIALLY_FILLED, EXPIRED_UNCONFIRMED})
MATCHED_HOLDING = frozenset({SIGNED, SUBMITTED, OPEN, UNKNOWN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED,
                             EXPIRED_UNCONFIRMED})
OPEN_ORDER_STATES = RESERVING
TERMINAL = frozenset({SETTLED, REJECTED})
CANCEL_SCOPES = frozenset({"ORDER_IDS", "MARKET", "EVENT", "ALL"})

# A 404 from GET /orders-v3/{orderId} proves nothing about an order in flight. A
# "reconciled absent" verdict (the only path to a re-send) needs: 404 by id AND by
# clientOrderId, a complete empty fills list, and at least this long since the
# last send attempt (POST maxWaitTime is capped at 15 s server-side; margin on top).
ABSENT_SETTLE_SECONDS = 60
# After expiry the exchange cannot match the order any more (docs: order-lifecycle EXPIRED).
EXPIRY_GRACE_SECONDS = 30
# A 404 after expiry is only a hint (EXPIRED_UNCONFIRMED, still reserved). It becomes EXPIRED (released)
# only when re-observed, with complete fills unchanged, at least this long after the first observation.
EXPIRY_CONFIRM_SECONDS = 24 * 3600
_COUNTED_FILL_STATUSES = frozenset({"MATCHED", "LOCKED", "SETTLED"})
_FILL_STATUSES = _COUNTED_FILL_STATUSES | {"FAILED"}

_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_REASON = re.compile(r"^[A-Z0-9_]{1,64}$")
_LEDGER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")


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
    signature_released: bool = False       # SIGNED was reached: the signed order may exist outside the signer
    unconfirmed_since: int | None = None   # ts of the first EXPIRED_UNCONFIRMED observation

    def rejected_hold_active(self, now: int) -> bool:
        """REJECTED after a released signature: still postable until expiry (+ grace) has passed."""
        return self.state == REJECTED and self.signature_released and now <= self.expiry + EXPIRY_GRACE_SECONDS

    def reserved_at(self, now: int) -> int:
        if self.state in RESERVING:
            return self.stake - self.filled
        return self.stake if self.rejected_hold_active(now) else 0

    @property
    def matched(self) -> int:
        return self.filled if self.state in MATCHED_HOLDING else 0

    def exposure_at(self, now: int) -> int:
        return self.reserved_at(now) + self.matched

    def is_open_at(self, now: int) -> bool:
        return self.state in OPEN_ORDER_STATES or self.rejected_hold_active(now)


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
        reason = "EXCHANGE_ORDER_" + status
        if order.state == REJECTED:
            # H3: a rejection reported after the signature was released can be contradicted by the exchange.
            if not order.signature_released:
                return none("EVIDENCE_REJECTED_NEVER_SIGNED")
            if target in (CANCELLED, EXPIRED) and not filled:
                return none("EVIDENCE_REJECTED_NO_EXECUTION")  # off the book, nothing executed: hold continues
            reason = "REJECTED_ORDER_FOUND_EXECUTED" if filled else "REJECTED_ORDER_FOUND_LIVE"
        pnl = None
        if target in (FILLED, CANCELLED, EXPIRED) and filled and fills_ok and fills.get("complete") is True:
            pnl = _settlement_pnl(order, fills["fills"], filled)
        return Evidence(target, filled, reason, sha, pnl)
    if kind == "NOT_FOUND":
        if order.state in (EXPIRED_UNCONFIRMED, EXPIRED, CANCELLED):
            return _not_found_after_expiry(order, fills if fills_ok else None, now, sha)
        if order.state in (REJECTED, PARTIALLY_FILLED):
            return _not_found_late_fill(order, fills if fills_ok else None, sha)
        if order.filled or not fills_ok or fills.get("complete") is not True or fills["fills"]:
            return none("EVIDENCE_NOT_FOUND_INCONCLUSIVE")
        if now > order.expiry + EXPIRY_GRACE_SECONDS:
            # NOT proof: a 404 may also mean "purged" or a lagging index. The stake stays reserved
            # (EXPIRED_UNCONFIRMED) until positive terminal evidence; see the module docstring.
            return Evidence(EXPIRED_UNCONFIRMED, 0, "NOT_FOUND_AFTER_EXPIRY_NO_FILLS", sha)
        client_kind = client_lookup.get("kind") if isinstance(client_lookup, Mapping) else None
        if (order.state == UNKNOWN and client_kind == "NOT_FOUND" and order.last_send_at is not None
                and now - order.last_send_at >= ABSENT_SETTLE_SECONDS):
            # DOCS_UNVERIFIED: the docs do not state that a 404 after the settle window proves the
            # order was never accepted. It only re-opens the SAME signed order with the SAME
            # clientOrderId for re-send; exposure stays fully reserved.
            return Evidence(SIGNED, 0, "RECONCILED_ABSENT", sha)
        return none("EVIDENCE_NOT_FOUND_INCONCLUSIVE")
    return none("EVIDENCE_LOOKUP_ERROR")


def _own_fill_sum(order: Order, fills: Mapping[str, Any] | None) -> int | None:
    """Sum of fillAmount over a COMPLETE fills list that belongs entirely to this order; FAILED fills do
    not count. Anything malformed, foreign, of unknown status or incomplete => None (learn nothing)."""
    if fills is None or fills.get("complete") is not True:
        return None
    total = 0
    for f in fills["fills"]:
        if not isinstance(f, Mapping) or not isinstance(f.get("orderId"), str) \
                or f["orderId"].lower() != order.order_id or f.get("status") not in _FILL_STATUSES:
            return None
        amount = _dec(f.get("fillAmount"))
        if amount is None:
            return None
        if f["status"] in _COUNTED_FILL_STATUSES:
            total += amount
    return total


def _not_found_late_fill(order: Order, fills: Mapping[str, Any] | None, sha: str) -> Evidence:
    """404 for a REJECTED (signature released) or PARTIALLY_FILLED order: the order row may be gone (it is
    purged ~10-20 min after INACTIVE), but complete fills of this order that exceed the recorded fill are
    an execution that must be recorded. Never releases anything else."""
    none = Evidence(None, None, "EVIDENCE_NOT_FOUND_INCONCLUSIVE", sha)
    if order.state == REJECTED and not order.signature_released:
        return none
    total = _own_fill_sum(order, fills)
    if total is None or total > order.stake or total <= order.filled:
        return none
    target = FILLED if total == order.stake else PARTIALLY_FILLED
    reason = "LATE_FILL_AFTER_REJECT" if order.state == REJECTED else "LATE_FILL_WHILE_NOT_FOUND"
    return Evidence(target, total, reason, sha)


def _not_found_after_expiry(order: Order, fills: Mapping[str, Any] | None, now: int, sha: str) -> Evidence:
    """404 for an order already EXPIRED_UNCONFIRMED / EXPIRED / CANCELLED: only fills can teach something."""
    none = Evidence(None, None, "EVIDENCE_NOT_FOUND_INCONCLUSIVE", sha)
    total = _own_fill_sum(order, fills)
    if total is None or total > order.stake or total < order.filled:
        return none
    if total > order.filled:
        # A late fill is recorded (and counted), never dropped.
        target = order.state
        if order.state == EXPIRED_UNCONFIRMED and total == order.stake:
            target = FILLED
        return Evidence(target, total, "LATE_FILL_AFTER_EXPIRY", sha)
    if order.state == EXPIRED_UNCONFIRMED:
        if order.unconfirmed_since is not None and now >= order.unconfirmed_since + EXPIRY_CONFIRM_SECONDS:
            pnl = _settlement_pnl(order, fills["fills"], total) if total else None
            # Same exchange bytes as the first observation, so a distinct evidence id (the dedupe in
            # apply_evidence would otherwise ignore the confirming re-observation).
            confirm_sha = "sha256:" + hashlib.sha256(("CONFIRM_AFTER_WINDOW|" + sha).encode("ascii")).hexdigest()
            return Evidence(EXPIRED, total, "NOT_FOUND_CONFIRMED_AFTER_WINDOW", confirm_sha, pnl)
        return Evidence(None, None, "NOT_FOUND_EXPIRY_UNCONFIRMED", sha)
    if order.filled:
        pnl = _settlement_pnl(order, fills["fills"], total)
        if pnl is not None:
            return Evidence(order.state, total, "EXCHANGE_FILLS_SETTLED", sha, pnl)
    return none


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
                 _create: bool = False, anchor: "an.AnchorStore | None" = None, owner_ack_head: str | None = None):
        if not isinstance(signer_address, str) or not _ADDRESS.fullmatch(signer_address.lower()):
            raise LedgerError("LEDGER_SIGNER_ADDRESS_INVALID")
        self._path = path
        self.signer_id = signer_id
        self.signer_address = signer_address.lower()
        self.ledger_id = f"{signer_id}:{self.signer_address}"
        self._clock = clock
        self._anchor = anchor
        self._lease = None
        self._seq = 0               # number of lines in the file (GENESIS = 1)
        self._lock = threading.RLock()
        self._orders: dict[str, Order] = {}
        self._by_order_id: dict[str, str] = {}
        self._nonces: set[str] = set()
        self._settlements: list[tuple[int, int]] = []  # (ts, pnl)
        self._prev = GENESIS_PREV
        self._fd: int | None = None
        self._size = 0              # bytes this instance knows are in the file
        self._last_line = b""       # last line (with "\n"), hash == self._prev
        self._poisoned = False
        self._flocked = False
        if anchor is not None:
            if not isinstance(signer_id, str) or not _LEDGER_ID.fullmatch(self.ledger_id):
                raise LedgerError("LEDGER_ID_INVALID")
            try:
                self._lease = anchor.acquire_writer(self.ledger_id)
            except an.AnchorLocked:
                raise LedgerError("LEDGER_LOCKED") from None  # another writer, on any host
            except Exception:
                raise LedgerError("LEDGER_ANCHOR_UNREACHABLE") from None
        try:
            if anchor is not None and _create and self._anchor_head() is not None:
                raise LedgerError("LEDGER_ANCHOR_EXISTS")  # a fresh file never replaces an anchored history
            self._fd = self._open_locked(path, create=_create, lock=anchor is None)
            self._flocked = anchor is None
            if _create:
                self._append({"kind": "GENESIS", "schema": LEDGER_SCHEMA, "signer_id": signer_id,
                              "signer_address": self.signer_address})
            else:
                hashes = self._replay()
                if anchor is not None:
                    self._verify_anchor(hashes, owner_ack_head)
        except BaseException:
            self.close()
            raise

    # ---- construction (explicit: a missing ledger never means "start from zero") ----
    @classmethod
    def create(cls, path: str, *, signer_id: str, signer_address: str,
               clock: Callable[[], float] = time.time, anchor: "an.AnchorStore | None" = None) -> "OrderLedger":
        return cls(path, signer_id=signer_id, signer_address=signer_address, clock=clock, _create=True,
                   anchor=anchor)

    @classmethod
    def open(cls, path: str, *, signer_id: str, signer_address: str,
             clock: Callable[[], float] = time.time, anchor: "an.AnchorStore | None" = None,
             owner_ack_head: str | None = None) -> "OrderLedger":
        """`owner_ack_head`: only for an owner who reviewed the local lines beyond the anchored head (or a
        ledger never anchored before): the exact local head hash to anchor. Anything else is refused."""
        return cls(path, signer_id=signer_id, signer_address=signer_address, clock=clock, anchor=anchor,
                   owner_ack_head=owner_ack_head)

    # ---- external anchor ----
    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    @property
    def fencing_token(self) -> int | None:
        return None if self._lease is None else self._lease.token

    @property
    def head_hash(self) -> str:
        return self._prev

    @property
    def seq(self) -> int:
        return self._seq

    def _anchor_head(self) -> "an.AnchorHead | None":
        try:
            return self._anchor.head(self.ledger_id)
        except Exception:
            raise LedgerError("LEDGER_ANCHOR_UNREACHABLE") from None

    def _anchor_advance(self, expected_seq: int, seq: int, head_hash: str) -> None:
        try:
            self._anchor.advance(self.ledger_id, token=self._lease.token, expected_seq=expected_seq, seq=seq,
                                 head_hash=head_hash)
        except an.AnchorFenced:
            raise LedgerError("LEDGER_FENCED") from None
        except an.AnchorConflict:
            raise LedgerError("LEDGER_ANCHOR_CONFLICT") from None
        except Exception:
            raise LedgerError("LEDGER_ANCHOR_WRITE_FAILED") from None

    def _verify_anchor(self, hashes: list[str], owner_ack_head: str | None) -> None:
        head = self._anchor_head()
        local_seq, local_head = len(hashes), self._prev
        acked = isinstance(owner_ack_head, str) and owner_ack_head == local_head
        if head is None:
            if not acked:
                raise LedgerError("LEDGER_ANCHOR_MISSING")
            self._anchor_advance(0, local_seq, local_head)
            return
        if head.seq > local_seq:
            raise LedgerError("LEDGER_ANCHOR_AHEAD_OF_LOCAL")  # truncated tail or rolled-back file
        if hashes[head.seq - 1] != head.head_hash:
            raise LedgerError("LEDGER_ANCHOR_HASH_MISMATCH")   # rewritten history / fork
        if head.seq < local_seq:
            if not acked:
                raise LedgerError("LEDGER_LOCAL_AHEAD_OF_ANCHOR")
            self._anchor_advance(head.seq, local_seq, local_head)

    @staticmethod
    def _open_locked(path: str, *, create: bool, lock: bool = True) -> int:
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
        except FileExistsError:
            raise LedgerError("LEDGER_ALREADY_EXISTS") from None
        except FileNotFoundError:
            raise LedgerError("LEDGER_MISSING") from None
        except OSError:
            raise LedgerError("LEDGER_UNREADABLE") from None
        if not lock:
            return fd  # anchored mode: the store's writer lease replaces the local flock
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LedgerError("LEDGER_LOCKED") from None  # another instance (any process) owns this ledger
        return fd

    def close(self) -> None:
        """Release the file lock. The instance refuses every later write (LEDGER_CLOSED)."""
        with self._lock:
            fd, self._fd = self._fd, None
            lease, self._lease = self._lease, None
            try:
                if fd is not None:
                    try:
                        if self._flocked:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
            finally:
                if lease is not None:
                    lease.release()

    def __enter__(self) -> "OrderLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    # ---- persistence ----
    def _writable(self) -> None:
        if self._fd is None:
            raise LedgerError("LEDGER_CLOSED")
        if self._poisoned:
            raise LedgerError("LEDGER_POISONED")

    def _check_head(self) -> None:
        """The file must still be exactly what this instance wrote: same inode, same size, same last line."""
        try:
            st = os.fstat(self._fd)
            on_disk = os.stat(self._path)
            same = (st.st_dev, st.st_ino) == (on_disk.st_dev, on_disk.st_ino) and st.st_size == self._size
            if same and self._last_line:
                n = len(self._last_line)
                same = os.pread(self._fd, n, self._size - n) == self._last_line
        except OSError:
            same = False
        if not same:
            self._poisoned = True
            raise LedgerError("LEDGER_HEAD_MISMATCH")

    def _append(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Write ONE fsync'd line. Any failure from here on poisons the instance (no further writes
        until restart + replay), because the file and the in-memory state may now disagree."""
        self._writable()
        self._check_head()
        rec = {"ts": int(self._clock()), "prev_sha256": self._prev, **rec}
        line = _canon(rec)
        data = (line + "\n").encode("ascii")
        try:
            view = memoryview(data)
            while view:
                n = os.write(self._fd, view)
                if n <= 0:
                    raise OSError("LEDGER_SHORT_WRITE")
                view = view[n:]
            os.fsync(self._fd)
        except Exception:
            self._poisoned = True
            raise LedgerError("LEDGER_WRITE_FAILED") from None
        except BaseException:
            self._poisoned = True
            raise
        self._size += len(data)
        self._last_line = data
        self._prev = _line_hash(line)
        self._seq += 1
        if self._anchor is not None:
            # Write-ahead order: the line is durable locally BEFORE the anchor moves, so a crash here leaves
            # the file one line ahead of the anchor (refused at start until the owner acknowledges it).
            try:
                self._anchor_advance(self._seq - 1, self._seq, self._prev)
            except BaseException:
                self._poisoned = True
                raise
        return rec

    def _commit(self, rec: dict[str, Any]) -> None:
        """Append + apply; if applying a written line fails, the instance is poisoned."""
        written = self._append(rec)
        try:
            self._apply(written)
        except Exception:
            self._poisoned = True
            raise LedgerError("LEDGER_APPLY_FAILED") from None
        except BaseException:
            self._poisoned = True
            raise

    def _read_all(self) -> bytes:
        size = os.fstat(self._fd).st_size
        chunks, off = [], 0
        while off < size:
            chunk = os.pread(self._fd, min(1 << 20, size - off), off)
            if not chunk:
                break
            chunks.append(chunk)
            off += len(chunk)
        return b"".join(chunks)

    def _replay(self) -> list[str]:
        prev, first, last, hashes = GENESIS_PREV, True, b"", []
        try:
            data = self._read_all()
        except OSError:
            raise LedgerError("LEDGER_UNREADABLE") from None
        if data and not data.endswith(b"\n"):
            raise LedgerError("LEDGER_CORRUPT")  # torn last line
        try:
            for raw in data.split(b"\n")[:-1] if data else []:
                try:
                    line = raw.decode("ascii")
                    rec = json.loads(line) if line else None
                except ValueError:
                    raise LedgerError("LEDGER_CORRUPT") from None
                if not isinstance(rec, dict):
                    raise LedgerError("LEDGER_CORRUPT")
                if rec.get("prev_sha256") != prev:
                    raise LedgerError("LEDGER_CHAIN_BROKEN")
                prev = _line_hash(line)
                hashes.append(prev)
                last = raw + b"\n"
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
        self._prev, self._size, self._last_line, self._seq = prev, len(data), last, len(hashes)
        return hashes

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
                          last_send_at=rec["ts"] if rec.get("reason") == "SEND_ATTEMPT" else new.last_send_at,
                          unconfirmed_since=(rec["ts"] if new.state == EXPIRED_UNCONFIRMED and
                                             o.state != EXPIRED_UNCONFIRMED else new.unconfirmed_since))
            self._orders[o.client_order_id] = new
            if new.state == SETTLED:
                self._settlements.append((int(rec["ts"]), int(new.realized_pnl)))
        elif kind == "CANCEL_REQUEST":
            # Consumes a cancel-approval nonce; changes no order state and no exposure.
            if rec["scope"] not in CANCEL_SCOPES or not isinstance(rec["order_ids"], list) \
                    or rec["approval_nonce"] in self._nonces:
                raise OrderStateError("LEDGER_CANCEL_REQUEST_INVALID")
            self._nonces.add(rec["approval_nonce"])
        else:
            raise OrderStateError("LEDGER_UNKNOWN_RECORD")

    @staticmethod
    def _next(o: Order, to: str, *, filled: Any, pnl: Any) -> Order:
        if to not in TRANSITIONS.get(o.state, frozenset()):
            raise OrderStateError(f"ILLEGAL_TRANSITION_{o.state}_TO_{to}")
        f = o.filled if filled is None else filled
        if type(f) is not int or not o.filled <= f <= o.stake:
            raise OrderStateError("FILLED_AMOUNT_INVALID")  # never decreases, never above stake
        if o.state == REJECTED:
            if not o.signature_released:
                raise OrderStateError("REJECTED_NEVER_SIGNED_IS_FINAL")
            if to in (CANCELLED, EXPIRED) and not f:
                raise OrderStateError("REJECTED_RELEASE_REQUIRES_EXECUTION")
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
        return replace(o, state=to, filled=f, signature_released=o.signature_released or to == SIGNED)

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
            now = int(self._clock())
            by_market: dict[str, int] = {}
            by_event: dict[str, int] = {}
            total = reserved = matched = open_orders = 0
            for o in self._orders.values():
                r, m = o.reserved_at(now), o.matched
                if r + m:
                    by_market[o.market_hash] = by_market.get(o.market_hash, 0) + r + m
                    by_event[o.event_id] = by_event.get(o.event_id, 0) + r + m
                total += r + m
                reserved += r
                matched += m
                open_orders += 1 if o.is_open_at(now) else 0
            return ExposureView(total=total, reserved_unmatched=reserved, matched=matched, by_market=by_market,
                                by_event=by_event, open_orders=open_orders,
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
            self._writable()
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
            self._commit(rec)
            return self._orders[client_order_id]

    def transition(self, client_order_id: str, to: str, *, reason: str, filled: int | None = None,
                   realized_pnl: int | None = None, evidence_sha256: str | None = None) -> Order:
        if not _REASON.fullmatch(reason or ""):
            raise OrderStateError("REASON_INVALID")
        with self._lock:
            self._writable()
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
            self._commit(rec)
            return self._orders[client_order_id]

    def record_cancel_request(self, *, approval_nonce: str, scope: str, binding_sha256: str,
                              order_ids: list[str], market_hash: str | None = None,
                              event_id: str | None = None) -> None:
        """One fsync'd CANCEL_REQUEST line, written BEFORE the cancel is sent: consumes the cancel approval's
        nonce (single use, survives restarts). It records the request only; no order state or exposure
        changes here -- only exchange evidence (apply_evidence) does that."""
        ids = list(order_ids) if isinstance(order_ids, (list, tuple)) else None
        if not (_NONCE.fullmatch(approval_nonce or "") and scope in CANCEL_SCOPES
                and isinstance(binding_sha256, str) and _SHA.fullmatch(binding_sha256)
                and ids is not None and len(ids) <= 1000 and len(set(ids)) == len(ids)
                and all(isinstance(i, str) and _HEX32.fullmatch(i) for i in ids)
                and (market_hash is None or (isinstance(market_hash, str) and _HEX32.fullmatch(market_hash)))
                and (event_id is None or (isinstance(event_id, str) and 0 < len(event_id) <= 64))):
            raise OrderStateError("CANCEL_REQUEST_FIELDS_INVALID")
        with self._lock:
            self._writable()
            if approval_nonce in self._nonces:
                raise OrderStateError("APPROVAL_NONCE_REPLAYED")
            rec = {"kind": "CANCEL_REQUEST", "approval_nonce": approval_nonce, "scope": scope,
                   "binding_sha256": binding_sha256, "order_ids": ids}
            if market_hash is not None:
                rec["market_hash"] = market_hash
            if event_id is not None:
                rec["event_id"] = event_id
            self._commit(rec)

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
