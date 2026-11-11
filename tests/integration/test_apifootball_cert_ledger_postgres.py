"""AF-07 on real PostgreSQL: bridge migration 0010 (API-FOOTBALL certification campaign ledger).

Each test runs in its own throwaway schema (search_path). Covers: the migration on an already
initialized database (0001-0009 with data, then 0010), append-only and budget enforcement in the
database, write-ahead reservations across restarts and re-runs, several PROCESSES reserving at once
(never above the cap), and the runner refusing with CAMPAIGN_LEDGER_MIGRATION_REQUIRED and zero
provider requests where 0010 is missing. Provider = httpx.MockTransport; no network.
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
            conn.execute(SQL("GRANT SELECT, INSERT ON {}.api_football_cert_campaigns, {}.api_football_cert_campaign_attempts TO {}")
                         .format(Identifier(name), Identifier(name), Identifier(role)))
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
