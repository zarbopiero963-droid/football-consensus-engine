"""Persistent, crash-safe, append-only state of the signer service (SQLite, WAL, synchronous=FULL).

Tables (the database itself refuses UPDATE / DELETE through triggers, except the documented counter):
- requests:  one immutable row per request (canonical payload hash, what the owner will see and sign);
- audit:     append-only, hash-chained entries; every state TRANSITION of a request is an audit row and
             the current state is always the last TRANSITION (rebuilt from disk, never kept only in memory);
- credentials: registered owner passkeys; only `sign_count` may change, and only upwards (trigger);
- enrollment_codes / enrollment_used: one-time passkey enrolment codes (stored as SHA-256 only).

Lifecycle (anything else is ILLEGAL_TRANSITION and is not written):
    REQUESTED -> PENDING_APPROVAL | REJECTED
    PENDING_APPROVAL -> APPROVED | REJECTED | EXPIRED
    APPROVED -> SIGNED (orders) | HANDED_TO_GATEWAY (cancels) | REJECTED
    SIGNED -> HANDED_TO_GATEWAY | REJECTED
    terminal: HANDED_TO_GATEWAY, REJECTED, EXPIRED
Audit rows carry only allowlisted fields: never a key, an HMAC secret, a signature or an assertion.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any, Callable, Mapping

from signer.webauthn import StoredCredential, b64url_decode, b64url_encode, sign_count_acceptable

REQUESTED, PENDING, APPROVED, SIGNED = "REQUESTED", "PENDING_APPROVAL", "APPROVED", "SIGNED"
HANDED, REJECTED, EXPIRED = "HANDED_TO_GATEWAY", "REJECTED", "EXPIRED"
TERMINAL = frozenset({HANDED, REJECTED, EXPIRED})
TRANSITIONS = {
    None: {REQUESTED},
    REQUESTED: {PENDING, REJECTED},
    PENDING: {APPROVED, REJECTED, EXPIRED},
    APPROVED: {SIGNED, HANDED, REJECTED},
    SIGNED: {HANDED, REJECTED},
    HANDED: set(), REJECTED: set(), EXPIRED: set(),
}
DETAIL_FIELDS = frozenset({"kind", "order_id", "intent_hash", "client_order_id", "gateway", "code", "decision",
                           "credential_prefix", "key_id", "data_source"})
GENESIS = "sha256:" + "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, key_id TEXT NOT NULL, client_request_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, record_json TEXT NOT NULL,
  UNIQUE (key_id, client_request_id));
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, event TEXT NOT NULL, request_id TEXT,
  state TEXT, reason TEXT, detail_json TEXT NOT NULL, prev_hash TEXT NOT NULL, line_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_request ON audit (request_id, seq);
CREATE TABLE IF NOT EXISTS credentials (
  credential_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, sign_count INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS enrollment_codes (code_hash TEXT PRIMARY KEY, issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS enrollment_used (code_hash TEXT PRIMARY KEY, used_at INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS requests_no_update BEFORE UPDATE ON requests BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS requests_no_delete BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS codes_no_update BEFORE UPDATE ON enrollment_codes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS codes_no_delete BEFORE DELETE ON enrollment_codes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS used_no_update BEFORE UPDATE ON enrollment_used BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS used_no_delete BEFORE DELETE ON enrollment_used BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS credentials_no_delete BEFORE DELETE ON credentials BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS credentials_counter_only BEFORE UPDATE ON credentials
  WHEN NEW.credential_id IS NOT OLD.credential_id OR NEW.public_key IS NOT OLD.public_key
    OR NEW.created_at IS NOT OLD.created_at OR NEW.sign_count < OLD.sign_count
    OR (NEW.sign_count = OLD.sign_count AND OLD.sign_count != 0)
  BEGIN SELECT RAISE(ABORT, 'SIGN_COUNT_NOT_INCREASING'); END;
"""


