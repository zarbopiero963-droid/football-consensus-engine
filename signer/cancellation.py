"""Order cancellation (governance §2.6: a cancel is a financial operation, owner-approved one by one).

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

Flow (CancellationService, all under the gateway lock):
1. live-trading flag re-verified (same owner-signed flag as submission);
2. the request is built and validated (ids, limits, market hash, event id);
3. the owner's CancelApproval is verified: Ed25519 over a canonical binding {account, maker, network,
   scope, order_ids (sorted) | market_hash | event_id}, TTL <= 120 s, addressed to this signer, nonce
   unused. A chat text, a tool output or a web page is not an approval;
4. pre-check: every targeted ledger order is reconciled from exchange READS first; orders that the
   exchange already shows terminal (filled, cancelled, expired) are not sent (by id: reported
   ALREADY_TERMINAL; if nothing is left, nothing is sent and the approval is NOT consumed). A subset of
   the approved ids may be sent, never more;
5. one fsync'd CANCEL_REQUEST ledger line consumes the nonce (single use, survives restarts) BEFORE the
   request goes out; it changes no order state and no exposure;
6. the request is sent; the reply is parsed (per id: CANCELLED / NOT_CANCELLED(reason) / UNCONFIRMED /
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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .gateway import GatewayRefused, SubmissionGateway
from .orders import (CANCELLED as ST_CANCELLED, EXPIRED, EXPIRED_UNCONFIRMED, FILLED, OPEN, PARTIALLY_FILLED,
                     REJECTED, SETTLED, SUBMITTED, UNKNOWN as ST_UNKNOWN, LedgerError, OrderStateError)
from .rest_adapter import ExchangeLimits, RestRequest, RestResponse

CANCEL_APPROVAL_DOMAIN = "sxbet.cancel_approval.v1"
CANCEL_BINDING_SCHEMA = "sxbet.cancel_binding.v1"
MAX_TTL_SECONDS = 120
CLOCK_SKEW_SECONDS = 5
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
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class CancelRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CancelApproval:
    binding: Mapping[str, Any]
    approval_nonce: str
    issued_at: int
    expires_at: int
    signer_id: str
    signature: bytes  # 64-byte Ed25519 signature over cancel_approval_message(...)

    def __repr__(self) -> str:
        nonce = self.approval_nonce[:6] if isinstance(self.approval_nonce, str) else "?"
        scope = self.binding.get("scope") if isinstance(self.binding, Mapping) else None
        return f"CancelApproval(scope={scope!r}, nonce={nonce}..., expires_at={self.expires_at})"


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


# ---------------------------------------------------------------- owner approval
def build_cancel_binding(*, account_id: str, maker: str, network: str, scope: str, order_ids: Sequence[str] = (),
                         market_hash: str | None = None, event_id: str | None = None) -> dict[str, Any]:
    """What the owner's approval device shows and signs for one cancel operation."""
    return {"schema": CANCEL_BINDING_SCHEMA, "account_id": account_id,
            "maker": maker.lower() if isinstance(maker, str) else maker, "network": network, "scope": scope,
            "order_ids": sorted(i.lower() if isinstance(i, str) else i for i in order_ids),
            "market_hash": market_hash, "event_id": event_id}


def cancel_approval_message(*, binding: Mapping[str, Any], approval_nonce: str, issued_at: int, expires_at: int,
                            signer_id: str) -> bytes:
    return _canon({"domain": CANCEL_APPROVAL_DOMAIN, "binding": dict(binding), "approval_nonce": approval_nonce,
                   "issued_at": issued_at, "expires_at": expires_at, "signer_id": signer_id})


def binding_sha256(binding: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canon(dict(binding))).hexdigest()


