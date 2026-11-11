"""AF-07 on real PostgreSQL: bridge migration 0010 (API-FOOTBALL certification campaign ledger).

Each test runs in its own throwaway schema (search_path). Covers: the migration on an already
initialized database (0001-0009 with data, then 0010), append-only and budget enforcement in the
database, write-ahead reservations across restarts and re-runs, several PROCESSES reserving at once
(never above the cap), the runner refusing with CAMPAIGN_LEDGER_MIGRATION_REQUIRED and zero
provider requests where 0010 is missing, RESERVED vs SENT (processes killed between reservation and
send, and between send and outcome), the real fcb_app with the WP-009 grant files (AF-12/AF-13) and
the 0009 requirement of the 0010 transaction (AF-11). Provider = httpx.MockTransport; no network.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402
from psycopg.sql import SQL, Identifier, Literal  # noqa: E402

from app import migrations  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from apifootball_campaign_kit import Provider, Quota, load, settings  # noqa: E402

batch = load("apifootball_cert_batch1_pg", "apifootball_cert_batch1.py")
SHA = "a" * 64  # a well-formed plan sha256 that is never approved unless a test approves it


def _approve(connect, budget, tag="", kind="BATCH1", sha=None):
    """AF-15: register an approved plan the way the approval workflow does (owner side, here the
    test superuser). Returns its plan_sha256. Idempotent for the same (sha, budget)."""
    import hashlib
    sha = sha or hashlib.sha256(f"plan-{budget}-{tag}".encode()).hexdigest()
    with connect() as conn:
        conn.execute("INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) "
                     "VALUES (%s, %s, %s, 'owner-test', 1) ON CONFLICT (plan_sha256) DO NOTHING", (sha, kind, budget))
    return sha


@pytest.fixture
def schema():
    dsn = os.environ["TEST_DATABASE_URL"]
    name = "af07_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
    yield name, connect
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))


@pytest.fixture(autouse=True)
def owner_authorized(monkeypatch):
    monkeypatch.setenv(batch.AUTH_VAR, "true")


def _apply_up_to(connect, tmp_path, last):
    directory = tmp_path / f"upto{last}"
    directory.mkdir()
    for m in migrations.load_migrations()[:last]:
        shutil.copyfile(migrations.BRIDGE.directory / m.name, directory / m.name)
    migrations.apply_migrations(connect, directory)


def _rows(connect):
    with connect() as conn:
        return conn.execute("SELECT version, name, sha256, applied_at FROM schema_migrations ORDER BY version").fetchall()


def test_0010_on_an_initialized_database_keeps_history_and_data(schema, tmp_path):
    name, connect = schema
    _apply_up_to(connect, tmp_path, 9)
    with connect() as conn:
        conn.execute("INSERT INTO api_football_quota_counters VALUES ('h', 'day', 1, 7)")
    before = _rows(connect)
    assert [r["version"] for r in before] == list(range(1, 10))
    migrations.apply_migrations(connect)  # the whole series: 0001-0009 verified, 0010 applied
    after = _rows(connect)
    assert [r["version"] for r in after] == list(range(1, 11)) and after[:9] == before
    assert after[9]["name"] == "0010_apifootball_cert_campaign_ledger.sql"
    with connect() as conn:
        assert conn.execute("SELECT used FROM api_football_quota_counters").fetchone()["used"] == 7
    migrations.apply_migrations(connect)  # second run: no-op
    assert _rows(connect) == after


def test_ledger_is_append_only_and_the_database_enforces_the_budget(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    sha2 = _approve(connect, 2)
    with connect() as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES ('camp-db-00001', %s)", (sha2,))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-db-00001', 1, 'C01')")
    for statement in ("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-db-00001', 3, 'C03')",
                      "UPDATE api_football_cert_plans SET budget = 78",
                      "UPDATE api_football_cert_campaigns SET plan_sha256 = plan_sha256",
                      "DELETE FROM api_football_cert_campaign_attempts",
                      "DELETE FROM api_football_cert_plans",
                      "TRUNCATE api_football_cert_campaign_attempts",
                      "TRUNCATE api_football_cert_plans CASCADE",
                      "INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) "
                      "VALUES ('" + "b" * 64 + "', 'BATCH1', 79, 'o', 1)",
                      "INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) "
                      "VALUES ('" + "c" * 64 + "', 'LIVE_FULL_MATCH_SNAPSHOT', 9, 'o', 1)",
                      "INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES ('camp-db-00002', '" + SHA + "')"):
        with pytest.raises(psycopg.Error):
            with connect() as conn:
                conn.execute(statement)
    with connect() as conn:
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-db-00001', 2, 'C02')")
    with pytest.raises(psycopg.Error):  # budget 2 reached
        with connect() as conn:
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-db-00001', 3, 'C03')")


def test_reservations_survive_restart_and_rerun(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    sha5, other = _approve(connect, 5), _approve(connect, 5, "other")
    first = batch.PostgresCampaignLedger(connect, schema=name)
    asyncio.run(first.open("camp-restart-01", 5, sha5))
    for i in range(3):
        assert asyncio.run(first.reserve("camp-restart-01", f"C0{i}")) == i + 1
    second = batch.PostgresCampaignLedger(connect, schema=name)  # a new process / a new dispatch
    asyncio.run(second.open("camp-restart-01", 5, sha5))
    assert asyncio.run(second.used("camp-restart-01")) == 3
    assert asyncio.run(second.reserve("camp-restart-01", "C10")) == 4
    assert asyncio.run(second.reserve("camp-restart-01", "C11")) == 5
    with pytest.raises(batch.BudgetExhausted):
        asyncio.run(second.reserve("camp-restart-01", "C12"))
    with pytest.raises(batch.LedgerUnavailable, match="CAMPAIGN_PLAN_MISMATCH"):  # same id, another plan
        asyncio.run(second.open("camp-restart-01", 78, other))
    with pytest.raises(batch.LedgerUnavailable, match="PLAN_NOT_APPROVED"):  # an unapproved plan
        asyncio.run(second.open("camp-restart-02", 78, SHA))
    assert asyncio.run(second.attempted_calls("camp-restart-01")) == {"C00", "C01", "C02", "C10", "C11"}


def _worker(dsn, name, campaign, out):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_worker", "apifootball_cert_batch1.py")

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
    ledger = mod.PostgresCampaignLedger(connect, schema=name)
    got = 0
    for i in range(40):
        try:
            asyncio.run(ledger.reserve(campaign, f"W{os.getpid() % 100000}{i}"[:32]))
            got += 1
        except mod.BudgetExhausted:
            break
    out.put(got)


def test_concurrent_processes_never_exceed_the_cap(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    ledger = batch.PostgresCampaignLedger(connect, schema=name)
    asyncio.run(ledger.open("camp-concurrent-1", 20, _approve(connect, 20)))
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(os.environ["TEST_DATABASE_URL"], name, "camp-concurrent-1", out))
             for _ in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    granted = sum(out.get(timeout=5) for _ in procs)
    with connect() as conn:
        seqs = [r["seq"] for r in conn.execute(
            "SELECT seq FROM api_football_cert_campaign_attempts WHERE campaign_id='camp-concurrent-1' ORDER BY seq")]
    assert granted == 20 and seqs == list(range(1, 21))


def test_runner_refuses_without_0010_and_sends_nothing(schema, tmp_path):
    name, connect = schema
    _apply_up_to(connect, tmp_path, 9)
    plan_text = batch.render_plan(batch.build_plan())
    provider = Provider()
    with pytest.raises(batch.LedgerUnavailable) as exc:
        asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-missing-001", settings=settings(),
                                  ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                  inner_transport=provider.transport(), quota=Quota(), plan_text=plan_text,
                                  source_sha="x"))
    assert str(exc.value) == "CAMPAIGN_LEDGER_MIGRATION_REQUIRED" and provider.sent == []


def test_runner_on_the_real_ledger_counts_every_attempt_and_reruns_send_nothing(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    plan_text = batch.render_plan(batch.build_plan())
    _approve(connect, 78, sha=batch.plan_sha256(plan_text))
    first = Provider()
    run = asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-runner-0001", settings=settings(),
                                    ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                    inner_transport=first.transport(), quota=Quota(), plan_text=plan_text,
                                    source_sha="x"))
    assert run["ledger_attempts_used"] == len(first.sent) <= 78
    again = Provider()
    asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-runner-0001", settings=settings(),
                              ledger=batch.PostgresCampaignLedger(connect, schema=name),
                              inner_transport=again.transport(), quota=Quota(), plan_text=plan_text, source_sha="x"))
    assert again.sent == []


def test_least_privilege_role_can_only_append(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    role = "af07_" + uuid.uuid4().hex[:8]
    dsn = os.environ["TEST_DATABASE_URL"]
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(role)))
    except psycopg.Error:
        pytest.skip("cannot create roles on this test database")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("GRANT USAGE ON SCHEMA {} TO {}").format(Identifier(name), Identifier(role)))
            conn.execute(SQL("GRANT SELECT, INSERT ON {}.api_football_cert_campaigns, {}.api_football_cert_campaign_attempts, "
                             "{}.api_football_cert_campaign_outcomes TO {}")
                         .format(Identifier(name), Identifier(name), Identifier(name), Identifier(role)))
            conn.execute(SQL("GRANT SELECT ON {}.api_football_cert_plans TO {}").format(Identifier(name), Identifier(role)))
            conn.execute(SQL("REVOKE ALL ON FUNCTION {}.api_football_cert_reject_mutation(), {}.api_football_cert_attempt_guard() FROM PUBLIC")
                         .format(Identifier(name), Identifier(name)))

        def as_role():
            conn = psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
            conn.execute(SQL("SET ROLE {}").format(Identifier(role)))
            return conn
        ledger = batch.PostgresCampaignLedger(as_role, schema=name)
        asyncio.run(ledger.open("camp-privilege-1", 2, _approve(connect, 2)))
        assert asyncio.run(ledger.reserve("camp-privilege-1", "C01")) == 1
        with pytest.raises(psycopg.Error):
            with as_role() as conn:
                conn.execute("UPDATE api_football_cert_campaign_attempts SET call_id='X'")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
            conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


# ------------------------------------------------ RESERVED vs SENT: crashes in real processes

def _crash_worker(dsn, name, where, marker):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    import httpx as _httpx
    from apifootball_campaign_kit import Provider as _Provider, Quota as _Quota, load as _load, settings as _settings
    mod = _load("apifootball_cert_batch1_crash", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")

    class DiesBeforeOutcome(mod.PostgresCampaignLedger):
        async def record_outcome(self, *args):
            os._exit(17)  # the provider answered; the process dies before the outcome is written

    def handler(request):
        if where == "between_reservation_and_send":
            os._exit(17)  # reserved and committed; dies before anything leaves
        Path(marker).write_text("sent", encoding="utf-8")
        return _Provider().default(request)
    ledger = (DiesBeforeOutcome if where == "between_send_and_outcome" else mod.PostgresCampaignLedger)(connect, schema=name)
    plan_text = mod.render_plan(mod.build_plan())
    asyncio.run(mod.execute(json.loads(plan_text), campaign_id="camp-crash-00001", settings=_settings(), ledger=ledger,
                            inner_transport=_httpx.MockTransport(handler), quota=_Quota(), plan_text=plan_text,
                            source_sha="x"))
    os._exit(0)


@pytest.mark.parametrize("where", ["between_reservation_and_send", "between_send_and_outcome"])
def test_a_killed_process_leaves_reserved_unknown_counted_and_never_retried(schema, tmp_path, where):
    name, connect = schema
    migrations.apply_migrations(connect)
    marker = tmp_path / "sent.txt"
    _approve(connect, 78, sha=batch.plan_sha256(batch.render_plan(batch.build_plan())))
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_crash_worker, args=(os.environ["TEST_DATABASE_URL"], name, where, str(marker)))
    proc.start()
    proc.join(120)
    assert proc.exitcode == 17
    assert marker.exists() == (where == "between_send_and_outcome")
    plan_text = batch.render_plan(batch.build_plan())
    first = json.loads(plan_text)["calls"][0]["call_id"]
    ledger = batch.PostgresCampaignLedger(connect, schema=name)
    assert asyncio.run(ledger.attempt_states("camp-crash-00001")) == [
        {"seq": 1, "call_id": first, "state": "RESERVED_UNKNOWN", "http_status": None}]
    again = Provider()
    run = asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-crash-00001", settings=settings(),
                                    ledger=ledger, inner_transport=again.transport(), quota=Quota(),
                                    plan_text=plan_text, source_sha="x"))
    entry = next(c for c in run["calls"] if c["call_id"] == first)
    assert entry["ledger_state"] == "RESERVED_UNKNOWN"  # consumed, never retried, never recovered
    assert run["ledger_attempts_used"] == 1 + len(again.sent) <= 78
    assert run["ledger_attempt_states"]["RESERVED_UNKNOWN"] == 1
    assert run["ledger_attempt_states"]["RESPONSE_RECEIVED"] == len(again.sent)


def test_outcomes_are_append_only_one_per_reserved_attempt(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    ledger = batch.PostgresCampaignLedger(connect, schema=name)
    asyncio.run(ledger.open("camp-outcome-001", 3, _approve(connect, 3)))
    seq = asyncio.run(ledger.reserve("camp-outcome-001", "C01"))
    asyncio.run(ledger.record_outcome("camp-outcome-001", seq, "HTTP_RESPONSE", 200))
    asyncio.run(ledger.record_outcome("camp-outcome-001", seq, "TRANSPORT_ERROR", None))  # first one stays
    assert asyncio.run(ledger.attempt_states("camp-outcome-001")) == [
        {"seq": 1, "call_id": "C01", "state": "RESPONSE_RECEIVED", "http_status": 200}]
    for statement in ("INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome) "
                      "VALUES ('camp-outcome-001', 2, 'TRANSPORT_ERROR')",            # no such reserved attempt
                      "INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome) "
                      "VALUES ('camp-outcome-001', 1, 'HTTP_RESPONSE')",              # status missing
                      "UPDATE api_football_cert_campaign_outcomes SET http_status = 500",
                      "DELETE FROM api_football_cert_campaign_outcomes",
                      "TRUNCATE api_football_cert_campaign_outcomes"):
        with pytest.raises(psycopg.Error):
            with connect() as conn:
                conn.execute(statement)


# ------------------------------------------------ AF-12 / AF-13: real fcb_app and the WP-009 grant files

AIVEN = ROOT / "sql" / "aiven"
LEDGER_TABLES = ("api_football_cert_plans", "api_football_cert_campaigns", "api_football_cert_campaign_attempts",
                 "api_football_cert_campaign_outcomes")
WP009_ROLES = ("fcb_app", "fcb_signer", "fcb_migrator")
APP_TEST_PASSWORD = "af16-" + uuid.uuid4().hex  # throwaway; the role is dropped after each test


def _dsn(dbname):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    params["dbname"] = dbname
    return make_conninfo(**params)


@pytest.fixture
def wp009(tmp_path):
    """Throwaway databases on this server with the three WP-009 roles (created here if absent and
    then dropped). Each database gets the bridge series in `public` up to a given version."""
    admin = os.environ["TEST_DATABASE_URL"]
    created, databases = [], []
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)", (list(WP009_ROLES),)).fetchone():
                pytest.skip("WP-009 roles already exist on this server; not touching them")
            for role in WP009_ROLES:
                if role == "fcb_app":  # a REAL login for the runtime role (random test password)
                    conn.execute(SQL("CREATE ROLE fcb_app LOGIN PASSWORD {}").format(Literal(APP_TEST_PASSWORD)))
                else:
                    conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(role)))
                created.append(role)
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("cannot create roles on this test database")

    def make(upto):
        db = f"af13_{upto}_" + uuid.uuid4().hex[:8]
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(SQL("CREATE DATABASE {}").format(Identifier(db)))
        databases.append(db)

        def connect():
            return psycopg.connect(_dsn(db), row_factory=dict_row)
        base = tmp_path / db
        base.mkdir()
        _apply_up_to(connect, base, upto)
        return db, connect
    yield make
    with psycopg.connect(admin, autocommit=True) as conn:
        for db in databases:
            conn.execute(SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(Identifier(db)))
        for role in created:
            conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))


def _run_file(connect, name, session_authorization=None):
    with connect() as conn:
        if session_authorization:
            conn.execute(SQL("SET SESSION AUTHORIZATION {}").format(Identifier(session_authorization)))
        conn.execute((AIVEN / name).read_text(encoding="utf-8"))


def _acl(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT c.relname, a.grantee::regrole::text AS grantee, a.privilege_type FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.grantee <> c.relowner "
            "UNION ALL SELECT c.relname || '.' || t.attname, a.grantee::regrole::text, a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute t ON t.attrelid = c.oid, "
            "LATERAL aclexplode(t.attacl) a WHERE n.nspname = 'public' AND t.attacl IS NOT NULL "
            "ORDER BY 1, 2, 3").fetchall()


def _as_app(db):
    """A REAL login as fcb_app with its own test credentials: no superuser session, no SET ROLE."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    params.update(dbname=db, user="fcb_app", password=APP_TEST_PASSWORD)
    conn = psycopg.connect(make_conninfo(**params), row_factory=dict_row, options="-c lock_timeout=2000")
    assert conn.execute("SELECT session_user = 'fcb_app' AND current_user = 'fcb_app' AS ok").fetchone()["ok"]
    return conn