class StoreError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _line_hash(prev: str, row: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256((prev + "\n" + canonical(dict(row))).encode("ascii")).hexdigest()


class ServiceStore:
    def __init__(self, path: str, clock: Callable[[], float]):
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._head = self._verify_chain()

    def close(self) -> None:
        self._db.close()

    # ---- audit chain
    def _verify_chain(self) -> str:
        prev = GENESIS
        for seq, at, event, rid, state, reason, detail, p, h in self._db.execute(
                "SELECT seq, at, event, request_id, state, reason, detail_json, prev_hash, line_hash FROM audit "
                "ORDER BY seq"):
            row = {"seq": seq, "at": at, "event": event, "request_id": rid, "state": state, "reason": reason,
                   "detail_json": detail}
            if p != prev or _line_hash(prev, row) != h:
                raise StoreError("STORE_AUDIT_CHAIN_BROKEN")
            prev = h
        return prev

    def _append(self, event: str, *, request_id: str | None = None, state: str | None = None,
                reason: str | None = None, detail: Mapping[str, Any] | None = None) -> None:
        detail = dict(detail or {})
        if set(detail) - DETAIL_FIELDS:
            raise StoreError("AUDIT_FIELD_NOT_ALLOWED")
        seq = (self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM audit").fetchone()[0]) + 1
        row = {"seq": seq, "at": int(self._clock()), "event": event, "request_id": request_id, "state": state,
               "reason": reason, "detail_json": canonical(detail)}
        h = _line_hash(self._head, row)
        self._db.execute("INSERT INTO audit (seq, at, event, request_id, state, reason, detail_json, prev_hash, "
                         "line_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (seq, row["at"], event, request_id, state, reason, row["detail_json"], self._head, h))
        self._head = h

    def audit(self, event: str, **kw: Any) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._append(event, **kw)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._head = self._verify_chain()
                raise

    def audit_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT seq, at, event, request_id, state, reason, detail_json FROM audit "
                                   "ORDER BY seq")
            return [{"seq": s, "at": a, "event": e, "request_id": r, "state": st, "reason": re_,
                     "detail": json.loads(d)} for s, a, e, r, st, re_, d in cur]

    # ---- requests
    def _state(self, request_id: str) -> str | None:
        row = self._db.execute("SELECT state FROM audit WHERE request_id = ? AND event = 'TRANSITION' "
                               "ORDER BY seq DESC LIMIT 1", (request_id,)).fetchone()
        return row[0] if row else None

    def _transition(self, request_id: str, new: str, reason: str | None, detail: Mapping[str, Any] | None) -> None:
        cur = self._state(request_id)
        if new not in TRANSITIONS[cur]:
            raise StoreError("ILLEGAL_TRANSITION")
        self._append("TRANSITION", request_id=request_id, state=new, reason=reason, detail=detail)

    def create(self, *, request_id: str, kind: str, key_id: str, client_request_id: str, payload_sha256: str,
               created_at: int, expires_at: int, record: Mapping[str, Any],
               initial: list[tuple[str, str | None]]) -> tuple[str, bool]:
        """Insert a request with its first transitions; idempotent on (key_id, client_request_id).

        Returns (request_id, created). Same id + same canonical payload => the existing request;
        same id + different payload => StoreError(DUPLICATE_REQUEST_CONFLICT)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT request_id, payload_sha256, kind FROM requests WHERE key_id = ? "
                                       "AND client_request_id = ?", (key_id, client_request_id)).fetchone()
                if row is not None:
                    self._db.execute("ROLLBACK")
                    if row[1] != payload_sha256 or row[2] != kind:
                        raise StoreError("DUPLICATE_REQUEST_CONFLICT")
                    return row[0], False
                self._db.execute("INSERT INTO requests (request_id, kind, key_id, client_request_id, payload_sha256, "
                                 "created_at, expires_at, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (request_id, kind, key_id, client_request_id, payload_sha256, created_at,
                                  expires_at, canonical(record)))
                for state, reason in initial:
                    self._transition(request_id, state, reason, {"kind": kind, "key_id": key_id})
                self._db.execute("COMMIT")
                return request_id, True
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                self._head = self._verify_chain()
                raise

    def transition(self, request_id: str, new: str, *, reason: str | None = None,
                   detail: Mapping[str, Any] | None = None, expect: str | None = None) -> None:
        """Atomic compare-and-append: with `expect`, refused (STATE_CHANGED) unless the state is still `expect`."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if expect is not None and self._state(request_id) != expect:
                    raise StoreError("STATE_CHANGED")
                self._transition(request_id, new, reason, detail)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._head = self._verify_chain()
                raise

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT request_id, kind, key_id, client_request_id, payload_sha256, created_at, "
                                   "expires_at, record_json FROM requests WHERE request_id = ?",
                                   (request_id,)).fetchone()
            if row is None:
                return None
            hist = [{"state": s, "reason": r, "at": a, "detail": json.loads(d)} for s, r, a, d in self._db.execute(
                "SELECT state, reason, at, detail_json FROM audit WHERE request_id = ? AND event = 'TRANSITION' "
                "ORDER BY seq", (request_id,))]
        return {"request_id": row[0], "kind": row[1], "key_id": row[2], "client_request_id": row[3],
                "payload_sha256": row[4], "created_at": row[5], "expires_at": row[6],
                "record": json.loads(row[7]), "state": hist[-1]["state"] if hist else None, "history": hist}

    def non_terminal(self) -> list[str]:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT request_id FROM requests ORDER BY created_at")]
            return [i for i in ids if self._state(i) not in TERMINAL]

    # ---- passkeys (signer.webauthn.CredentialStore)
    def add_credential(self, cred: StoredCredential) -> None:
        with self._lock:
            try:
                self._db.execute("INSERT INTO credentials (credential_id, public_key, sign_count, created_at) "
                                 "VALUES (?, ?, ?, ?)", (b64url_encode(cred.credential_id), cred.public_key.hex(),
                                                         cred.sign_count, int(self._clock())))
            except sqlite3.IntegrityError:
                raise StoreError("WEBAUTHN_CREDENTIAL_EXISTS") from None

    def get_credential(self, credential_id: bytes) -> StoredCredential | None:
        with self._lock:
            row = self._db.execute("SELECT public_key, sign_count FROM credentials WHERE credential_id = ?",
                                   (b64url_encode(bytes(credential_id)),)).fetchone()
        return None if row is None else StoredCredential(bytes(credential_id), bytes.fromhex(row[0]), row[1])

    def update_sign_count(self, credential_id: bytes, old: int, new: int) -> bool:
        if not sign_count_acceptable(old, new):
            return False
        with self._lock:
            try:
                cur = self._db.execute("UPDATE credentials SET sign_count = ? WHERE credential_id = ? AND "
                                       "sign_count = ?", (new, b64url_encode(bytes(credential_id)), old))
            except sqlite3.DatabaseError:
                return False
            return cur.rowcount == 1

    def credential_ids(self) -> list[bytes]:
        with self._lock:
            return [b64url_decode(r[0]) for r in self._db.execute("SELECT credential_id FROM credentials "
                                                                  "ORDER BY created_at")]

    # ---- enrolment codes
    def add_enrollment_code(self, code_hash: str, issued_at: int, expires_at: int) -> None:
        with self._lock:
            self._db.execute("INSERT INTO enrollment_codes (code_hash, issued_at, expires_at) VALUES (?, ?, ?)",
                             (code_hash, issued_at, expires_at))

    def enrollment_code_valid(self, code_hash: str, now: int) -> bool:
        with self._lock:
            row = self._db.execute("SELECT issued_at, expires_at FROM enrollment_codes WHERE code_hash = ?",
                                   (code_hash,)).fetchone()
            used = self._db.execute("SELECT 1 FROM enrollment_used WHERE code_hash = ?", (code_hash,)).fetchone()
        return row is not None and used is None and row[0] <= now < row[1]

    def consume_enrollment_code(self, code_hash: str) -> bool:
        with self._lock:
            try:
                self._db.execute("INSERT INTO enrollment_used (code_hash, used_at) VALUES (?, ?)",
                                 (code_hash, int(self._clock())))
            except sqlite3.IntegrityError:
                return False
            return True


class CredentialView:
    """signer.webauthn.CredentialStore adapter over the service store (keeps `get` unambiguous)."""

    def __init__(self, store: ServiceStore):
        self._s = store

    def get(self, credential_id: bytes) -> StoredCredential | None:
        return self._s.get_credential(credential_id)

    def update_sign_count(self, credential_id: bytes, old: int, new: int) -> bool:
        return self._s.update_sign_count(credential_id, old, new)
