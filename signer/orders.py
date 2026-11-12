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
CANCELLED / FILLED raise `filled` (self-transition), and EXPIRED -> FILLED is allowed,
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
ledger (a) takes a writer LEASE from the store (LEDGER_LOCKED for a second
writer, on any host) BEFORE opening the file, and still takes the local flock
too (defence in depth on one host), (b) after EVERY fsync'd append advances the
anchored head (seq = number of lines, head hash, fencing token) -- write-ahead
order: local line first, anchor second, so a crash in between leaves the local
file exactly one step AHEAD of the anchor, never behind -- and (c) refuses to
start when the local tail is behind the anchor (truncation / rollback:
LEDGER_ANCHOR_AHEAD_OF_LOCAL), different at the anchored seq (fork / rewrite:
LEDGER_ANCHOR_HASH_MISMATCH), ahead of it (LEDGER_LOCAL_AHEAD_OF_ANCHOR), or
not anchored at all (LEDGER_ANCHOR_MISSING). Local-ahead and missing are
accepted only with an owner-SIGNED `anchor.LedgerHeadAck` (Ed25519, domain
sxbet.ledger_head_ack.v1, over ledger_id + anchored head + local head, short
TTL), i.e. an explicit owner decision after reviewing the extra lines; a bare
hash string is refused (LEDGER_HEAD_ACK_*). An unreachable store
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

CANCEL_REQUEST lines (signer/cancellation.py) consume the single-use id of the owner command (or, for
lines written before owner-command authorization, a cancel-approval nonce) and record what was requested;
they never change any order state or exposure.

Owner-command authorization (§2.6, owner decision 2): an AUTHORIZE or CANCEL_REQUEST line written for an
owner command carries `command_id` (single use: consumed for ever, in the same namespace as the legacy
approval nonces, also after a restart), `command_fingerprint_sha256` (the fingerprint of the PARSED and
resolved operation plus its reference token: the same command, however spelled, never authorises a second
operation: COMMAND_REPLAYED), `command_text_sha256`, `client_ref_sha256` and
`command_received_at`. The command text itself is never written.

KILL_SWITCH lines (signer/killswitch.py): ENGAGE (trigger, source), CANCEL_INTENT (order ids the kill
switch wants cancelled; nothing is sent from here), CANCEL_VERIFIED (per-order state read back from the
ledger after reconciliation) and RELEASE (the id of an owner-signed release, single use, bound to the seq of
the ENGAGE line it releases). While engaged, `kill_switch_engaged` is True and the signer and the gateway
refuse every new order. The state survives restarts (replay).

