"""External anchoring of the order-ledger head + single-writer lease with fencing (governance §2.6).

Why: a hash chain cannot detect truncation of its own TAIL, or the replacement of the whole file with a
shorter valid one, by someone with write access to the signer host. Anchoring the head (seq, hash)
outside the host turns both into a refusal to start. The same store replaces the local `flock` with a
writer lease that works across hosts and filesystems.

`AnchorStore` (Protocol) is optional: without it the ledger keeps its local-only mode (flock). With it:
- writer lease: `acquire_writer(ledger_id)` succeeds for one writer at a time and returns a strictly
  increasing fencing token; every anchored head carries the token of the writer that wrote it, and an
  advance with a token that is not the current one is refused (AnchorFenced). A stale writer, whose
  session died and whose lease was taken by another process, therefore cannot advance the head.
- head: append-only history of (seq, head_hash, fencing_token); seq strictly increases.
- advance is a compare-and-set on the current seq (AnchorConflict otherwise).
Any I/O problem is AnchorUnavailable: the ledger fails closed (refuses to start / poisons itself).

PostgresAnchorStore (tables from sql/migrations/0008_signer_ledger_anchor.sql):
- lease: a dedicated session holds `pg_try_advisory_lock(<key of ledger_id>)` for the ledger's lifetime;
  under it the lease row's fencing_token is incremented. If that session dies the advisory lock is
  released by PostgreSQL, another writer gets token + 1, and the old writer is fenced at its next advance.
- advance: a short transaction (lock_timeout / statement_timeout) that locks the lease row FOR UPDATE,
  checks token and expected seq, and inserts the new head. The database itself also refuses UPDATE,
  DELETE, TRUNCATE, a non-increasing seq and a token other than the lease's (triggers in 0008).
The connection factory is injected (`connect`): this module imports no database driver and no network
library; the signer process passes e.g. `psycopg.connect` bound to its own DSN.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol

_LEDGER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class AnchorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AnchorUnavailable(AnchorError):
    """The store could not be reached or did not answer definitively."""


class AnchorLocked(AnchorError):
    """Another writer holds the lease for this ledger."""


class AnchorFenced(AnchorError):
    """The caller's fencing token is not the current one: a newer writer exists."""


class AnchorConflict(AnchorError):
    """The anchored head is not at the seq the caller expected."""


@dataclass(frozen=True)
class AnchorHead:
    seq: int
    head_hash: str
    fencing_token: int


class WriterLease(Protocol):
    token: int

    def release(self) -> None: ...


class AnchorStore(Protocol):
    def acquire_writer(self, ledger_id: str) -> WriterLease: ...

    def head(self, ledger_id: str) -> AnchorHead | None: ...

    def advance(self, ledger_id: str, *, token: int, expected_seq: int, seq: int, head_hash: str) -> None: ...