def _app_ledger_privileges(db):
    """Table-level privileges, plus the columns fcb_app may INSERT (column-level) and UPDATE."""
    with _as_app(db) as conn:
        out = {}
        for t in LEDGER_TABLES:
            privs = {p: conn.execute("SELECT has_table_privilege('fcb_app', %s, %s) AS ok", (t, p)).fetchone()["ok"]
                     for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")}
            for p in ("INSERT", "UPDATE", "REFERENCES"):
                privs[p + "_COLUMNS"] = tuple(r["attname"] for r in conn.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
                    "AND has_column_privilege('fcb_app', attrelid, attnum, %s) ORDER BY attnum", (t, p)))
            out[t] = privs
        return out


WRITABLE = {"api_football_cert_plans": (),  # AF-15/AF-16: SELECT only
            "api_football_cert_campaigns": ("campaign_id", "plan_sha256"),
            "api_football_cert_campaign_attempts": ("campaign_id", "seq", "call_id"),
            "api_football_cert_campaign_outcomes": ("campaign_id", "seq", "outcome", "http_status")}
# Review #199 P3-2: SELECT on the table, INSERT only on the writer's columns (never the audit stamps).
EXPECTED_APP = {t: {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False,
                    "REFERENCES": False, "TRIGGER": False, "INSERT_COLUMNS": WRITABLE[t], "UPDATE_COLUMNS": (),
                    "REFERENCES_COLUMNS": ()} for t in LEDGER_TABLES}


