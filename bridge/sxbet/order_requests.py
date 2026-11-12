"""MCP side of the owner order-request queue (issue #187, S7; governance §2.6). Never signs, never sends.

The MCP tools built on this module only WRITE a request row (`signer_order_requests`, append-only) to the
signer's own database and READ the signer's events (`signer_order_request_events`). Everything that matters
for money happens in the isolated signer process (signer/service.py), which re-validates the request, verifies
the owner's literal command against the order on its OWN reads, applies the owner-signed limits and the kill
switch, signs and submits. Nothing under bridge/ imports signer/: the request contract below is duplicated on
purpose and a test asserts it matches signer/request_queue.py byte for byte (schema, hash, field set).

Disabled by default: the tools exist only with SXBET_ENABLED and SXBET_ORDER_REQUESTS_ENABLED both true and a
complete configuration (`OrderRequestSettings`). No tool argument can enable them, change the network, the
maker, limits, modes or the kill switch.

Idempotency (owner requirement 3): the request id is DERIVED from (network, kind, idempotency key), so a
second call with the same key can never create a second request. The same key with the same order and the
same command text returns the existing request (`ALREADY_QUEUED`, nothing new written); the same key with
anything different is refused (`IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`). The signer refuses the
same situations again on its own ledger.

States (owner requirements 1, 2, 4): the status reported to the model is a pure function of the append-only
signer events (written from the signer's ledger, i.e. from exchange replies and reconciliation reads) and,
when an order id is known, of a fresh exchange read (GET /orders-v3/{orderId} + GET /fills-v3). It is never
deduced by the model. Every view carries `final` (True only when the outcome can no longer change: FILLED is
final for the fill amount, it can still become SETTLED in the ledger):
    QUEUED            no signer event yet (the request may still expire unprocessed)
    PROCESSING        claimed / signed, outcome of the send not recorded yet: never "not sent"
    REFUSED           refused by the signer BEFORE any signature (final); for a cancel, also "nothing sent"
    UNKNOWN           anything ambiguous: a send that may have happened, a signed order never sent (signature
                      outstanding until its expiry), an exchange rejection (not final while a signature is out), a
                      final state without its fill amount, inconsistent or conflicting evidence. Reconcile first.
    ACCEPTED          on the exchange; no fill seen in the evidence read (a snapshot: see `fill_evidence`)
    PARTIALLY_FILLED  some fill proven (exchange read or ledger); `resting: False` when the rest is inactive
    FILLED            fully filled (exchange read or final ledger state FILLED)
    CANCELLED         inactive with nothing filled IN THE EVIDENCE READ (exchange read, or ledger CANCELLED with
                      filled = 0); not final: a late fill can appear until settlement
    EXPIRED           expired with nothing filled in the evidence read (same rules as CANCELLED)
    SETTLED           settled by the exchange; the matched amount is `filled_base_units` (never "full")
Cancel requests: REFUSED (nothing sent), UNKNOWN, PROCESSING, and for a cancel that was sent: ACCEPTED (every
order reported cancelled; scope cancels: request accepted), NOT_CANCELLED, PARTIALLY_CANCELLED. A cancel reply is
not proof: read each order until INACTIVE.

Residual risk (owner decision "Opzione 1", 12-11-2026): a compromised MCP can write a request with a forged
command text. It is bounded, not eliminated, by the signer: single-use command id and fingerprint, deterministic
verification of the text against the order on the signer's reads, owner-signed limits, persistent kill switch,
idempotency and reconciliation. See docs/sxbet/README.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

REQUEST_SCHEMA = "sxbet.order_request.v1"
KINDS = ("PLACE", "CANCEL")
CANCEL_SCOPES = ("ORDER_IDS", "MARKET", "EVENT", "ALL")
EVENT_STATES = ("CLAIMED", "REFUSED", "SIGNED", "SUBMITTED", "UNKNOWN", "DONE")
TERMINAL_EVENTS = frozenset({"REFUSED", "DONE"})
MAX_COMMAND_CHARS = 500
# client_ref_sha256 is audit-only in the signer. The bridge has no verified conversation reference to offer
# (the OAuth middleware exposes none to tools): it says so explicitly instead of inventing one.
CLIENT_REF_UNAVAILABLE = "bridge:no-conversation-reference"
MCP_STATES = ("QUEUED", "PROCESSING", "REFUSED", "UNKNOWN", "ACCEPTED", "PARTIALLY_FILLED", "FILLED",
              "CANCELLED", "EXPIRED", "SETTLED", "NOT_CANCELLED", "PARTIALLY_CANCELLED")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Fields that legitimately differ between two calls carrying the same order and the same command.
_VOLATILE = ("request_id", "created_at")
_VOLATILE_COMMAND = ("command_id", "received_at")


class RequestInvalid(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _need(cond: bool, code: str) -> None:
    if not cond:
        raise RequestInvalid(code)


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


def text_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_id_for(network: str, kind: str, idempotency_key: str) -> str:
    """Deterministic: one key can never produce two requests of the same kind on the same network."""
    raw = f"{REQUEST_SCHEMA}|{network}|{kind}|{idempotency_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def deterministic_salt(network: str, maker: str, idempotency_key: str) -> int:
    """Salt of the signed order, derived from the key so a retried call builds the very same intent."""
    raw = f"sxbet.order_salt.v1|{network}|{maker.lower()}|{idempotency_key}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest(), "big")


def owner_command_text(text: Any) -> str:
    """The owner's literal message, unchanged (the signer hashes and verifies exactly these bytes)."""
    _need(isinstance(text, str) and text.strip() != "", "OWNER_COMMAND_MISSING")
    _need(len(text) <= MAX_COMMAND_CHARS, "OWNER_COMMAND_TOO_LONG")
    _need(all(c.isprintable() for c in text), "OWNER_COMMAND_NOT_PRINTABLE")
    return text


