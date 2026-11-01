"""Submission gateway: DISABLED by default, and with NO real transport in this repository.

Strict separation: prepare (bridge simulation, no key) -> sign (signer/core.py,
owner approval) -> submit (this module). The gateway never signs and never
holds a key; it only forwards an already signed order after checking it is
exactly the approved one, and it reconciles state from exchange reads.

Activation: `SubmissionGateway(...)` refuses to construct unless it receives a
`LiveTradingFlag` produced by `load_live_trading_flag` from an owner-signed
(Ed25519) config bound to this signer_id and network, with a short expiry.
Default: disabled. What actually protects activation, stated precisely:
- the flag carries the exact signed bytes and the owner's signature, and the
  gateway RE-VERIFIES that signature with the owner public key it is given at
  construction (from its own config, never from the flag), at construction and
  on EVERY submit; enabled / signer_id / network / expires_at are read only
  from those verified bytes, never from attributes a caller could change;
- expiry is re-checked on every submit: once `now >= expires_at` every submit
  is refused with LIVE_TRADING_FLAG_EXPIRED (read-only `reconcile` still works);
- `LiveTradingFlag(...)` called directly raises GatewayDisabled: only this
  module (via a private sentinel) builds one. This blocks accidental or naive
  forging; it is NOT a security boundary against code running in the signer
  process (Python has no private members). The boundary is the owner's Ed25519
  private key, which never exists on the signer host.
Governance §2.6 still applies on top: the owner alone sets the flag, and no
MCP tool argument reaches this module (bridge/ never imports signer/).

Transport: `SubmissionTransport` is an abstract Protocol. This repository ships
NO implementation that talks to a network; tests use an in-memory fake. A real
transport is trading code: separate PR, owner review and owner merge.

Payload (https://docs.sx.bet/api-reference/post-orders-v3, verified 01-11-2026):
    {"orders": [{marketHash, maker, totalBetSize, percentageOdds, salt, expiry, baseToken,
                 isMakerBettingOutcomeOne, timeInForce, orderSignature, clientOrderId}]}
`waitForOutcome` is not sent (default false: the reply only says SUBMITTED;
the outcome comes from reconciliation). DOCS_UNVERIFIED: the schema describes
`salt` as a "32-byte hex string" while the official Python sample sends the
decimal string of the same uint256; this module keeps the signer's decimal
string (Python sample) and the owner must confirm the format on testnet.

Reply classification (fail closed):
- 200, status "success", exactly one row, orderId == our order id, status SUBMITTED -> SUBMITTED
- 200, one row for our order id with status FAILED, or HTTP 400 with an error `message` -> REJECTED
  (the signature was released, so the ledger keeps the whole stake reserved until
  the order's expiry + grace has passed: see signer/orders.py)
- timeout, transport error, 401/403/5xx, malformed body, other order id -> UNKNOWN
  (UNKNOWN never means "not placed": capital stays reserved until reconciled).
Write-ahead: SIGNED -> UNKNOWN(SEND_ATTEMPT) is persisted BEFORE the transport
is called, so a crash mid-send leaves UNKNOWN, never a silent re-send.
Retry: only the same signed order with the same clientOrderId, only from
SIGNED, i.e. after reconciliation proved the previous attempt absent.
DOCS_UNVERIFIED: the exchange behaviour on re-sending the same order id /
clientOrderId after a timeout (docs only say clientOrderId is unique per address).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_account import Account

from . import eip712
from .audit import AuditLog
from .intent import IntentInvalid, intent_hash, validate
from .orders import (REJECTED, SIGNED, SUBMITTED, UNKNOWN, LedgerError, OrderLedger, OrderStateError,
                     evidence_from_exchange)

LIVE_FLAG_DOMAIN = "sxbet.live_trading_flag.v1"
LIVE_FLAG_ENV = "SXBET_LIVE_TRADING_FLAG"
MAX_FLAG_TTL_SECONDS = 24 * 3600
SEND_EXPIRY_MARGIN_SECONDS = 12
PAYLOAD_ORDER_KEYS = frozenset({"marketHash", "maker", "totalBetSize", "percentageOdds", "salt", "expiry",
                                "baseToken", "isMakerBettingOutcomeOne", "timeInForce", "orderSignature",
                                "clientOrderId"})
_HEX_SIG = re.compile(r"^[0-9a-f]{128}$")
_FLAG_BODY_KEYS = frozenset({"live_trading", "signer_id", "network", "issued_at", "expires_at"})
_SEAL = object()  # module-private: only this module constructs LiveTradingFlag (see module docstring)


class GatewayDisabled(RuntimeError):
    pass


class GatewayRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TransportTimeout(Exception):
    """Raised by a transport when it cannot tell whether the exchange received the request."""


class SubmissionTransport(Protocol):
    """Abstract. No network implementation exists in this repository (see module docstring)."""

    def submit_orders(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return {"http_status": int, "body": <json>} or raise TransportTimeout."""

    def get_order(self, order_id: str) -> Mapping[str, Any]:
        """GET /orders-v3/{orderId}: {"kind": "FOUND", "order": OrderV3} | {"kind": "NOT_FOUND"} | {"kind": "ERROR"}."""

    def get_order_by_client_id(self, client_order_id: str) -> Mapping[str, Any]:
        """GET /orders-v3/client/{clientOrderId}: same shape as get_order."""

    def get_fills(self, order_id: str) -> Mapping[str, Any]:
        """GET /fills-v3?orderId=: {"kind": "OK", "fills": [FillV3], "complete": bool} | {"kind": "ERROR"}."""