def _app_cannot_change_the_ledger(db):
    """As fcb_app: every way to change existing rows or switch the guards off is refused."""
    for statement in ("UPDATE api_football_cert_plans SET budget = 78",
                      "UPDATE api_football_cert_campaigns SET plan_sha256 = plan_sha256",
                      "UPDATE api_football_cert_campaign_attempts SET call_id = 'X'",
                      "DELETE FROM api_football_cert_campaign_attempts",
                      "DELETE FROM api_football_cert_campaign_outcomes",
                      "TRUNCATE api_football_cert_campaign_attempts",
                      "TRUNCATE api_football_cert_campaigns CASCADE",
                      "ALTER TABLE api_football_cert_campaign_attempts DISABLE TRIGGER api_football_cert_attempt_budget",
                      "ALTER TABLE api_football_cert_campaign_attempts DISABLE TRIGGER ALL",
                      "ALTER TABLE api_football_cert_campaigns ADD COLUMN x int",
                      "DROP TABLE api_football_cert_campaign_outcomes",
                      "DROP TRIGGER api_football_cert_attempts_immutable ON api_football_cert_campaign_attempts",
                      "SET session_replication_role = replica",
                      "SET ROLE fcb_migrator",
                      "SET SESSION AUTHORIZATION fcb_migrator"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with _as_app(db) as conn:
                conn.execute(statement)
    with _as_app(db) as conn:  # no path to the owner
        assert conn.execute("SELECT pg_has_role('fcb_app', 'fcb_migrator', 'MEMBER') AS m").fetchone()["m"] is False


def _app_cannot_forge_timestamps_or_shadow_the_budget(db, admin_connect):
    """Review #199 P3-1/P3-2 as the real fcb_app:
    * a pg_temp.api_football_cert_campaigns (budget 78) first in search_path does not lift the real
      budget: the guard reads the ledger through the firing table's schema, never the caller's path;
    * the audit timestamps cannot be set (INSERT and COPY: no column privilege), and a writer that
      does hold the privilege (the owner) gets them overwritten server-side."""
    campaign = "camp-shadow-" + uuid.uuid4().hex[:6]
    plan1 = _approve(admin_connect, 1, campaign)
    with _as_app(db) as conn:
        conn.execute("INSERT INTO public.api_football_cert_campaigns (campaign_id, plan_sha256) "
                     "VALUES (%s, %s)", (campaign, plan1))
        conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                     "VALUES (%s, 1, 'C01')", (campaign,))
    with pytest.raises(psycopg.errors.RaiseException, match="API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED"):
        with _as_app(db) as conn:
            # every table the trigger reads, shadowed in pg_temp with a larger budget / no attempts
            conn.execute("CREATE TEMP TABLE api_football_cert_plans (plan_sha256 text, budget int)")
            conn.execute("CREATE TEMP TABLE api_football_cert_campaigns (campaign_id text, plan_sha256 text)")
            conn.execute("CREATE TEMP TABLE api_football_cert_campaign_attempts (campaign_id text, seq int)")
            conn.execute("INSERT INTO pg_temp.api_football_cert_plans VALUES (%s, 78)", (plan1,))
            conn.execute("INSERT INTO pg_temp.api_football_cert_campaigns VALUES (%s, %s)", (campaign, plan1))
            conn.execute("SET search_path = pg_temp, public")
            conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                         "VALUES (%s, 2, 'C02')", (campaign,))
    for statement in (
            "INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id, reserved_at) "
            f"VALUES ('{campaign}', 2, 'C02', '2000-01-01')",
            "INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256, created_at) "
            f"VALUES ('camp-forged-0001', '{plan1}', '2000-01-01')",
            "INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status, recorded_at) "
            f"VALUES ('{campaign}', 1, 'HTTP_RESPONSE', 200, '2000-01-01')"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with _as_app(db) as conn:
                conn.execute(statement)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with _as_app(db) as conn:
            with conn.cursor().copy("COPY api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status, "
                                    "recorded_at) FROM STDIN") as copy:
                copy.write_row((campaign, 1, "HTTP_RESPONSE", 200, "2000-01-01"))
    with admin_connect() as conn:  # a writer allowed to name the column: the value is overwritten
        with conn.cursor().copy("COPY api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status, "
                                "recorded_at) FROM STDIN") as copy:
            copy.write_row((campaign, 1, "HTTP_RESPONSE", 200, "2000-01-01"))
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256, created_at) "
                     "VALUES (%s, %s, '2000-01-01')", (campaign + "-o", plan1))
        stamps = conn.execute(
            "SELECT (SELECT created_at FROM api_football_cert_campaigns WHERE campaign_id = %s) AS c, "
            "(SELECT reserved_at FROM api_football_cert_campaign_attempts WHERE campaign_id = %s) AS r, "
            "(SELECT recorded_at FROM api_football_cert_campaign_outcomes WHERE campaign_id = %s) AS o",
            (campaign + "-o", campaign, campaign)).fetchone()
        assert all(v.year >= 2026 for v in stamps.values()), stamps