def idempotency_key(value: Any) -> str:
    _need(isinstance(value, str) and _IDEMPOTENCY.fullmatch(value) is not None, "INVALID_IDEMPOTENCY_KEY")
    return value


def _command(text: str, now: int) -> dict[str, Any]:
    return {"command_id": secrets.token_hex(16), "command_text": text, "command_text_sha256": text_sha256(text),
            "received_at": now, "client_ref_sha256": text_sha256(CLIENT_REF_UNAVAILABLE)}


def build_place_request(*, network: str, intent: Mapping[str, Any], post_only: bool, command_text: str,
                        now: int) -> dict[str, Any]:
    _need(network in ("mainnet", "testnet"), "INVALID_NETWORK")
    _need(isinstance(intent, Mapping) and intent.get("network") == network, "INTENT_NETWORK_MISMATCH")
    _need(intent.get("maker") is not None and intent.get("salt") is not None, "INTENT_NOT_SIGNING_READY")
    _need(isinstance(post_only, bool), "INVALID_POST_ONLY")
    key = idempotency_key(intent.get("idempotency_key"))
    return {"schema": REQUEST_SCHEMA, "request_id": request_id_for(network, "PLACE", key), "kind": "PLACE",
            "network": network, "created_at": now, "command": _command(owner_command_text(command_text), now),
            "intent": dict(intent), "post_only": post_only}


