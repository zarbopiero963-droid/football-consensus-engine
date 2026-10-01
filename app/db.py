from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
from app.migrations import apply_migrations


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def connect():
    return psycopg.connect(database_url(), row_factory=dict_row)


def migrate() -> None:
    apply_migrations(connect)