def _app_can_append(db, campaign):
    def connect():
        return _as_app(db)
    ledger = batch.PostgresCampaignLedger(connect, schema="public")
    asyncio.run(ledger.open(campaign, 3, _approve(lambda: psycopg.connect(_dsn(db)), 3, campaign)))
    seq = asyncio.run(ledger.reserve(campaign, "C01"))
    asyncio.run(ledger.record_outcome(campaign, seq, "HTTP_RESPONSE", 200))
    return asyncio.run(ledger.attempt_states(campaign))


def test_wp009_grant_files_then_0010_grant_then_rerun_keep_fcb_app_at_select_insert(wp009):
    """AF-12/AF-13 on real roles, in the roles workflow's file order: 0001-0010 -> phase 1
    (grant_bridge_runtime.sql) -> fcb_app S I only, cannot change rows or disable triggers -> phase 1
    re-run -> phase 2 (prepare_bridge_roles.sql + finalize_bridge_privileges.sql: ownership to
    fcb_migrator) -> the 0010 grant file as fcb_migrator -> phase 2 files again: privileges identical."""
    db, connect = wp009(10)
    _run_file(connect, "grant_bridge_runtime.sql")
    phase1 = _acl(connect)
    assert _app_ledger_privileges(db) == EXPECTED_APP
    assert _app_can_append(db, "camp-wp009-p1-01")[0]["state"] == "RESPONSE_RECEIVED"
    _app_cannot_change_the_ledger(db)
    _app_cannot_forge_timestamps_or_shadow_the_budget(db, connect)
    _run_file(connect, "grant_bridge_runtime.sql")  # phase 1 re-run
    assert _acl(connect) == phase1
    for name in ("prepare_bridge_roles.sql", "finalize_bridge_privileges.sql"):
        _run_file(connect, name)
    with connect() as conn:
        owners = {r["relname"]: r["owner"] for r in conn.execute(
            "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c WHERE c.relname = ANY(%s)",
            (list(LEDGER_TABLES),))}
    assert owners == {t: "fcb_migrator" for t in LEDGER_TABLES}
    _run_file(connect, "apifootball_cert_ledger_0010_grant.sql", session_authorization="fcb_migrator")
    phase2 = _acl(connect)
    assert phase2 == phase1  # same grants whoever applied them
    assert _app_ledger_privileges(db) == EXPECTED_APP
    for name in ("prepare_bridge_roles.sql", "finalize_bridge_privileges.sql"):  # phase 2 files re-run
        _run_file(connect, name)
    assert _acl(connect) == phase2 and _app_ledger_privileges(db) == EXPECTED_APP
    assert [s["state"] for s in _app_can_append(db, "camp-wp009-p2-01")] == ["RESPONSE_RECEIVED"]
    _app_cannot_change_the_ledger(db)
    _app_cannot_forge_timestamps_or_shadow_the_budget(db, connect)
    with pytest.raises(psycopg.errors.RaiseException):  # even the owner: the triggers refuse
        with connect() as conn:
            conn.execute("SET ROLE fcb_migrator")
            conn.execute("UPDATE api_football_cert_campaign_attempts SET call_id = 'X'")


