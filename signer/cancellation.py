"""Order cancellation (governance §2.6: a cancel is a financial operation, commanded by the owner one by one).

Contract (fetched 01-11-2026 from https://docs.sx.bet/<path>.md):
- developers/cancelling-orders: "There are four cancel routes. All four return `200` even when nothing
  was cancelled, so a `200` alone never confirms an order is off the book." By id: "waits for the
  matching engine, reports per order"; market / event / all: "confirms only that a cancel command was
  accepted". "when the matching engine removes your order, you get an update with status INACTIVE and
  inactiveReason USER_REQUESTED".
- api-reference/delete-orders-v3: DELETE /orders-v3, body {"orders": [{"orderId"}]}, "At most
  `limits.maxCancelOrders` unique items (100 today)"; 200 data {cancelled, notCancelled, unconfirmed}
  of {orderId, commandId?, reason?}; notCancelled reason NOT_FOUND | CANCEL_PENDING | NOT_ACTIVE;
  unconfirmed: "NOT a failure -- the cancel may still land. Re-read GET /orders-v3 rather than
  retrying blindly."
- api-reference/delete-orders-v3-market (?marketHash=), -event (?eventId=), -all (no body, no params):
  200 data {cancelledSubmitted: [{orderId, commandId}]}: "This is not confirmation that it is off the book."
Rate limits (developers/rate-limits) are the sender's concern (DELETE /orders-v3/all: 60 per 60 s).
Cancels stay allowed while the kill switch is engaged: they only reduce risk.

Flow (CancellationService, all under the gateway lock):
1. live-trading flag re-verified (same owner-signed flag as submission);
2. the request is built and validated (ids, limits, market hash, event id);
3. the owner's explicit cancel COMMAND is verified (signer/command.py; it replaces the per-cancel Ed25519
   CancelApproval, intentional change after the §2.6 rewrite): an `OwnerCommandAuthorization` with a NEW
   single-use command id, the literal text ("Cancella l'ordine 0x...", "Cancel all orders", "Cancella gli
   ordini sul mercato 0x...", "... dell'evento <id>"), its sha256 and received_at inside the short window.
   The signer parses the text itself and the command must name exactly this operation: scope, the same
   set of order ids, the same market hash or event id (COMMAND_MISMATCH_<FIELD>). The command id and the
   canonical text hash must be unused (COMMAND_ID_ALREADY_CONSUMED, COMMAND_TEXT_REPLAYED);
4. pre-check: every targeted ledger order is reconciled from exchange READS first; orders that the
   exchange already shows terminal (filled, cancelled, expired) are not sent (by id: reported
   ALREADY_TERMINAL; if nothing is left, nothing is sent and the command is NOT consumed). A subset of
   the commanded ids may be sent, never more;
5. the live flag and the command window are checked AGAIN (the reads of step 4 take time): refused =>
   command not consumed, nothing sent; then one fsync'd CANCEL_REQUEST ledger line consumes the command id
   and text hash (single use, survives restarts) BEFORE the request goes out; it changes no order state and
   no exposure;
6. flag and window once more right before the send (refused => CANCEL_NOT_SENT, command stays spent); the
   request is sent; the reply is parsed (per id: CANCELLED / NOT_CANCELLED(reason) / UNCONFIRMED /
   UNKNOWN; scopes: command accepted or not);
7. every targeted order is reconciled again from exchange reads. ONLY that evidence changes the ledger:
   INACTIVE + USER_REQUESTED -> CANCELLED (remaining reservation released, matched part kept), fills
   -> PARTIALLY_FILLED / FILLED. A "cancelled" bucket, an accepted command, a timeout or an ambiguous
   reply never release anything by themselves: the order stays reserved and is re-read later.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import command as cmd
from .gateway import GatewayRefused, SubmissionGateway
from .orders import (CANCELLED as ST_CANCELLED, EXPIRED, EXPIRED_UNCONFIRMED, FILLED, OPEN, PARTIALLY_FILLED,
                     REJECTED, SETTLED, SUBMITTED, UNKNOWN as ST_UNKNOWN, LedgerError, OrderStateError)
from .rest_adapter import ExchangeLimits, RestRequest, RestResponse

CANCEL_BINDING_SCHEMA = "sxbet.cancel_binding.v1"
DEFAULT_COMMAND_MAX_AGE_SECONDS = 120
SCOPE_ORDER_IDS, SCOPE_MARKET, SCOPE_EVENT, SCOPE_ALL = "ORDER_IDS", "MARKET", "EVENT", "ALL"
SCOPES = frozenset({SCOPE_ORDER_IDS, SCOPE_MARKET, SCOPE_EVENT, SCOPE_ALL})
CANCELLED, NOT_CANCELLED, UNCONFIRMED, UNKNOWN = "CANCELLED", "NOT_CANCELLED", "UNCONFIRMED", "UNKNOWN"
NOT_CANCELLED_REASONS = frozenset({"NOT_FOUND", "CANCEL_PENDING", "NOT_ACTIVE"})
BINDING_KEYS = frozenset({"schema", "account_id", "maker", "network", "scope", "order_ids", "market_hash",
                          "event_id"})
# Orders that may be on the book: the only ones a cancel can affect.
_CANCELLABLE = frozenset({SUBMITTED, OPEN, PARTIALLY_FILLED, ST_UNKNOWN})
_TERMINAL = frozenset({FILLED, ST_CANCELLED, EXPIRED, SETTLED})

_HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
_EVENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class CancelRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CancelOutcome:
    bucket: str
    reason: str | None = None
    command_id: str | None = None


@dataclass(frozen=True)
class ScopeOutcome:
    accepted: bool
    reason: str
    submitted: tuple[tuple[str, str], ...] = ()


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


# ---------------------------------------------------------------- request description (recorded as a hash)
def build_cancel_binding(*, account_id: str, maker: str, network: str, scope: str, order_ids: Sequence[str] = (),
                         market_hash: str | None = None, event_id: str | None = None) -> dict[str, Any]:
    """Canonical description of one cancel operation; its sha256 is written in the CANCEL_REQUEST line."""
    return {"schema": CANCEL_BINDING_SCHEMA, "account_id": account_id,
            "maker": maker.lower() if isinstance(maker, str) else maker, "network": network, "scope": scope,
            "order_ids": sorted(i.lower() if isinstance(i, str) else i for i in order_ids),
            "market_hash": market_hash, "event_id": event_id}


def binding_sha256(binding: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canon(dict(binding))).hexdigest()


# ---------------------------------------------------------------- request builders
def _ids(order_ids: Any, limits: ExchangeLimits) -> list[str]:
    if not isinstance(order_ids, (list, tuple)) or not 1 <= len(order_ids) <= limits.max_cancel_orders:
        raise CancelRefused("CANCEL_BATCH_SIZE_INVALID")
    if not all(isinstance(i, str) and _HEX32.fullmatch(i) for i in order_ids):
        raise CancelRefused("CANCEL_ORDER_ID_INVALID")
    if len(set(order_ids)) != len(order_ids):
        raise CancelRefused("CANCEL_DUPLICATE_ORDER_ID")
    return list(order_ids)


def build_cancel_by_ids(order_ids: Sequence[str], *, limits: ExchangeLimits) -> RestRequest:
    ids = _ids(order_ids, limits)
    return RestRequest("DELETE", "/orders-v3", (), {"orders": [{"orderId": i} for i in ids]})


def build_cancel_market(market_hash: str) -> RestRequest:
    if not isinstance(market_hash, str) or not _HEX32.fullmatch(market_hash):
        raise CancelRefused("CANCEL_MARKET_HASH_INVALID")
    return RestRequest("DELETE", "/orders-v3/market", (("marketHash", market_hash),), None)


def build_cancel_event(event_id: str) -> RestRequest:
    if not isinstance(event_id, str) or not _EVENT.fullmatch(event_id):
        raise CancelRefused("CANCEL_EVENT_ID_INVALID")
    return RestRequest("DELETE", "/orders-v3/event", (("eventId", event_id),), None)


def build_cancel_all() -> RestRequest:
    return RestRequest("DELETE", "/orders-v3/all", (), None)


# ---------------------------------------------------------------- response parsers
def _data(response: Any) -> tuple[Any, str]:
    if response is None:
        return None, "TRANSPORT_TIMEOUT"
    if not isinstance(response, RestResponse) or response.http_status != 200 \
            or not isinstance(response.body, Mapping) or response.body.get("status") != "success" \
            or not isinstance(response.body.get("data"), Mapping):
        return None, "REPLY_NOT_DEFINITIVE"
    return response.body["data"], ""


def parse_cancel_by_ids(response: Any, order_ids: Sequence[str]) -> dict[str, CancelOutcome]:
    """Every requested id lands in exactly one documented bucket, else UNKNOWN (fail closed)."""
    data, why = _data(response)
    if data is None:
        return {i: CancelOutcome(UNKNOWN, why) for i in order_ids}
    seen: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for key, bucket in (("cancelled", CANCELLED), ("notCancelled", NOT_CANCELLED), ("unconfirmed", UNCONFIRMED)):
        items = data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(x, Mapping) and isinstance(x.get("orderId"), str)
                                                  for x in items):
            return {i: CancelOutcome(UNKNOWN, "REPLY_MALFORMED") for i in order_ids}
        for x in items:
            seen.setdefault(x["orderId"].lower(), []).append((bucket, x))
    out = {}
    for i in order_ids:
        entries = seen.get(i, [])
        if len(entries) != 1:
            out[i] = CancelOutcome(UNKNOWN, "REPLY_ID_MISSING" if not entries else "REPLY_ID_AMBIGUOUS")
            continue
        bucket, x = entries[0]
        cmd = x.get("commandId") if isinstance(x.get("commandId"), str) else None
        reason = None
        if bucket == NOT_CANCELLED:
            reason = x.get("reason") if x.get("reason") in NOT_CANCELLED_REASONS else "UNDOCUMENTED_REASON"
        out[i] = CancelOutcome(bucket, reason, cmd)
    return out


def parse_cancel_scope(response: Any) -> ScopeOutcome:
    """market / event / all: at most 'the command was accepted' -- never 'the orders are off the book'."""
    data, why = _data(response)
    if data is None:
        return ScopeOutcome(False, why)
    items = data.get("cancelledSubmitted")
    if not isinstance(items, list) or not all(isinstance(x, Mapping) and isinstance(x.get("orderId"), str)
                                              and isinstance(x.get("commandId"), str) for x in items):
        return ScopeOutcome(False, "REPLY_MALFORMED")
    return ScopeOutcome(True, "CANCEL_COMMAND_ACCEPTED",
                        tuple((x["orderId"].lower(), x["commandId"]) for x in items))


# ---------------------------------------------------------------- flow
class CancellationService:
    def __init__(self, gateway: SubmissionGateway, *, account_id: str,
                 command_max_age_seconds: int = DEFAULT_COMMAND_MAX_AGE_SECONDS):
        """`command_max_age_seconds`: the deployment wires the owner-signed risk config value
        (`RiskLimits.command_max_age_seconds`, <= 300 s)."""
        if not isinstance(gateway, SubmissionGateway) or not gateway._official:
            raise CancelRefused("CANCEL_REQUIRES_OFFICIAL_ADAPTER")
        if not isinstance(account_id, str) or not account_id:
            raise CancelRefused("ACCOUNT_ID_MISSING")
        if type(command_max_age_seconds) is not int or \
                not 0 < command_max_age_seconds <= cmd.COMMAND_MAX_AGE_HARD_CAP_SECONDS:
            raise CancelRefused("COMMAND_WINDOW_NOT_CONFIGURED")
        self._gw = gateway
        self._adapter = gateway._transport
        self._ledger = gateway._ledger
        self._account_id = account_id
        self._max_age = command_max_age_seconds

    # ---- helpers ----
    def _now(self) -> int:
        return int(self._gw._clock())

    def _binding(self, scope: str, *, order_ids: Sequence[str] = (), market_hash: str | None = None,
                 event_id: str | None = None) -> dict[str, Any]:
        return build_cancel_binding(account_id=self._account_id, maker=self._ledger.signer_address,
                                    network=self._gw._network, scope=scope, order_ids=order_ids,
                                    market_hash=market_hash, event_id=event_id)

    def _authorize(self, authorization: Any, scope: str, *, order_ids: Sequence[str] = (),
                   market_hash: str | None = None, event_id: str | None = None) -> cmd.OwnerCommandAuthorization:
        """Verify the owner's cancel command WITHOUT consuming it; return the private snapshot."""
        try:
            auth = cmd.snapshot(authorization)
            cmd.verify_record(auth, now=self._now(), max_age_seconds=self._max_age)
            if self._ledger.command_consumed(auth.command_id):
                raise cmd.CommandRefused("COMMAND_ID_ALREADY_CONSUMED")
            if self._ledger.command_text_consumed(cmd.canonical_sha256(auth.command_text)):
                raise cmd.CommandRefused("COMMAND_TEXT_REPLAYED")
            cmd.verify_cancel(cmd.parse(auth.command_text), scope=scope, order_ids=tuple(order_ids),
                              market_hash=market_hash, event_id=event_id)
        except cmd.CommandRefused as exc:
            raise CancelRefused(exc.code) from None
        return auth

    def _still_valid(self, auth: cmd.OwnerCommandAuthorization) -> None:
        """The pre-check reads can take a while (up to 3 per order): flag and command window are checked
        again at the last moment, with the same rule as the first check."""
        self._gw._check_flag()
        if self._now() - auth.received_at > self._max_age:
            raise CancelRefused("COMMAND_EXPIRED")

    def _consume(self, auth: cmd.OwnerCommandAuthorization, binding: Mapping[str, Any], scope: str,
                 order_ids: list[str], market_hash: str | None = None, event_id: str | None = None) -> None:
        self._still_valid(auth)  # refused here => command NOT consumed, nothing sent
        try:
            self._ledger.record_cancel_request(command=cmd.ledger_fields(auth), scope=scope,
                                               binding_sha256=binding_sha256(binding), order_ids=order_ids,
                                               market_hash=market_hash, event_id=event_id)
        except (OrderStateError, LedgerError) as exc:
            raise CancelRefused(exc.code) from None  # nothing was sent

    def _before_send(self, auth: cmd.OwnerCommandAuthorization, scope: str) -> None:
        """Last check right before the request leaves. The command is already consumed (fail closed: a refusal
        here sends nothing and a retry needs a new owner command)."""
        try:
            self._still_valid(auth)
        except (CancelRefused, GatewayRefused) as exc:
            self._log("CANCEL_NOT_SENT", scope, reason=exc.code, auth=auth)
            raise

    def _reconcile(self, client_order_id: str) -> str:
        """Exchange reads -> evidence -> ledger (the ONLY way a cancel changes exposure)."""
        self._gw._reconcile_locked(client_order_id)
        return self._ledger.get(client_order_id).state

    def _log(self, decision: str, scope: str, *, reason: str | None = None, order_id: str | None = None,
             bucket: str | None = None, auth: cmd.OwnerCommandAuthorization | None = None) -> None:
        self._gw._audit.append(event="CANCEL", decision=decision, reason=reason, signer_id=self._ledger.signer_id,
                               order_id=order_id, network=self._gw._network, scope=scope, bucket=bucket,
                               owner_command_id=None if auth is None else auth.command_id,
                               owner_command_sha256=None if auth is None else auth.command_text_sha256)

    def _cancellable(self, o) -> bool:
        return o.state in _CANCELLABLE or (o.state == REJECTED and o.rejected_hold_active(self._now()))

    @staticmethod
    def _skip_bucket(o) -> str:
        if o.state in _TERMINAL or o.state == REJECTED:
            return "ALREADY_TERMINAL"
        if o.state == EXPIRED_UNCONFIRMED:
            return "EXPIRED_NOT_ON_BOOK"
        return "NOT_SUBMITTED"

    # ---- by id (synchronous route, per-order outcome) ----
    def cancel_orders(self, order_ids: Sequence[str], authorization: Any) -> dict[str, Any]:
        with self._gw._lock:
            self._gw._check_flag()
            ids = _ids(order_ids, self._adapter.limits)
            binding = self._binding(SCOPE_ORDER_IDS, order_ids=ids)
            auth = self._authorize(authorization, SCOPE_ORDER_IDS, order_ids=ids)
            known = [self._ledger.by_order_id(i) for i in ids]
            if any(o is None for o in known):
                raise CancelRefused("CANCEL_ORDER_NOT_IN_LEDGER")
            report: dict[str, dict[str, Any]] = {}
            to_send = []
            for o in known:  # pre-check from exchange reads
                state = self._reconcile(o.client_order_id)
                cur = self._ledger.get(o.client_order_id)
                report[o.order_id] = {"client_order_id": o.client_order_id, "state_before": state}
                if self._cancellable(cur):
                    to_send.append(o.order_id)
                else:
                    report[o.order_id].update(bucket=self._skip_bucket(cur), reason=None, state=state)
            if not to_send:
                return {"scope": SCOPE_ORDER_IDS, "sent": False, "orders": report}  # command not consumed
            request = build_cancel_by_ids(to_send, limits=self._adapter.limits)
            self._consume(auth, binding, SCOPE_ORDER_IDS, to_send)
            self._log("CANCEL_REQUESTED", SCOPE_ORDER_IDS, auth=auth)
            self._before_send(auth, SCOPE_ORDER_IDS)
            parsed = parse_cancel_by_ids(self._adapter.send(request), to_send)
            for oid in to_send:
                r = parsed[oid]
                state = self._reconcile(report[oid]["client_order_id"])
                report[oid].update(bucket=r.bucket, reason=r.reason, command_id=r.command_id, state=state)
                self._log("CANCEL_RESULT_" + r.bucket, SCOPE_ORDER_IDS, reason=r.reason, order_id=oid,
                          bucket=r.bucket)
            return {"scope": SCOPE_ORDER_IDS, "sent": True, "orders": report}

    # ---- market / event / all (asynchronous routes: command accepted at most) ----
    def cancel_market(self, market_hash: str, authorization: Any) -> dict[str, Any]:
        return self._scoped(SCOPE_MARKET, lambda: build_cancel_market(market_hash), authorization,
                            lambda o: o.market_hash == market_hash, market_hash=market_hash)

    def cancel_event(self, event_id: str, authorization: Any) -> dict[str, Any]:
        return self._scoped(SCOPE_EVENT, lambda: build_cancel_event(event_id), authorization,
                            lambda o: o.event_id == event_id, event_id=event_id)

    def cancel_all(self, authorization: Any) -> dict[str, Any]:
        return self._scoped(SCOPE_ALL, build_cancel_all, authorization, lambda o: True)

    def _scoped(self, scope: str, build: Callable[[], RestRequest], authorization: Any,
                select: Callable[[Any], bool], *, market_hash: str | None = None,
                event_id: str | None = None) -> dict[str, Any]:
        with self._gw._lock:
            self._gw._check_flag()
            request = build()
            binding = self._binding(scope, market_hash=market_hash, event_id=event_id)
            auth = self._authorize(authorization, scope, market_hash=market_hash, event_id=event_id)
            report: dict[str, dict[str, Any]] = {}
            targets = []
            for o in self._ledger.orders():
                if select(o) and self._cancellable(o):
                    report[o.order_id] = {"client_order_id": o.client_order_id,
                                          "state_before": self._reconcile(o.client_order_id)}
                    targets.append(o.order_id)
            self._consume(auth, binding, scope, sorted(targets), market_hash=market_hash, event_id=event_id)
            self._log("CANCEL_REQUESTED", scope, auth=auth)
            self._before_send(auth, scope)
            outcome = parse_cancel_scope(self._adapter.send(request))
            submitted = {oid for oid, _ in outcome.submitted}
            for oid in targets:
                state = self._reconcile(report[oid]["client_order_id"])
                report[oid].update(state=state, command_submitted=oid in submitted)
            foreign = sorted(i for i in submitted if self._ledger.by_order_id(i) is None)
            self._log("CANCEL_COMMAND_" + ("ACCEPTED" if outcome.accepted else "NOT_CONFIRMED"), scope,
                      reason=outcome.reason)
            return {"scope": scope, "sent": True, "accepted": outcome.accepted, "reason": outcome.reason,
                    "orders": report, "foreign_order_ids": foreign}


__all__ = ["CancelOutcome", "CancelRefused", "CancellationService", "ScopeOutcome", "build_cancel_all",
           "build_cancel_binding", "build_cancel_by_ids", "build_cancel_event", "build_cancel_market",
           "binding_sha256", "parse_cancel_by_ids", "parse_cancel_scope", "GatewayRefused"]
