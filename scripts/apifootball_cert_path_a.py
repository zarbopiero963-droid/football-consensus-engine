"""PATH A (owner mandate FCB-OWNER-APIFOOTBALL-PATH-A-20261112): the API-FOOTBALL certification
ledger in a DEDICATED database `fcb_cert` on the same Aiven service, without WP-009.

* No ownership change of any existing object, no change to the Render DATABASE_URL, no new service.
* defaultdb (bridge 0001-0008, owned by the administrative identity) only gains two roles and the
  runner's minimal grant on the three shared quota-coordinator tables (CLAUDE.md §2.4).
* fcb_cert is OWNED by the NOLOGIN role fcb_cert_owner, which also owns every ledger object and the
  SECURITY DEFINER budget trigger. The ledger DDL is bridge migration 0010 byte-for-byte (pinned).
* The runner logs in as fcb_cert_runner (SCRAM), to defaultdb for the coordinator and to fcb_cert
  for the ledger: SELECT + column INSERT on the ledger, nothing on the counter, the plans (read-only),
  OAuth, SX Bet or any other table.
* The administrator (avnadmin) keeps only PostgreSQL's automatic ADMIN-only membership in the two
  roles it creates; every step that must act AS fcb_cert_owner grants SET for one transaction and
  revokes it before COMMIT (asserted). CREATE DATABASE cannot run in a transaction: that single step
  revokes in `finally` and asserts the end state.

Nothing here prints a password, a verifier, a DSN or a host. Usage (owner-gated workflows only):
  install   ADMIN_DSN_VAR RUNNER_PASSWORD_VAR   steps 1-3 (idempotent)
  approve   ADMIN_DSN_VAR SHA KIND BUDGET ACTOR RUN_ID
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AIVEN = ROOT / "sql" / "aiven"
COORDINATOR_DATABASE = "defaultdb"
LEDGER_DATABASE = "fcb_cert"
OWNER_ROLE = "fcb_cert_owner"
RUNNER_ROLE = "fcb_cert_runner"
LEDGER_MIGRATION = ROOT / "sql" / "migrations" / "bridge" / "0010_apifootball_cert_campaign_ledger.sql"
# The ledger DDL that is installed: bridge 0010, unchanged (tests/test_apifootball_cert_path_a.py).
LEDGER_MIGRATION_SHA256 = "64fc8345f9ae6ec5a601acd4f2240865bfe35f1af60d47528993c8d511dc2ea8"


def _sql(name: str) -> str:
    return (AIVEN / name).read_text(encoding="utf-8")


def ledger_migration_block() -> str:
    """The 0010 DDL as one DO block, tracked in fcb_cert.public.apifootball_cert_schema_migrations."""
    from psycopg.sql import Literal
    raw = LEDGER_MIGRATION.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != LEDGER_MIGRATION_SHA256:
        raise ValueError("LEDGER_MIGRATION_SHA256_MISMATCH")
    name = Literal(LEDGER_MIGRATION.name).as_string()
    body = Literal(raw.decode("utf-8")).as_string()
    block = f'''
    DECLARE applied public.apifootball_cert_schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM public.apifootball_cert_schema_migrations WHERE version = 10;
        IF FOUND THEN
            IF applied.name <> {name} OR applied.sha256 <> '{checksum}' THEN
                RAISE EXCEPTION 'FCB_CERT_PATH_A_LEDGER_DRIFT';
            END IF;
        ELSE
            EXECUTE {body};
            INSERT INTO public.apifootball_cert_schema_migrations (version, name, sha256) VALUES (10, {name}, '{checksum}');
        END IF;
    END;'''
    return "DO " + Literal(block).as_string()


def install_roles(connect_defaultdb, runner_verifier: str) -> None:
    """Step 1, one transaction in defaultdb."""
    with connect_defaultdb() as conn:
        conn.execute("SELECT pg_catalog.set_config('fcb_cert.runner_verifier', %s, true)", (runner_verifier,))
        conn.execute(_sql("apifootball_cert_path_a_roles.sql"))


def create_database(connect_defaultdb_autocommit) -> str:
    """Step 2: CREATE DATABASE fcb_cert OWNER fcb_cert_owner (outside a transaction, as PostgreSQL
    requires). Returns CREATED or ALREADY_PRESENT; refuses a database with another owner."""
    with connect_defaultdb_autocommit() as conn:
        conn.autocommit = True
        row = conn.execute("SELECT o.rolname FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles o "
                           "ON o.oid OPERATOR(pg_catalog.=) d.datdba WHERE d.datname OPERATOR(pg_catalog.=) %s",
                           (LEDGER_DATABASE,)).fetchone()
        if row is not None:
            if (row["rolname"] if isinstance(row, dict) else row[0]) != OWNER_ROLE:
                raise RuntimeError("FCB_CERT_PATH_A_DATABASE_OWNER_UNEXPECTED")
            state = "ALREADY_PRESENT"
        else:
            conn.execute(f"GRANT {OWNER_ROLE} TO CURRENT_USER WITH INHERIT FALSE, SET TRUE")
            try:
                conn.execute(f"CREATE DATABASE {LEDGER_DATABASE} OWNER {OWNER_ROLE} TEMPLATE template0 ENCODING 'UTF8'")
            finally:
                conn.execute(f"REVOKE {OWNER_ROLE} FROM CURRENT_USER")
            state = "CREATED"
        ok = conn.execute("SELECT NOT pg_catalog.pg_has_role(SESSION_USER, %s, 'SET') "
                          "AND NOT pg_catalog.pg_has_role(SESSION_USER, %s, 'USAGE') AS ok",
                          (OWNER_ROLE, OWNER_ROLE)).fetchone()
        if not (ok["ok"] if isinstance(ok, dict) else ok[0]):
            raise RuntimeError("FCB_CERT_PATH_A_OWNER_MEMBERSHIP_UNEXPECTED")
        return state


def install_ledger(connect_ledger) -> None:
    """Step 3, one transaction in fcb_cert: 0010 as fcb_cert_owner, runner grants, assertions."""
    with connect_ledger() as conn:
        conn.execute(_sql("apifootball_cert_path_a_ledger_pre.sql"))
        conn.execute(ledger_migration_block())
        conn.execute(_sql("apifootball_cert_path_a_ledger_post.sql"))


def approve_plan(connect_ledger, sha: str, kind: str, budget: int, actor: str, run_id: str) -> None:
    with connect_ledger() as conn:
        for key, value in (("plan_sha256", sha), ("plan_kind", kind), ("budget", str(budget)),
                           ("approved_by", actor), ("run_id", str(run_id))):
            conn.execute("SELECT pg_catalog.set_config(%s, %s, true)", ("fcb_cert." + key, value))
        conn.execute(_sql("apifootball_cert_path_a_plan_approve.sql"))


def main(argv: list[str]) -> int:
    import psycopg
    from psycopg.rows import dict_row
    if len(argv) >= 3 and argv[0] == "install":
        dsn, password = os.environ.get(argv[1], ""), os.environ.get(argv[2], "")
        if not dsn or len(password) < 32:
            print("FCB_CERT_PATH_A_INPUT_MISSING")
            return 2
        sys.path.insert(0, str(ROOT / "scripts"))
        from fcb_db_roles import scram_verifier
        install_roles(lambda: psycopg.connect(dsn, dbname=COORDINATOR_DATABASE, row_factory=dict_row),
                      scram_verifier(password))
        state = create_database(lambda: psycopg.connect(dsn, dbname=COORDINATOR_DATABASE, row_factory=dict_row,
                                                        autocommit=True))
        install_ledger(lambda: psycopg.connect(dsn, dbname=LEDGER_DATABASE, row_factory=dict_row))
        print(f"FCB_CERT_PATH_A_INSTALLED database={state}")
        return 0
    if len(argv) == 7 and argv[0] == "approve":
        dsn = os.environ.get(argv[1], "")
        approve_plan(lambda: psycopg.connect(dsn, dbname=LEDGER_DATABASE, row_factory=dict_row),
                     argv[2], argv[3], int(argv[4]), argv[5], argv[6])
        print("FCB_CERT_PATH_A_PLAN_APPROVED")
        return 0
    print("usage: install ADMIN_DSN_VAR RUNNER_PASSWORD_VAR | approve ADMIN_DSN_VAR SHA KIND BUDGET ACTOR RUN_ID")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
