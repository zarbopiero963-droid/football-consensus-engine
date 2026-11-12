"""Isolated signer service loop (issue #187, S6; governance §2.6). Runs ONLY in the signer process.

    MCP (Render) --INSERT--> signer_order_requests (append-only, signer database)
                                   |
    SignerService.process_once():  claim (one CLAIMED per request) -> validate -> re-hash -> network ->
                                   freshness -> kill switch -> Signer.sign_order (owner command verified by
                                   the signer on its OWN reads, owner-signed limits) -> SubmissionGateway.submit
                                   (owner-signed LIVE flag; no transport in this repository yet) -> events
    MCP <--SELECT-- signer_order_request_events (fixed codes, order id, state: never a signature or key)

What this module never does: return a signature to the MCP, read a key from a request, change limits, modes
or the kill switch because of a request, retry a send (the gateway decides, UNKNOWN is reconciled first),
or process a request twice (one CLAIMED row per request in the database). A request whose processing was
interrupted (crash) is recovered from the signer's own ledger, never re-sent blindly.
The service is transport-agnostic: in this repository it runs in tests against the simulated exchange; a
permanent private worker (owner decision) can run the same loop later without rewriting the signer.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from . import command as cmd
from .cancellation import CancelRefused
from .core import Signer, SignerRefused
from .gateway import GatewayRefused
from .orders import AUTHORIZED, SIGNED, UNKNOWN
from .request_queue import QueueError, RequestQueue, canonical_sha256, event_detail, validate_request

DEFAULT_MAX_REQUEST_AGE_SECONDS = 120   # never above the owner-signed command window (checked again by the signer)
_FINAL_ORDER_STATES = frozenset({"FILLED", "CANCELLED", "EXPIRED", "SETTLED", "REJECTED"})


class SignerService:
    def __init__(self, queue: RequestQueue, signer: Signer, gateway: Any, cancellation: Any, *, network: str,
                 signer_id: str, clock: Callable[[], float] = time.time,
                 max_request_age_seconds: int = DEFAULT_MAX_REQUEST_AGE_SECONDS):
        if network not in ("mainnet", "testnet"):
            raise ValueError("SERVICE_NETWORK_INVALID")
        if type(max_request_age_seconds) is not int or not 1 <= max_request_age_seconds <= 300:
            raise ValueError("SERVICE_MAX_REQUEST_AGE_INVALID")
        self._q = queue
        self._signer = signer
        self._gw = gateway
        self._cx = cancellation
        self._network = network
        self._signer_id = signer_id
        self._clock = clock
        self._max_age = max_request_age_seconds

    # ---------------------------------------------------------------- loop
    def process_once(self, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for req in self._q.pending(self._network, limit):
            if not self._q.claim(req["request_id"], self._signer_id):
                continue  # another instance or an earlier run owns it
            out.append(self._handle(req))
        return out

    def _event(self, request_id: str, event: str, **detail: Any) -> dict[str, Any]:
        d = event_detail(**detail)
        self._q.append_event(request_id, event, d, self._signer_id)
        return {"request_id": request_id, "event": event, "detail": d}

    def _refuse(self, request_id: str, code: str, **extra: Any) -> dict[str, Any]:
        return self._event(request_id, "REFUSED", code=code, **extra)

    def _handle(self, req: Mapping[str, Any]) -> dict[str, Any]:
        rid = req["request_id"]
        try:
            p = validate_request(req["payload"])
        except QueueError as exc:
            return self._refuse(rid, exc.code)
        if p["request_id"] != rid or canonical_sha256(p) != req.get("payload_sha256"):
            return self._refuse(rid, "REQUEST_HASH_MISMATCH")
        if p["network"] != self._network:
            return self._refuse(rid, "REQUEST_NETWORK_MISMATCH")
        now = int(self._clock())
        if not (p["created_at"] <= now + 5 and now - p["created_at"] <= self._max_age):
            return self._refuse(rid, "REQUEST_EXPIRED")
        c = p["command"]
        auth = cmd.OwnerCommandAuthorization(c["command_id"], c["command_text"], c["command_text_sha256"],
                                             c["received_at"], c["client_ref_sha256"])
        if p["kind"] == "PLACE":
            return self._place(rid, p, auth)
        return self._cancel(rid, p, auth)

    def _place(self, rid: str, p: Mapping[str, Any], auth: Any) -> dict[str, Any]:
        intent = p["intent"]
        cid0 = intent.get("idempotency_key")
        existing = self._signer._ledger.get(cid0) if isinstance(cid0, str) else None
        if existing is not None:
            # A second request for an order the ledger already knows (a retry, a duplicate or a replay by a
            # compromised writer): never signed or sent again, reported explicitly, never as a new submission.
            return self._event(rid, "DONE", code="DUPLICATE_OF_EXISTING_ORDER", client_order_id=cid0,
                               order_id=existing.order_id, state=existing.state, sent=False)
        try:
            signed = self._signer.sign_order(intent, auth, post_only=p["post_only"])
        except SignerRefused as exc:
            return self._refuse(rid, exc.code)
        cid = intent.get("idempotency_key")
        self._event(rid, "SIGNED", order_id=signed.get("order_id"), client_order_id=cid)
        try:
            res = self._gw.submit(signed, intent)
        except GatewayRefused as exc:
            # Signed, never sent: the ledger keeps the stake reserved until the signed expiry (signer/orders.py).
            return self._event(rid, "DONE", code=exc.code, client_order_id=cid, order_id=signed.get("order_id"),
                               sent=False, note="SIGNED_NOT_SENT_RESERVED_UNTIL_EXPIRY")
        state = res.get("state")
        if state == UNKNOWN:
            return self._event(rid, "UNKNOWN", client_order_id=cid, order_id=res.get("order_id"), state=state,
                               note="RECONCILE_BEFORE_ANY_RETRY")
        return self._event(rid, "SUBMITTED", client_order_id=cid, order_id=res.get("order_id"), state=state,
                           sent=res.get("sent", True))

    def _cancel(self, rid: str, p: Mapping[str, Any], auth: Any) -> dict[str, Any]:
        scope = p["scope"]
        try:
            if scope == "ORDER_IDS":
                rep = self._cx.cancel_orders(p["order_ids"], auth)
            elif scope == "MARKET":
                rep = self._cx.cancel_market(p["market_hash"], auth)
            elif scope == "EVENT":
                rep = self._cx.cancel_event(p["event_id"], auth)
            else:
                rep = self._cx.cancel_all(auth)
        except (CancelRefused, GatewayRefused, SignerRefused) as exc:
            return self._refuse(rid, getattr(exc, "code", "CANCEL_REFUSED"), scope=scope)
        orders = {oid: {k: v for k, v in r.items() if k in ("bucket", "reason", "state")}
                  for oid, r in (rep.get("orders") or {}).items()}
        return self._event(rid, "DONE", scope=scope, sent=bool(rep.get("sent")), orders=orders,
                           note="CANCEL_REPLY_IS_NOT_PROOF_STATE_FROM_READS")

    # ---------------------------------------------------------------- after a restart / periodically
    def recover(self) -> list[dict[str, Any]]:
        """Requests this signer claimed but never finished (crash between steps), resolved from its own ledger:
        never re-sent blindly. No ledger row: the command was not consumed, so the request is processed again."""
        out = []
        for req in self._q.unfinished(self._network, self._signer_id):
            rid, last = req["request_id"], req.get("last_state")
            p = req["payload"]
            cid = (p.get("intent") or {}).get("idempotency_key") if isinstance(p, Mapping) else None
            o = self._signer._ledger.get(cid) if (p.get("kind") == "PLACE" and isinstance(cid, str)) else None
            if p.get("kind") == "PLACE" and o is None and last == "CLAIMED":
                out.append(self._handle(req))
            elif o is not None and o.state == AUTHORIZED:
                self._signer.abandon_unsigned(cid)  # no signature ever left: release the reservation
                out.append(self._refuse(rid, "INTERRUPTED_BEFORE_SIGNATURE", client_order_id=cid))
            elif o is not None and o.state == SIGNED:
                out.append(self._event(rid, "DONE", client_order_id=cid, order_id=o.order_id, sent=False,
                                       code="INTERRUPTED_AFTER_SIGNATURE",
                                       note="SIGNED_NOT_SENT_RESERVED_UNTIL_EXPIRY"))
            elif o is not None:
                out.append(self._event(rid, "UNKNOWN" if o.state == UNKNOWN else "SUBMITTED", client_order_id=cid,
                                       order_id=o.order_id, state=o.state, note="RECOVERED_FROM_LEDGER"))
            elif p.get("kind") == "CANCEL" and last == "CLAIMED":
                out.append(self._event(rid, "UNKNOWN", scope=p.get("scope"),
                                       note="CANCEL_INTERRUPTED_RECONCILE_FROM_READS_BEFORE_A_NEW_COMMAND"))
        return out

    def sync_outcomes(self) -> list[dict[str, Any]]:
        """Final order states learnt by reconciliation (gateway / monitor reads) are mirrored as DONE events."""
        out = []
        for req in self._q.unfinished(self._network, self._signer_id):
            if req.get("last_state") not in ("SUBMITTED", "UNKNOWN"):
                continue
            cid = (req["payload"].get("intent") or {}).get("idempotency_key")
            o = self._signer._ledger.get(cid) if isinstance(cid, str) else None
            if o is not None and o.state in _FINAL_ORDER_STATES:
                out.append(self._event(req["request_id"], "DONE", client_order_id=cid, order_id=o.order_id,
                                       state=o.state))
        return out


__all__ = ["DEFAULT_MAX_REQUEST_AGE_SECONDS", "SignerService"]
