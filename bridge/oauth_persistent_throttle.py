"""Persistent (PostgreSQL) layer of the OAuth anti-brute-force limits (owner decisions D01-D12).

Layer 1 stays bridge/oauth_throttle.py (in memory, first filter, unchanged: D09). This module is
layer 2: the same limits (D01: consent failures 5/address/15 min + 50/h global; D03:
registration 10/address/h + 100/h global, OBSERVE ONLY) kept in `oauth_throttle_buckets`
(migration 0009), so a restart, a spin-down or a second worker no longer resets them.

Modes (MCP_OAUTH_THROTTLE_MODE, owner-controlled server setting; default OFF):
  off      today's behaviour: layer 1 only, this module never touches the database;
  shadow   the persistent check runs and its outcome is logged (fixed codes), never blocks;
  enforce  consent: a persistent deny is a 429 and an unavailable check is a uniform 503,
           both BEFORE the password is compared (D04). Registration stays observe-only (D03).
  any other value -> CONFIG_INVALID: new protected OAuth operations (consent POST, registration)
           are refused with the uniform 503 until corrected. Bearer /mcp calls and /oauth/token
           (refresh tokens) never pass through this module (D12).
Moving to enforce, and any move away from it, is an owner action (D10, see
docs/certification/areas/oauth_persistent_throttle.md); the mode is never set by code.

Keys (D07): HMAC-SHA256 of the normalised client address (IPv4 address; IPv6 /64; IPv4-mapped
IPv6 as IPv4) with a dedicated secret, MCP_OAUTH_THROTTLE_KEY = "<key id>:<secret>". During a
rotation MCP_OAUTH_THROTTLE_KEY_PREVIOUS holds the old one: its buckets are still CHECKED (never
written) so a rotation never silently resets a source. The plain address, the secret and the
key hash are never logged or returned.

Algorithm (D08): GCRA on the database clock (statement_timestamp()). Per scope, every writer
locks the scope's global row first (fixed lock order), then ONE conditional
INSERT ... ON CONFLICT DO UPDATE writes the per-key and global rows only when both conform.
A denied request writes nothing, so denied keys leave no row (bounded cardinality); decayed
rows are removed by a bounded cleanup (LIMIT, FOR UPDATE SKIP LOCKED). Every transaction runs
under lock_timeout and statement_timeout so a saturated database fails fast.

Connections (anti-DoS): the checks run on the OAuth store thread pool (bridge/oauth.py _store,
STORE_CONCURRENCY workers) and at most DB_CONCURRENCY of them at once per process; any excess is
"unavailable" at once without opening a connection. Like the other OAuth store calls, each check
(and each refund) opens one short-lived connection (_conn, no connection pool), bounded by
CONNECT_TIMEOUT_SECONDS plus the statement timeout, so the throttle adds at most DB_CONCURRENCY
database connections per process and always leaves STORE_CONCURRENCY - DB_CONCURRENCY workers. Layer 1 runs first, so the database sees at most the layer-1 budget
(<= 50 consent + 100 registration events per hour and process) no matter how many keys flood.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

from bridge import oauth_throttle

LOG = logging.getLogger(__name__)

MODE_ENV = "MCP_OAUTH_THROTTLE_MODE"
KEY_ENV = "MCP_OAUTH_THROTTLE_KEY"
PREVIOUS_KEY_ENV = "MCP_OAUTH_THROTTLE_KEY_PREVIOUS"
MODES = ("off", "shadow", "enforce")
CONFIG_INVALID = "CONFIG_INVALID"
KEY_ID_RE = re.compile(r"^[a-z0-9]{1,8}$")
MIN_SECRET_CHARS = 32

# D03: registration is persisted but observe-only. Enforcing it needs a separate owner GO and a
# code change (deliberately not an environment switch).
REGISTRATION_ENFORCE = False

# Anti-DoS bounds.
DB_CONCURRENCY = 2            # concurrent throttle DB calls per process (the OAuth pool has 4)
CONNECT_TIMEOUT_SECONDS = 2
STATEMENT_TIMEOUT_MS = 1500
LOCK_TIMEOUT_MS = 500
GC_BATCH = 64

# D06: new or rotated OAuth passwords: at least 20 random characters (documentation + warning).
PASSWORD_POLICY_MIN_CHARS = 20


@dataclass(frozen=True)
class Limit:
    scope: str
    per_key: int
    per_key_window: float
    global_: int
    global_window: float

    def params(self) -> tuple[float, float, float, float]:
        """GCRA emission interval and burst tolerance (seconds) for the key and global rows."""
        ke, ge = self.per_key_window / self.per_key, self.global_window / self.global_
        return ke, self.per_key_window - ke, ge, self.global_window - ge


# D01 / D03: the same limits as layer 1.
CONSENT = Limit("consent", 5, 900.0, 50, 3600.0)
REGISTRATION = Limit("registration", 10, 3600.0, 100, 3600.0)


# ------------------------------------------------------------------ configuration

def mode() -> str:
    """'off' | 'shadow' | 'enforce' | CONFIG_INVALID. Unset or empty means off (this PR's default)."""
    raw = os.environ.get(MODE_ENV)
    if raw is None or not raw.strip():
        return "off"
    value = raw.strip().lower()
    return value if value in MODES else CONFIG_INVALID


@dataclass(frozen=True)
class Keyring:
    state: str                       # OK | MISSING | INVALID
    current: tuple[str, bytes] | None = None
    previous: tuple[str, bytes] | None = None


def _parse_key(raw: str) -> tuple[str, bytes] | None:
    kid, sep, secret = raw.strip().partition(":")
    if not sep or not KEY_ID_RE.fullmatch(kid) or len(secret) < MIN_SECRET_CHARS:
        return None
    return kid, secret.encode("utf-8")


def keyring() -> Keyring:
    """Never logs or returns the secret outside this process. A malformed PREVIOUS key makes the
    whole keyring INVALID (fail-safe: a rotation can never silently disable protection)."""
    raw = os.environ.get(KEY_ENV, "")
    if not raw.strip():
        return Keyring("MISSING")
    current = _parse_key(raw)
    if current is None:
        return Keyring("INVALID")
    previous = None
    raw_prev = os.environ.get(PREVIOUS_KEY_ENV, "")
    if raw_prev.strip():
        previous = _parse_key(raw_prev)
        if previous is None or previous[0] == current[0] or previous[1] == current[1]:
            return Keyring("INVALID")
    return Keyring("OK", current, previous)


def status() -> dict[str, Any]:
    """Fixed codes for /healthz; no secret, no address, no key hash."""
    m, keys = mode(), keyring()
    if m == CONFIG_INVALID:
        ready, reg = False, CONFIG_INVALID
    elif m == "enforce":
        ready, reg = keys.state == "OK", ("enforce" if REGISTRATION_ENFORCE else "shadow")
    else:
        ready, reg = True, ("off" if m == "off" else "shadow")
    return {"oauth_throttle_mode": m, "oauth_throttle_registration_mode": reg,
            "oauth_throttle_key_state": keys.state,
            "oauth_throttle_rotation_active": keys.previous is not None,
            "oauth_new_authorizations_ready": ready}


def password_meets_policy() -> bool:
    """D06: only this boolean is ever logged, never the password or its length."""
    return len(os.environ.get("MCP_OAUTH_PASSWORD", "")) >= PASSWORD_POLICY_MIN_CHARS


# ------------------------------------------------------------------ addresses and keys

def normalise_address(raw: str) -> str:
    """IPv4 as is; IPv4-mapped IPv6 as IPv4; other IPv6 grouped by /64 (one subscriber's
    prefix); anything unparseable shares ONE bucket so junk values cannot mint keys."""
    try:
        ip = ipaddress.ip_address(raw.strip().split("%", 1)[0])
    except ValueError:
        return "unparsed"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address):
        return f"v4:{ip}"
    net = ipaddress.IPv6Network(f"{ip}/64", strict=False)
    return f"v6:{net.network_address}/64"