def lock_key(ledger_id: str) -> int:
    """Signed 64-bit advisory-lock key derived from the ledger id (namespaced)."""
    digest = hashlib.sha256(("fce.signer.ledger.v1:" + ledger_id).encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _check_id(ledger_id: Any) -> str:
    if not isinstance(ledger_id, str) or not _LEDGER_ID.fullmatch(ledger_id):
        raise AnchorUnavailable("ANCHOR_LEDGER_ID_INVALID")
    return ledger_id


def _col(row: Any, i: int, name: str) -> Any:
    return row[name] if isinstance(row, dict) else row[i]


class _PgLease:
    def __init__(self, conn: Any, key: int, token: int):
        self._conn, self._key, self.token = conn, key, token

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
        except Exception:
            pass  # closing the session releases the lock anyway
        finally:
            try:
                conn.close()
            except Exception:
                pass


class PostgresAnchorStore:
    def __init__(self, connect: Callable[[], Any], *, holder: str = "signer", timeout_ms: int = 2500):
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30_000:
            raise ValueError("ANCHOR_TIMEOUT_INVALID")
        self._connect = connect
        self._holder = holder[:200]
        self._timeout_ms = timeout_ms

    def _timeouts(self, cur: Any, local: bool) -> None:
        scope = "LOCAL " if local else ""
        cur.execute(f"SET {scope}statement_timeout = {self._timeout_ms}")
        cur.execute(f"SET {scope}lock_timeout = {self._timeout_ms}")

    def acquire_writer(self, ledger_id: str) -> _PgLease:
        ledger_id = _check_id(ledger_id)
        key = lock_key(ledger_id)
        try:
            conn = self._connect()
        except Exception:
            raise AnchorUnavailable("ANCHOR_UNREACHABLE") from None
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                self._timeouts(cur, local=False)
                cur.execute("SELECT set_config('application_name', %s, false)",
                            ("fce-signer-lease:" + ledger_id[:40],))
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                got = _col(cur.fetchone(), 0, "pg_try_advisory_lock")
                if got is not True:
                    raise AnchorLocked("ANCHOR_WRITER_LOCKED")
                cur.execute(
                    "INSERT INTO signer_ledger_writer_leases (ledger_id, fencing_token, holder) VALUES (%s, 1, %s) "
                    "ON CONFLICT (ledger_id) DO UPDATE SET fencing_token = signer_ledger_writer_leases.fencing_token"
                    " + 1, holder = EXCLUDED.holder, acquired_at = clock_timestamp() RETURNING fencing_token",
                    (ledger_id, self._holder))
                token = int(_col(cur.fetchone(), 0, "fencing_token"))
            return _PgLease(conn, key, token)
        except AnchorError:
            conn.close()
            raise
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise AnchorUnavailable("ANCHOR_UNREACHABLE") from None

    def head(self, ledger_id: str) -> AnchorHead | None:
        ledger_id = _check_id(ledger_id)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                self._timeouts(cur, local=True)
                cur.execute("SELECT seq, head_hash, fencing_token FROM signer_ledger_anchors WHERE ledger_id = %s "
                            "ORDER BY seq DESC LIMIT 1", (ledger_id,))
                row = cur.fetchone()
        except Exception:
            raise AnchorUnavailable("ANCHOR_UNREACHABLE") from None
        if row is None:
            return None
        return AnchorHead(int(_col(row, 0, "seq")), str(_col(row, 1, "head_hash")),
                          int(_col(row, 2, "fencing_token")))

    def advance(self, ledger_id: str, *, token: int, expected_seq: int, seq: int, head_hash: str) -> None:
        ledger_id = _check_id(ledger_id)
        if not (type(token) is int and type(expected_seq) is int and type(seq) is int and seq > expected_seq >= 0
                and isinstance(head_hash, str) and _HASH.fullmatch(head_hash)):
            raise AnchorConflict("ANCHOR_ADVANCE_INVALID")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                self._timeouts(cur, local=True)
                cur.execute("SELECT fencing_token FROM signer_ledger_writer_leases WHERE ledger_id = %s FOR UPDATE",
                            (ledger_id,))
                row = cur.fetchone()
                if row is None or int(_col(row, 0, "fencing_token")) != token:
                    raise AnchorFenced("ANCHOR_FENCED")
                cur.execute("SELECT seq FROM signer_ledger_anchors WHERE ledger_id = %s ORDER BY seq DESC LIMIT 1",
                            (ledger_id,))
                row = cur.fetchone()
                current = 0 if row is None else int(_col(row, 0, "seq"))
                if current != expected_seq:
                    raise AnchorConflict("ANCHOR_SEQ_CONFLICT")
                cur.execute("INSERT INTO signer_ledger_anchors (ledger_id, seq, head_hash, fencing_token) "
                            "VALUES (%s, %s, %s, %s)", (ledger_id, seq, head_hash, token))
                conn.commit()
        except AnchorError:
            raise
        except Exception:
            raise AnchorUnavailable("ANCHOR_UNREACHABLE") from None
