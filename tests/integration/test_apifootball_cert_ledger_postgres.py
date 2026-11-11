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
from psycopg.sql import SQL, Identifier  # noqa: E402

from app import migrations  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from apifootball_campaign_kit import Provider, Quota, load, settings  # noqa: E402

batch = load("apifootball_cert_batch1_pg", "apifootball_cert_batch1.py")
SHA = "a" * 64


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
    with connect() as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns VALUES ('camp-db-00001', 2, %s)", (SHA,))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-db-00001', 1, 'C01')")
    for statement in ("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-db-00001', 3, 'C03')",
                      "UPDATE api_football_cert_campaigns SET budget = 78",
                      "DELETE FROM api_football_cert_campaign_attempts",
                      "TRUNCATE api_football_cert_campaign_attempts",
                      "INSERT INTO api_football_cert_campaigns VALUES ('camp-db-00002', 79, '" + SHA + "')"):
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
    first = batch.PostgresCampaignLedger(connect, schema=name)
    asyncio.run(first.open("camp-restart-01", 5, SHA))
    for i in range(3):
        assert asyncio.run(first.reserve("camp-restart-01", f"C0{i}")) == i + 1
    second = batch.PostgresCampaignLedger(connect, schema=name)  # a new process / a new dispatch
    asyncio.run(second.open("camp-restart-01", 5, SHA))
    assert asyncio.run(second.used("camp-restart-01")) == 3
    assert asyncio.run(second.reserve("camp-restart-01", "C10")) == 4
    assert asyncio.run(second.reserve("camp-restart-01", "C11")) == 5
    with pytest.raises(batch.BudgetExhausted):
        asyncio.run(second.reserve("camp-restart-01", "C12"))
    with pytest.raises(batch.LedgerUnavailable):  # same id, another plan or budget: refused
        asyncio.run(second.open("camp-restart-01", 78, SHA))
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
    asyncio.run(ledger.open("camp-concurrent-1", 20, SHA))
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
            conn.execute(SQL("REVOKE ALL ON FUNCTION {}.api_football_cert_reject_mutation(), {}.api_football_cert_attempt_guard() FROM PUBLIC")
                         .format(Identifier(name), Identifier(name)))

        def as_role():
            conn = psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
            conn.execute(SQL("SET ROLE {}").format(Identifier(role)))
            return conn
        ledger = batch.PostgresCampaignLedger(as_role, schema=name)
        asyncio.run(ledger.open("camp-privilege-1", 2, SHA))
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
    asyncio.run(ledger.open("camp-outcome-001", 3, SHA))
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
LEDGER_TABLES = ("api_football_cert_campaigns", "api_football_cert_campaign_attempts", "api_football_cert_campaign_outcomes")
WP009_ROLES = ("fcb_app", "fcb_signer", "fcb_migrator")


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
            for role in WP009_ROLES:
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
                    conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(role)))
                    created.append(role)
    except psycopg.Error:
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
    conn = psycopg.connect(_dsn(db), row_factory=dict_row, options="-c lock_timeout=2000")
    conn.execute("SET ROLE fcb_app")
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


WRITABLE = {"api_football_cert_campaigns": ("campaign_id", "budget", "plan_sha256"),
            "api_football_cert_campaign_attempts": ("campaign_id", "seq", "call_id"),
            "api_football_cert_campaign_outcomes": ("campaign_id", "seq", "outcome", "http_status")}
# Review #199 P3-2: SELECT on the table, INSERT only on the writer's columns (never the audit stamps).
EXPECTED_APP = {t: {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False,
                    "REFERENCES": False, "TRIGGER": False, "INSERT_COLUMNS": WRITABLE[t], "UPDATE_COLUMNS": (),
                    "REFERENCES_COLUMNS": ()} for t in LEDGER_TABLES}


def _app_cannot_change_the_ledger(db):
    """As fcb_app: every way to change existing rows or switch the guards off is refused."""
    for statement in ("UPDATE api_football_cert_campaigns SET budget = 78",
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
                      "SET session_replication_role = replica"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with _as_app(db) as conn:
                conn.execute(statement)
    with _as_app(db) as conn:  # no path to the owner (SET ROLE is judged on the session user here)
        assert conn.execute("SELECT pg_has_role('fcb_app', 'fcb_migrator', 'MEMBER') AS m").fetchone()["m"] is False


def _app_cannot_forge_timestamps_or_shadow_the_budget(db, admin_connect):
    """Review #199 P3-1/P3-2 as the real fcb_app:
    * a pg_temp.api_football_cert_campaigns (budget 78) first in search_path does not lift the real
      budget: the guard reads the ledger through the firing table's schema, never the caller's path;
    * the audit timestamps cannot be set (INSERT and COPY: no column privilege), and a writer that
      does hold the privilege (the owner) gets them overwritten server-side."""
    campaign = "camp-shadow-" + uuid.uuid4().hex[:6]
    with _as_app(db) as conn:
        conn.execute("INSERT INTO public.api_football_cert_campaigns (campaign_id, budget, plan_sha256) "
                     "VALUES (%s, 1, %s)", (campaign, SHA))
        conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                     "VALUES (%s, 1, 'C01')", (campaign,))
    with pytest.raises(psycopg.errors.RaiseException, match="API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED"):
        with _as_app(db) as conn:
            conn.execute("CREATE TEMP TABLE api_football_cert_campaigns (campaign_id text, budget int)")
            conn.execute("CREATE TEMP TABLE api_football_cert_campaign_attempts (campaign_id text, seq int)")
            conn.execute("INSERT INTO pg_temp.api_football_cert_campaigns VALUES (%s, 78)", (campaign,))
            conn.execute("SET search_path = pg_temp, public")
            conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                         "VALUES (%s, 2, 'C02')", (campaign,))
    for statement in (
            "INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id, reserved_at) "
            f"VALUES ('{campaign}', 2, 'C02', '2000-01-01')",
            "INSERT INTO api_football_cert_campaigns (campaign_id, budget, plan_sha256, created_at) "
            f"VALUES ('camp-forged-0001', 1, '{SHA}', '2000-01-01')",
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
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, budget, plan_sha256, created_at) "
                     "VALUES (%s, 1, %s, '2000-01-01')", (campaign + "-o",  SHA))
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
    asyncio.run(ledger.open(campaign, 3, SHA))
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
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, budget, plan_sha256) VALUES ('camp-temp-0001', 1, %s)", (SHA,))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-temp-0001', 1, 'C01')")
    with pytest.raises(psycopg.errors.RaiseException, match="BUDGET_EXHAUSTED"):
        with connect() as conn:
            conn.execute("CREATE TEMP TABLE api_football_cert_campaigns (campaign_id text, budget int)")
            conn.execute("INSERT INTO pg_temp.api_football_cert_campaigns VALUES ('camp-temp-0001', 78)")
            conn.execute(SQL("SET search_path = pg_temp, {}").format(Identifier(name)))
            conn.execute(SQL("INSERT INTO {}.api_football_cert_campaign_attempts (campaign_id, seq, call_id) "
                             "VALUES ('camp-temp-0001', 2, 'C02')").format(Identifier(name)))