Counters for the risk engine v2 (stake per UTC day, orders per hour / per day, last authorization time) are
derived from the AUTHORIZE lines (`authorized_at` = the line's ts) on every replay: nothing in memory only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
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
    # FILLED -> FILLED only to record a late fill (strictly increasing, <= stake; _next refuses anything else).
    # Regression: tests/test_sxbet_orders_filled_late_fill.py.
    FILLED: frozenset({FILLED, SETTLED}),
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
KILL_SWITCH_ACTIONS = frozenset({"ENGAGE", "CANCEL_INTENT", "CANCEL_VERIFIED", "RELEASE"})
# Persistent kill-switch marker OUTSIDE the ledger (owner gate P2-A, issue #187): written before every ENGAGE
# line, checked on every open/create. The JSON marker is removed only after an owner-signed RELEASE of an
# engagement recorded after it; the empty flag and the inode witness are also cleared once a recovery has
# recorded the engagement with a JSON marker just before it (signer/killswitch.py). An engagement the ledger could not record therefore survives a restart and keeps the signer
# blocked until it is recorded (risk-reducing) and then released by the owner. Never resumes on its own.
KILL_MARKER_SCHEMA = "sxbet.kill_switch_marker.v1"
KILL_MARKER_SUFFIX = ".killswitch.json"
KILL_MARKER_UNRECORDED = "KILL_SWITCH_UNRECORDED_ENGAGEMENT"
KILL_MARKER_INVALID = "KILL_SWITCH_MARKER_INVALID"
KILL_QUARANTINE_SUFFIX = ".killswitch-quarantine"
# Inode witness (review f667594 P2-1): the set-group-ID mode bit on the LEDGER FILE itself, set with fchmod
# (an inode metadata update: no data or directory block, so it works when writes fail) BEFORE a quarantine.
# mv / cp -p keep it, so a ledger restored by hand still opens blocked. Without group-execute the kernel does
# not clear it on write. Cleared only after the owner-signed release.
KILL_INODE_WITNESS = stat.S_ISGID
COMMAND_FIELDS = frozenset({"command_id", "command_text_sha256", "command_fingerprint_sha256", "client_ref_sha256",
                            "command_received_at"})

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
    post_only: bool = False                # commanded post-only (from the AUTHORIZE line): per order, review P2-3
    unconfirmed_since: int | None = None   # ts of the first EXPIRED_UNCONFIRMED observation
    authorized_at: int = 0                 # ts of the AUTHORIZE line (risk v2 counters, rebuilt on replay)

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
    orders_since: Callable[[int], int]       # AUTHORIZE lines with ts >= the argument
    stake_since: Callable[[int], int]        # their summed stake
    last_authorized_at: int | None


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
        if order.state in (EXPIRED_UNCONFIRMED, EXPIRED, CANCELLED, FILLED):
            # FILLED too: the row is purged ~10-20 min after INACTIVE, settlement comes hours later, so only
            # complete SETTLED fills can settle it (regression: tests/test_sxbet_orders_filled_purged_settlement.py).
            return _not_found_after_expiry(order, fills if fills_ok else None, now, sha)
        if order.state in (REJECTED, PARTIALLY_FILLED):
            return _not_found_late_fill(order, fills if fills_ok else None, sha)
        if order.filled or not fills_ok or fills.get("complete") is not True or fills["fills"]:
            return none("EVIDENCE_NOT_FOUND_INCONCLUSIVE")
        if now > order.expiry + EXPIRY_GRACE_SECONDS:
            # NOT proof: a 404 may also mean "purged" or a lagging index. The stake stays reserved
            # (EXPIRED_UNCONFIRMED) until positive terminal evidence; see the module docstring.
            # Same bytes as a RECONCILED_ABSENT seen before expiry: a distinct evidence id, else the dedupe in
            # apply_evidence drops it (regression: tests/test_sxbet_orders_absence_evidence.py).
            return Evidence(EXPIRED_UNCONFIRMED, 0, "NOT_FOUND_AFTER_EXPIRY_NO_FILLS", _context_sha("AFTER_EXPIRY", sha))
        client_kind = client_lookup.get("kind") if isinstance(client_lookup, Mapping) else None
        if (order.state == UNKNOWN and client_kind == "NOT_FOUND" and order.last_send_at is not None
                and now - order.last_send_at >= ABSENT_SETTLE_SECONDS):
            # DOCS_UNVERIFIED: the docs do not state that a 404 after the settle window proves the
            # order was never accepted. It only re-opens the SAME signed order with the SAME
            # clientOrderId for re-send; exposure stays fully reserved.
            # One verdict per send attempt: a later attempt that is absent again has the same bytes.
            return Evidence(SIGNED, 0, "RECONCILED_ABSENT",
                            _context_sha(f"RECONCILED_ABSENT_AFTER_SEND_AT_{order.last_send_at}", sha))
        return none("EVIDENCE_NOT_FOUND_INCONCLUSIVE")
    return none("EVIDENCE_LOOKUP_ERROR")


def _context_sha(context: str, sha: str) -> str:
    """Evidence id of a verdict that depends on WHEN identical exchange bytes were observed."""
    return "sha256:" + hashlib.sha256((context + "|" + sha).encode("ascii")).hexdigest()


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
    """404 for an order already EXPIRED_UNCONFIRMED / EXPIRED / CANCELLED / FILLED: only fills can teach something."""
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
                 _create: bool = False, anchor: "an.AnchorStore | None" = None,
                 owner_ack: "an.LedgerHeadAck | None" = None, owner_public_key: Any = None,
                 kill_marker_path: str | None = None):
        if not isinstance(signer_address, str) or not _ADDRESS.fullmatch(signer_address.lower()):
            raise LedgerError("LEDGER_SIGNER_ADDRESS_INVALID")
        if not OrderLedger._witness_capable(path, _create):
            raise LedgerError("LEDGER_INODE_WITNESS_UNAVAILABLE")  # fail closed before any side effect
        if _create and _quarantine_siblings(path):
            # A quarantined ledger of this path exists: a new ledger never replaces it silently (P3-6).
            raise LedgerError("LEDGER_QUARANTINE_PENDING")
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
        self._nonces: set[str] = set()          # consumed single-use ids: approval nonces and owner command ids
        self._command_texts: set[str] = set()   # consumed owner-command fingerprints
        self._volatile_kill: str | None = None  # kill switch engaged while the ledger could not record it
        self.kill_marker_path = kill_marker_path or path + KILL_MARKER_SUFFIX
        self.kill_marker_status: str | None = None  # None | CONSISTENT | UNRECORDED | INVALID | STALE_REMOVED
        self._kill_engagements: list[list] = []  # [engage_seq, released] in ledger order (marker checks)
        # The ONE send/reconcile lock of this ledger, shared by every gateway, cancellation service and monitor
        # built on it (review P2-4): the monitor no longer needs a submittable gateway to get it.
        self.send_lock = threading.Lock()
        # Serialises kill-switch engage (marker + ENGAGE) and release (RELEASE + marker removal): review 30f5a96 P2-1.
        self.kill_lock = threading.RLock()
        self.genesis_sha256: str | None = None  # identity of this ledger file (bound into the LIVE flag)
        self._kill: dict[str, Any] = {"engaged": False, "engage_seq": None, "trigger": None, "intent_ids": ()}
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
            # The local flock is kept in anchored mode too (defence in depth: a misconfigured or split store
            # cannot let two instances on one host write the same file); the lease guards across hosts.
            self._fd = self._open_locked(path, create=_create)
            self._flocked = True
            # Re-check on the OPENED file (review 7ea81ac P3-A/P3-B): a file swapped between the pre-open stat and
            # the open, or a group the pre-open prediction got wrong, is refused before GENESIS or any anchor use.
            if not _fd_witness_capable(self._fd):
                if _create:
                    try:  # remove the empty file this instance just created (O_EXCL), never anything else
                        if os.path.samestat(os.fstat(self._fd), os.stat(path)) and os.fstat(self._fd).st_size == 0:
                            os.unlink(path)
                    except OSError:
                        pass
                raise LedgerError("LEDGER_INODE_WITNESS_UNAVAILABLE")
            if _create:
                # Random nonce: a new ledger file never has the identity of an old one (review P2-5).
                self._append({"kind": "GENESIS", "schema": LEDGER_SCHEMA, "signer_id": signer_id,
                              "signer_address": self.signer_address, "ledger_nonce": os.urandom(16).hex()})
                self.genesis_sha256 = self._prev
            else:
                hashes = self._replay()
                if anchor is not None:
                    self._verify_anchor(hashes, owner_ack, owner_public_key)
            self._check_kill_marker()
        except BaseException:
            self.close()
            raise

    # ---- construction (explicit: a missing ledger never means "start from zero") ----
    @classmethod
    def create(cls, path: str, *, signer_id: str, signer_address: str,
               clock: Callable[[], float] = time.time, anchor: "an.AnchorStore | None" = None,
               kill_marker_path: str | None = None) -> "OrderLedger":
        return cls(path, signer_id=signer_id, signer_address=signer_address, clock=clock, _create=True,
                   anchor=anchor, kill_marker_path=kill_marker_path)

    @classmethod
    def open(cls, path: str, *, signer_id: str, signer_address: str,
             clock: Callable[[], float] = time.time, anchor: "an.AnchorStore | None" = None,
             owner_ack: "an.LedgerHeadAck | None" = None, owner_public_key: Any = None,
             kill_marker_path: str | None = None) -> "OrderLedger":
        """`owner_ack`: only for an owner who reviewed the local lines beyond the anchored head (or a ledger
        never anchored before): an `anchor.LedgerHeadAck` signed with the owner's Ed25519 key over exactly
        (ledger_id, anchored head, local head), short TTL, verified with `owner_public_key` (the signer's own
        configuration, never the ack). A bare hash string or anything else is refused."""
        return cls(path, signer_id=signer_id, signer_address=signer_address, clock=clock, anchor=anchor,
                   owner_ack=owner_ack, owner_public_key=owner_public_key, kill_marker_path=kill_marker_path)

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

    def _owner_acked(self, ack: Any, key: Any, head: "an.AnchorHead | None", local_seq: int,
                     local_head: str, missing_code: str) -> None:
        """Only an owner-signed LedgerHeadAck for exactly this (anchored head -> local head) unlocks start."""
        if ack is None:
            raise LedgerError(missing_code)
        try:
            an.verify_head_ack(ack, key, ledger_id=self.ledger_id, anchored=head, local_seq=local_seq,
                               local_head_hash=local_head, now=int(self._clock()))
        except an.HeadAckInvalid as exc:
            raise LedgerError(exc.code) from None

    def _verify_anchor(self, hashes: list[str], owner_ack: Any, owner_public_key: Any) -> None:
        head = self._anchor_head()
        local_seq, local_head = len(hashes), self._prev
        if head is None:
            self._owner_acked(owner_ack, owner_public_key, None, local_seq, local_head, "LEDGER_ANCHOR_MISSING")
            self._anchor_advance(0, local_seq, local_head)
            return
        if head.seq > local_seq:
            raise LedgerError("LEDGER_ANCHOR_AHEAD_OF_LOCAL")  # truncated tail or rolled-back file
        if hashes[head.seq - 1] != head.head_hash:
            raise LedgerError("LEDGER_ANCHOR_HASH_MISMATCH")   # rewritten history / fork
        if head.seq < local_seq:
            self._owner_acked(owner_ack, owner_public_key, head, local_seq, local_head,
                              "LEDGER_LOCAL_AHEAD_OF_ANCHOR")
            self._anchor_advance(head.seq, local_seq, local_head)

    @staticmethod
    def _open_locked(path: str, *, create: bool) -> int:
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
        except FileExistsError:
            raise LedgerError("LEDGER_ALREADY_EXISTS") from None
        except FileNotFoundError:
            raise LedgerError("LEDGER_MISSING") from None
        except OSError:
            raise LedgerError("LEDGER_UNREADABLE") from None
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
            self._apply(written, seq=self._seq)
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
                self._apply(rec, seq=len(hashes))
        except (KeyError, TypeError, ValueError, OrderStateError):
            raise LedgerError("LEDGER_CORRUPT") from None
        if first:
            raise LedgerError("LEDGER_EMPTY")
        self.genesis_sha256 = hashes[0]
        self._prev, self._size, self._last_line, self._seq = prev, len(data), last, len(hashes)
        return hashes

    def _consume(self, rec: Mapping[str, Any], code: str) -> None:
        """One single-use id per line: a legacy approval nonce, or an owner command id + its text hash."""
        if "command_id" in rec:
            if "approval_nonce" in rec or rec["command_id"] in self._nonces \
                    or rec["command_fingerprint_sha256"] in self._command_texts:
                raise OrderStateError(code)
            self._nonces.add(rec["command_id"])
            self._command_texts.add(rec["command_fingerprint_sha256"])
        else:
            if rec["approval_nonce"] in self._nonces:
                raise OrderStateError(code)
            self._nonces.add(rec["approval_nonce"])

    def _apply(self, rec: Mapping[str, Any], seq: int | None = None) -> None:
        kind = rec["kind"]
        if kind == "AUTHORIZE":
            o = Order(client_order_id=rec["client_order_id"], intent_hash=rec["intent_hash"],
                      order_id=rec["order_id"], market_hash=rec["market_hash"], event_id=rec["event_id"],
                      stake=int(rec["stake"]), expiry=int(rec["expiry"]), state=AUTHORIZED,
                      authorized_at=int(rec["ts"]), post_only=rec.get("post_only") is True)
            if o.client_order_id in self._orders:
                raise OrderStateError("LEDGER_DUPLICATE_AUTHORIZE")
            self._consume(rec, "LEDGER_DUPLICATE_AUTHORIZE")
            self._orders[o.client_order_id] = o
            self._by_order_id[o.order_id] = o.client_order_id
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
            # Consumes a single-use id (owner command or legacy cancel approval); no state, no exposure change.
            if rec["scope"] not in CANCEL_SCOPES or not isinstance(rec["order_ids"], list):
                raise OrderStateError("LEDGER_CANCEL_REQUEST_INVALID")
            self._consume(rec, "LEDGER_CANCEL_REQUEST_INVALID")
        elif kind == "KILL_SWITCH":
            self._apply_kill(rec, seq)
        else:
            raise OrderStateError("LEDGER_UNKNOWN_RECORD")

    def _apply_kill(self, rec: Mapping[str, Any], seq: int | None) -> None:
        action = rec["action"]
        if action not in KILL_SWITCH_ACTIONS:
            raise OrderStateError("LEDGER_KILL_SWITCH_INVALID")
        if action == "ENGAGE":
            if not isinstance(rec["trigger"], str) or type(seq) is not int:
                raise OrderStateError("LEDGER_KILL_SWITCH_INVALID")
            for e in self._kill_engagements:  # a new ENGAGE supersedes an open one (review 30f5a96 P3-5)
                e[1] = True
            self._kill = {"engaged": True, "engage_seq": seq, "trigger": rec["trigger"], "intent_ids": ()}
            self._kill_engagements.append([seq, False])
        elif action == "CANCEL_INTENT":
            if not self._kill["engaged"] or not isinstance(rec["order_ids"], list):
                raise OrderStateError("LEDGER_KILL_SWITCH_INVALID")
            self._kill = {**self._kill, "intent_ids": tuple(rec["order_ids"])}
        elif action == "CANCEL_VERIFIED":
            if not isinstance(rec["results"], dict):
                raise OrderStateError("LEDGER_KILL_SWITCH_INVALID")
        else:  # RELEASE: owner-signed, bound to the ENGAGE line it releases, release id single use
            if not self._kill["engaged"] or rec["engage_seq"] != self._kill["engage_seq"] \
                    or rec["release_id"] in self._nonces:
                raise OrderStateError("LEDGER_KILL_SWITCH_RELEASE_INVALID")
            self._nonces.add(rec["release_id"])
            self._kill = {"engaged": False, "engage_seq": None, "trigger": None, "intent_ids": ()}
            for e in self._kill_engagements:
                if e[0] == rec["engage_seq"]:
                    e[1] = True

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

    def command_consumed(self, command_id: str) -> bool:
        """An owner command id (or any single-use id) already consumed: refused for ever."""
        with self._lock:
            return command_id in self._nonces

    def command_fingerprint_consumed(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._command_texts

    # ---- persistent kill-switch marker (owner gate P2-A) ----
    @property
    def kill_flag_path(self) -> str:
        """Zero-byte fallback marker: creating an empty file needs no data block, so it can succeed when the
        JSON marker cannot be written; it is also created before a quarantine (review 30f5a96 P2-2)."""
        return self.kill_marker_path + ".flag"

    @staticmethod
    def _witness_capable(path: str, create: bool) -> bool:
        """Checked BEFORE any side effect (no GENESIS, no anchor move, review 63d14d3 P3-1). The kernel keeps
        S_ISGID set by fchmod only if the file's group is one of the caller's groups or the caller has
        CAP_FSETID (root without it, e.g. in a container, is not enough: review 63d14d3 P3-2); a non-root
        caller must also own the file."""
        try:
            if create:
                d = os.stat(os.path.dirname(os.path.abspath(path)) or ".")
                uid, gid = os.geteuid(), (d.st_gid if d.st_mode & stat.S_ISGID else os.getegid())
            else:
                st = os.stat(path)
                uid, gid = st.st_uid, st.st_gid
        except OSError:
            return True  # missing / unreadable: the open itself reports it with its own fixed code
        if os.geteuid() != 0 and uid != os.geteuid():
            return False
        return gid in {os.getegid(), *os.getgroups()} or _has_cap_fsetid()

    def _check_kill_marker(self) -> None:
        """On every open/create: a marker whose engagement the ledger does not hold keeps the signer blocked."""
        flag = os.path.lexists(self.kill_flag_path) or self.inode_witness_set()
        try:
            with open(self.kill_marker_path, "rb") as fh:
                raw = fh.read(4097)
        except FileNotFoundError:
            raw = b"" if flag else None
        except OSError:
            raw = None
            flag = True  # unreadable marker: fail closed below
        if raw == b"" and flag and not os.path.lexists(self.kill_marker_path):
            # (the flag here may also be the inode witness of a quarantined-and-restored ledger)
            # Only the empty flag: it carries no seq, so it may belong to a LATER engagement than the one on the
            # ledger (review c139822 P3-1). Always UNRECORDED: the KillSwitch records a new ENGAGE that
            # supersedes the old one, and the owner signs the release of THAT engagement.
            self.kill_marker_status = "UNRECORDED"
            self._volatile_kill = KILL_MARKER_UNRECORDED
            return
        if raw is None and not flag:
            self.kill_marker_status = None
            return
        marker = _parse_kill_marker(raw, self.ledger_id, self.genesis_sha256)
        if marker is None:  # unreadable, malformed, foreign or of another ledger file: fail closed
            self.kill_marker_status = "INVALID"
            self._volatile_kill = KILL_MARKER_INVALID
            return
        after = [e for e in self._kill_engagements if e[0] > marker["ledger_seq"]]
        if self._kill["engaged"] and after and not flag:
            self.kill_marker_status = "CONSISTENT"  # recorded and still engaged: the normal release applies
        elif after and all(e[1] for e in after) and not flag:
            # Recorded after the marker and closed by an owner-signed RELEASE: the marker is stale.
            self._unlink(self.kill_marker_path)
            self.kill_marker_status = "STALE_REMOVED"
        else:
            self.kill_marker_status = "UNRECORDED"
            self._volatile_kill = KILL_MARKER_UNRECORDED

    def write_kill_marker(self, *, trigger: str, source: str) -> bool:
        """Atomic, fsync'd marker written BEFORE the ENGAGE line (falls back to the empty flag). False = neither."""
        with self._lock:
            body = json.dumps({"schema": KILL_MARKER_SCHEMA, "ledger_id": self.ledger_id,
                               "genesis_sha256": self.genesis_sha256, "ledger_seq": self._seq,
                               "trigger": trigger, "source": source, "marked_at": int(self._clock()),
                               "marker_id": os.urandom(16).hex()},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            # Random temp name, never following a symlink nor reusing a file (review 30f5a96 P3-1).
            tmp = f"{self.kill_marker_path}.tmp-{os.urandom(8).hex()}"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_CLOEXEC", 0), 0o600)
                try:
                    os.write(fd, body)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, self.kill_marker_path)
                _fsync_dir(self.kill_marker_path)
                return True
            except OSError:
                self._unlink(tmp)
                return self._create_kill_flag()

    def _create_kill_flag(self) -> bool:
        try:
            fd = os.open(self.kill_flag_path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0), 0o600)
            os.close(fd)
            _fsync_dir(self.kill_flag_path)
            return True
        except OSError:
            return os.path.lexists(self.kill_flag_path)

    @staticmethod
    def _unlink(path: str) -> bool:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            _fsync_dir(path)
        except OSError:
            pass
        return True

    def inode_witness_set(self) -> bool:
        try:
            return self._fd is not None and bool(os.fstat(self._fd).st_mode & KILL_INODE_WITNESS)
        except OSError:
            return True  # cannot tell: fail closed

    def set_inode_witness(self) -> bool:
        with self._lock:
            try:
                mode = stat.S_IMODE(os.fstat(self._fd).st_mode)
                os.fchmod(self._fd, (mode & ~stat.S_IXGRP) | KILL_INODE_WITNESS)
                try:
                    os.fsync(self._fd)  # review f1e8cb8 P3: the mode change is durable before the rename
                except OSError:
                    pass
                # The kernel drops S_ISGID silently when the file's group is not the caller's: verify.
                return bool(os.fstat(self._fd).st_mode & KILL_INODE_WITNESS)
            except (OSError, TypeError):
                return False

    def _clear_inode_witness(self) -> bool:
        try:
            mode = stat.S_IMODE(os.fstat(self._fd).st_mode)
            if mode & KILL_INODE_WITNESS:
                os.fchmod(self._fd, mode & ~KILL_INODE_WITNESS)
            return not self.inode_witness_set()
        except (OSError, TypeError):
            return False

    def drop_kill_flag_after_recorded_engagement(self) -> bool:
        """Flag and inode witness are superseded once the JSON marker written JUST before the current ENGAGE
        line is on disk (marker seq == engage seq - 1), on a sound ledger with no other volatile engagement
        (review f667594 P3-1). The caller holds `kill_lock`."""
        with self._lock:
            if not self._kill["engaged"] or self._poisoned or self._volatile_kill not in (None, KILL_MARKER_UNRECORDED,
                                                                                          KILL_MARKER_INVALID):
                return False
            try:
                with open(self.kill_marker_path, "rb") as fh:
                    marker = _parse_kill_marker(fh.read(4097), self.ledger_id, self.genesis_sha256)
            except OSError:
                return False
            if marker is None or marker["ledger_seq"] != self._kill["engage_seq"] - 1:
                return False
            return self._unlink(self.kill_flag_path) and self._clear_inode_witness()

    def remove_kill_marker_after_release(self, released_engage_seq: int) -> str:
        """After an owner-signed RELEASE only. Removes the marker only if it predates the released engagement
        (a marker written by a later engagement is never removed, review 30f5a96 P2-1), the empty flag and the
        inode witness. Returns REMOVED, KEPT_LATER_ENGAGEMENT or UNREMOVABLE."""
        with self._lock:
            if self._kill["engaged"]:
                raise OrderStateError("KILL_SWITCH_NOT_RELEASED")
            try:
                with open(self.kill_marker_path, "rb") as fh:
                    raw = fh.read(4097)
            except FileNotFoundError:
                raw = None
            except OSError:
                return "UNREMOVABLE"
            if raw is not None:
                marker = _parse_kill_marker(raw, self.ledger_id, self.genesis_sha256)
                if marker is not None and marker["ledger_seq"] >= released_engage_seq:
                    return "KEPT_LATER_ENGAGEMENT"
                if not self._unlink(self.kill_marker_path):
                    return "UNREMOVABLE"
            ok = self._unlink(self.kill_flag_path) and self._clear_inode_witness()
            return "REMOVED" if ok else "UNREMOVABLE"

    def quarantine_for_kill_switch(self) -> bool:
        return self.quarantine_for_kill_switch_detailed()[0]

    def quarantine_for_kill_switch_detailed(self) -> tuple[bool, bool]:
        """Last resort when neither the marker nor the ENGAGE line could be written: the empty flag is tried
        once more, then the ledger file is renamed (a metadata operation that works when writes fail), the
        instance is poisoned, and a restart finds no ledger (LEDGER_MISSING): nothing resumes without the
        owner. The file content is never modified. Restoring the file requires the owner procedure in
        signer/README.md (record the engagement, then the owner-signed release)."""
        with self._lock:
            self._poisoned = True
            witness = self.set_inode_witness()  # survives mv / cp -p of the quarantined file (review f667594 P2-1)
            self._create_kill_flag()
            target = f"{self._path}{KILL_QUARANTINE_SUFFIX}-{int(self._clock())}-{os.urandom(4).hex()}"
            try:
                if os.path.lexists(target):
                    return False, witness
                os.rename(self._path, target)
            except OSError:
                return False, witness
            try:
                _fsync_dir(self._path)
            except OSError:
                pass  # renamed; the directory entry may need the next sync, reported as quarantined
            return True, witness

    @property
    def kill_switch_engaged(self) -> bool:
        """Engaged on the ledger OR engaged in memory because the ledger could not record it (review P3)."""
        with self._lock:
            return bool(self._kill["engaged"]) or self._volatile_kill is not None

    @property
    def volatile_kill(self) -> str | None:
        with self._lock:
            return self._volatile_kill

    def restore_volatile_kill(self, value: str | None) -> None:
        with self._lock:
            self._volatile_kill = value

    def engage_volatile_kill(self, trigger: str) -> None:
        with self._lock:
            self._volatile_kill = trigger

    def clear_volatile_kill(self) -> None:
        with self._lock:
            self._volatile_kill = None

    def kill_switch_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._kill)

    def orders_since(self, ts: int) -> int:
        with self._lock:
            return sum(1 for o in self._orders.values() if o.authorized_at >= ts)

    def stake_since(self, ts: int) -> int:
        with self._lock:
            return sum(o.stake for o in self._orders.values() if o.authorized_at >= ts)

    def last_authorized_at(self) -> int | None:
        with self._lock:
            return max((o.authorized_at for o in self._orders.values()), default=None)

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
                                realized_pnl_since=self.realized_pnl_since, orders_since=self.orders_since,
                                stake_since=self.stake_since, last_authorized_at=self.last_authorized_at())

    # ---- writes ----
    @staticmethod
    def _command_ok(command: Any) -> bool:
        return (isinstance(command, Mapping) and set(command) == COMMAND_FIELDS
                and isinstance(command["command_id"], str) and _NONCE.fullmatch(command["command_id"]) is not None
                and all(isinstance(command[k], str) and _SHA.fullmatch(command[k]) for k in
                        ("command_text_sha256", "command_fingerprint_sha256", "client_ref_sha256"))
                and type(command["command_received_at"]) is int)

    def _check_single_use(self, approval_nonce: Any, command: Any, replay_code: str) -> dict[str, Any]:
        """Exactly one of `approval_nonce` (legacy) or `command` (owner command). Returns the line fields."""
        if command is not None:
            if approval_nonce is not None or not self._command_ok(command):
                raise OrderStateError("AUTHORIZE_FIELDS_INVALID")
            if command["command_id"] in self._nonces:
                raise OrderStateError("COMMAND_ID_ALREADY_CONSUMED")
            if command["command_fingerprint_sha256"] in self._command_texts:
                raise OrderStateError("COMMAND_REPLAYED")
            return dict(command)
        if not _NONCE.fullmatch(approval_nonce or ""):
            raise OrderStateError("AUTHORIZE_FIELDS_INVALID")
        if approval_nonce in self._nonces:
            raise OrderStateError(replay_code)
        return {"approval_nonce": approval_nonce}

    def authorize(self, *, client_order_id: str, intent_hash: str, order_id: str, market_hash: str, event_id: str,
                  stake: int, expiry: int, approval_nonce: str | None = None,
                  command: Mapping[str, Any] | None = None, post_only: bool | None = None,
                  precheck: Callable[[ExposureView], None] | None = None) -> Order:
        """Atomically: re-check the single-use id (and command text) + key unused, run `precheck` on the live
        exposure, then persist ONE fsync'd AUTHORIZE line that consumes the id and reserves the full stake."""
        if not (_KEY.fullmatch(client_order_id or "")
                and _HEX32.fullmatch(order_id or "") and _HEX32.fullmatch(market_hash or "")
                and isinstance(event_id, str) and event_id and type(stake) is int and stake > 0
                and type(expiry) is int and (post_only is None or type(post_only) is bool)
                and (command is not None or _NONCE.fullmatch(approval_nonce or ""))):
            raise OrderStateError("AUTHORIZE_FIELDS_INVALID")
        with self._lock:
            self._writable()
            single_use = self._check_single_use(approval_nonce, command, "APPROVAL_NONCE_REPLAYED")
            if client_order_id in self._orders:
                raise OrderStateError("IDEMPOTENCY_KEY_ALREADY_AUTHORIZED")
            if order_id in self._by_order_id:
                raise OrderStateError("ORDER_ID_ALREADY_AUTHORIZED")
            # Under the ledger lock (review P2-1): an ENGAGE written by another thread between the signer's
            # first check and here is seen, so no order is authorized after the kill switch engaged.
            if self._kill["engaged"] or self._volatile_kill is not None:
                raise OrderStateError("KILL_SWITCH_ENGAGED")
            if precheck is not None:
                precheck(self.exposure())
                if self._kill["engaged"] or self._volatile_kill is not None:  # engaged by the precheck's thread
                    raise OrderStateError("KILL_SWITCH_ENGAGED")
            rec = {"kind": "AUTHORIZE", "client_order_id": client_order_id, "intent_hash": intent_hash,
                   "order_id": order_id, "market_hash": market_hash, "event_id": event_id, "stake": str(stake),
                   "expiry": expiry, **single_use}
            if post_only is not None:
                rec["post_only"] = post_only
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

    def record_cancel_request(self, *, scope: str, binding_sha256: str, order_ids: list[str],
                              approval_nonce: str | None = None, command: Mapping[str, Any] | None = None,
                              market_hash: str | None = None, event_id: str | None = None) -> None:
        """One fsync'd CANCEL_REQUEST line, written BEFORE the cancel is sent: consumes the owner command's
        single-use id and text hash (or a legacy cancel approval nonce); survives restarts. It records the
        request only; no order state or exposure changes here -- only exchange evidence does that."""
        ids = list(order_ids) if isinstance(order_ids, (list, tuple)) else None
        if not ((command is not None or _NONCE.fullmatch(approval_nonce or "")) and scope in CANCEL_SCOPES
                and isinstance(binding_sha256, str) and _SHA.fullmatch(binding_sha256)
                and ids is not None and len(ids) <= 1000 and len(set(ids)) == len(ids)
                and all(isinstance(i, str) and _HEX32.fullmatch(i) for i in ids)
                and (market_hash is None or (isinstance(market_hash, str) and _HEX32.fullmatch(market_hash)))
                and (event_id is None or (isinstance(event_id, str) and 0 < len(event_id) <= 64))):
            raise OrderStateError("CANCEL_REQUEST_FIELDS_INVALID")
        with self._lock:
            self._writable()
            single_use = self._check_single_use(approval_nonce, command, "APPROVAL_NONCE_REPLAYED")
            rec = {"kind": "CANCEL_REQUEST", **single_use, "scope": scope,
                   "binding_sha256": binding_sha256, "order_ids": ids}
            if market_hash is not None:
                rec["market_hash"] = market_hash
            if event_id is not None:
                rec["event_id"] = event_id
            self._commit(rec)

    def record_kill_switch(self, action: str, **fields: Any) -> int:
        """One fsync'd KILL_SWITCH line (signer/killswitch.py builds the fields). Returns its seq."""
        allowed = {"ENGAGE": {"trigger", "source", "triggers"}, "CANCEL_INTENT": {"order_ids"},
                   "CANCEL_VERIFIED": {"results"}, "RELEASE": {"release_id", "release_sha256", "engage_seq"}}
        if action not in KILL_SWITCH_ACTIONS or set(fields) != allowed[action]:
            raise OrderStateError("KILL_SWITCH_FIELDS_INVALID")
        with self._lock:
            self._writable()
            if action in ("CANCEL_INTENT", "RELEASE") and not self._kill["engaged"]:
                raise OrderStateError("KILL_SWITCH_NOT_ENGAGED")
            if action == "RELEASE":
                if fields["engage_seq"] != self._kill["engage_seq"]:
                    raise OrderStateError("KILL_SWITCH_RELEASE_MISMATCH")
                if fields["release_id"] in self._nonces:
                    raise OrderStateError("KILL_SWITCH_RELEASE_REPLAYED")
            self._commit({"kind": "KILL_SWITCH", "action": action, **fields})
            return self._seq

    def engage_kill_switch(self, *, trigger: str, source: str, triggers: list[str],
                           intents: Callable[[list[Order]], list[str]]) -> list[str]:
        """ENGAGE + CANCEL_INTENT written under ONE hold of the ledger lock (review P2-1): no AUTHORIZE can
        slip between them, so the intent snapshot covers every order that exists when the switch engages."""
        with self._lock:
            self.record_kill_switch("ENGAGE", trigger=trigger, source=source, triggers=triggers)
            ids = intents(list(self._orders.values()))
            self.record_kill_switch("CANCEL_INTENT", order_ids=ids)
            return ids

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


