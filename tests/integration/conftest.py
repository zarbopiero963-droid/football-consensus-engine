"""Explicit schema provisioning for isolated PostgreSQL integration tests.

Production runtime never executes DDL. The CI database is disposable, so tests
install the coordinator migration contracts here before exercising runtime code.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def provision_coordinator_schema():
    if not os.environ.get("TEST_DATABASE_URL"):
        yield
        return

    import psycopg
    from bridge.api_football.quota import _SCHEMA as api_football_schema
    from bridge.sxbet.ratelimit import _SCHEMA as sxbet_schema

    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        for statement in (*api_football_schema, *sxbet_schema):
            conn.execute(statement)
        conn.commit()
    yield
