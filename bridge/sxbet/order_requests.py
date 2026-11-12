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
deduced by the model:
    QUEUED            no signer event yet (the request may still expire unprocessed)
    PROCESSING        claimed / signed, outcome of the send not recorded yet: never "not sent"
    REFUSED           refused by the signer BEFORE any signature left, or signed but never sent (ledger proof)
    UNKNOWN           anything ambiguous: a send that may have happened, an exchange rejection (not final while
                      a signature is out), inconsistent evidence. Reconcile before any new command.
    ACCEPTED          on the exchange, nothing filled (exchange reply or read)
    PARTIALLY_FILLED  some fill proven by the exchange
    FILLED            fully filled (exchange)
    CANCELLED         inactive on the exchange with nothing filled (exchange read or final ledger state)
    EXPIRED           final ledger state EXPIRED
Cancel requests report REFUSED / UNKNOWN / PROCESSING / ACCEPTED (the exchange accepted the cancel request:
per-order buckets are attached; the reply is not proof, read each order to confirm it is INACTIVE).

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
              "CANCELLED", "EXPIRED")

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
    return None


_LEDGER_TO_MCP = {"SUBMITTED": "ACCEPTED", "OPEN": "ACCEPTED", "PARTIALLY_FILLED": "PARTIALLY_FILLED",
                  "FILLED": "FILLED", "SETTLED": "FILLED", "CANCELLED": "CANCELLED", "EXPIRED": "EXPIRED"}
# Exchange read (bridge/sxbet/simulation.state_from_lookup) -> MCP state. NOT_ON_EXCHANGE is never mapped to
# "not sent": a 404 is weak evidence and is reported, not promoted.
_LOOKUP_TO_MCP = {"SUBMITTED": "ACCEPTED", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
                  "CANCELLED": "CANCELLED"}
_RANK = {"ACCEPTED": 0, "PARTIALLY_FILLED": 1, "FILLED": 2}


def queue_view(kind: str, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """MCP state from the signer events alone (labelled; the exchange read is merged by `merge_exchange`)."""
    bad = _check_events(events)
    if bad:
        return {"state": "UNKNOWN", "code": bad, "evidence": "QUEUE_EVENTS"}
    if not events:
        return {"state": "QUEUED", "evidence": "QUEUE_EVENTS"}
    last = events[-1]
    ev, d = last["state"], dict(last["detail"])
    out: dict[str, Any] = {"evidence": "QUEUE_EVENTS", "last_event": ev, "code": d.get("code"),
                           "note": d.get("note")}
    for k in ("order_id", "client_order_id"):
        ids = [e["detail"].get(k) for e in events if isinstance(e["detail"].get(k), str)]
        if ids:
            out[k] = ids[-1]
    if kind == "CANCEL":
        out["scope"] = d.get("scope")
        if ev == "CLAIMED":
            return {**out, "state": "PROCESSING"}
        if ev == "REFUSED":
            return {**out, "state": "REFUSED"}
        # "accepted" is set only by the scope cancels; cancel-by-ids reports per-order buckets instead.
        if ev == "DONE" and d.get("sent") is True and d.get("accepted") is not False:
            return {**out, "state": "ACCEPTED", "orders": d.get("orders"),
                    "warning": "The cancel reply is not proof: read each order (sxbet_order_status) until INACTIVE."}
        if ev == "DONE" and d.get("sent") is False:
            return {**out, "state": "REFUSED", "sent": False}
        return {**out, "state": "UNKNOWN"}
    if ev in ("CLAIMED", "SIGNED"):
        return {**out, "state": "PROCESSING"}
    if ev == "REFUSED":
        return {**out, "state": "REFUSED"}
    if ev == "UNKNOWN":
        return {**out, "state": "UNKNOWN", "ledger_state": d.get("state")}
    if ev == "SUBMITTED":
        mapped = _LEDGER_TO_MCP.get(d.get("state"))
        if mapped is None or d.get("sent") is False:
            # e.g. REJECTED (not final while a signature is out) or a duplicate the gateway did not send
            return {**out, "state": "UNKNOWN", "ledger_state": d.get("state"),
                    "code": d.get("code") or "LEDGER_STATE_NOT_CONCLUSIVE"}
        return {**out, "state": mapped, "ledger_state": d.get("state")}
    # DONE
    if d.get("sent") is False and "state" not in d:
        return {**out, "state": "REFUSED", "sent": False}
    mapped = _LEDGER_TO_MCP.get(d.get("state"))
    if mapped is None:
        return {**out, "state": "UNKNOWN", "ledger_state": d.get("state"),
                "code": d.get("code") or "LEDGER_STATE_NOT_CONCLUSIVE"}
    return {**out, "state": mapped, "ledger_state": d.get("state")}


def merge_exchange(view: Mapping[str, Any], lookup_state: str | None) -> dict[str, Any]:
    """Combine the queue view with a fresh exchange reconciliation. Deterministic precedence:
    - REFUSED (nothing ever reached the exchange) is never overridden;
    - a conclusive exchange state is fresher than the queue: it wins when it is the same or a later stage of the
      same life (ACCEPTED < PARTIALLY_FILLED < FILLED), and it resolves UNKNOWN / PROCESSING;
    - an exchange state that contradicts a later or final queue state is a conflict: UNKNOWN."""
    out = dict(view)
    out["exchange_state"] = lookup_state
    if view["state"] in ("REFUSED", "QUEUED") or lookup_state is None:
        return out
    ex = _LOOKUP_TO_MCP.get(lookup_state)
    if ex is None:
        return out  # e.g. NOT_ON_EXCHANGE: reported as exchange_state, never promoted to "not sent"
    q = view["state"]
    if q in ("UNKNOWN", "PROCESSING") or q == ex:
        out.update(state=ex, evidence="EXCHANGE_READ")
    elif q in _RANK and ex in _RANK and _RANK[ex] >= _RANK[q]:
        out.update(state=ex, evidence="EXCHANGE_READ")
    elif q in _RANK and ex == "CANCELLED" and q == "ACCEPTED":
        out.update(state=ex, evidence="EXCHANGE_READ")
    else:
        out.update(state="UNKNOWN", evidence="CONFLICT", code="QUEUE_AND_EXCHANGE_DISAGREE", queue_state=q)
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
        if dsn == e.get("DATABASE_URL", "").strip():
            # The queue lives in the signer's own database, never in the bridge database (WP-009).
            return cls(error="SXBET_SIGNER_QUEUE_MUST_NOT_BE_THE_BRIDGE_DATABASE")
        if not _ADDRESS.fullmatch(maker):
            return cls(error="SXBET_MAKER_ADDRESS_INVALID")
        return cls(enabled=True, queue_dsn=dsn, maker=maker.lower())

    def __repr__(self) -> str:  # never print the DSN
        return f"OrderRequestSettings(enabled={self.enabled}, maker={self.maker!r}, error={self.error!r})"


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
