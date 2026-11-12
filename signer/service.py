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
from .intent import IntentInvalid, intent_hash, validate
from .orders import AUTHORIZED, SIGNED, UNKNOWN
from .request_queue import QueueError, RequestQueue, canonical_sha256, event_detail, validate_request

DEFAULT_MAX_REQUEST_AGE_SECONDS = 120   # never above the owner-signed command window (checked again by the signer)
# REJECTED is not final: a rejected order whose signature left can still turn out executed (signer/orders.py H3).
_FINAL_ORDER_STATES = frozenset({"FILLED", "CANCELLED", "EXPIRED", "SETTLED"})


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
        # One process per signer_id; processing, recovery and sync never run concurrently, also across several
        # service objects built on the same signer (lock shared per Signer: review 67e80eb P3-3).
        self._lock = signer._ledger.service_lock

    # ---------------------------------------------------------------- loop
    def process_once(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
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
        try:
            clean = validate(intent, odds_ladder_step_size=self._signer.config.odds_ladder_step_size)
        except IntentInvalid as exc:
            return self._refuse(rid, str(exc))
        cid0 = clean["idempotency_key"]
        existing = self._signer._ledger.get(cid0)
        if existing is not None:
            # A second request for a key the ledger already knows: never signed or sent again, and never reported
            # as a success or with the old order's state (its command was not verified): review 1a9e09e P2-1.
            if existing.intent_hash != intent_hash(clean):
                return self._refuse(rid, "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT", client_order_id=cid0)
            return self._refuse(rid, "DUPLICATE_REQUEST", client_order_id=cid0)
        try:
            signed = self._signer.sign_order(intent, auth, post_only=p["post_only"])
        except SignerRefused as exc:
            return self._refuse(rid, exc.code)
        cid = intent.get("idempotency_key")
        self._event(rid, "SIGNED", order_id=signed.get("order_id"), client_order_id=cid)
        try:
            res = self._gw.submit(signed, intent)
        except GatewayRefused as exc:
            o = self._signer._ledger.get(cid)
            if o is not None and o.state == SIGNED:
                # Refused before any send: the ledger keeps the stake reserved until the signed expiry.
                return self._event(rid, "DONE", code=exc.code, client_order_id=cid, order_id=signed.get("order_id"),
                                   sent=False, note="SIGNED_NOT_SENT_RESERVED_UNTIL_EXPIRY")
            # Past the write-ahead: it may have been sent (review 1a9e09e P2-2). Never "not sent".
            return self._event(rid, "UNKNOWN", code=exc.code, client_order_id=cid, order_id=signed.get("order_id"),
                               state=None if o is None else o.state, note="RECONCILE_BEFORE_ANY_RETRY")
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
        except (CancelRefused, SignerRefused) as exc:
            return self._refuse(rid, getattr(exc, "code", "CANCEL_REFUSED"), scope=scope)
        except GatewayRefused as exc:  # may be after the send: never a terminal refusal (review P3)
            return self._event(rid, "UNKNOWN", code=exc.code, scope=scope,
                               note="CANCEL_MAY_HAVE_BEEN_SENT_RECONCILE_FROM_READS")
        orders = {oid: {k: v for k, v in r.items() if k in ("bucket", "reason", "state")}
                  for oid, r in (rep.get("orders") or {}).items()}
        if rep.get("sent") and rep.get("accepted") is False:
            return self._event(rid, "UNKNOWN", scope=scope, sent=True, accepted=False, code=rep.get("reason"),
                               note="CANCEL_NOT_ACCEPTED_STATE_FROM_READS")
        return self._event(rid, "DONE", scope=scope, sent=bool(rep.get("sent")), accepted=rep.get("accepted"),
                           orders=orders or None, note="CANCEL_REPLY_IS_NOT_PROOF_STATE_FROM_READS")

    # ---------------------------------------------------------------- after a restart / periodically
    def recover(self) -> list[dict[str, Any]]:
        """Requests this signer claimed but never finished (crash between steps), resolved from its own ledger:
        never re-sent blindly. No ledger row: the command was not consumed, so the request is processed again.
        Runs under the service lock, never concurrently with process_once (review 1a9e09e P2-3), and records
        only transitions: a request already recorded SUBMITTED / UNKNOWN is left to sync_outcomes (P2-4)."""
        with self._lock:
            return self._recover()

    def _recover(self) -> list[dict[str, Any]]:
        out = []
        for req in self._q.unfinished(self._network, self._signer_id):
            rid, last = req["request_id"], req.get("last_state")
            p = req["payload"]
            if not isinstance(p, Mapping):
                out.append(self._refuse(rid, "REQUEST_MALFORMED"))
                continue
            if last not in ("CLAIMED", "SIGNED"):
                continue  # already recorded as SUBMITTED / UNKNOWN: nothing new to append
            cid = (p.get("intent") or {}).get("idempotency_key") if isinstance(p.get("intent"), Mapping) else None
            o = self._signer._ledger.get(cid) if (p.get("kind") == "PLACE" and isinstance(cid, str)) else None
            command_id = (p.get("command") or {}).get("command_id") if isinstance(p.get("command"), Mapping) else None
            ours = o is not None and self._signer._ledger.order_of_command(command_id) == cid
            if p.get("kind") == "PLACE" and o is None and last == "CLAIMED":
                out.append(self._handle(req))
            elif o is not None and not ours:
                # The ledger order belongs to ANOTHER request with the same key (a duplicate claimed before a
                # crash): never reported as this request's outcome (review 67e80eb P2-B).
                out.append(self._handle(req) if last == "CLAIMED" else
                           self._refuse(rid, "DUPLICATE_REQUEST", client_order_id=cid))
            elif o is not None and o.state == AUTHORIZED:
                self._signer.abandon_unsigned(cid)  # ours, no signature ever left: release the reservation
                out.append(self._refuse(rid, "INTERRUPTED_BEFORE_SIGNATURE", client_order_id=cid))
            elif o is not None and o.state == SIGNED:
                out.append(self._event(rid, "DONE", client_order_id=cid, order_id=o.order_id, sent=False,
                                       code="INTERRUPTED_AFTER_SIGNATURE",
                                       note="SIGNED_NOT_SENT_RESERVED_UNTIL_EXPIRY"))
            elif o is not None:
                # Ours and past the send write-ahead: UNKNOWN unless the ledger shows it reached the exchange.
                ev = "SUBMITTED" if o.state in ("SUBMITTED", "OPEN", "PARTIALLY_FILLED", "FILLED") else "UNKNOWN"
                out.append(self._event(rid, ev, client_order_id=cid, order_id=o.order_id, state=o.state,
                                       note="RECOVERED_FROM_LEDGER"))
            elif p.get("kind") == "CANCEL" and last == "CLAIMED":
                out.append(self._event(rid, "UNKNOWN", scope=p.get("scope"),
                                       note="CANCEL_INTERRUPTED_RECONCILE_FROM_READS_BEFORE_A_NEW_COMMAND"))
        return out

    def sync_outcomes(self) -> list[dict[str, Any]]:
        """Final order states learnt by reconciliation (gateway / monitor reads) are mirrored as DONE events."""
        with self._lock:
            return self._sync_outcomes()

    def _sync_outcomes(self) -> list[dict[str, Any]]:
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
