"""FCB-WP-009 (R2): the PostgreSQL identity the runtime actually uses, for authenticated evidence.

Returned by the allowlisted, read-only MCP tool `apifootball_health` so the protected E2E workflow
can prove, through an authenticated MCP call, that the deployed runtime connects as `fcb_app`
before phase 2 of the roles cutover. Only the two ROLE NAMES are returned (current_user and
session_user), validated against a strict pattern; never the host, port, database, DSN, user
password or any error message. Bounded: 3 s connect timeout, 3 s overall.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable

ROLE_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
TIMEOUT_SECONDS = 3.0


def _default_connect():
    import psycopg
    from app.db import database_url
    return psycopg.connect(database_url(), connect_timeout=3)


def database_identity(connect: Callable | None = None) -> dict[str, Any]:
    """{"status": "OK", "current_user": ..., "session_user": ...} or a fixed status."""
    if connect is None:
        if not os.environ.get("DATABASE_URL", "").strip():
            return {"status": "NOT_CONFIGURED"}
        connect = _default_connect
    try:
        with connect() as conn:
            row = conn.execute("SELECT CURRENT_USER::pg_catalog.text, SESSION_USER::pg_catalog.text").fetchone()
    except Exception:  # never the message: it could carry connection details
        return {"status": "UNAVAILABLE"}
    values = list(row.values()) if isinstance(row, dict) else list(row or ())
    if len(values) != 2 or not all(isinstance(v, str) and ROLE_RE.fullmatch(v) for v in values):
        return {"status": "UNEXPECTED"}
    return {"status": "OK", "current_user": values[0], "session_user": values[1]}


async def database_identity_async(connect: Callable | None = None) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(database_identity, connect), timeout=TIMEOUT_SECONDS)
    except Exception:
        return {"status": "UNAVAILABLE"}
