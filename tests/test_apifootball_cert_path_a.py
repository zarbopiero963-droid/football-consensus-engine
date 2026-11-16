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
    # Owner ratification FCB-A2-R1-WP009-DECISIONS (intentional change): step 1 creates the roles and
    # grants NOTHING; a runner USAGE on public left by the earlier variant is revoked (the SECURITY
    # DEFINER functions need none, WP-009 phase 1 revoked PUBLIC's). No table or column grant at all.
    code = _code("apifootball_cert_path_a_roles.sql")
    assert code.index("SET LOCAL createrole_self_grant = '';") < code.index("CREATE ROLE")
    assert not re.search(r"GRANT [^;]*\bON TABLE\b", code)
    assert re.findall(r"^GRANT [^;]*;", code, re.M) == []
    assert re.findall(r"^REVOKE [^;]*;", code, re.M) == ["REVOKE USAGE, CREATE ON SCHEMA public FROM fcb_cert_runner;"]
    assert "'api_football_quota_counters'" not in code  # no table is exempted from "no privilege"
    assert re.findall(r"CREATE ROLE (\w+) NOLOGIN", code) == ["fcb_cert_owner", "fcb_cert_runner"]
    assert "WITH LOGIN" in code and "PASSWORD %L" in code  # a SCRAM verifier, never a cleartext password
    # PATH A2 variant R1 (owner decision D2, intentional change): NO table or column GRANT to the runner;
    # step 1a (administrator) creates the empty schema, step 1b (tables' owner) the four functions.
    schema = _code("apifootball_cert_path_a_quota_schema.sql")
    assert "CREATE SCHEMA fcb_cert_quota AUTHORIZATION %I" in schema and not re.search(r"\bGRANT\b", schema)
    fns = _code("apifootball_cert_path_a_quota_functions.sql")
    assert not re.search(r"GRANT [^;]*\bON TABLE\b|GRANT [^;]*\bON (ALL|SEQUENCE)", fns)
    assert re.findall(r"^GRANT [^;]*?TO (\w+);", fns, re.M | re.S) == ["fcb_cert_runner", "fcb_cert_runner"]
    assert "GRANT USAGE ON SCHEMA fcb_cert_quota TO fcb_cert_runner;" in fns
    assert fns.count("SECURITY DEFINER") == 4 and fns.count("SET search_path = pg_catalog, pg_temp") == 4
    assert fns.count("IF SESSION_USER OPERATOR(pg_catalog.<>) 'fcb_cert_runner'::pg_catalog.name THEN") == 4
    assert not re.search(r"\b(CREATE|ALTER|DROP)\s+ROLE\b|PASSWORD|WITH GRANT OPTION", fns)  # never a rotation
    assert "FCB_CERT_PATH_A_QUOTA_GUARD_FAILED" in fns and "3::pg_catalog.int8" in fns
    assert "'fcb_cert_owner', 'fcb_cert_runner', 'fcb_app', 'fcb_signer'" in fns
    # every relation in the bodies is schema-qualified
    for table in ("api_football_quota_counters", "api_football_quota_leases", "api_football_quota_attempts"):
        assert re.findall(r"(\S+)\b" + table, fns) and all(p.endswith("public.") or p.endswith("'") or p.endswith("[")
                                                        or p.endswith("ARRAY['") for p in re.findall(r"(\S*)" + table, fns))


def test_the_quota_assert_embeds_the_canonical_state_query_byte_for_byte():
    state = "\n".join(l for l in (ROOT / "sql/aiven/apifootball_cert_quota_state.sql").read_text(encoding="utf-8").splitlines()
                      if not l.lstrip().startswith("--")).strip()
    assert state.endswith(";")
    assert state[:-1] in (ROOT / "sql/aiven/apifootball_cert_path_a_quota_assert.sql").read_text(encoding="utf-8")
    # the pinned body hashes are those of the committed function file
    import hashlib
    fns = (ROOT / "sql/aiven/apifootball_cert_path_a_quota_functions.sql").read_text(encoding="utf-8")
    for name, tag in (("reserve", "fcbcertreserve"), ("release", "fcbcertrelease"), ("snapshot", "fcbcertsnapshot"),
                      ("mark_exhausted", "fcbcertexhausted")):
        body = fns.split(f"${tag}$", 2)[1]
        assert f"'{name}|" in state and hashlib.md5(body.encode()).hexdigest() in state, name


def test_runner_rotation_is_step_one_only_and_asserts_the_exact_grants():
    src = (ROOT / "scripts" / "apifootball_cert_path_a.py").read_text(encoding="utf-8")
    body = src[src.index("def rotate_runner("):src.index("def create_database(")]
    assert "apifootball_cert_path_a_roles.sql" in body and "apifootball_cert_path_a_quota_assert.sql" in body
    assert "quota_functions" not in body and "coordinator_grants" not in body  # no re-definition / re-grant


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
