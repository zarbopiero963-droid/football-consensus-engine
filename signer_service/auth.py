"""MCP -> signer request authentication: HMAC-SHA256, ±30 s skew, persistent nonce replay cache.

Canonical string (UTF-8, '\\n'-joined):
    FCB-SIGNER-HMAC-V1, METHOD, path[?query], hex(SHA-256(body)), timestamp, nonce, key_id
Headers: X-FCB-Key-Id, X-FCB-Timestamp (unix seconds), X-FCB-Nonce ([A-Za-z0-9_-]{16,64}),
X-FCB-Signature (lowercase hex HMAC-SHA256 with the key's secret).

Order of checks: key known -> timestamp within ±30 s -> constant-time signature compare -> nonce recorded
(INSERT, primary key) in SQLite. The nonce is written only for an authentic request, so unauthenticated
traffic cannot fill the cache, and it survives restarts. Entries older than the skew window are pruned
(a request that old is refused by the timestamp check anyway).

Least privilege: a key carries an explicit scope set. The MCP key gets only
{"request:create", "request:read"}: it can file an order / cancel REQUEST and read its status. No scope
exists for approving, signing, enrolling a passkey or changing limits; those routes never accept HMAC.
Every refusal is a fixed code (AuthError); secrets and exception text never leave this module.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping

SCHEME = "FCB-SIGNER-HMAC-V1"
MAX_SKEW_SECONDS = 30
SCOPE_CREATE, SCOPE_READ = "request:create", "request:read"
MCP_SCOPES = frozenset({SCOPE_CREATE, SCOPE_READ})
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TS = re.compile(r"^[0-9]{1,12}$")
_SIG = re.compile(r"^[0-9a-f]{64}$")
MIN_SECRET_BYTES = 32


class AuthError(Exception):
    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class HmacKey:
    key_id: str
    secret: bytes = field(repr=False)
    scopes: frozenset[str] = MCP_SCOPES

    def __post_init__(self):
        if not _KEY_ID.fullmatch(self.key_id) or not isinstance(self.secret, bytes) \
                or len(self.secret) < MIN_SECRET_BYTES or not self.scopes <= MCP_SCOPES:
            raise ValueError("AUTH_CONFIG_INVALID")


def canonical_string(method: str, path: str, body: bytes, timestamp: str, nonce: str, key_id: str) -> bytes:
    return "\n".join([SCHEME, method.upper(), path, hashlib.sha256(body).hexdigest(), timestamp, nonce,
                      key_id]).encode("utf-8")


def sign(secret: bytes, method: str, path: str, body: bytes, timestamp: str, nonce: str, key_id: str) -> str:
    return hmac.new(secret, canonical_string(method, path, body, timestamp, nonce, key_id), hashlib.sha256).hexdigest()


def client_headers(key: HmacKey, method: str, path: str, body: bytes, *, timestamp: int, nonce: str) -> dict[str, str]:
    """What a future MCP-side client sends (pure function; this repository ships no such client)."""
    ts = str(timestamp)
    return {"X-FCB-Key-Id": key.key_id, "X-FCB-Timestamp": ts, "X-FCB-Nonce": nonce,
            "X-FCB-Signature": sign(key.secret, method, path, body, ts, nonce, key.key_id)}


class NonceCache:
    """SQLite-backed replay cache: (key_id, nonce) can be used once, across restarts."""

    def __init__(self, path: str, clock: Callable[[], float]):
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS hmac_nonces (key_id TEXT NOT NULL, nonce TEXT NOT NULL, "
                         "seen_at INTEGER NOT NULL, PRIMARY KEY (key_id, nonce))")

    def use(self, key_id: str, nonce: str) -> bool:
        now = int(self._clock())
        with self._lock:
            self._db.execute("DELETE FROM hmac_nonces WHERE seen_at < ?", (now - 4 * MAX_SKEW_SECONDS,))
            try:
                self._db.execute("INSERT INTO hmac_nonces (key_id, nonce, seen_at) VALUES (?, ?, ?)",
                                 (key_id, nonce, now))
            except sqlite3.IntegrityError:
                return False
            return True

    def close(self) -> None:
        self._db.close()


class HmacVerifier:
    def __init__(self, keys: Mapping[str, HmacKey], nonces: NonceCache, clock: Callable[[], float]):
        self._keys = dict(keys)
        self._nonces = nonces
        self._clock = clock

    def verify(self, *, method: str, path: str, body: bytes, headers: Mapping[str, str], scope: str) -> HmacKey:
        key_id = headers.get("x-fcb-key-id")
        ts = headers.get("x-fcb-timestamp")
        nonce = headers.get("x-fcb-nonce")
        sig = headers.get("x-fcb-signature")
        if not all(isinstance(v, str) for v in (key_id, ts, nonce, sig)):
            raise AuthError("AUTH_MISSING")
        if not _KEY_ID.fullmatch(key_id) or not _TS.fullmatch(ts) or not _NONCE.fullmatch(nonce) \
                or not _SIG.fullmatch(sig):
            raise AuthError("AUTH_MALFORMED")
        key = self._keys.get(key_id)
        if key is None:
            # Compute anyway so an unknown key id costs the same as a known one.
            # Same work and the same code as a bad signature: no oracle on which key ids exist.
            hmac.compare_digest(sign(b"\x00" * MIN_SECRET_BYTES, method, path, body, ts, nonce, key_id), sig)
            raise AuthError("AUTH_SIGNATURE_INVALID")
        if abs(int(self._clock()) - int(ts)) > MAX_SKEW_SECONDS:
            raise AuthError("AUTH_TIMESTAMP_SKEW")
        if not hmac.compare_digest(sign(key.secret, method, path, body, ts, nonce, key_id), sig):
            raise AuthError("AUTH_SIGNATURE_INVALID")
        if scope not in key.scopes:
            raise AuthError("AUTH_SCOPE_DENIED", 403)
        if not self._nonces.use(key_id, nonce):
            raise AuthError("AUTH_NONCE_REPLAYED")
        return key