def _fsync_dir(path: str) -> None:
    fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_kill_marker(raw: bytes | None, ledger_id: str, genesis_sha256: str | None) -> dict[str, Any] | None:
    if raw is None or len(raw) > 4096:
        return None
    try:
        doc = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != KILL_MARKER_SCHEMA or doc.get("ledger_id") != ledger_id:
        return None
    if type(doc.get("ledger_seq")) is not int or doc["ledger_seq"] < 0:
        return None
    # Bound to this ledger FILE, not only to signer_id:address shared by recreated ledgers (review 30f5a96 P3-2).
    if genesis_sha256 is None or doc.get("genesis_sha256") != genesis_sha256:
        return None
    return doc


def _has_cap_fsetid() -> bool:
    """CAP_FSETID (bit 4) in the effective capability set; False when it cannot be read (fail closed)."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << 4))
    except (OSError, ValueError):
        pass
    return False


def _quarantine_siblings(path: str) -> bool:
    base = os.path.basename(path) + KILL_QUARANTINE_SUFFIX
    try:
        return any(n.startswith(base) for n in os.listdir(os.path.dirname(os.path.abspath(path)) or "."))
    except OSError:
        return True  # cannot tell: fail closed


def _fd_witness_capable(fd: int) -> bool:
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    euid = os.geteuid()
    if euid != 0 and st.st_uid != euid:
        return False
    return st.st_gid in {os.getegid(), *os.getgroups()} or _has_cap_fsetid()