def verify_cancel_approval(approval: Any, public_key: Ed25519PublicKey, *, expected_binding: Mapping[str, Any],
                           signer_id: str, now: int) -> dict[str, Any]:
    """Return a private snapshot of the verified binding, or raise CancelRefused. Never consumes the nonce."""
    if not isinstance(approval, CancelApproval):
        raise CancelRefused("CANCEL_APPROVAL_MISSING")
    try:
        binding = json.loads(_canon(dict(approval.binding)))
    except Exception:
        raise CancelRefused("CANCEL_APPROVAL_MALFORMED") from None
    if (not isinstance(binding, dict) or set(binding) != BINDING_KEYS or binding["schema"] != CANCEL_BINDING_SCHEMA
            or not isinstance(approval.approval_nonce, str) or not _NONCE.fullmatch(approval.approval_nonce)
            or type(approval.issued_at) is not int or type(approval.expires_at) is not int
            or not isinstance(approval.signature, (bytes, bytearray)) or len(approval.signature) != 64):
        raise CancelRefused("CANCEL_APPROVAL_MALFORMED")
    if approval.signer_id != signer_id:
        raise CancelRefused("CANCEL_APPROVAL_WRONG_SIGNER")
    if approval.expires_at - approval.issued_at > MAX_TTL_SECONDS or approval.expires_at <= approval.issued_at:
        raise CancelRefused("CANCEL_APPROVAL_TTL_TOO_LONG")
    if approval.issued_at > now + CLOCK_SKEW_SECONDS:
        raise CancelRefused("CANCEL_APPROVAL_NOT_YET_VALID")
    if now >= approval.expires_at:
        raise CancelRefused("CANCEL_APPROVAL_EXPIRED")
    msg = cancel_approval_message(binding=binding, approval_nonce=approval.approval_nonce,
                                  issued_at=approval.issued_at, expires_at=approval.expires_at,
                                  signer_id=approval.signer_id)
    try:
        public_key.verify(bytes(approval.signature), msg)
    except (InvalidSignature, TypeError, ValueError):
        raise CancelRefused("CANCEL_APPROVAL_SIGNATURE_INVALID") from None
    if binding != json.loads(_canon(dict(expected_binding))):
        raise CancelRefused("CANCEL_APPROVAL_BINDING_MISMATCH")
    return binding


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
    def __init__(self, gateway: SubmissionGateway, *, owner_public_key: Ed25519PublicKey, account_id: str):
        if not isinstance(gateway, SubmissionGateway) or not gateway._official:
            raise CancelRefused("CANCEL_REQUIRES_OFFICIAL_ADAPTER")
        if not isinstance(owner_public_key, Ed25519PublicKey):
            raise CancelRefused("OWNER_PUBLIC_KEY_INVALID")
        if not isinstance(account_id, str) or not account_id:
            raise CancelRefused("ACCOUNT_ID_MISSING")
        self._gw = gateway
        self._adapter = gateway._transport
        self._ledger = gateway._ledger
        self._owner_key = owner_public_key
        self._account_id = account_id

    # ---- helpers ----
    def _now(self) -> int:
        return int(self._gw._clock())

    def _binding(self, scope: str, *, order_ids: Sequence[str] = (), market_hash: str | None = None,
                 event_id: str | None = None) -> dict[str, Any]:
        return build_cancel_binding(account_id=self._account_id, maker=self._ledger.signer_address,
                                    network=self._gw._network, scope=scope, order_ids=order_ids,
                                    market_hash=market_hash, event_id=event_id)

    def _authorize(self, approval: Any, binding: Mapping[str, Any]) -> None:
        verify_cancel_approval(approval, self._owner_key, expected_binding=binding,
                               signer_id=self._ledger.signer_id, now=self._now())
        if self._ledger.nonce_consumed(approval.approval_nonce):
            raise CancelRefused("CANCEL_APPROVAL_NONCE_REPLAYED")

    def _consume(self, approval: CancelApproval, binding: Mapping[str, Any], scope: str, order_ids: list[str],
                 market_hash: str | None = None, event_id: str | None = None) -> None:
        try:
            self._ledger.record_cancel_request(approval_nonce=approval.approval_nonce, scope=scope,
                                               binding_sha256=binding_sha256(binding), order_ids=order_ids,
                                               market_hash=market_hash, event_id=event_id)
        except OrderStateError as exc:
            raise CancelRefused("CANCEL_APPROVAL_NONCE_REPLAYED" if exc.code == "APPROVAL_NONCE_REPLAYED"
                                else exc.code) from None
        except LedgerError as exc:
            raise CancelRefused(exc.code) from None  # nothing was sent

    def _reconcile(self, client_order_id: str) -> str:
        """Exchange reads -> evidence -> ledger (the ONLY way a cancel changes exposure)."""
        self._gw._reconcile_locked(client_order_id)
        return self._ledger.get(client_order_id).state

    def _log(self, decision: str, scope: str, *, reason: str | None = None, order_id: str | None = None,
             bucket: str | None = None, nonce: str | None = None) -> None:
        self._gw._audit.append(event="CANCEL", decision=decision, reason=reason, signer_id=self._ledger.signer_id,
                               order_id=order_id, network=self._gw._network, scope=scope, bucket=bucket,
                               approval_nonce_prefix=nonce[:6] if isinstance(nonce, str) else None)

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
    def cancel_orders(self, order_ids: Sequence[str], approval: Any) -> dict[str, Any]:
        with self._gw._lock:
            self._gw._check_flag()
            ids = _ids(order_ids, self._adapter.limits)
            binding = self._binding(SCOPE_ORDER_IDS, order_ids=ids)
            self._authorize(approval, binding)
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
                return {"scope": SCOPE_ORDER_IDS, "sent": False, "orders": report}  # approval not consumed
            request = build_cancel_by_ids(to_send, limits=self._adapter.limits)
            self._consume(approval, binding, SCOPE_ORDER_IDS, to_send)
            self._log("CANCEL_REQUESTED", SCOPE_ORDER_IDS, nonce=approval.approval_nonce)
            parsed = parse_cancel_by_ids(self._adapter.send(request), to_send)
            for oid in to_send:
                r = parsed[oid]
                state = self._reconcile(report[oid]["client_order_id"])
                report[oid].update(bucket=r.bucket, reason=r.reason, command_id=r.command_id, state=state)
                self._log("CANCEL_RESULT_" + r.bucket, SCOPE_ORDER_IDS, reason=r.reason, order_id=oid,
                          bucket=r.bucket)
            return {"scope": SCOPE_ORDER_IDS, "sent": True, "orders": report}

    # ---- market / event / all (asynchronous routes: command accepted at most) ----
    def cancel_market(self, market_hash: str, approval: Any) -> dict[str, Any]:
        return self._scoped(SCOPE_MARKET, lambda: build_cancel_market(market_hash), approval,
                            lambda o: o.market_hash == market_hash, market_hash=market_hash)

    def cancel_event(self, event_id: str, approval: Any) -> dict[str, Any]:
        return self._scoped(SCOPE_EVENT, lambda: build_cancel_event(event_id), approval,
                            lambda o: o.event_id == event_id, event_id=event_id)

    def cancel_all(self, approval: Any) -> dict[str, Any]:
        return self._scoped(SCOPE_ALL, build_cancel_all, approval, lambda o: True)

    def _scoped(self, scope: str, build: Callable[[], RestRequest], approval: Any, select: Callable[[Any], bool],
                *, market_hash: str | None = None, event_id: str | None = None) -> dict[str, Any]:
        with self._gw._lock:
            self._gw._check_flag()
            request = build()
            binding = self._binding(scope, market_hash=market_hash, event_id=event_id)
            self._authorize(approval, binding)
            report: dict[str, dict[str, Any]] = {}
            targets = []
            for o in self._ledger.orders():
                if select(o) and self._cancellable(o):
                    report[o.order_id] = {"client_order_id": o.client_order_id,
                                          "state_before": self._reconcile(o.client_order_id)}
                    targets.append(o.order_id)
            self._consume(approval, binding, scope, sorted(targets), market_hash=market_hash, event_id=event_id)
            self._log("CANCEL_REQUESTED", scope, nonce=approval.approval_nonce)
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


__all__ = ["CancelApproval", "CancelOutcome", "CancelRefused", "CancellationService", "ScopeOutcome",
           "build_cancel_all", "build_cancel_binding", "build_cancel_by_ids", "build_cancel_event",
           "build_cancel_market", "cancel_approval_message", "parse_cancel_by_ids", "parse_cancel_scope",
           "verify_cancel_approval", "GatewayRefused"]