def test_wp009_grant_files_without_0010_are_a_noop_for_the_ledger(wp009):
    db, connect = wp009(9)
    for name in ("grant_bridge_runtime.sql", "grant_bridge_runtime.sql", "prepare_bridge_roles.sql",
                 "finalize_bridge_privileges.sql", "finalize_bridge_privileges.sql"):
        _run_file(connect, name)
    with connect() as conn:
        assert all(conn.execute("SELECT to_regclass(%s) AS r", (t,)).fetchone()["r"] is None for t in LEDGER_TABLES)
        assert conn.execute("SELECT has_table_privilege('fcb_app', 'oauth_throttle_buckets', 'SELECT,INSERT,UPDATE,DELETE') AS ok").fetchone()["ok"]


def test_require_0009_guard_refuses_a_0008_database_and_passes_on_0009_and_0010(wp009):
    for upto, ok in ((8, False), (9, True), (10, True)):
        db, connect = wp009(upto)
        if ok:
            _run_file(connect, "apifootball_cert_ledger_0010_require_0009.sql")
        else:
            with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_LEDGER_REQUIRES_BRIDGE_0009"):
                _run_file(connect, "apifootball_cert_ledger_0010_require_0009.sql")


def _verify_codes(connect):
    text = (AIVEN / "verify_bridge_roles.sql").read_text(encoding="utf-8")
    query = text[:text.index(" \\gset fcb_")]
    with connect() as conn:
        state = conn.execute(query).fetchone()["roles_state"]
    return set(state.split(":", 1)[1].split(",")) if state.startswith("NOT_HARDENED:") else {state}


def test_wp009_verify_model_differential_of_0009_and_0010(wp009):
    """DECLARED: the WP-009 verify model (verify_bridge_roles.sql: pinned routines, triggers and
    runtime allowlist of 0001-0008) is NOT changed by this PR. On the same simplified role setup,
    the checks that 0009 and 0010 make fail are exactly these (the roles workflow also pins
    0001-0008 counts and migration rows, so on a 0009/0010 database it refuses before writing)."""
    codes = {}
    for upto in (8, 9, 10):
        db, connect = wp009(upto)
        for name in ("grant_bridge_runtime.sql", "prepare_bridge_roles.sql", "finalize_bridge_privileges.sql"):
            _run_file(connect, name)
        codes[upto] = _verify_codes(connect)
    added_by_0009 = codes[9] - codes[8]
    added_by_0010 = codes[10] - codes[9]
    assert added_by_0009 == VERIFY_CODES_ADDED_BY_0009, added_by_0009
    assert added_by_0010 == VERIFY_CODES_ADDED_BY_0010, added_by_0010
    assert codes[8] <= codes[9] <= codes[10]