def build_cancel_request(*, network: str, scope: Any, key: Any, command_text: str, now: int,
                         order_ids: Any = None, market_hash: Any = None, event_id: Any = None) -> dict[str, Any]:
    _need(network in ("mainnet", "testnet"), "INVALID_NETWORK")
    _need(scope in CANCEL_SCOPES, "INVALID_CANCEL_SCOPE")
    k = idempotency_key(key)
    p: dict[str, Any] = {"schema": REQUEST_SCHEMA, "request_id": request_id_for(network, "CANCEL", k),
                         "kind": "CANCEL", "network": network, "created_at": now,
                         "command": _command(owner_command_text(command_text), now), "scope": scope}
    extra = {"ORDER_IDS": order_ids, "MARKET": market_hash, "EVENT": event_id}
    for s, v in extra.items():
        _need(s == scope or v is None, "CANCEL_ARGUMENT_NOT_FOR_THIS_SCOPE")
    if scope == "ORDER_IDS":
        _need(isinstance(order_ids, list) and 1 <= len(order_ids) <= 100
              and all(isinstance(i, str) and _HASH.fullmatch(i) for i in order_ids)
              and len({i.lower() for i in order_ids}) == len(order_ids), "INVALID_ORDER_IDS")
        p["order_ids"] = [i.lower() for i in order_ids]
    elif scope == "MARKET":
        _need(isinstance(market_hash, str) and _HASH.fullmatch(market_hash) is not None, "INVALID_MARKET_HASH")
        p["market_hash"] = market_hash.lower()
    elif scope == "EVENT":
        _need(isinstance(event_id, str) and _EVENT_ID.fullmatch(event_id) is not None, "INVALID_EVENT_ID")
        p["event_id"] = event_id
    return p


