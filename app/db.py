from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
from app.migrations import apply_migrations


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    if os.environ.get("FCB_DATABASE_TLS_VERIFY_FULL", "").lower() in {"1", "true", "yes", "on"}:
        try:
            parsed = urlsplit(value)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError as exc:
            raise RuntimeError("DATABASE_TLS_DSN_INVALID") from exc
        if query.get("sslmode") != ["verify-full"]:
            raise RuntimeError("DATABASE_TLS_VERIFY_FULL_REQUIRED")
        root_cert = os.environ.get("PGSSLROOTCERT", "")
        if not root_cert or not Path(root_cert).is_file():
            raise RuntimeError("DATABASE_TLS_CA_UNAVAILABLE")
    return value


def connect():
    return psycopg.connect(database_url(), row_factory=dict_row)


def migrate() -> None:
    apply_migrations(connect)