VERIFY_CODES_ADDED_BY_0009 = {"unexpected_relation_grant"}  # PR #198: fcb_app S I U D on oauth_throttle_buckets
# 0010: 3 functions (pinned search_path), 7 triggers, clock_timestamp() defaults, and column-level
# INSERT grants (review #199 P3-2; the WP-009 model allows no column privilege).
VERIFY_CODES_ADDED_BY_0010 = {"column_grant", "expression_functions", "routine_set", "trigger_set"}


def test_readiness_requires_all_three_0010_tables_once_recorded(schema, monkeypatch):
    """Review #199 P3-3: 0010 recorded and its _outcomes table missing is MISSING_TABLES (3/3)."""
    from bridge.schema_readiness import evaluate
    name, connect = schema
    migrations.apply_migrations(connect)
    monkeypatch.delenv("MCP_OAUTH_THROTTLE_MODE", raising=False)
    assert evaluate(connect)["state"] == "READY"
    with connect() as conn:
        conn.execute("DROP TABLE api_football_cert_campaign_outcomes")
    result = evaluate(connect)
    assert result["state"] == "MISSING_TABLES" and result["missing_tables"] == ["api_football_cert_campaign_outcomes"]


def test_stamp_and_guard_hold_with_a_hostile_search_path_in_any_schema(schema):
    """Review #199 P3-1 independent of roles: a temporary shadow table never feeds the guard."""
    name, connect = schema
    migrations.apply_migrations(connect)
    with connect() as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES ('camp-temp-0001', %s)",
                     (_approve(connect, 1),))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-temp-0001', 1, 'C01')")
    with pytest.raises(psycopg.errors.RaiseException, match="BUDGET_EXHAUSTED"):
        with connect() as conn:
            conn.execute("CREATE TEMP TABLE api_football_cert_plans (plan_sha256 text, budget int)")
            conn.execute("INSERT INTO pg_temp.api_football_cert_plans SELECT plan_sha256, 78 FROM api_football_cert_campaigns")
            conn.execute(SQL("SET search_path = pg_temp, {}").format(Identifier(name)))
            conn.execute(SQL("INSERT INTO {}.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                             "VALUES ('camp-temp-0001', 2, 'C02')").format(Identifier(name)))


# ------------------------------------------------ AF-15: one budget per APPROVED PLAN, shared by its campaigns

def _plan_worker(dsn, name, campaign, plan, mode, out):
    """One process, its own campaign_id of the shared plan. mode:
    normal  reserve until the plan budget refuses;
    abort   before each reservation, an INSERT of an attempt in a transaction that is rolled back;
    kill    killed (os._exit) right after its 3rd COMMITTED reservation, before any outcome;
    kill_uncommitted  2 committed reservations, then an attempt INSERTed (guard passed, plan lock
            held) in an open transaction, and the process is killed BEFORE the commit."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_plan_worker", "apifootball_cert_batch1.py")

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
    ledger = mod.PostgresCampaignLedger(connect, schema=name)
    asyncio.run(ledger.open(campaign, 78, plan))
    got = 0
    for i in range(80):
        if mode == "abort":
            conn = connect()
            try:
                conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                             "VALUES (%s, %s, 'ABORTED')", (campaign, got + 1))
            except psycopg.Error:
                pass
            conn.rollback()  # never committed: never sent, never counted
            conn.close()
        try:
            asyncio.run(ledger.reserve(campaign, f"P{i}"))
            got += 1
        except mod.BudgetExhausted:
            break
        if mode == "kill" and got == 3:
            os._exit(9)
        if mode == "kill_uncommitted" and got == 2:
            conn = connect()
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                         "VALUES (%s, 3, 'UNCOMMITTED')", (campaign,))
            os._exit(9)  # no commit: the server rolls the transaction back
    out.put(got)


def _plan_counts(connect, plan):
    with connect() as conn:
        rows = conn.execute("SELECT a.campaign_id, array_agg(a.seq ORDER BY a.seq) AS seqs, "
                            "bool_or(a.call_id = 'ABORTED') AS aborted FROM api_football_cert_campaign_attempts a "
                            "JOIN api_football_cert_campaigns c USING (campaign_id) WHERE c.plan_sha256 = %s "
                            "GROUP BY a.campaign_id", (plan,)).fetchall()
    return rows


def test_af15_concurrent_campaigns_of_one_plan_never_exceed_the_plan_budget_with_kills_and_aborts(schema):
    """INVARIANT 2 (atomic budget), real processes on PostgreSQL 18, one approved plan (budget 25):
    phase A: three processes KILLED - two after 3 COMMITTED reservations each, one after 2 committed
    reservations and a 3rd INSERTed in a transaction that never commits;
    phase B: six concurrent processes (own campaign_ids) race for the rest, two of them interleaving
    rolled-back inserts; then restarts / new campaign_ids of the plan get nothing.
    Committed reservations stay and count; uncommitted / aborted ones leave no row and no budget."""
    name, connect = schema
    migrations.apply_migrations(connect)
    plan = _approve(connect, 25, "concurrent")
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    dsn = os.environ["TEST_DATABASE_URL"]

    def spawn(modes, offset):
        procs = [ctx.Process(target=_plan_worker, args=(dsn, name, f"camp-plan-{offset + i:04d}", plan, mode, out))
                 for i, mode in enumerate(modes)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(180)
        return [p.exitcode for p in procs]
    assert spawn(["kill", "kill", "kill_uncommitted"], 0) == [9, 9, 9]
    rows = {r["campaign_id"]: r for r in _plan_counts(connect, plan)}
    assert {c: r["seqs"] for c, r in rows.items()} == {"camp-plan-0000": [1, 2, 3], "camp-plan-0001": [1, 2, 3],
                                                       "camp-plan-0002": [1, 2]}  # committed stay, uncommitted gone
    assert spawn(["normal", "abort", "normal", "abort", "normal", "normal"], 10) == [0] * 6
    granted = sum(out.get(timeout=5) for _ in range(6))
    rows = _plan_counts(connect, plan)
    assert sum(len(r["seqs"]) for r in rows) == 25 and granted == 25 - 8  # never above the plan budget
    assert all(r["seqs"] == list(range(1, len(r["seqs"]) + 1)) and not r["aborted"] for r in rows)
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM api_football_cert_campaign_attempts "
                            "WHERE call_id IN ('UNCOMMITTED', 'ABORTED')").fetchone()["n"] == 0
    # restart / re-dispatch / new campaign_id of the same plan: nothing renewed
    ledger = batch.PostgresCampaignLedger(connect, schema=name)
    for campaign in ("camp-plan-0002", "camp-plan-new-01"):
        asyncio.run(ledger.open(campaign, 78, plan))
        with pytest.raises(batch.BudgetExhausted):
            asyncio.run(ledger.reserve(campaign, "AGAIN"))
    assert asyncio.run(ledger.plan_usage("camp-plan-new-01")) == {"plan_sha256": plan, "budget": 25, "used": 25}
    with pytest.raises(psycopg.errors.RaiseException, match="BUDGET_EXHAUSTED"):  # the database alone refuses too
        with connect() as conn:
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                         "VALUES ('camp-plan-new-01', 1, 'DIRECT')")


def test_af15_a_new_campaign_id_of_an_approved_plan_gets_no_new_budget_and_unapproved_plans_send_nothing(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    plan_text = batch.render_plan(batch.build_plan())
    provider = Provider()
    with pytest.raises(batch.LedgerUnavailable, match="PLAN_NOT_APPROVED"):  # the committed plan, not approved
        asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-unapproved-1", settings=settings(),
                                  ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                  inner_transport=provider.transport(), quota=Quota(), plan_text=plan_text,
                                  source_sha="x"))
    assert provider.sent == []
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM api_football_cert_campaigns").fetchone()["n"] == 0
    _approve(connect, 5, sha=batch.plan_sha256(plan_text))  # approved with a budget of 5
    sent = []
    for campaign in ("camp-approved-01", "camp-approved-02", "camp-approved-01"):
        provider = Provider()
        run = asyncio.run(batch.execute(json.loads(plan_text), campaign_id=campaign, settings=settings(),
                                        ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                        inner_transport=provider.transport(), quota=Quota(), plan_text=plan_text,
                                        source_sha="x"))
        sent.append(len(provider.sent))
        assert run["plan_budget"]["budget"] == 5 and run["plan_budget"]["used"] <= 5
    assert sent == [5, 0, 0]
    # a regenerated / edited plan text is another sha: not approved, nothing sent
    edited = plan_text.replace('"PREPARED_NOT_AUTHORIZED"', '"EDITED"')
    provider = Provider()
    with pytest.raises(batch.LedgerUnavailable, match="PLAN_NOT_APPROVED"):
        asyncio.run(batch.execute(json.loads(edited), campaign_id="camp-edited-001", settings=settings(),
                                  ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                  inner_transport=provider.transport(), quota=Quota(), plan_text=edited, source_sha="x"))
    assert provider.sent == []


def test_af15_snapshot_budget_belongs_to_its_approved_plan_and_is_not_renewed_by_a_new_snapshot_id(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    text = batch.render_snapshot_plan()
    provider = Provider()
    with pytest.raises(batch.LedgerUnavailable, match="PLAN_NOT_APPROVED"):
        asyncio.run(batch.execute_snapshot(snapshot_id="snap-unapproved-1", fixture_id=101, settings=settings(),
                                           ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                           inner_transport=provider.transport(), quota=Quota()))
    assert provider.sent == []
    _approve(connect, 8, kind="LIVE_FULL_MATCH_SNAPSHOT", sha=batch.plan_sha256(text))
    total = 0
    for sid in ("snap-approved-01", "snap-approved-02", "snap-approved-03"):
        provider = Provider()
        asyncio.run(batch.execute_snapshot(snapshot_id=sid, fixture_id=101, settings=settings(),
                                           ledger=batch.PostgresCampaignLedger(connect, schema=name),
                                           inner_transport=provider.transport(), quota=Quota()))
        total += len(provider.sent)
    assert total <= 8


# ------------------------------------------------ INVARIANT 1: approved plans are immutable for fcb_app

def _setup_phase2(wp009_make):
    db, connect = wp009_make(10)
    for name in ("grant_bridge_runtime.sql", "prepare_bridge_roles.sql", "finalize_bridge_privileges.sql"):
        _run_file(connect, name)
    _run_file(connect, "apifootball_cert_ledger_0010_grant.sql", session_authorization="fcb_migrator")
    return db, connect


def _run_approval(connect, sha, kind, budget, actor="owner", run_id="42", as_role="fcb_migrator"):
    text = (AIVEN / "apifootball_cert_plan_approve.sql").read_text(encoding="utf-8")
    for var, value in (("plan_sha256", sha), ("plan_kind", kind), ("budget", str(budget)),
                       ("approved_by", actor), ("run_id", run_id)):
        text = text.replace(f":'{var}'", "'" + value.replace("'", "''") + "'")
    with connect() as conn:
        conn.execute(SQL("SET SESSION AUTHORIZATION {}").format(Identifier(as_role)))
        conn.execute(text)


def test_invariant1_plan_approval_sql_registers_once_never_renews_and_only_as_fcb_migrator(wp009):
    db, connect = _setup_phase2(wp009)
    plan_text = batch.render_plan(batch.build_plan())
    sha = batch.plan_sha256(plan_text)
    assert sha == __import__("hashlib").sha256(batch.PLAN_PATH.read_bytes()).hexdigest()  # = the committed file
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PLAN_NOT_MIGRATOR"):
        _run_approval(connect, sha, "BATCH1", 78, as_role="fcb_app")
    _run_approval(connect, sha, "BATCH1", 78)
    _run_approval(connect, sha, "BATCH1", 78)  # re-approval: no-op, nothing renewed
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PLAN_CONFLICT"):
        _run_approval(connect, sha, "BATCH1", 40)  # another budget for the same plan: refused
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PLAN_INPUT_INVALID"):
        _run_approval(connect, "not-a-sha", "BATCH1", 78)
    with connect() as conn:
        rows = conn.execute("SELECT plan_kind, budget, approved_by, github_run_id, approved_role, "
                            "approved_at > now() - interval '1 hour' AS fresh FROM api_football_cert_plans").fetchall()
    assert rows == [{"plan_kind": "BATCH1", "budget": 78, "approved_by": "owner", "github_run_id": 42,
                     "approved_role": "fcb_migrator", "fresh": True}]


def test_invariant1_fcb_app_cannot_create_change_delete_or_replace_an_approved_plan(wp009):
    db, connect = _setup_phase2(wp009)
    sha = _approve(connect, 2, "immutable")
    other = "d" * 64
    for statement in (
            f"INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) VALUES ('{other}', 'BATCH1', 78, 'x', 1)",
            f"INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) VALUES ('{sha}', 'BATCH1', 78, 'x', 1) "
            "ON CONFLICT (plan_sha256) DO UPDATE SET budget = 78",
            "UPDATE api_football_cert_plans SET budget = 78",
            "DELETE FROM api_football_cert_plans",
            "TRUNCATE api_football_cert_plans CASCADE",
            "ALTER TABLE api_football_cert_plans DISABLE TRIGGER ALL",
            "ALTER TABLE api_football_cert_plans DROP CONSTRAINT api_football_cert_plans_budget_check",
            "DROP TABLE api_football_cert_plans CASCADE",
            "COPY api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) FROM STDIN"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with _as_app(db) as conn:
                conn.execute(statement)
    with pytest.raises(psycopg.errors.RaiseException, match="APPEND_ONLY"):  # even the owner (delete+insert)
        with connect() as conn:
            conn.execute("SET ROLE fcb_migrator")
            conn.execute("DELETE FROM api_football_cert_plans")
    # a pg_temp plan with a larger budget does not lift the approved one (2)
    campaign = "camp-immut-0001"
    with _as_app(db) as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES (%s, %s)", (campaign, sha))
        for seq in (1, 2):
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES (%s, %s, 'C')",
                         (campaign, seq))
    with pytest.raises(psycopg.errors.RaiseException, match="BUDGET_EXHAUSTED"):
        with _as_app(db) as conn:
            conn.execute("CREATE TEMP TABLE api_football_cert_plans (plan_sha256 text, budget int)")
            conn.execute("INSERT INTO pg_temp.api_football_cert_plans VALUES (%s, 78)", (sha,))
            conn.execute("SET search_path = pg_temp, public")
            conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                         "VALUES (%s, 3, 'C')", (campaign,))
    with connect() as conn:
        assert conn.execute("SELECT budget FROM api_football_cert_plans WHERE plan_sha256 = %s", (sha,)).fetchone()["budget"] == 2


# ------------------------------------------------ INVARIANT 3: a reserved attempt is evidence for good

def test_invariant3_a_reserved_attempt_cannot_vanish_be_reused_or_be_rewritten_as_never_made(wp009):
    db, connect = _setup_phase2(wp009)
    sha = _approve(connect, 5, "evidence")
    campaign = "camp-evidence-01"
    with _as_app(db) as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES (%s, %s)", (campaign, sha))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES (%s, 1, 'C01')", (campaign,))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES (%s, 2, 'C02')", (campaign,))
        conn.execute("INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status) "
                     "VALUES (%s, 1, 'HTTP_RESPONSE', 200)", (campaign,))
    refused = {  # statement -> expected error class
        f"INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('{campaign}', 1, 'OTHER')":
            psycopg.errors.RaiseException,    # same seq for another call: refused (guard; PK backstop)
        f"INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('{campaign}', 2, 'C02')":
            psycopg.errors.RaiseException,
        f"INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status) VALUES ('{campaign}', 1, 'TRANSPORT_ERROR', NULL)":
            psycopg.errors.UniqueViolation,   # one outcome per attempt
        f"INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status) VALUES ('{campaign}', 1, 'TRANSPORT_ERROR', NULL) "
        "ON CONFLICT (campaign_id, seq) DO UPDATE SET outcome = 'TRANSPORT_ERROR'":
            psycopg.errors.InsufficientPrivilege,
        "UPDATE api_football_cert_campaign_outcomes SET http_status = 500": psycopg.errors.InsufficientPrivilege,
        "DELETE FROM api_football_cert_campaign_outcomes": psycopg.errors.InsufficientPrivilege,
        "DELETE FROM api_football_cert_campaign_attempts WHERE seq = 2": psycopg.errors.InsufficientPrivilege,
        "UPDATE api_football_cert_campaign_attempts SET call_id = 'X' WHERE seq = 2": psycopg.errors.InsufficientPrivilege,
        "TRUNCATE api_football_cert_campaign_attempts CASCADE": psycopg.errors.InsufficientPrivilege,
        f"INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status) VALUES ('{campaign}', 9, 'TRANSPORT_ERROR', NULL)":
            psycopg.errors.ForeignKeyViolation,   # no outcome without a reserved attempt
    }
    for statement, error in refused.items():
        with pytest.raises(error):
            with _as_app(db) as conn:
                conn.execute(statement)
    for statement in ("DELETE FROM api_football_cert_campaign_attempts WHERE seq = 2",
                      "UPDATE api_football_cert_campaign_outcomes SET outcome = 'TRANSPORT_ERROR', http_status = NULL"):
        with pytest.raises(psycopg.errors.RaiseException, match="APPEND_ONLY"):  # even the owner
            with connect() as conn:
                conn.execute("SET ROLE fcb_migrator")
                conn.execute(statement)

    def as_app():
        return _as_app(db)
    ledger = batch.PostgresCampaignLedger(as_app, schema="public")
    states = asyncio.run(ledger.attempt_states(campaign))
    assert states == [{"seq": 1, "call_id": "C01", "state": "RESPONSE_RECEIVED", "http_status": 200},
                      {"seq": 2, "call_id": "C02", "state": "RESERVED_UNKNOWN", "http_status": None}]
    asyncio.run(ledger.record_outcome(campaign, 1, "TRANSPORT_ERROR", None))  # runner path: first outcome stays
    assert asyncio.run(ledger.attempt_states(campaign))[0]["state"] == "RESPONSE_RECEIVED"