# Internal hops whose forwarding headers are honoured: RFC 1918, shared address space (RFC 6598),
# loopback and IPv6 ULA. Render's internal proxy reaches the app from such an address
# (DOCS_UNVERIFIED: to be confirmed by the shadow logs, field forwarded=trusted|ignored).
TRUSTED_PROXY_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
    "::1/128", "fc00::/7"))


def _peer_is_trusted_proxy(peer: str | None) -> bool:
    """Forwarding headers are honoured only from an internal hop (Render's proxy). A public
    peer reached the app directly: its headers are client-chosen, so its own address is used."""
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in TRUSTED_PROXY_NETWORKS)


def client_key_source(request) -> tuple[str, bool]:
    """(address, forwarded_trusted). Never consults the User-Agent or any other header."""
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None)
    if _peer_is_trusted_proxy(peer):
        return oauth_throttle.client_address(request), True
    return (peer or "unknown")[:64], False


def key_hash(kid: str, secret: bytes, address: str) -> str:
    digest = hmac.new(secret, normalise_address(address).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{kid}:{digest}"


# ------------------------------------------------------------------ storage (synchronous)

def _conn():
    import psycopg
    from app.db import database_url
    return psycopg.connect(database_url(), connect_timeout=CONNECT_TIMEOUT_SECONDS,
                           options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                                   f"-c lock_timeout={LOCK_TIMEOUT_MS}")


_LOCK_GLOBAL = (
    "INSERT INTO oauth_throttle_buckets (scope, key_hash, tat, updated_at) "
    "VALUES (%(scope)s, '*', statement_timestamp(), statement_timestamp()) "
    "ON CONFLICT (scope, key_hash) DO NOTHING",
    "SELECT 1 FROM oauth_throttle_buckets WHERE scope = %(scope)s AND key_hash = '*' FOR UPDATE",
)

# The single conditional write: both rows (and, during a rotation, the previous key's row,
# checked only) must conform, otherwise nothing is written.
_ACQUIRE = """
WITH ts AS (SELECT statement_timestamp() AS now),
v (key_hash, emission, tolerance, write) AS (
    SELECT * FROM (VALUES
        ('*'::text, make_interval(secs => %(ge)s), make_interval(secs => %(gt)s), true),
        (%(key)s::text, make_interval(secs => %(ke)s), make_interval(secs => %(kt)s), true),
        (%(prev)s::text, make_interval(secs => %(ke)s), make_interval(secs => %(kt)s), false)
    ) AS raw (key_hash, emission, tolerance, write) WHERE raw.key_hash IS NOT NULL),
cur AS (
    SELECT v.key_hash, v.emission, v.tolerance, v.write, ts.now,
           GREATEST(COALESCE(b.tat, ts.now), ts.now) AS base
    FROM v CROSS JOIN ts
    LEFT JOIN oauth_throttle_buckets b ON b.scope = %(scope)s AND b.key_hash = v.key_hash),
verdict AS (
    SELECT bool_and(base <= now + tolerance) AS ok,
           max(GREATEST(base - tolerance - now, interval '0')) AS wait FROM cur),
up AS (
    INSERT INTO oauth_throttle_buckets AS b (scope, key_hash, tat, updated_at)
    SELECT %(scope)s, cur.key_hash, cur.base + cur.emission, cur.now
    FROM cur CROSS JOIN verdict WHERE verdict.ok AND cur.write
    ORDER BY cur.key_hash
    ON CONFLICT (scope, key_hash) DO UPDATE SET tat = EXCLUDED.tat, updated_at = EXCLUDED.updated_at
    RETURNING 1)
SELECT verdict.ok, extract(epoch FROM verdict.wait)::float8 AS wait,
       (SELECT count(*) FROM up) AS written
FROM verdict
"""

_REFUND = """
UPDATE oauth_throttle_buckets AS b
SET tat = GREATEST(b.tat - v.emission, statement_timestamp()), updated_at = statement_timestamp()
FROM (VALUES ('*'::text, make_interval(secs => %(ge)s)),
             (%(key)s::text, make_interval(secs => %(ke)s))) AS v (key_hash, emission)
WHERE b.scope = %(scope)s AND b.key_hash = v.key_hash
"""

_GC = """
DELETE FROM oauth_throttle_buckets WHERE ctid = ANY(ARRAY(
    SELECT ctid FROM oauth_throttle_buckets
    WHERE key_hash <> '*' AND tat < statement_timestamp()
    ORDER BY tat LIMIT %(limit)s FOR UPDATE SKIP LOCKED))
"""


def _params(limit: Limit, key: str, prev: str | None) -> dict[str, Any]:
    ke, kt, ge, gt = limit.params()
    return {"scope": limit.scope, "key": key, "prev": prev, "ke": ke, "kt": kt, "ge": ge, "gt": gt}


def acquire(limit: Limit, key: str, prev: str | None = None, connect=None) -> tuple[bool, int]:
    """(allowed, retry_after_seconds). Raises on any storage problem (callers map it)."""
    connect = connect or _conn
    params = _params(limit, key, prev)
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in _LOCK_GLOBAL:          # fixed lock order: the global row first
                cur.execute(statement, params)
            cur.execute(_ACQUIRE, params)
            ok, wait, _written = cur.fetchone()
        conn.commit()
        try:
            with conn.cursor() as cur:
                cur.execute(_GC, {"limit": GC_BATCH})
            conn.commit()
        except Exception:
            conn.rollback()
            LOG.warning("oauth_throttle_gc_failed")
    retry = 0 if ok else max(1, int(-(-float(wait or 0) // 1)))
    return bool(ok), retry


def refund(limit: Limit, key: str, connect=None) -> None:
    """Give back one acquired event (consent with the right password: only failures count)."""
    connect = connect or _conn
    params = _params(limit, key, None)
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in _LOCK_GLOBAL:
                cur.execute(statement, params)
            cur.execute(_REFUND, params)
        conn.commit()


# ------------------------------------------------------------------ async gates

_inflight = 0
_inflight_lock = threading.Lock()


class ThrottleBusy(Exception):
    """More than DB_CONCURRENCY throttle calls in flight: no connection is opened."""


async def _db(fn, *args):
    global _inflight
    from bridge import oauth
    with _inflight_lock:
        if _inflight >= DB_CONCURRENCY:
            raise ThrottleBusy()
        _inflight += 1
    try:
        return await oauth._store(fn, *args)
    finally:
        with _inflight_lock:
            _inflight -= 1


@dataclass
class Decision:
    """action: allow | deny | refuse. `refuse` is the uniform 503 (config or storage)."""
    action: str
    retry_after: int = 0
    code: str = "OK"
    refund_key: str | None = None


ALLOW = Decision("allow")


def _log(limit: Limit, effective: str, outcome: str, code: str, forwarded: bool | None = None) -> None:
    # Fixed codes only: never the address, the key hash, the secret or exception text.
    if forwarded is None:
        LOG.info("oauth_throttle scope=%s mode=%s outcome=%s code=%s", limit.scope, effective, outcome, code)
    else:
        LOG.info("oauth_throttle scope=%s mode=%s outcome=%s code=%s forwarded=%s", limit.scope,
                 effective, outcome, code, "trusted" if forwarded else "ignored")


async def check(limit: Limit, request) -> Decision:
    """Run before the protected operation (for consent: BEFORE the password compare)."""
    m = mode()
    if m == "off":
        return ALLOW
    if m == CONFIG_INVALID:
        _log(limit, m, "refuse", CONFIG_INVALID)
        return Decision("refuse", code=CONFIG_INVALID)
    enforcing = m == "enforce" and (limit is not REGISTRATION or REGISTRATION_ENFORCE)
    keys = keyring()
    effective = "enforce" if enforcing else "shadow"
    if keys.state != "OK":
        code = f"KEY_{keys.state}"
        if m == "enforce":
            # Enforce without a usable secret: the protection cannot run, so new protected
            # operations are refused (D04/D07), registration included.
            _log(limit, effective, "refuse", code)
            return Decision("refuse", code=code)
        _log(limit, effective, "skipped", code)
        return ALLOW
    address, forwarded = client_key_source(request)
    kid, secret = keys.current
    key = key_hash(kid, secret, address)
    prev = key_hash(*keys.previous, address) if keys.previous else None
    try:
        allowed, retry = await _db(acquire, limit, key, prev)
    except ThrottleBusy:
        _log(limit, effective, "refuse" if enforcing else "unavailable", "STORE_BUSY", forwarded)
        return Decision("refuse", code="STORE_BUSY") if enforcing else ALLOW
    except Exception:
        _log(limit, effective, "refuse" if enforcing else "unavailable", "STORE_UNAVAILABLE", forwarded)
        return Decision("refuse", code="STORE_UNAVAILABLE") if enforcing else ALLOW
    if allowed:
        _log(limit, effective, "allow", "OK", forwarded)
        return Decision("allow", refund_key=key)
    _log(limit, effective, "deny" if enforcing else "would_deny", "LIMITED", forwarded)
    return Decision("deny", retry_after=retry, code="LIMITED") if enforcing else ALLOW


async def refund_after_success(limit: Limit, decision: Decision) -> None:
    """Best effort: a failed refund only leaves one extra counted event."""
    if decision.refund_key is None:
        return
    try:
        await _db(refund, limit, decision.refund_key)
    except Exception:
        LOG.warning("oauth_throttle scope=%s refund_failed", limit.scope)
