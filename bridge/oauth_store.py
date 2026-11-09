from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.db import database_url


# WP-007: bounded storage calls. Note: the `options` keyword replaces any `options=` inside
# DATABASE_URL (psycopg gives keywords precedence); the Aiven URI carries none today. A slow or unreachable database must surface as a fast,
# fixed 503 at the endpoint, never as a request hung for libpq's default (~130 s).
CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000


def _conn():
    return psycopg.connect(database_url(), row_factory=dict_row,
                           connect_timeout=CONNECT_TIMEOUT_SECONDS,
                           options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


def now():
    return datetime.now(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_code(code: str, *, client_id: str, redirect_uri: str, code_challenge: str,
              resource: str, scope: str, expires_minutes: int = 5) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO oauth_authorization_codes
               (code_hash,client_id,redirect_uri,code_challenge,resource,scope,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (digest(code), client_id, redirect_uri, code_challenge, resource, scope,
             now() + timedelta(minutes=expires_minutes)),
        )
        conn.commit()


def consume_code(code: str) -> dict[str, Any] | None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE oauth_authorization_codes
               SET used_at=NOW()
               WHERE code_hash=%s AND used_at IS NULL AND expires_at>NOW()
               RETURNING *""",
            (digest(code),),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def save_access_token(token: str, *, client_id: str, resource: str, scope: str,
                      expires_minutes: int = 60) -> datetime:
    exp = now() + timedelta(minutes=expires_minutes)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO oauth_access_tokens
               (token_hash,client_id,resource,scope,expires_at)
               VALUES (%s,%s,%s,%s,%s)""",
            (digest(token), client_id, resource, scope, exp),
        )
        conn.commit()
    return exp


def save_refresh_token(token: str, *, client_id: str, resource: str, scope: str,
                       expires_days: int = 30) -> datetime:
    exp = now() + timedelta(days=expires_days)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO oauth_refresh_tokens
               (token_hash,client_id,resource,scope,expires_at)
               VALUES (%s,%s,%s,%s,%s)""",
            (digest(token), client_id, resource, scope, exp),
        )
        conn.commit()
    return exp


def _insert_pair(cur, *, access: str, refresh: str, client_id: str, resource: str, scope: str,
                 access_minutes: int, refresh_days: int) -> None:
    cur.execute(
        """INSERT INTO oauth_access_tokens (token_hash,client_id,resource,scope,expires_at)
           VALUES (%s,%s,%s,%s,%s)""",
        (digest(access), client_id, resource, scope, now() + timedelta(minutes=access_minutes)),
    )
    cur.execute(
        """INSERT INTO oauth_refresh_tokens (token_hash,client_id,resource,scope,expires_at)
           VALUES (%s,%s,%s,%s,%s)""",
        (digest(refresh), client_id, resource, scope, now() + timedelta(days=refresh_days)),
    )


def exchange_code(code: str, *, client_id: str, redirect_uri: str, verifier_matches,
                  access: str, refresh: str, access_minutes: int = 60,
                  refresh_days: int = 30) -> dict[str, Any] | None:
    """Consume an authorization code and issue the token pair in ONE transaction (WP-007).

    The presented code is burned even when its binding fails (single use): the consumption is
    committed and None is returned. On success both tokens are stored with the consumption, so
    a storage failure part-way can no longer leave a burned code with an orphan access token.
    `verifier_matches(code_challenge) -> bool` performs the PKCE check in constant time.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE oauth_authorization_codes
               SET used_at=NOW()
               WHERE code_hash=%s AND used_at IS NULL AND expires_at>NOW()
               RETURNING *""",
            (digest(code),),
        )
        row = cur.fetchone()
        if (not row or row["client_id"] != client_id or row["redirect_uri"] != redirect_uri
                or not verifier_matches(row["code_challenge"])):
            conn.commit()
            return None
        _insert_pair(cur, access=access, refresh=refresh, client_id=client_id,
                     resource=row["resource"], scope=row["scope"],
                     access_minutes=access_minutes, refresh_days=refresh_days)
        conn.commit()
        return {"resource": row["resource"], "scope": row["scope"]}


def refresh_and_issue(token: str, *, client_id: str, access: str, refresh: str,
                      access_minutes: int = 60, refresh_days: int = 30) -> dict[str, Any] | None:
    """Rotate a refresh token and issue the new pair in ONE transaction (WP-007).

    Only the client the token was issued to can rotate it (a wrong client_id no longer burns
    someone else's token). On any storage failure nothing is committed: the old refresh token
    stays valid, so the client is never locked out by a half-done rotation.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE oauth_refresh_tokens
               SET revoked_at=NOW()
               WHERE token_hash=%s AND client_id=%s AND revoked_at IS NULL AND expires_at>NOW()
               RETURNING *""",
            (digest(token), client_id),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        _insert_pair(cur, access=access, refresh=refresh, client_id=client_id,
                     resource=row["resource"], scope=row["scope"],
                     access_minutes=access_minutes, refresh_days=refresh_days)
        conn.commit()
        return {"resource": row["resource"], "scope": row["scope"]}


def valid_access_token(token: str, resource: str, required_scope: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT scope FROM oauth_access_tokens
               WHERE token_hash=%s AND revoked_at IS NULL AND expires_at>NOW()
                 AND resource=%s""",
            (digest(token), resource),
        )
        row = cur.fetchone()
    if not row:
        return False
    scopes = set((row["scope"] or "").split())
    return required_scope in scopes


def rotate_refresh_token(token: str) -> dict[str, Any] | None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE oauth_refresh_tokens
               SET revoked_at=NOW()
               WHERE token_hash=%s AND revoked_at IS NULL AND expires_at>NOW()
               RETURNING *""",
            (digest(token),),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


# RFC 7591 DCR clients are PUBLIC client identifiers, not bearer credentials.
# Store only a hash of each opaque ID in the existing OAuth PostgreSQL backend.
_DCR_PREFIX = "fcbdcr1_"
_DCR_ID_PATTERN = re.compile(r"^fcbdcr1_[A-Za-z0-9_-]{32}$")
_DCR_TTL_DAYS = 730


def register_claude_dcr_client(redirect_uri: str) -> str:
    """Register an opaque public client, restricted at authorization to Claude's callback.

    The table is provisioned only by the bridge migration series. Runtime never
    performs DDL, so the application identity can remain least-privileged.
    """
    client_id = _DCR_PREFIX + secrets.token_urlsafe(24)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_claude_dcr_clients WHERE expires_at <= NOW()"
        )
        cur.execute(
            """INSERT INTO oauth_claude_dcr_clients
               (client_id_hash, redirect_uri, expires_at)
               VALUES (%s, %s, NOW() + INTERVAL '730 days')""",
            (digest(client_id), redirect_uri),
        )
        conn.commit()
    return client_id


def valid_claude_dcr_client(client_id: str, redirect_uri: str) -> bool:
    if not _DCR_ID_PATTERN.fullmatch(client_id):
        return False
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM oauth_claude_dcr_clients
               WHERE client_id_hash=%s AND redirect_uri=%s
                 AND expires_at > NOW()""",
            (digest(client_id), redirect_uri),
        )
        return cur.fetchone() is not None