class LiveTradingFlag:
    """Result of `load_live_trading_flag`. Not constructible directly (GatewayDisabled).

    When enabled it carries the owner-signed bytes and signature; the gateway re-verifies them and
    trusts nothing else in this object."""

    __slots__ = ("enabled", "source", "_message", "_signature")

    def __init__(self, *args: Any, _seal: object = None, _message: bytes = b"", _signature: bytes = b"",
                 **kwargs: Any):
        if _seal is not _SEAL or len(args) != 2 or kwargs:
            raise GatewayDisabled("LIVE_TRADING_FLAG_NOT_FROM_OWNER_CONFIG")
        enabled, source = args
        object.__setattr__(self, "enabled", enabled is True)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "_message", bytes(_message))
        object.__setattr__(self, "_signature", bytes(_signature))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("LiveTradingFlag is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("LiveTradingFlag is immutable")

    def __reduce__(self):
        raise TypeError("LiveTradingFlag cannot be serialised")

    def _body(self) -> dict[str, Any]:
        try:
            body = json.loads(self._message.decode("ascii")) if self._message else {}
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @property
    def signer_id(self) -> Any:
        return self._body().get("signer_id")

    @property
    def network(self) -> Any:
        return self._body().get("network")

    @property
    def expires_at(self) -> Any:
        return self._body().get("expires_at")

    def __repr__(self) -> str:
        return f"LiveTradingFlag(enabled={self.enabled}, source={self.source!r}, expires_at={self.expires_at!r})"


def _flag(enabled: bool, source: str, message: bytes = b"", signature: bytes = b"") -> LiveTradingFlag:
    return LiveTradingFlag(enabled, source, _seal=_SEAL, _message=message, _signature=signature)


DISABLED = _flag(False, "DEFAULT_DISABLED")


def live_flag_message(body: Mapping[str, Any]) -> bytes:
    return json.dumps({"domain": LIVE_FLAG_DOMAIN, **dict(body)}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _verified_flag_body(flag: Any, owner_public_key: Any, *, signer_id: str, network: str, now: int) -> str:
    """'' if the flag's owner signature, binding and validity window all verify at `now`, else a fixed code."""
    if not isinstance(flag, LiveTradingFlag) or flag.enabled is not True or flag.source != "OWNER_SIGNED":
        return "LIVE_TRADING_DISABLED"
    if not isinstance(owner_public_key, Ed25519PublicKey):
        return "LIVE_TRADING_OWNER_KEY_INVALID"
    try:
        owner_public_key.verify(flag._signature, flag._message)
    except (InvalidSignature, TypeError, ValueError):
        return "LIVE_TRADING_FLAG_SIGNATURE_INVALID"
    body = flag._body()
    if body.get("domain") != LIVE_FLAG_DOMAIN or set(body) - {"domain"} != _FLAG_BODY_KEYS \
            or live_flag_message({k: body[k] for k in _FLAG_BODY_KEYS}) != flag._message:
        return "LIVE_TRADING_FLAG_SIGNATURE_INVALID"
    if body["live_trading"] != "ENABLED" or body["signer_id"] != signer_id or body["network"] != network:
        return "LIVE_TRADING_FLAG_NOT_FOR_THIS_SIGNER"
    issued, expires = body["issued_at"], body["expires_at"]
    if type(issued) is not int or type(expires) is not int or expires - issued > MAX_FLAG_TTL_SECONDS \
            or issued >= expires or issued > now:
        return "LIVE_TRADING_FLAG_TTL_INVALID"
    if now >= expires:
        return "LIVE_TRADING_FLAG_EXPIRED"
    return ""


def load_live_trading_flag(path: str | None, owner_public_key: Ed25519PublicKey, *, signer_id: str, network: str,
                           now: int) -> LiveTradingFlag:
    """Owner-signed file {"live_trading": "ENABLED", "signer_id", "network", "issued_at", "expires_at",
    "signature"}. Anything else (missing, unsigned, bad signature, other signer/network, expired) => DISABLED."""
    if not path:
        return DISABLED
    try:
        with open(path, encoding="ascii") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _flag(False, "FLAG_UNREADABLE")
    keys = _FLAG_BODY_KEYS | {"signature"}
    if not isinstance(doc, dict) or set(doc) != keys or not isinstance(doc["signature"], str) \
            or not _HEX_SIG.fullmatch(doc["signature"]):
        return _flag(False, "FLAG_MALFORMED")
    body = {k: doc[k] for k in _FLAG_BODY_KEYS}
    try:
        message = live_flag_message(body)
        signature = bytes.fromhex(doc["signature"])
        owner_public_key.verify(signature, message)
    except (InvalidSignature, TypeError, ValueError):
        return _flag(False, "FLAG_SIGNATURE_INVALID")
    if doc["live_trading"] != "ENABLED" or doc["signer_id"] != signer_id or doc["network"] != network:
        return _flag(False, "FLAG_NOT_FOR_THIS_SIGNER")
    if type(doc["issued_at"]) is not int or type(doc["expires_at"]) is not int \
            or not doc["issued_at"] <= now < doc["expires_at"] \
            or doc["expires_at"] - doc["issued_at"] > MAX_FLAG_TTL_SECONDS:
        return _flag(False, "FLAG_EXPIRED_OR_TTL_INVALID")
    return _flag(True, "OWNER_SIGNED", message, signature)


def classify_reply(reply: Any, order_id: str) -> tuple[str, str]:
    """(state, reason). Fail closed to UNKNOWN on anything not definitive."""
    if not isinstance(reply, Mapping):
        return UNKNOWN, "REPLY_MALFORMED"
    status, body = reply.get("http_status"), reply.get("body")
    if status == 400 and isinstance(body, Mapping) and isinstance(body.get("message"), (str, list)):
        return REJECTED, "EXCHANGE_REJECTED_400"
    if status != 200 or not isinstance(body, Mapping) or body.get("status") != "success":
        return UNKNOWN, "REPLY_NOT_DEFINITIVE"
    data = body.get("data")
    rows = data.get("orders") if isinstance(data, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return UNKNOWN, "REPLY_ROWS_AMBIGUOUS"
    row = rows[0]
    if not isinstance(row.get("orderId"), str) or row["orderId"].lower() != order_id:
        return UNKNOWN, "REPLY_OTHER_ORDER_ID"
    if row.get("status") == "SUBMITTED":
        return SUBMITTED, "EXCHANGE_SUBMITTED"
    if row.get("status") == "FAILED":
        return REJECTED, "EXCHANGE_FAILED"
    return UNKNOWN, "REPLY_STATUS_UNKNOWN"


class SubmissionGateway:
    def __init__(self, flag: LiveTradingFlag, *, owner_public_key: Ed25519PublicKey, transport: SubmissionTransport,
                 ledger: OrderLedger, audit: AuditLog, odds_ladder_step_size: int, network: str,
                 clock: Callable[[], float] = time.time):
        if not isinstance(ledger, OrderLedger):
            raise GatewayDisabled("LIVE_TRADING_DISABLED")
        code = _verified_flag_body(flag, owner_public_key, signer_id=ledger.signer_id, network=network,
                                   now=int(clock()))
        if code:
            raise GatewayDisabled(code)
        self._flag = flag
        self._owner_key = owner_public_key
        self._transport = transport
        self._ledger = ledger
        self._audit = audit
        self._step = odds_ladder_step_size
        self._network = network
        self._clock = clock
        self._lock = threading.Lock()  # one send at a time: duplicate protection across threads

    def _log(self, decision: str, reason: str | None, o) -> None:
        self._audit.append(event="GATEWAY", decision=decision, reason=reason, signer_id=self._ledger.signer_id,
                           idempotency_key=o.client_order_id, intent_hash=o.intent_hash, order_id=o.order_id,
                           total_bet_size=str(o.stake), network=self._network)

    def _verify(self, signed: Mapping[str, Any], intent: Mapping[str, Any]) -> tuple[dict, Any]:
        try:
            clean = validate(intent, odds_ladder_step_size=self._step)
        except IntentInvalid as exc:
            raise GatewayRefused(str(exc)) from None
        if not isinstance(signed, Mapping) or not isinstance(signed.get("order"), Mapping):
            raise GatewayRefused("SIGNED_ORDER_MALFORMED")
        ihash = intent_hash(clean)
        if signed.get("intent_hash") != ihash:
            raise GatewayRefused("SIGNED_ORDER_INTENT_MISMATCH")
        msg = eip712.signable(clean)
        oid = eip712.order_id(msg)
        if signed.get("order_id") != oid:
            raise GatewayRefused("SIGNED_ORDER_ID_MISMATCH")
        order = dict(signed["order"])
        sig = order.get("orderSignature")
        if set(order) != PAYLOAD_ORDER_KEYS or not isinstance(sig, str):
            raise GatewayRefused("SIGNED_ORDER_PAYLOAD_KEYS")
        if order != eip712.order_payload(clean, sig):
            raise GatewayRefused("SIGNED_ORDER_PAYLOAD_NOT_THE_APPROVED_INTENT")
        try:
            signer = Account.recover_message(msg, signature=sig)
        except Exception:
            raise GatewayRefused("SIGNATURE_UNRECOVERABLE") from None
        if signer.lower() != clean["maker"] or signer.lower() != self._ledger.signer_address:
            raise GatewayRefused("SIGNATURE_NOT_BY_MAKER")
        o = self._ledger.get(clean["idempotency_key"])
        if o is None:
            raise GatewayRefused("ORDER_NOT_AUTHORIZED_IN_LEDGER")
        if o.intent_hash != ihash or o.order_id != oid:
            raise GatewayRefused("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD")
        return {"orders": [order]}, o

    def submit(self, signed: Mapping[str, Any], intent: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            # Re-verified on every submit: owner signature over the flag bytes, binding, expiry.
            code = _verified_flag_body(self._flag, self._owner_key, signer_id=self._ledger.signer_id,
                                       network=self._network, now=int(self._clock()))
            if code:
                raise GatewayRefused(code)
            payload, o = self._verify(signed, intent)
            if o.state == UNKNOWN:
                raise GatewayRefused("STATE_UNKNOWN_RECONCILE_FIRST")
            if o.state != SIGNED:
                # Idempotent: the same clientOrderId never goes out twice; return what we know.
                self._log("DUPLICATE_NOT_SENT", o.state, o)
                return {"client_order_id": o.client_order_id, "order_id": o.order_id, "state": o.state,
                        "sent": False}
            now = int(self._clock())
            if o.expiry < now + SEND_EXPIRY_MARGIN_SECONDS:
                raise GatewayRefused("ORDER_EXPIRED_OR_TOO_SOON_NOT_SENT")
            # Write-ahead: a crash from here on leaves UNKNOWN, never "not placed".
            try:
                o = self._ledger.transition(o.client_order_id, UNKNOWN, reason="SEND_ATTEMPT")
            except LedgerError as exc:
                raise GatewayRefused(exc.code) from None  # nothing was sent
            try:
                reply = self._transport.submit_orders(payload)
            except TransportTimeout:
                reply, state, reason = None, UNKNOWN, "TRANSPORT_TIMEOUT"
            except Exception:
                reply, state, reason = None, UNKNOWN, "TRANSPORT_ERROR"
            else:
                state, reason = classify_reply(reply, o.order_id)
            if state != UNKNOWN:
                try:
                    o = self._ledger.transition(o.client_order_id, state, reason=reason)
                except LedgerError as exc:
                    # The ledger keeps UNKNOWN (write-ahead) and is poisoned: reconcile after restart.
                    self._log(UNKNOWN, exc.code, o)
                    raise GatewayRefused(exc.code) from None
            self._log(state, reason, o)
            return {"client_order_id": o.client_order_id, "order_id": o.order_id, "state": o.state,
                    "reason": reason, "sent": True}

    def reconcile(self, client_order_id: str) -> dict[str, Any]:
        """Read-only exchange lookups -> evidence -> ledger. Never sends an order."""
        with self._lock:
            o = self._ledger.get(client_order_id)
            if o is None:
                raise GatewayRefused("ORDER_NOT_AUTHORIZED_IN_LEDGER")
            lookup = self._safe(self._transport.get_order, o.order_id)
            client = self._safe(self._transport.get_order_by_client_id, o.client_order_id)
            fills = self._safe(self._transport.get_fills, o.order_id)
            ev = evidence_from_exchange(o, lookup=lookup, client_lookup=client, fills=fills, now=int(self._clock()))
            try:
                new = self._ledger.apply_evidence(client_order_id, ev)
            except LedgerError as exc:
                self._log("EVIDENCE_NOT_RECORDED", exc.code, o)
                raise GatewayRefused(exc.code) from None
            except OrderStateError as exc:
                self._log("EVIDENCE_CONFLICT", exc.code, o)
                return {"client_order_id": client_order_id, "state": o.state, "reason": exc.code, "changed": False}
            self._log("RECONCILED", ev.reason, new)
            return {"client_order_id": client_order_id, "state": new.state, "reason": ev.reason,
                    "changed": new != o, "filled": str(new.filled)}

    @staticmethod
    def _safe(fn, arg) -> Mapping[str, Any]:
        try:
            out = fn(arg)
        except Exception:
            return {"kind": "ERROR"}
        return out if isinstance(out, Mapping) else {"kind": "ERROR"}
