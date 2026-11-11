"""Monitoring worker for the isolated signer: pure logic, network-free, NEVER submits, signs or cancels (§2.6).

What one `Monitor.tick()` does:
1. reconciliation: every ledger order that may still change (SUBMITTED, OPEN, PARTIALLY_FILLED, UNKNOWN,
   EXPIRED_UNCONFIRMED, SIGNED past expiry, REJECTED with a live hold, FILLED / CANCELLED / EXPIRED holding a
   matched amount until settlement) is re-read through the INJECTED read functions (`get_order`,
   `get_order_by_client_id`, `get_fills`, the shapes of signer.gateway.SubmissionTransport) and the evidence is
   applied with the same `evidence_from_exchange` / `OrderLedger.apply_evidence` the gateway uses, re-reading
   the order, reading the exchange and applying all under the MANDATORY shared gateway lock. The monitor is
   bound to THE gateway of THIS ledger (`gateway=`; review #195 P3): it takes `gateway._lock` itself, so an
   arbitrary context manager can no longer be passed as "the lock" (MONITOR_GATEWAY_REQUIRED /
   MONITOR_GATEWAY_FOREIGN). Only the
   three read methods are reachable from here (`_ReadOnly`): a sender, an adapter's `send`, `submit_*` or a
   cancel are never called;
2. expiry handling: alerts for orders past expiry that are still reserved, EXPIRED_UNCONFIRMED orders and the
   time their confirmation window ends, AUTHORIZED orders never signed (operator: Signer.abandon_unsigned),
   UNKNOWN orders and orders reconciled absent (a re-send stays a gateway decision of the owner's process);
3. heartbeat POLICY evaluation (`evaluate_heartbeat`): POST /heartbeat/v3 is native SX Bet, but there is no
   heartbeat sender in this repository; the monitor only says when a renewal is due or the lapse happened
   (a lapse cancels ALL orders with HEARTBEAT_TIMEOUT, reconciled by step 1);
4. rules: for every market with matched exposure an injected `market_snapshot(market_hash)` supplies the
   position / market / prices / hedge simulation, and an injected `evaluate_rules` (e.g.
   bridge.sxbet.rules.evaluate, which signer/ must not import) evaluates the owner's rules. The trailing-stop
   PEAK is persisted here (monotonic, saved BEFORE any proposal is emitted), so a restart never forgets it.
   With no stored peak and no integer mark-to-market the peak is passed as None, never invented as 0
   (review #195 P3): an evaluator that refuses None yields RULES_EVALUATION_REFUSED, not a fake value.
   A proposal is only a proposal: `executable`, `signed` and `submitted` must be False (else MONITOR_PROPOSAL_
   CLAIMS_EXECUTABLE); executing it needs a fresh owner approval through the normal signer path.

Persistence: `MonitorState` is one JSON file written atomically (temp file + fsync + rename + directory
fsync) with a sha256 over the canonical body, bound to one ledger_id. A missing file is created only by the
explicit `MonitorState.create`; a corrupt or foreign file fails closed (MonitorError). Order state is never
kept here: after a restart it is rebuilt from the ledger replay.
Integrity (review #195 P3): every field is type-checked on open (exact key set; trailing rows exactly
{peak: int, updated_at: int}; heartbeat_ack_at int or null; proposals_seen a list of strings; ticks a
non-negative int) => MONITOR_STATE_CORRUPT otherwise. With `mac_key` (>= 32 bytes, from the signer host's
own configuration) the body is protected by HMAC-SHA256 instead of a bare sha256, so someone who can write
the file but does not hold the key cannot forge a peak; a keyed file opened without the key, or a plain file
opened with a key (downgrade), fails closed. WITHOUT a key the sha256 only detects accidental corruption,
not deliberate tampering by someone with write access to the file: that is a documented residual risk (the
trailing peak only feeds proposals, never an order).

Tick report: `orders_submitted`, `orders_cancelled` and `network_writes` are always 0 BY CONSTRUCTION (the
monitor holds only the three read methods); the report says so (`write_counters:
CONSTANT_BY_CONSTRUCTION`). `exchange_reads` is measured.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .gateway import SubmissionGateway
from .orders import (AUTHORIZED, CANCELLED, EXPIRED, EXPIRED_UNCONFIRMED, EXPIRY_CONFIRM_SECONDS, EXPIRY_GRACE_SECONDS,
                     FILLED, OPEN, PARTIALLY_FILLED, REJECTED, SIGNED, SUBMITTED, UNKNOWN, LedgerError, OrderLedger,
                     OrderStateError, evidence_from_exchange)

MONITOR_SCHEMA = "sxbet.monitor_state.v1"
INFO, WARN, CRITICAL = "INFO", "WARN", "CRITICAL"
_LIVE = frozenset({SUBMITTED, OPEN, PARTIALLY_FILLED, UNKNOWN, EXPIRED_UNCONFIRMED})
_HOLDING = frozenset({FILLED, CANCELLED, EXPIRED})
_MAX_SEEN = 1000
_BODY_KEYS = frozenset({"schema", "ledger_id", "trailing", "heartbeat_ack_at", "proposals_seen", "ticks"})


class MonitorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Alert:
    severity: str
    code: str
    client_order_id: str | None = None
    market_hash: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HeartbeatPolicy:
    """Owner/deployment policy for POST /heartbeat/v3 (timeoutSeconds 0..3600 documented)."""
    timeout_seconds: int
    renew_margin_seconds: int

    def __post_init__(self):
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600:
            raise MonitorError("HEARTBEAT_TIMEOUT_INVALID")
        if type(self.renew_margin_seconds) is not int or not 0 < self.renew_margin_seconds < self.timeout_seconds:
            raise MonitorError("HEARTBEAT_MARGIN_INVALID")


def evaluate_heartbeat(policy: HeartbeatPolicy | None, *, last_ack_at: int | None, open_orders: int,
                       now: int) -> list[Alert]:
    """Pure. Never sends anything: it says what the heartbeat process (not part of this repository) must do."""
    if policy is None:
        return [Alert(INFO, "HEARTBEAT_NOT_CONFIGURED", detail={"open_orders": open_orders})] if open_orders else []
    if last_ack_at is None:
        return [Alert(WARN, "HEARTBEAT_NEVER_ACKNOWLEDGED", detail={"open_orders": open_orders})] if open_orders \
            else []
    age = now - last_ack_at
    detail = {"age_seconds": age, "timeout_seconds": policy.timeout_seconds, "open_orders": open_orders}
    if age > policy.timeout_seconds:
        return [Alert(CRITICAL, "HEARTBEAT_LAPSED_ORDERS_MAY_BE_CANCELLED", detail=detail)]
    if age >= policy.timeout_seconds - policy.renew_margin_seconds:
        return [Alert(WARN, "HEARTBEAT_RENEW_DUE", detail=detail)]
    return []


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


class MonitorState:
    """Persisted monitor memory: trailing peaks, last heartbeat ack, proposals already emitted, tick count."""

    def __init__(self, path: str, body: dict[str, Any], mac_key: bytes | None = None):
        self._path = path
        self._body = body
        self._mac_key = _check_key(mac_key)

    @classmethod
    def create(cls, path: str, *, ledger_id: str, mac_key: bytes | None = None) -> "MonitorState":
        if os.path.exists(path):
            raise MonitorError("MONITOR_STATE_ALREADY_EXISTS")
        st = cls(path, {"schema": MONITOR_SCHEMA, "ledger_id": ledger_id, "trailing": {}, "heartbeat_ack_at": None,
                        "proposals_seen": [], "ticks": 0}, mac_key)
        st.save()
        return st

    @classmethod
    def open(cls, path: str, *, ledger_id: str, mac_key: bytes | None = None) -> "MonitorState":
        key = _check_key(mac_key)
        try:
            with open(path, "rb") as fh:
                doc = json.loads(fh.read().decode("ascii"))
        except FileNotFoundError:
            raise MonitorError("MONITOR_STATE_MISSING") from None
        except (OSError, ValueError, UnicodeDecodeError):
            raise MonitorError("MONITOR_STATE_CORRUPT") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("body"), dict):
            raise MonitorError("MONITOR_STATE_CORRUPT")
        body = doc["body"]
        if key is None:
            if set(doc) == {"body", "hmac_sha256"}:
                raise MonitorError("MONITOR_STATE_MAC_KEY_REQUIRED")
            if set(doc) != {"body", "sha256"} or doc["sha256"] != hashlib.sha256(_canon(body)).hexdigest():
                raise MonitorError("MONITOR_STATE_CORRUPT")
        else:
            if set(doc) != {"body", "hmac_sha256"}:
                raise MonitorError("MONITOR_STATE_MAC_MISSING")  # never downgrade to an unkeyed file
            if not isinstance(doc["hmac_sha256"], str) or not hmac.compare_digest(doc["hmac_sha256"],
                                                                                   _mac(key, body)):
                raise MonitorError("MONITOR_STATE_MAC_INVALID")
        _check_body(body)
        if body["ledger_id"] != ledger_id:
            raise MonitorError("MONITOR_STATE_FOREIGN")
        return cls(path, body, key)

    def save(self) -> None:
        _check_body(self._body)
        if self._mac_key is None:
            doc = {"body": self._body, "sha256": hashlib.sha256(_canon(self._body)).hexdigest()}
        else:
            doc = {"body": self._body, "hmac_sha256": _mac(self._mac_key, self._body)}
        data = _canon(doc)
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        dfd = os.open(os.path.dirname(os.path.abspath(self._path)), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    @property
    def ledger_id(self) -> str:
        return self._body["ledger_id"]

    @property
    def ticks(self) -> int:
        return self._body["ticks"]

    def peak(self, market_hash: str) -> int | None:
        row = self._body["trailing"].get(market_hash)
        return None if row is None else row["peak"]

    def raise_peak(self, market_hash: str, value: int, now: int) -> int:
        """Monotonic: the stored peak never goes down. Returns the peak now stored (not yet saved)."""
        cur = self.peak(market_hash)
        if cur is None or value > cur:
            self._body["trailing"][market_hash] = {"peak": value, "updated_at": now}
            return value
        return cur

    def reset_peak(self, market_hash: str) -> None:
        """Only when the position is gone (no matched exposure left on the market)."""
        self._body["trailing"].pop(market_hash, None)

    @property
    def heartbeat_ack_at(self) -> int | None:
        return self._body["heartbeat_ack_at"]

    def record_heartbeat_ack(self, at: int) -> None:
        """Fed by whatever process owns the heartbeat (none in this repository)."""
        if type(at) is not int:
            raise MonitorError("HEARTBEAT_ACK_INVALID")
        self._body["heartbeat_ack_at"] = at
        self.save()

    def seen(self, proposal_hash: str) -> bool:
        return proposal_hash in self._body["proposals_seen"]

    def mark_seen(self, proposal_hash: str) -> None:
        seen = self._body["proposals_seen"]
        if proposal_hash not in seen:
            seen.append(proposal_hash)
            del seen[:-_MAX_SEEN]

    def bump(self) -> None:
        self._body["ticks"] += 1


def _check_key(key: Any) -> bytes | None:
    if key is None:
        return None
    if not isinstance(key, (bytes, bytearray)) or len(key) < 32:
        raise MonitorError("MONITOR_STATE_MAC_KEY_INVALID")
    return bytes(key)


def _mac(key: bytes, body: Mapping[str, Any]) -> str:
    return hmac.new(key, b"sxbet.monitor_state.v1\x00" + _canon(body), hashlib.sha256).hexdigest()


def _check_body(body: Any) -> None:
    """Type-check every field: a well-hashed but ill-typed body is still refused (fail closed)."""
    ok = (isinstance(body, dict) and set(body) == _BODY_KEYS and body["schema"] == MONITOR_SCHEMA
          and isinstance(body["ledger_id"], str) and isinstance(body["trailing"], dict)
          and all(isinstance(k, str) and isinstance(v, dict) and set(v) == {"peak", "updated_at"}
                  and type(v["peak"]) is int and type(v["updated_at"]) is int for k, v in body["trailing"].items())
          and (body["heartbeat_ack_at"] is None or type(body["heartbeat_ack_at"]) is int)
          and isinstance(body["proposals_seen"], list) and len(body["proposals_seen"]) <= _MAX_SEEN
          and all(isinstance(x, str) for x in body["proposals_seen"])
          and type(body["ticks"]) is int and body["ticks"] >= 0)
    if not ok:
        raise MonitorError("MONITOR_STATE_CORRUPT")


class _ReadOnly:
    """Exposes ONLY the three documented reads of the injected object; nothing else is reachable."""

    __slots__ = ("_get_order", "_get_order_by_client_id", "_get_fills", "calls")

    def __init__(self, reads: Any):
        self._get_order = reads.get_order
        self._get_order_by_client_id = reads.get_order_by_client_id
        self._get_fills = reads.get_fills
        self.calls = 0  # measured: exchange reads issued

    def _safe(self, fn, arg) -> Mapping[str, Any]:
        self.calls += 1
        try:
            out = fn(arg)
        except Exception:
            return {"kind": "ERROR"}
        return out if isinstance(out, Mapping) else {"kind": "ERROR"}

    def lookups(self, o) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        return (self._safe(self._get_order, o.order_id), self._safe(self._get_order_by_client_id, o.client_order_id),
                self._safe(self._get_fills, o.order_id))


class Monitor:
    def __init__(self, ledger: OrderLedger, reads: Any, state: MonitorState, *, clock: Callable[[], float] = time.time,
                 heartbeat_policy: HeartbeatPolicy | None = None, rules: Mapping[str, Any] | None = None,
                 evaluate_rules: Callable[..., Mapping[str, Any]] | None = None,
                 market_snapshot: Callable[[str], Mapping[str, Any] | None] | None = None,
                 gateway: SubmissionGateway | None = None, settlement_poll_seconds: int = 300):
        if not isinstance(ledger, OrderLedger):
            raise MonitorError("MONITOR_LEDGER_INVALID")
        if not isinstance(state, MonitorState) or state.ledger_id != ledger.ledger_id:
            raise MonitorError("MONITOR_STATE_FOREIGN")
        if rules is not None and (evaluate_rules is None or market_snapshot is None):
            raise MonitorError("MONITOR_RULES_NEED_EVALUATOR_AND_SNAPSHOT")
        self._ledger = ledger
        self._reads = _ReadOnly(reads)
        self._state = state
        self._clock = clock
        self._hb = heartbeat_policy
        self._rules = copy.deepcopy(dict(rules)) if rules is not None else None
        self._evaluate = evaluate_rules
        self._snapshot = market_snapshot
        # Mandatory: the SAME lock as the gateway / cancellation service in the signer process, taken from THE
        # gateway of this ledger (review #195 P3): no private lock, no arbitrary context manager.
        if not isinstance(gateway, SubmissionGateway):
            raise MonitorError("MONITOR_GATEWAY_REQUIRED")
        if gateway._ledger is not ledger:
            raise MonitorError("MONITOR_GATEWAY_FOREIGN")
        self._lock = gateway._lock
        self._poll = settlement_poll_seconds
        self._last_poll: dict[str, int] = {}

    # ---------------------------------------------------------------- 1. reconciliation
    def _due(self, o, now: int) -> bool:
        if o.state in _LIVE:
            return True
        if o.state == SIGNED:
            return now > o.expiry + EXPIRY_GRACE_SECONDS
        if o.state == REJECTED:
            return o.rejected_hold_active(now)
        if o.state in _HOLDING and o.filled:
            return now - self._last_poll.get(o.client_order_id, -10 ** 12) >= self._poll
        return False

    def _reconcile(self, now: int, report: dict) -> None:
        for key in [x.client_order_id for x in self._ledger.orders()]:
            # Like gateway._reconcile_locked: the current order is re-read, the exchange is read and the evidence
            # is applied all under the shared lock, so no gateway send / reconcile can interleave with a snapshot.
            with self._lock:
                o = self._ledger.get(key)
                if o is None or not self._due(o, now):
                    continue
                self._last_poll[o.client_order_id] = now
                lookup, client, fills = self._reads.lookups(o)
                ev = evidence_from_exchange(o, lookup=lookup, client_lookup=client, fills=fills, now=now)
                try:
                    new = self._ledger.apply_evidence(o.client_order_id, ev)
                except OrderStateError as exc:
                    report["alerts"].append(Alert(WARN, "EVIDENCE_CONFLICT", o.client_order_id, o.market_hash,
                                                  {"reason": exc.code}))
                    continue
                except LedgerError as exc:
                    report["alerts"].append(Alert(CRITICAL, "LEDGER_WRITE_FAILED_STOPPED", o.client_order_id,
                                                  o.market_hash, {"reason": exc.code}))
                    raise MonitorError(exc.code) from None
            if new.state != o.state or new.filled != o.filled:
                report["reconciled"].append({"client_order_id": o.client_order_id, "from": o.state, "to": new.state,
                                             "filled": new.filled, "reason": ev.reason})
                if o.state == UNKNOWN and new.state == SIGNED:
                    report["alerts"].append(Alert(WARN, "RECONCILED_ABSENT_RESEND_IS_A_GATEWAY_DECISION",
                                                  o.client_order_id, o.market_hash))

    # ---------------------------------------------------------------- 2. expiry handling
    def _expiry_alerts(self, now: int, report: dict) -> None:
        for o in self._ledger.orders():
            past = now > o.expiry + EXPIRY_GRACE_SECONDS
            if o.state == AUTHORIZED:
                report["alerts"].append(Alert(WARN, "AUTHORIZED_NEVER_SIGNED_REVIEW_ABANDON_UNSIGNED",
                                              o.client_order_id, o.market_hash, {"reserved": o.reserved_at(now)}))
            elif o.state == UNKNOWN:
                report["alerts"].append(Alert(WARN, "UNKNOWN_RECONCILIATION_PENDING", o.client_order_id,
                                              o.market_hash, {"reserved": o.reserved_at(now)}))
            elif o.state == EXPIRED_UNCONFIRMED:
                report["alerts"].append(Alert(INFO, "EXPIRY_UNCONFIRMED_STAKE_HELD", o.client_order_id, o.market_hash,
                                              {"confirm_after": (o.unconfirmed_since or now) + EXPIRY_CONFIRM_SECONDS,
                                               "reserved": o.reserved_at(now)}))
            elif o.state in (SIGNED, SUBMITTED, OPEN, PARTIALLY_FILLED) and past:
                report["alerts"].append(Alert(WARN, "PAST_EXPIRY_STILL_RESERVED", o.client_order_id, o.market_hash,
                                              {"reserved": o.reserved_at(now), "expiry": o.expiry}))
            if o.state == PARTIALLY_FILLED:
                report["alerts"].append(Alert(INFO, "PARTIAL_FILL_RESIDUAL_RESERVED", o.client_order_id,
                                              o.market_hash, {"filled": o.filled, "reserved": o.reserved_at(now)}))

    # ---------------------------------------------------------------- 4. rules (proposals only)
    def _rules_pass(self, now: int, report: dict) -> None:
        markets = sorted({o.market_hash for o in self._ledger.orders() if o.matched})
        for mh in list(self._state._body["trailing"]):
            if mh not in markets:
                self._state.reset_peak(mh)
        for mh in markets:
            snap = self._snapshot(mh)
            if not isinstance(snap, Mapping):
                report["alerts"].append(Alert(WARN, "MARKET_SNAPSHOT_UNAVAILABLE_RULES_NOT_EVALUATED", None, mh))
                continue
            position = snap.get("market_position") or {}
            mtm = (position.get("mark_to_market") or {}).get("value_base_units")
            cfg = copy.deepcopy(self._rules)
            trailing = cfg.get("trailing_stop")
            if isinstance(trailing, dict) and trailing.get("enabled"):
                stored = self._state.peak(mh)
                # Missing stays missing: no stored peak and no integer mark-to-market => None, never 0.
                base = stored if stored is not None else (mtm if type(mtm) is int else None)
                trailing["peak_mark_to_market_base_units"] = base
            try:
                verdict = self._evaluate(cfg, market_position=position, market=snap.get("market"), now_unix=now,
                                         data_age_seconds=snap.get("data_age_seconds"),
                                         taker_percentage_odds=snap.get("taker_percentage_odds"),
                                         hedge=snap.get("hedge"))
            except Exception as exc:  # e.g. the evaluator refuses a None peak: report, never substitute a value
                report["alerts"].append(Alert(WARN, "RULES_EVALUATION_REFUSED", None, mh,
                                              {"reason": type(exc).__name__}))
                if type(mtm) is int:
                    self._state.raise_peak(mh, mtm, now)
                self._state.save()
                continue
            if type(mtm) is int:
                self._state.raise_peak(mh, mtm, now)
            self._state.save()  # the peak is durable BEFORE anything is emitted
            for r in verdict.get("results", []):
                if r.get("result") == "ALERT":
                    report["alerts"].append(Alert(WARN, "RULE_" + str(r.get("reason")), None, mh,
                                                  {"rule": r.get("rule")}))
            p = verdict.get("proposal")
            if p:
                if p.get("executable") is not False or p.get("signed") is not False or p.get("submitted") is not False:
                    raise MonitorError("MONITOR_PROPOSAL_CLAIMS_EXECUTABLE")
                h = p.get("proposal_hash")
                report["proposals"].append({**p, "market_hash": mh, "status": "AWAITING_OWNER_APPROVAL",
                                            "source": "MONITOR", "repeat": bool(h) and self._state.seen(h)})
                if h:
                    self._state.mark_seen(h)

    # ---------------------------------------------------------------- tick
    def tick(self) -> dict[str, Any]:
        now = int(self._clock())
        reads_before = self._reads.calls
        # orders_submitted / orders_cancelled / network_writes: 0 BY CONSTRUCTION (only reads are reachable).
        report: dict[str, Any] = {"at": now, "reconciled": [], "alerts": [], "proposals": [], "orders_submitted": 0,
                                  "orders_cancelled": 0, "network_writes": 0,
                                  "write_counters": "CONSTANT_BY_CONSTRUCTION", "exchange_reads": 0}
        self._reconcile(now, report)
        self._expiry_alerts(now, report)
        report["alerts"].extend(evaluate_heartbeat(self._hb, last_ack_at=self._state.heartbeat_ack_at,
                                                   open_orders=self._ledger.exposure().open_orders, now=now))
        if self._rules is not None:
            self._rules_pass(now, report)
        self._state.bump()
        self._state.save()
        report["exchange_reads"] = self._reads.calls - reads_before
        return report
