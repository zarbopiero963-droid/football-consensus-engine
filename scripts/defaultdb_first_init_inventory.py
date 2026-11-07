#!/usr/bin/env python3
"""Sanitized, read-only inventory for the WP-005 first initialization gate.

The report contains catalog metadata and row counts only.  It never selects table
contents and never prints the DSN, role name, host, token, or other credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
AIVEN = ROOT / "sql" / "aiven"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_CREATE_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def _expected_bridge_tables() -> set[str]:
    tables: set[str] = set()
    for path in sorted((ROOT / "sql" / "migrations" / "bridge").glob("*.sql")):
        tables.update(m.group(1).lower() for m in _CREATE_TABLE.finditer(path.read_text(encoding="utf-8")))
    tables.add("schema_migrations")
    return tables


def _scalar(conn: psycopg.Connection[Any], query: str, params: tuple[Any, ...] = ()) -> Any:
    # Passing even an empty tuple makes psycopg parse every '%' in a raw SQL
    # file as a client-side placeholder.  The audited precheck legitimately
    # contains LIKE patterns, so omit the params argument when there are none.
    cursor = conn.execute(query, params) if params else conn.execute(query)
    return cursor.fetchone()[0]


def _precheck_state(conn: psycopg.Connection[Any]) -> str:
    state = _scalar(conn, (AIVEN / "defaultdb_precheck.sql").read_text(encoding="utf-8"))
    if state == "TRACKED":
        state = _scalar(conn, (AIVEN / "defaultdb_precheck_tracked.sql").read_text(encoding="utf-8"))
    return str(state)


def _count_or_unknown(
    conn: psycopg.Connection[Any], schema: str, table: str
) -> int | str:
    """Count one table while preserving the outer read-only transaction on ACL errors."""
    conn.execute("SAVEPOINT inventory_count")
    try:
        count = int(_scalar(
            conn,
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            ),
        ))
    except psycopg.errors.InsufficientPrivilege:
        conn.execute("ROLLBACK TO SAVEPOINT inventory_count")
        conn.execute("RELEASE SAVEPOINT inventory_count")
        return "UNKNOWN_INSUFFICIENT_PRIVILEGE"
    conn.execute("RELEASE SAVEPOINT inventory_count")
    return count


def collect_inventory(dsn: str, *, schema: str = "public", source_sha: str = "UNKNOWN") -> dict[str, Any]:
    if not _IDENT.fullmatch(schema):
        raise ValueError("invalid schema name")

    # autocommit is used only so BEGIN READ ONLY is the first transaction command.
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Keep pg_catalog implicit: PostgreSQL prepends it to current_schemas(true).
        # Listing it explicitly after the target schema would make the target resolve first
        # and the existing fail-closed precheck would (correctly) reject the session.
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute("BEGIN TRANSACTION READ ONLY")
        try:
            if _scalar(conn, "SELECT current_setting('transaction_read_only')") != "on":
                raise RuntimeError("read-only transaction was not enforced")

            database = str(_scalar(conn, "SELECT current_database()"))
            version_num = int(_scalar(conn, "SELECT current_setting('server_version_num')"))
            database_size = int(_scalar(conn, "SELECT pg_database_size(current_database())"))
            connections = int(_scalar(
                conn,
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()",
            ))
            state = _precheck_state(conn)

            schemas = [r[0] for r in conn.execute(
                """SELECT nspname FROM pg_namespace
                   WHERE nspname NOT LIKE 'pg_temp%' AND nspname NOT LIKE 'pg_toast_temp%'
                   ORDER BY nspname"""
            ).fetchall()]
            expected_schemas = {
                schema,
                "public",
                "pg_catalog",
                "information_schema",
                "pg_toast",
                "_avnadmin_monitor",
            }
            unexpected_schemas = [name for name in schemas if name not in expected_schemas]

            relations = conn.execute(
                """SELECT c.relname, c.relkind
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s
                     AND c.relkind IN ('r','p','v','m','S','f')
                   ORDER BY c.relkind, c.relname""",
                (schema,),
            ).fetchall()
            relation_kinds = {
                "r": "table", "p": "partitioned_table", "v": "view",
                "m": "materialized_view", "S": "sequence", "f": "foreign_table",
            }
            objects = [
                {"name": str(name), "kind": relation_kinds[str(kind)]}
                for name, kind in relations
            ]
            table_names = [str(name) for name, kind in relations if kind in ("r", "p", "f")]
            row_counts: dict[str, int | str] = {}
            for table in table_names:
                row_counts[table] = _count_or_unknown(conn, schema, table)

            functions = [r[0] for r in conn.execute(
                """SELECT p.proname FROM pg_proc p
                   JOIN pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = %s ORDER BY p.proname""",
                (schema,),
            ).fetchall()]
            triggers = [r[0] for r in conn.execute(
                """SELECT t.tgname FROM pg_trigger t
                   JOIN pg_class c ON c.oid = t.tgrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND NOT t.tgisinternal ORDER BY t.tgname""",
                (schema,),
            ).fetchall()]
            indexes = [r[0] for r in conn.execute(
                """SELECT c.relname FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND c.relkind IN ('i','I') ORDER BY c.relname""",
                (schema,),
            ).fetchall()]

            migration_rows: list[dict[str, Any]] | str
            if "schema_migrations" in table_names:
                conn.execute("SAVEPOINT inventory_migrations")
                try:
                    migration_rows = [
                        {"version": int(v), "name": str(n), "sha256": str(h)}
                        for v, n, h in conn.execute(
                            sql.SQL("SELECT version, name, sha256 FROM {}.schema_migrations ORDER BY version").format(
                                sql.Identifier(schema)
                            )
                        ).fetchall()
                    ]
                except psycopg.errors.InsufficientPrivilege:
                    conn.execute("ROLLBACK TO SAVEPOINT inventory_migrations")
                    migration_rows = "UNKNOWN_INSUFFICIENT_PRIVILEGE"
                conn.execute("RELEASE SAVEPOINT inventory_migrations")
            else:
                migration_rows = []

            expected = _expected_bridge_tables()
            unexpected_objects = sorted(
                obj["name"] for obj in objects
                if obj["kind"] in {"table", "partitioned_table", "foreign_table"}
                and obj["name"] not in expected
            )
            unknown_counts = sorted(name for name, count in row_counts.items() if isinstance(count, str))
            total_rows = sum(count for count in row_counts.values() if isinstance(count, int))
            empty_proven = (
                state == "EMPTY"
                and not objects
                and not functions
                and not triggers
                and not indexes
                and not unexpected_schemas
                and not unknown_counts
                and total_rows == 0
            )

            report = {
                "report": "FCB_WP005_FIRST_INIT_INVENTORY_V1",
                "source_sha": source_sha,
                "database": database,
                "server_version_num": version_num,
                "transaction_read_only": True,
                "schema": schema,
                "precheck_state": state,
                "database_size_bytes": database_size,
                "database_connections": connections,
                "schemas": schemas,
                "unexpected_schemas": unexpected_schemas,
                "objects": objects,
                "functions": functions,
                "triggers": triggers,
                "indexes": indexes,
                "migration_rows": migration_rows,
                "row_counts": row_counts,
                "unknown_row_counts": unknown_counts,
                "unexpected_objects": unexpected_objects,
                "application_rows_total": total_rows,
                "first_time_initialization_proven": empty_proven,
            }
            conn.execute("ROLLBACK")
            return report
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="public")
    args = parser.parse_args()
    dsn = os.environ.get("FCB_DEFAULTDB_URL", "")
    if not dsn:
        raise SystemExit("FCB_DEFAULTDB_URL is required")
    report = collect_inventory(dsn, schema=args.schema, source_sha=os.environ.get("GITHUB_SHA", "UNKNOWN"))
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
