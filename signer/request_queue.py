"""Persistent request queue between the MCP bridge and the isolated signer (issue #187, S6; governance §2.6).

The MCP writes ONE row per owner request (`signer_order_requests`, append-only); the signer claims it and
appends every state change as a new row (`signer_order_request_events`, append-only). The schema is the draft
`sql/drafts/signer/0002_signer_order_requests.sql` (to be promoted to the signer series with WP-009; never the
bridge database). This module imports no database driver: the
Postgres store takes an injected `connect()` (e.g. `psycopg.connect` with the signer's own DSN and role).

Request contract `sxbet.order_request.v1` (the MCP side builds it; the signer re-validates everything and
trusts nothing):
    {"schema", "request_id" (32 lowercase hex), "kind" PLACE|CANCEL, "network" mainnet|testnet,
     "created_at" (unix s), "command": {"command_id", "command_text", "command_text_sha256", "received_at",
     "client_ref_sha256"},
     PLACE:  "intent" (the canonical intent of bridge/sxbet/simulation.py, with maker and salt),
             "post_only" (bool)
     CANCEL: "scope" ORDER_IDS|MARKET|EVENT|ALL plus "order_ids" | "market_hash" | "event_id"}
The command text is the owner's literal message in the MCP client chat; the signer verifies it
deterministically against the order (signer/command.py). Nothing in a request can change limits, modes or
the kill switch. Event details carry fixed codes and identifiers only: never a signature, a key or the
command text.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping, Protocol, Sequence

REQUEST_SCHEMA = "sxbet.order_request.v1"
KINDS = ("PLACE", "CANCEL")
CANCEL_SCOPES = ("ORDER_IDS", "MARKET", "EVENT", "ALL")
EVENT_STATES = ("CLAIMED", "REFUSED", "SIGNED", "SUBMITTED", "UNKNOWN", "DONE")
TERMINAL_STATES = frozenset({"REFUSED", "DONE"})
DETAIL_KEYS = frozenset({"code", "order_id", "client_order_id", "state", "sent", "orders", "note", "scope", "accepted"})
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # same rule as signer/cancellation.py


class QueueError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


def _need(cond: bool, code: str) -> None:
    if not cond:
        raise QueueError(code)


def validate_request(payload: Any) -> dict[str, Any]:
    """Shape check of a request read from the queue; semantic checks stay in the signer. Fixed codes."""
    _need(isinstance(payload, Mapping), "REQUEST_MALFORMED")
    _need(payload.get("schema") == REQUEST_SCHEMA, "REQUEST_SCHEMA_UNSUPPORTED")
    _need(isinstance(payload.get("request_id"), str) and _HEX32.fullmatch(payload["request_id"]) is not None,
          "REQUEST_ID_INVALID")
    _need(payload.get("kind") in KINDS, "REQUEST_KIND_INVALID")
    _need(payload.get("network") in ("mainnet", "testnet"), "REQUEST_NETWORK_INVALID")
    _need(type(payload.get("created_at")) is int and payload["created_at"] > 0, "REQUEST_CREATED_AT_INVALID")
    c = payload.get("command")
    _need(isinstance(c, Mapping) and set(c) == {"command_id", "command_text", "command_text_sha256", "received_at",
                                                 "client_ref_sha256"}, "REQUEST_COMMAND_MALFORMED")
    _need(all(isinstance(c[k], str) for k in ("command_id", "command_text", "command_text_sha256",
                                               "client_ref_sha256")) and type(c["received_at"]) is int,
          "REQUEST_COMMAND_MALFORMED")
    allowed = {"schema", "request_id", "kind", "network", "created_at", "command"}
    if payload["kind"] == "PLACE":
        _need(isinstance(payload.get("intent"), Mapping), "REQUEST_INTENT_MISSING")
        _need(isinstance(payload.get("post_only"), bool), "REQUEST_POST_ONLY_INVALID")
        allowed |= {"intent", "post_only"}
    else:
        scope = payload.get("scope")
        _need(scope in CANCEL_SCOPES, "REQUEST_CANCEL_SCOPE_INVALID")
        allowed |= {"scope"}
        if scope == "ORDER_IDS":
            ids = payload.get("order_ids")
            _need(isinstance(ids, list) and 1 <= len(ids) <= 100 and len(set(ids)) == len(ids)
                  and all(isinstance(i, str) and _HASH.fullmatch(i) for i in ids), "REQUEST_ORDER_IDS_INVALID")
            allowed |= {"order_ids"}
        elif scope == "MARKET":
            _need(isinstance(payload.get("market_hash"), str) and _HASH.fullmatch(payload["market_hash"]) is not None,
                  "REQUEST_MARKET_HASH_INVALID")
            allowed |= {"market_hash"}
        elif scope == "EVENT":
            _need(isinstance(payload.get("event_id"), str) and _EVENT_ID.fullmatch(payload["event_id"]) is not None,
                  "REQUEST_EVENT_ID_INVALID")
            allowed |= {"event_id"}
    _need(set(payload) <= allowed, "REQUEST_UNEXPECTED_FIELD")
    return dict(payload)


def event_detail(**fields: Any) -> dict[str, Any]:
    """Only allow-listed keys reach the queue (never a signature, key or command text)."""
    bad = set(fields) - DETAIL_KEYS
    if bad:
        raise QueueError("EVENT_DETAIL_FIELD_NOT_ALLOWED")
    return {k: v for k, v in fields.items() if v is not None}


class RequestQueue(Protocol):
    def pending(self, network: str, limit: int) -> list[dict[str, Any]]:
        """Requests of this network with no event yet, oldest first: {request_id, kind, payload, payload_sha256}."""
        ...

    def claim(self, request_id: str, signer_id: str) -> bool:
        """Append CLAIMED; False when another instance (or an earlier run) already claimed it."""
        ...

    def append_event(self, request_id: str, state: str, detail: Mapping[str, Any], signer_id: str) -> int:
        ...

    def unfinished(self, network: str, signer_id: str) -> list[dict[str, Any]]:
        """Requests claimed by this signer whose last event is not terminal (for recovery after a restart)."""
        ...

    def events(self, request_id: str) -> list[dict[str, Any]]:
        ...


class PostgresRequestQueue:
    """Signer-side queue on the signer database (migration signer/0002). `connect()` returns a DB-API connection
    (psycopg style, `%s` parameters); each call runs in its own short transaction."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

    def _run(self, sql: str, params: Sequence[Any] = (), *, fetch: bool = True,
             lock_request: str | None = None) -> list[tuple]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '2500ms'")
                    if lock_request is not None:
                        # Serialises seq allocation per request without any table privilege (a row lock needs
                        # UPDATE, which the least-privilege signer role does not have: review 67e80eb P2-A).
                        cur.execute("SELECT pg_advisory_xact_lock(hashtext('signer_order_request:' || %s))",
                                    (lock_request,))
                    cur.execute(sql, params)
                    return list(cur.fetchall()) if fetch else []
        finally:
            conn.close()

    def pending(self, network: str, limit: int) -> list[dict[str, Any]]:
        rows = self._run(
            "SELECT r.request_id, r.kind, r.payload::text, r.payload_sha256 FROM signer_order_requests r "
            "WHERE r.network = %s AND NOT EXISTS (SELECT 1 FROM signer_order_request_events e "
            "WHERE e.request_id = r.request_id) ORDER BY r.created_at, r.request_id LIMIT %s", (network, limit))
        return [{"request_id": r[0], "kind": r[1], "payload": json.loads(r[2]), "payload_sha256": r[3]} for r in rows]

    def claim(self, request_id: str, signer_id: str) -> bool:
        try:
            rows = self._run(
                "INSERT INTO signer_order_request_events (request_id, seq, state, detail, signer_id) "
                "VALUES (%s, 1, 'CLAIMED', '{}'::jsonb, %s) ON CONFLICT DO NOTHING RETURNING request_id",
                (request_id, signer_id), lock_request=request_id)
        except Exception as exc:  # the guard trigger refuses a late claim on a finished request (review P3-1)
            if "SIGNER_ORDER_REQUEST_ALREADY_TERMINAL" in str(exc):
                return False
            raise
        return bool(rows)

    def append_event(self, request_id: str, state: str, detail: Mapping[str, Any], signer_id: str) -> int:
        if state not in EVENT_STATES or state == "CLAIMED":
            raise QueueError("EVENT_STATE_INVALID")
        rows = self._run(
            "INSERT INTO signer_order_request_events (request_id, seq, state, detail, signer_id) "
            "SELECT %s, coalesce(max(seq), 0) + 1, %s, %s::jsonb, %s FROM signer_order_request_events "
            "WHERE request_id = %s RETURNING seq",
            (request_id, state, json.dumps(dict(detail), sort_keys=True), signer_id, request_id),
            lock_request=request_id)
        return rows[0][0]

    def unfinished(self, network: str, signer_id: str) -> list[dict[str, Any]]:
        rows = self._run(
            "SELECT r.request_id, r.kind, r.payload::text, r.payload_sha256, last.state FROM signer_order_requests r "
            "JOIN LATERAL (SELECT e.state FROM signer_order_request_events e WHERE e.request_id = r.request_id "
            "ORDER BY e.seq DESC LIMIT 1) last ON true "
            "WHERE r.network = %s AND EXISTS (SELECT 1 FROM signer_order_request_events c "
            "WHERE c.request_id = r.request_id AND c.state = 'CLAIMED' AND c.signer_id = %s) "
            "AND last.state NOT IN ('REFUSED', 'DONE') ORDER BY r.created_at, r.request_id", (network, signer_id))
        return [{"request_id": r[0], "kind": r[1], "payload": json.loads(r[2]), "payload_sha256": r[3],
                 "last_state": r[4]} for r in rows]

    def events(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._run("SELECT seq, state, detail::text, signer_id FROM signer_order_request_events "
                         "WHERE request_id = %s ORDER BY seq", (request_id,))
        return [{"seq": r[0], "state": r[1], "detail": json.loads(r[2]), "signer_id": r[3]} for r in rows]


__all__ = ["CANCEL_SCOPES", "EVENT_STATES", "KINDS", "PostgresRequestQueue", "QueueError", "REQUEST_SCHEMA",
           "RequestQueue", "TERMINAL_STATES", "canonical_sha256", "event_detail", "validate_request"]
