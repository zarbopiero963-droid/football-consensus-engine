"""TEST-ONLY in-memory request queue with the same semantics as signer/request_queue.PostgresRequestQueue:
append-only rows, one CLAIMED per request, events in seq order. Plus a helper that builds what the MCP writes."""
import json
import secrets
from dataclasses import asdict

from signer.request_queue import REQUEST_SCHEMA, TERMINAL_STATES, canonical_sha256


class MemoryQueue:
    def __init__(self):
        self.requests = []        # (request_id, kind, payload, payload_sha256, network)
        self.events = {}          # request_id -> [{"seq", "state", "detail", "signer_id"}]

    def insert(self, payload, *, sha=None):
        assert payload["request_id"] not in {r[0] for r in self.requests}, "duplicate request_id"
        self.requests.append((payload["request_id"], payload["kind"], json.loads(json.dumps(payload)),
                              sha or canonical_sha256(payload), payload["network"]))

    def pending(self, network, limit):
        return [{"request_id": r[0], "kind": r[1], "payload": json.loads(json.dumps(r[2])), "payload_sha256": r[3]}
                for r in self.requests if r[4] == network and r[0] not in self.events][:limit]

    def claim(self, request_id, signer_id):
        evs = self.events.setdefault(request_id, [])
        if any(e["state"] == "CLAIMED" for e in evs):
            return False
        evs.append({"seq": len(evs) + 1, "state": "CLAIMED", "detail": {}, "signer_id": signer_id})
        return True

    def append_event(self, request_id, state, detail, signer_id):
        assert state != "CLAIMED" and request_id in self.events
        evs = self.events[request_id]
        evs.append({"seq": len(evs) + 1, "state": state, "detail": json.loads(json.dumps(dict(detail))),
                    "signer_id": signer_id})
        return len(evs)

    def unfinished(self, network, signer_id):
        out = []
        for r in self.requests:
            evs = self.events.get(r[0], [])
            if r[4] != network or not evs or evs[-1]["state"] in TERMINAL_STATES:
                continue
            if any(e["state"] == "CLAIMED" and e["signer_id"] == signer_id for e in evs):
                out.append({"request_id": r[0], "kind": r[1], "payload": json.loads(json.dumps(r[2])),
                            "payload_sha256": r[3], "last_state": evs[-1]["state"]})
        return out

    def events_of(self, request_id):
        return list(self.events.get(request_id, []))

    def states(self, request_id):
        return [e["state"] for e in self.events.get(request_id, [])]


def place_request(intent, auth, *, network="mainnet", created_at, post_only=False, request_id=None):
    """What the MCP side (S7) writes: the canonical intent and the owner's literal command, nothing else."""
    return {"schema": REQUEST_SCHEMA, "request_id": request_id or secrets.token_hex(16), "kind": "PLACE",
            "network": network, "created_at": created_at, "command": asdict(auth), "intent": dict(intent),
            "post_only": post_only}


def cancel_request(auth, *, scope, network="mainnet", created_at, order_ids=None, market_hash=None, event_id=None):
    p = {"schema": REQUEST_SCHEMA, "request_id": secrets.token_hex(16), "kind": "CANCEL", "network": network,
         "created_at": created_at, "command": asdict(auth), "scope": scope}
    if order_ids is not None:
        p["order_ids"] = list(order_ids)
    if market_hash is not None:
        p["market_hash"] = market_hash
    if event_id is not None:
        p["event_id"] = event_id
    return p