def same_request(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Same order and same command text: only the per-call fields (ids and timestamps) may differ."""
    def core(p: Mapping[str, Any]) -> Any:
        c = {k: v for k, v in p.items() if k not in _VOLATILE}
        if isinstance(c.get("command"), Mapping):
            c["command"] = {k: v for k, v in c["command"].items() if k not in _VOLATILE_COMMAND}
        return json.loads(json.dumps(c, sort_keys=True))
    return core(a) == core(b)


# ---------------------------------------------------------------- state from evidence
# Allowed successor of each signer event (signer/service.py: process, recover, sync_outcomes). Anything else is
# evidence the MCP cannot trust as it is (S7 review of ce4a702, P3-4).
_NEXT = {"CLAIMED": frozenset({"SIGNED", "REFUSED", "UNKNOWN", "SUBMITTED", "DONE"}),
         "SIGNED": frozenset({"SUBMITTED", "UNKNOWN", "DONE", "REFUSED"}),
         "SUBMITTED": frozenset({"DONE"}),
         "UNKNOWN": frozenset({"DONE"}),
         "REFUSED": frozenset(), "DONE": frozenset()}


def _check_events(events: Sequence[Mapping[str, Any]]) -> str | None:
    """Structural evidence check; any violation means the events cannot be trusted as they are."""
    for i, e in enumerate(events, start=1):
        if not isinstance(e, Mapping) or e.get("seq") != i or e.get("state") not in EVENT_STATES \
                or not isinstance(e.get("detail"), Mapping):
            return "QUEUE_EVIDENCE_MALFORMED"
    states = [e["state"] for e in events]
    if states and states[0] != "CLAIMED":
        return "QUEUE_EVIDENCE_NOT_CLAIMED_FIRST"
    if states.count("CLAIMED") > 1:
        return "QUEUE_EVIDENCE_DUPLICATE_CLAIM"
    if any(s in TERMINAL_EVENTS for s in states[:-1]):
        return "QUEUE_EVIDENCE_EVENT_AFTER_TERMINAL"
    if len({e.get("signer_id") for e in events}) > 1:
        return "QUEUE_EVIDENCE_MIXED_WRITERS"
    if any(b not in _NEXT[a] for a, b in zip(states, states[1:])):
        return "QUEUE_EVIDENCE_TRANSITION_NOT_ALLOWED"
    return None


_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")  # same rule as signer/orders.py (review 47a770b P3-2)


def _units(v: Any) -> int | None:
    return int(v) if isinstance(v, str) and _DECIMAL.fullmatch(v) is not None else None


# Exchange read (bridge/sxbet/simulation.state_from_lookup, EXPIRED split by inactiveReason in the tool).
# NOT_ON_EXCHANGE is never mapped: a 404 is weak evidence and is reported, not promoted.
_LOOKUP_TO_MCP = {"SUBMITTED": "ACCEPTED", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
                  "CANCELLED": "CANCELLED", "EXPIRED": "EXPIRED"}
_RANK = {"ACCEPTED": 0, "PARTIALLY_FILLED": 1, "FILLED": 2}
_INACTIVE_NO_FILL = frozenset({"CANCELLED", "EXPIRED"})


def _place_view(events: Sequence[Mapping[str, Any]], out: dict[str, Any], stake: int | None) -> dict[str, Any]:
    states = [e["state"] for e in events]
    last = events[-1]
    ev, d = last["state"], last["detail"]
    if ev in ("CLAIMED", "SIGNED"):
        return {**out, "state": "PROCESSING", "final": False}
    if ev == "REFUSED":
        if "SIGNED" in states:
            # A refusal recorded after this request's SIGNED event: a signature may be outstanding.
            return {**out, "state": "UNKNOWN", "final": False, "code": d.get("code") or "REFUSED_AFTER_SIGNATURE"}
        return {**out, "state": "REFUSED", "final": True}
    if ev == "UNKNOWN":
        return {**out, "state": "UNKNOWN", "final": False, "ledger_state": d.get("state")}
    ledger, filled = d.get("state"), _units(d.get("filled"))
    if filled is not None:
        out["filled_base_units"] = str(filled)
        out["filled_source"] = "SIGNER_LEDGER_SNAPSHOT"  # as of the signer's event, not a fresh read (P3-3)
    if ev == "SUBMITTED":
        if d.get("sent") is False:  # e.g. a duplicate the gateway did not send: not proof of anything here
            return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger,
                    "code": d.get("code") or "LEDGER_STATE_NOT_CONCLUSIVE"}
        if ledger in ("SUBMITTED", "OPEN"):
            return {**out, "state": "ACCEPTED", "final": False, "ledger_state": ledger}
        if ledger == "PARTIALLY_FILLED":
            return {**out, "state": ledger, "final": False, "ledger_state": ledger}
        if ledger == "FILLED":
            # The send-time event carries no amount: never "fully filled" without it (review 47a770b P2-1).
            return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger, "code": "FILL_AMOUNT_NOT_PROVEN"}
        # REJECTED is not final while a signature is out (signer/orders.py H3); anything else is not conclusive.
        return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger,
                "code": d.get("code") or "LEDGER_STATE_NOT_CONCLUSIVE"}
    # DONE
    if ledger is None and d.get("sent") is False:
        # Signed, never sent (ledger proof at that moment), the stake stays reserved until the signed expiry and
        # the signature is outstanding: NOT a final refusal (S7 review of ce4a702, P2-1). Exchange read follows.
        return {**out, "state": "UNKNOWN", "final": False, "code": "SIGNED_NOT_SENT_SIGNATURE_OUTSTANDING",
                "note": "Signed but not sent according to the signer's ledger; the signed order stays reserved "
                        "until its expiry. Not a final refusal: reconcile before any new command."}
    # `ledger_final`: the signer recorded a final ledger state (merge_exchange treats a contradicting read as a
    # conflict). `final` (for the model) is narrower: CANCELLED / EXPIRED / a partial fill can still gain a late
    # fill until settlement (signer/orders.py LATE_FILL_AFTER_EXPIRY, CANCELLED self-transition): review of
    # 8396424, P2-A.
    if ledger == "FILLED":
        # signer/orders.py records an INACTIVE/FILLED row with a remainder as ledger FILLED with filled < stake:
        # FILLED here only when the matched amount IS the signed stake (review of 47a770b, P2-1).
        if filled is None or stake is None:
            return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger, "code": "FILL_AMOUNT_NOT_PROVEN"}
        if filled == stake:
            return {**out, "state": "FILLED", "final": True, "ledger_final": True, "ledger_state": ledger}
        if 0 < filled < stake:
            return {**out, "state": "PARTIALLY_FILLED", "final": False, "ledger_final": True, "resting": False,
                    "ledger_state": ledger, "code": "FILLED_AMOUNT_BELOW_STAKE"}
        return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger, "code": "FILL_AMOUNT_INCONSISTENT"}
    if ledger == "SETTLED":
        # Settled by the exchange: some stake was matched; how much is filled_base_units (ledger), never "full".
        return {**out, "state": "SETTLED", "final": True, "ledger_final": True, "ledger_state": ledger}
    if ledger in _INACTIVE_NO_FILL:
        if filled is None:
            return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger,
                    "code": "FILL_AMOUNT_NOT_PROVEN"}
        late = "Not final: a late fill can still appear until settlement; read the fills before relying on it."
        if filled == 0:
            return {**out, "state": ledger, "final": False, "ledger_final": True, "ledger_state": ledger,
                    "warning": late}
        return {**out, "state": "PARTIALLY_FILLED", "final": False, "ledger_final": True, "resting": False,
                "ledger_state": ledger, "warning": late}
    return {**out, "state": "UNKNOWN", "final": False, "ledger_state": ledger,
            "code": d.get("code") or "LEDGER_STATE_NOT_CONCLUSIVE"}


def _bucket_state(orders: Any) -> str:
    """Cancel by ids: the state follows the per-order buckets of the exchange reply (S7 review P2-2)."""
    if not isinstance(orders, Mapping) or not orders:
        return "UNKNOWN"
    buckets = {o.get("bucket") if isinstance(o, Mapping) else None for o in orders.values()}
    if buckets == {"CANCELLED"}:
        return "ACCEPTED"
    if buckets == {"NOT_CANCELLED"}:
        return "NOT_CANCELLED"
    if buckets == {"CANCELLED", "NOT_CANCELLED"}:
        return "PARTIALLY_CANCELLED"
    return "UNKNOWN"  # UNKNOWN / UNCONFIRMED / missing: the cancel may or may not have landed


def _cancel_view(events: Sequence[Mapping[str, Any]], out: dict[str, Any]) -> dict[str, Any]:
    last = events[-1]
    ev, d = last["state"], last["detail"]
    out["scope"] = d.get("scope")
    if isinstance(d.get("orders"), Mapping):
        out["orders"] = d["orders"]
    if ev == "CLAIMED":
        return {**out, "state": "PROCESSING", "final": False}
    if ev == "REFUSED":
        return {**out, "state": "REFUSED", "final": True}
    if ev == "DONE" and d.get("sent") is False:
        return {**out, "state": "REFUSED", "final": True, "sent": False}  # command not consumed, nothing sent
    if ev == "DONE" and d.get("sent") is True:
        if d.get("scope") == "ORDER_IDS":
            st = _bucket_state(d.get("orders"))
        else:
            st = "ACCEPTED" if d.get("accepted") is True else "UNKNOWN"
        return {**out, "state": st, "final": st != "UNKNOWN",
                "warning": "A cancel reply is not proof: read each order (sxbet_order_status) until INACTIVE."}
    return {**out, "state": "UNKNOWN", "final": False}


def queue_view(kind: str, events: Sequence[Mapping[str, Any]], *, intent_total: Any = None) -> dict[str, Any]:
    """MCP state from the signer events alone (labelled; the exchange read is merged by `merge_exchange`)."""
    bad = _check_events(events)
    if bad:
        return {"state": "UNKNOWN", "final": False, "code": bad, "evidence": "QUEUE_EVENTS"}
    if not events:
        return {"state": "QUEUED", "final": False, "evidence": "QUEUE_EVENTS"}
    d = events[-1]["detail"]
    out: dict[str, Any] = {"evidence": "QUEUE_EVENTS", "last_event": events[-1]["state"], "code": d.get("code"),
                           "note": d.get("note")}
    for k in ("order_id", "client_order_id"):
        ids = [e["detail"].get(k) for e in events if isinstance(e["detail"].get(k), str)]
        if ids:
            out[k] = ids[-1]
    return _cancel_view(events, out) if kind == "CANCEL" else _place_view(events, out, _units(intent_total))


_NO_FILL_CLAIMED = frozenset({"ACCEPTED", "CANCELLED", "EXPIRED"})
# Documented inactiveReason values (docs/sxbet/magic_and_proxy_wallet.md; same set as signer/orders.py).
_CANCEL_REASONS = frozenset({"USER_REQUESTED", "NO_LIQUIDITY", "INSUFFICIENT_BALANCE", "EVENT_LIFECYCLE",
                             "MARKET_HALTED", "HEARTBEAT_TIMEOUT", "POST_ONLY_WOULD_CROSS", "SYSTEM"})


def exchange_row_state(row: Any, *, order_id: str, intent_total: Any) -> dict[str, Any]:
    """One GET /orders-v3/{orderId} row -> lookup state, with the signer's fail-closed guards (evidence_from_exchange,
    review of 21082c0 P2-1): another order, a size that is not the signed stake, remaining outside 0..total, FILLED
    without a fill, an unknown status or inactiveReason -> state None (never promoted). Returned states use the
    lookup vocabulary of merge_exchange: SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED."""
    def none(reason: str) -> dict[str, Any]:
        return {"state": None, "reason": reason, "resting": None}
    if not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or row["id"].lower() != order_id.lower():
        return none("EVIDENCE_OTHER_ORDER")
    total, remaining, stake = _units(row.get("totalBetSize")), _units(row.get("remainingSize")), _units(intent_total)
    if total is None or remaining is None or stake is None or total != stake or remaining > total:
        return none("EVIDENCE_SIZE_MISMATCH")
    filled = total - remaining
    status, reason = row.get("status"), row.get("inactiveReason")
    if status not in ("PENDING", "ACTIVE", "INACTIVE"):
        return none("EVIDENCE_STATUS_UNKNOWN")
    if filled == total and not (status == "INACTIVE" and reason == "FILLED"):
        # Whole stake matched but not reported as FILLED: the signer records no evidence either (review P3-5).
        return none("EVIDENCE_FULL_FILL_NOT_MARKED_FILLED")
    if status in ("PENDING", "ACTIVE"):
        return {"state": "PARTIALLY_FILLED" if filled else "SUBMITTED", "reason": "EXCHANGE_ORDER_" + status,
                "resting": True, "filled": filled}
    if reason == "FILLED":
        if not filled:
            return none("EVIDENCE_FILLED_WITHOUT_FILL")
        st = "FILLED" if filled == total else "PARTIALLY_FILLED"
    elif reason == "EXPIRED":
        st = "PARTIALLY_FILLED" if filled else "EXPIRED"
    elif reason in _CANCEL_REASONS:
        st = "PARTIALLY_FILLED" if filled else "CANCELLED"
    else:
        return none("EVIDENCE_INACTIVE_REASON_UNKNOWN")
    return {"state": st, "reason": "EXCHANGE_ORDER_INACTIVE_" + str(reason), "resting": False, "filled": filled}


def _merge(view: Mapping[str, Any], lookup_state: str | None, fills_seen: bool) -> dict[str, Any]:
    """Combine the queue view with a fresh exchange reconciliation. Deterministic precedence:
    - REFUSED / QUEUED / SETTLED are never changed by an order read (SETTLED comes after the order lifecycle);
    - fill rows on the exchange against a state that claims nothing filled: UNKNOWN (conflict);
    - a final ledger state stands unless the read contradicts it: then UNKNOWN (conflict);
    - otherwise a conclusive read that proves presence on the exchange (ACCEPTED, PARTIALLY_FILLED, FILLED) wins
      unless it is an earlier stage than the queue's (conflict);
    - an inactive-without-fills read (CANCELLED / EXPIRED) replaces only ACCEPTED: it never resolves UNKNOWN or
      PROCESSING, because a signature may still be outstanding (review P3-2)."""
    out = dict(view)
    out["exchange_state"] = lookup_state
    q = view["state"]
    ex = _LOOKUP_TO_MCP.get(lookup_state) if lookup_state is not None else None
    if q in ("REFUSED", "QUEUED", "SETTLED") or ex is None:
        return out
    if view.get("ledger_final"):
        if ex != q and not (q == "PARTIALLY_FILLED" and ex == "PARTIALLY_FILLED"):
            out.update(state="UNKNOWN", final=False, evidence="CONFLICT", code="QUEUE_AND_EXCHANGE_DISAGREE",
                       queue_state=q)
        return out
    if ex in _RANK:
        if q in _RANK and _RANK[ex] < _RANK[q]:
            out.update(state="UNKNOWN", evidence="CONFLICT", code="QUEUE_AND_EXCHANGE_DISAGREE", queue_state=q)
        else:
            out.update(state=ex, evidence="EXCHANGE_READ")
        return out
    if q == "ACCEPTED":  # ex is CANCELLED / EXPIRED: inactive with nothing filled
        out.update(state=ex, evidence="EXCHANGE_READ")
    elif q in _RANK:     # the queue proves a fill, the read says nothing filled: never pick one silently
        out.update(state="UNKNOWN", evidence="CONFLICT", code="QUEUE_AND_EXCHANGE_DISAGREE", queue_state=q)
    return out


def merge_exchange(view: Mapping[str, Any], lookup_state: str | None, *, fills_seen: bool = False,
                   ex_resting: bool | None = None) -> dict[str, Any]:
    if (view.get("ledger_final") and view["state"] == "PARTIALLY_FILLED" and lookup_state == "PARTIALLY_FILLED"
            and ex_resting is True):
        # The ledger says the remainder is no longer resting, the row shows it resting (review 21082c0 P3-2).
        out = dict(view)
        out.update(exchange_state=lookup_state, state="UNKNOWN", final=False, evidence="CONFLICT",
                   code="QUEUE_AND_EXCHANGE_DISAGREE", queue_state=view["state"])
        out.pop("resting", None)
        return out
    out = _merge(view, lookup_state, fills_seen)
    if out.get("evidence") == "EXCHANGE_READ":
        # The exchange row now decides the state: queue-only codes and notes (e.g. "signed but not sent") move
        # under queue_* so they are never read as describing the new state (review 21082c0 P3-3).
        if out["state"] != view["state"]:
            out["queue_state"] = view["state"]
            for k in ("code", "note", "warning"):
                if out.get(k) is not None:
                    out["queue_" + k] = out.pop(k)
        if ex_resting is not None:
            out["resting"] = ex_resting
    elif out.get("evidence") == "CONFLICT":
        out.pop("resting", None)
    # Checked on the MERGED result, whatever the queue state was: fill rows on the exchange for this order and a
    # result that claims nothing filled (ACCEPTED / CANCELLED / EXPIRED) is a conflict, never "no fill seen"
    # (reviews of 8396424 P2-A and c9c32e7 P2-1: e.g. PROCESSING / UNKNOWN + ACTIVE unfilled row + fill rows).
    if fills_seen and out["state"] in _NO_FILL_CLAIMED and lookup_state not in ("PARTIALLY_FILLED", "FILLED"):
        out.update(state="UNKNOWN", final=False, evidence="CONFLICT", code="FILLS_SEEN_ON_EXCHANGE",
                   queue_state=view["state"])
        out.pop("resting", None)  # review 47a770b P3-4
    return out


# ---------------------------------------------------------------- configuration and storage
@dataclass(frozen=True)
class OrderRequestSettings:
    enabled: bool = False
    queue_dsn: str = ""
    maker: str = ""
    error: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OrderRequestSettings":
        """Fail closed: enabled but incomplete -> not enabled, with a fixed error code (never the values)."""
        e = os.environ if env is None else env
        if e.get("SXBET_ORDER_REQUESTS_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return cls()
        dsn = e.get("SXBET_SIGNER_QUEUE_DATABASE_URL", "").strip()
        maker = e.get("SXBET_MAKER_ADDRESS", "").strip()
        if not dsn:
            return cls(error="SXBET_SIGNER_QUEUE_DATABASE_URL_MISSING")
        if _same_database(dsn, e.get("DATABASE_URL", "").strip()):
            # The queue lives in the signer's own database, never in the bridge database (WP-009).
            return cls(error="SXBET_SIGNER_QUEUE_MUST_NOT_BE_THE_BRIDGE_DATABASE")
        if not _ADDRESS.fullmatch(maker):
            return cls(error="SXBET_MAKER_ADDRESS_INVALID")
        return cls(enabled=True, queue_dsn=dsn, maker=maker.lower())

    def __repr__(self) -> str:  # never print the DSN
        return f"OrderRequestSettings(enabled={self.enabled}, maker={self.maker!r}, error={self.error!r})"


def _db_identity(dsn: str) -> tuple:
    """(host, port, database) of a URL DSN, case-folded, default port 5432; any other form: the raw string.
    A best-effort guard only (review P3-6): the binding control is the WP-009 role grants."""
    if "://" not in dsn:
        return ("RAW", dsn)
    try:
        u = urlsplit(dsn)
        q = parse_qs(u.query)
        host = (u.hostname or (q.get("host") or [""])[0]).lower()
        port = u.port or int((q.get("port") or ["5432"])[0])
        db = unquote(u.path.lstrip("/")) or (q.get("dbname") or [""])[0]
    except ValueError:
        return ("RAW", dsn)
    return (host, port, db)


def _same_database(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a == b or _db_identity(a) == _db_identity(b))


class QueueUnavailable(RuntimeError):
    def __init__(self, code: str = "SIGNER_QUEUE_UNAVAILABLE"):
        super().__init__(code)
        self.code = code


class PostgresRequestWriter:
    """MCP role on the signer database: INSERT + SELECT on signer_order_requests, SELECT on the events. It can
    never write an event, update or delete anything (append-only triggers + grants, WP-009)."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

    def _run(self, sql: str, params: Sequence[Any]) -> list[tuple]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '2500ms'")
                    cur.execute(sql, params)
                    return list(cur.fetchall())
        finally:
            conn.close()

    def submit(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """("INSERTED", None) or ("EXISTS", stored_payload). Exceptions mean the outcome is UNKNOWN."""
        rows = self._run(
            "INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
            "VALUES (%s, %s, %s::jsonb, %s, %s) ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
            (payload["request_id"], payload["kind"], json.dumps(dict(payload), sort_keys=True),
             canonical_sha256(payload), payload["network"]))
        if rows:
            return "INSERTED", None
        stored = self.request(payload["request_id"])
        if stored is None:
            raise QueueUnavailable("SIGNER_QUEUE_INCONSISTENT")
        return "EXISTS", stored

    def request(self, request_id: str) -> dict[str, Any] | None:
        rows = self._run("SELECT payload::text FROM signer_order_requests WHERE request_id = %s", (request_id,))
        return json.loads(rows[0][0]) if rows else None

    def events(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._run("SELECT seq, state, detail::text, signer_id FROM signer_order_request_events "
                         "WHERE request_id = %s ORDER BY seq", (request_id,))
        return [{"seq": r[0], "state": r[1], "detail": json.loads(r[2]), "signer_id": r[3]} for r in rows]


def postgres_connect(dsn: str) -> Callable[[], Any]:
    def connect() -> Any:
        import psycopg  # bridge dependency; imported lazily so the module stays importable offline
        return psycopg.connect(dsn, connect_timeout=3)
    return connect


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


__all__ = ["CANCEL_SCOPES", "CLIENT_REF_UNAVAILABLE", "EVENT_STATES", "KINDS", "MCP_STATES",
           "OrderRequestSettings", "PostgresRequestWriter", "QueueUnavailable", "REQUEST_SCHEMA",
           "RequestInvalid", "build_cancel_request", "build_place_request", "canonical_sha256",
           "deterministic_salt", "merge_exchange", "postgres_connect", "queue_view", "request_id_for",
           "same_request", "valid_request_id"]
