"""Offline doubles for the official REST adapter, cancellation and ledger anchoring tests.

- FakeSender: an in-memory `RestSender`. It records every RestRequest and answers from a script or from
  in-memory exchange state (OrderV3 rows, FillV3 rows). It never opens a socket.
- MemoryAnchorStore: an in-memory `AnchorStore` with the same semantics as the PostgreSQL one
  (append-only heads, monotonic seq, one writer lease, fencing token), plus switches that simulate an
  unreachable database and a lease taken over by another writer.
No network, no API key, no real endpoint.
"""
from __future__ import annotations

import threading

from signer import anchor as an
from signer.rest_adapter import RestResponse, SenderTimeout


def ok(rows):
    return RestResponse(200, {"status": "success", "data": {"orders": rows}})


def submitted_row(order_id, client_order_id=None, outcome=None, status="SUBMITTED", command_id="cmd-1"):
    r = {"orderId": order_id, "status": status, "commandId": command_id}
    if client_order_id is not None:
        r["clientOrderId"] = client_order_id
    if outcome is not None:
        r["outcome"] = outcome
    return r


def order_row(oid, status, remaining, reason=None, total=5_000_000, market="0x" + "ab" * 32):
    return {"id": oid, "marketHash": market, "totalBetSize": str(total), "remainingSize": str(remaining),
            "status": status, "inactiveReason": reason}


class FakeSender:
    def __init__(self):
        self.requests = []
        self.script = []        # RestResponse | "timeout" | Exception | callable(request) -> RestResponse
        self.orders = {}        # order_id -> OrderV3 row
        self.by_client = {}     # clientOrderId -> OrderV3 row
        self.fills = {}         # order_id -> [FillV3]
        self.fill_page = 100
        self.get_error = None   # "timeout" | int status: every GET fails that way
        self.on_delete = None   # callable(request) run before answering a DELETE (simulate the engine acting)
        self.lock = threading.Lock()

    def writes(self):
        return [r for r in self.requests if r.method != "GET"]

    def send(self, req):
        with self.lock:
            self.requests.append(req)
            if req.method == "GET":
                return self._get(req)
            if req.method == "DELETE" and self.on_delete is not None:
                self.on_delete(req)
            r = self.script.pop(0) if self.script else None
        if r == "timeout":
            raise SenderTimeout()
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(req)
        if r is None:
            raise AssertionError("FakeSender: no scripted reply for " + req.method + " " + req.path)
        return r

    def _get(self, req):
        if self.get_error == "timeout":
            raise SenderTimeout()
        if isinstance(self.get_error, int):
            return RestResponse(self.get_error, {"message": "Service Unavailable"})
        q = dict(req.query)
        if req.path.startswith("/orders-v3/client/"):
            row = self.by_client.get(req.path.rsplit("/", 1)[1])
        elif req.path.startswith("/orders-v3/"):
            row = self.orders.get(req.path.rsplit("/", 1)[1])
        elif req.path == "/fills-v3":
            rows = self.fills.get(q.get("orderId"), [])
            start = int(q.get("nextKey", "0"))
            page = rows[start:start + self.fill_page]
            data = {"fills": page}
            if start + self.fill_page < len(rows):
                data["nextKey"] = str(start + self.fill_page)
            return RestResponse(200, {"status": "success", "data": data})
        else:
            return RestResponse(404, {"message": "Not Found"})
        if row is None:
            return RestResponse(404, {"message": "Order not found", "error": "Not Found", "statusCode": 404})
        return RestResponse(200, {"status": "success", "data": {"order": row}})


class _MemLease:
    def __init__(self, store, ledger_id, token):
        self.store, self.ledger_id, self.token = store, ledger_id, token
        self.released = False

    def release(self):
        with self.store.lock:
            if not self.released and self.store.holders.get(self.ledger_id) is self:
                del self.store.holders[self.ledger_id]
            self.released = True


class MemoryAnchorStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.heads = {}     # ledger_id -> [AnchorHead] (append-only history)
        self.tokens = {}    # ledger_id -> current fencing token
        self.holders = {}   # ledger_id -> _MemLease
        self.unreachable = False
        self.advances = 0

    def _up(self):
        if self.unreachable:
            raise an.AnchorUnavailable("ANCHOR_UNREACHABLE")

    def acquire_writer(self, ledger_id):
        with self.lock:
            self._up()
            if ledger_id in self.holders:
                raise an.AnchorLocked("ANCHOR_WRITER_LOCKED")
            token = self.tokens.get(ledger_id, 0) + 1
            self.tokens[ledger_id] = token
            lease = _MemLease(self, ledger_id, token)
            self.holders[ledger_id] = lease
            return lease

    def take_over(self, ledger_id):
        """Simulate the old writer's session dying and another writer acquiring the lease."""
        with self.lock:
            self.holders.pop(ledger_id, None)
        return self.acquire_writer(ledger_id)

    def head(self, ledger_id):
        with self.lock:
            self._up()
            rows = self.heads.get(ledger_id)
            return rows[-1] if rows else None

    def advance(self, ledger_id, *, token, expected_seq, seq, head_hash):
        with self.lock:
            self._up()
            if self.tokens.get(ledger_id) != token:
                raise an.AnchorFenced("ANCHOR_FENCED")
            rows = self.heads.setdefault(ledger_id, [])
            current = rows[-1].seq if rows else 0
            if current != expected_seq or seq <= current:
                raise an.AnchorConflict("ANCHOR_SEQ_CONFLICT")
            rows.append(an.AnchorHead(seq=seq, head_hash=head_hash, fencing_token=token))
            self.advances += 1
