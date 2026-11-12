"""PATH A, offline: the installer's SQL and the runner's defaults (the real-PostgreSQL proof is
tests/integration/test_apifootball_cert_path_a_postgres.py)."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from apifootball_campaign_kit import load  # noqa: E402

path_a = load("apifootball_cert_path_a_offline", "apifootball_cert_path_a.py")
batch = load("apifootball_cert_batch1_path_a_offline", "apifootball_cert_batch1.py")
AIVEN = ROOT / "sql" / "aiven"
FILES = ("apifootball_cert_path_a_roles.sql", "apifootball_cert_path_a_ledger_pre.sql",
         "apifootball_cert_path_a_ledger_post.sql", "apifootball_cert_path_a_plan_approve.sql")


def _text(name):
    return (AIVEN / name).read_text(encoding="utf-8")


def _code(name):
    return "\n".join(line for line in _text(name).splitlines() if not line.lstrip().startswith("--"))


def test_the_installed_ledger_ddl_is_bridge_0010_byte_for_byte():
    raw = path_a.LEDGER_MIGRATION.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == path_a.LEDGER_MIGRATION_SHA256
    assert path_a.LEDGER_MIGRATION.name == "0010_apifootball_cert_campaign_ledger.sql"
    block = path_a.ledger_migration_block()
    assert block.startswith("DO ") and "apifootball_cert_schema_migrations" in block and "schema_migrations WHERE" in block


def test_no_file_names_the_administrator_drops_or_changes_ownership():
    for name in FILES:
        code = _code(name)
        assert not re.search(r"\bavnadmin\b", code), name  # the administrator is SESSION_USER / CURRENT_USER only
        assert not re.search(r"^\s*(DROP|TRUNCATE|DELETE|UPDATE|ALTER\s+TABLE)\b", code, re.I | re.M), name
        assert not re.search(r"\bOWNER\s+TO\b", code, re.I), name


def test_every_temporary_owner_grant_is_revoked_in_the_same_transaction_and_asserted():
    for name in ("apifootball_cert_path_a_ledger_pre.sql", "apifootball_cert_path_a_plan_approve.sql"):
        code = _code(name) + (_code("apifootball_cert_path_a_ledger_post.sql") if "pre" in name else "")
        grant = code.index("GRANT fcb_cert_owner TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;")
        revoke = code.index("REVOKE fcb_cert_owner FROM CURRENT_USER;")
        assert grant < revoke and code.count("GRANT fcb_cert_owner TO") == 1
        tail = code[revoke:]
        assert "pg_has_role(SESSION_USER, 'fcb_cert_owner', 'SET')" in tail and "RAISE EXCEPTION" in tail
    src = (ROOT / "scripts" / "apifootball_cert_path_a.py").read_text(encoding="utf-8")
    assert "finally:\n                conn.execute(f\"REVOKE {OWNER_ROLE} FROM CURRENT_USER\")" in src


def test_defaultdb_gets_only_two_roles_and_the_minimal_coordinator_grant():
    code = _code("apifootball_cert_path_a_roles.sql")
    assert code.index("SET LOCAL createrole_self_grant = '';") < code.index("CREATE ROLE")
    granted = set(re.findall(r"GRANT [^;]*?ON TABLE ([^;]*?) TO fcb_cert_runner", code))
    tables = {t.strip() for g in granted for t in g.split(",")}
    assert tables == {"public.api_football_quota_counters", "public.api_football_quota_leases",
                      "public.api_football_quota_attempts"}
    assert "GRANT UPDATE (used) ON TABLE public.api_football_quota_counters" in code
    assert not re.search(r"GRANT [^;]*\b(TRUNCATE|REFERENCES|TRIGGER|ALL)\b", code)
    assert re.findall(r"CREATE ROLE (\w+) NOLOGIN", code) == ["fcb_cert_owner", "fcb_cert_runner"]
    assert "WITH LOGIN" in code and "PASSWORD %L" in code  # a SCRAM verifier, never a cleartext password


def test_the_runner_defaults_to_the_dedicated_ledger_database_and_owner():
    assert (batch.LEDGER_DATABASE, batch.LEDGER_OWNER_ROLE) == (path_a.LEDGER_DATABASE, path_a.OWNER_ROLE)
    ledger = batch.PostgresCampaignLedger()  # no connection is opened here
    assert ledger._owner_role == "fcb_cert_owner"
    legacy = batch.PostgresCampaignLedger(lambda: None)  # explicit connect (tests, fakes): no PATH A checks
    assert legacy._owner_role is None
    for sql in (batch.RUNNER_IDENTITY_SQL, batch.LEDGER_INTEGRITY_SQL):
        assert "pg_catalog.pg_roles" in sql and "rolsuper" in sql or "rolcanlogin" in sql


def test_identity_check_refuses_the_administrator_shape():
    class Conn:
        def __init__(self, ok):
            self.ok = ok

        def execute(self, sql):
            assert sql == batch.RUNNER_IDENTITY_SQL
            return self

        def fetchone(self):
            return {"ok": self.ok}
    assert batch.runner_identity_problem(Conn(True)) is None
    assert batch.runner_identity_problem(Conn(False)) == "RUNNER_IDENTITY_NOT_DEDICATED"


def test_f1_pinned_function_fingerprints_derive_from_the_pinned_0010_file():
    text = path_a.LEDGER_MIGRATION.read_text(encoding="utf-8")
    found = re.findall(r"CREATE FUNCTION (\w+)\(\) RETURNS trigger LANGUAGE plpgsql( SECURITY DEFINER)?\s+"
                       r"SET search_path = pg_catalog, pg_temp AS \$\$(.*?)\$\$;", text, re.S)
    assert len(found) == 4 and text.count("CREATE FUNCTION") == 4
    expected = ";".join(sorted(
        f'{name}||trigger|plpgsql|{"true" if secdef else "false"}|{{"search_path=pg_catalog, pg_temp"}}|v|'
        f'{hashlib.md5(body.encode()).hexdigest()}' for name, secdef, body in found))
    assert batch.LEDGER_FUNCTION_SIGNATURES == expected
    post = _text("apifootball_cert_path_a_ledger_post.sql")
    for name, _secdef, body in found:  # the installer asserts the same bodies
        assert f"'{name}|v|{hashlib.md5(body.encode()).hexdigest()}" in post


def test_f3_f4_identity_and_database_checks_are_in_every_layer():
    assert "m.roleid OPERATOR(pg_catalog.=) r.oid" in batch.RUNNER_IDENTITY_SQL
    assert "10::pg_catalog.oid" in batch.RUNNER_IDENTITY_SQL
    for name in ("defaultdb_precheck.sql", "defaultdb_restore_guard.sql", "defaultdb_restore_post_guard.sql",
                 "verify_bridge_roles.sql"):
        assert "m.roleid OPERATOR(pg_catalog.=) r.oid" in _text(name), name
    src = (ROOT / "scripts" / "apifootball_cert_batch1.py").read_text(encoding="utf-8")
    assert '"LEDGER_DATABASE_WRONG"' in src and '"COORDINATOR_DATABASE_WRONG"' in src
    assert batch.COORDINATOR_DATABASE == "defaultdb" and batch.LEDGER_DATABASE == "fcb_cert"


def test_p1_pinned_plans_are_the_committed_plan_files_and_their_budgets():
    import json
    batch1 = ROOT / "docs" / "certification" / "apifootball" / "batch1_plan.json"
    snapshot = ROOT / "docs" / "certification" / "apifootball" / "live_snapshot_plan.json"
    expected = {hashlib.sha256(batch1.read_bytes()).hexdigest(): ("BATCH1", json.loads(batch1.read_text())["max_attempts_total"]),
                hashlib.sha256(snapshot.read_bytes()).hexdigest(): ("LIVE_FULL_MATCH_SNAPSHOT", batch.SNAPSHOT_CAP)}
    assert batch.PINNED_PLANS == expected
    assert expected[batch.plan_sha256(batch.render_plan(batch.build_plan()))] == ("BATCH1", 78)
    assert expected[batch.plan_sha256(batch.render_snapshot_plan())] == ("LIVE_FULL_MATCH_SNAPSHOT", 8)


def test_p2a_pinned_triggers_derive_from_the_pinned_0010_file():
    text = path_a.LEDGER_MIGRATION.read_text(encoding="utf-8")
    bits = {"ROW": 1, "BEFORE": 2, "INSERT": 4, "DELETE": 8, "UPDATE": 16, "TRUNCATE": 32}
    found = re.findall(r"CREATE TRIGGER (\w+) (BEFORE|AFTER) ([A-Z ]+?)\s+ON (\w+)\s+FOR EACH (ROW|STATEMENT) "
                       r"EXECUTE FUNCTION (\w+)\(\);", text)
    assert len(found) == 12 == text.count("CREATE TRIGGER")
    expected = []
    for name, when, events, table, level, function in found:
        tgtype = (bits["ROW"] if level == "ROW" else 0) + (bits["BEFORE"] if when == "BEFORE" else 0)
        tgtype += sum(bits[e] for e in events.split(" OR "))
        expected.append(f"{table}|{name}|{function}()|true|{tgtype}|O|0|true|")
    assert batch.LEDGER_TRIGGER_SIGNATURES == ";".join(sorted(expected))


def test_p1_pinned_checks_derive_from_the_pinned_0010_file():
    """Names: PostgreSQL's generated names for the 0010 CHECKs (column-level <table>_<column>_check,
    table-level <table>_check); literals: every bound, pattern and value of the file's expressions. The
    exact pg_get_constraintdef text is asserted on the installed ledger by the integration tests."""
    text = path_a.LEDGER_MIGRATION.read_text(encoding="utf-8")
    names, literals = set(), {}
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", text, re.S):
        table_checks = 0
        for line in body.splitlines():
            line = line.strip()
            m = re.match(r"(\w+) [A-Z]+.*?CHECK \((.*)\),?$", line)
            if m and not line.startswith("CHECK"):
                name = f"{table}_{m.group(1)}_check"
            elif line.startswith("CHECK"):
                name = f"{table}_check" + (str(table_checks) if table_checks else "")
                table_checks += 1
            else:
                continue
            names.add((table, name))
            literals[name] = set(re.findall(r"'[^']*'|\b\d+\b", line.split("CHECK", 1)[1]))
    pinned = {}
    for sig in batch.LEDGER_CHECK_SIGNATURES.split(";"):
        table, name, definition, validated = sig.split("|", 3) if sig.count("|") == 3 else sig.rsplit("|", 1)[0].split("|", 2) + [sig.rsplit("|", 1)[1]]
        pinned[name] = (table, definition, validated)
    assert {(t, n) for n, (t, _d, _v) in pinned.items()} == names and len(names) == 13
    for name, (_t, definition, validated) in pinned.items():
        assert validated == "true"
        for lit in literals[name]:
            assert lit in definition, (name, lit)
