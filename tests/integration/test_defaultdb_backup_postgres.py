"""FCB-WP-009 backup: scripts/fcb_db_backup.py `run` end to end on PostgreSQL 18, offline.

Source: a throwaway database restored to bridge 0001-0008 by its non-superuser owner (the Aiven
shape of test_wp009_roles_runtime_postgres.py), before and after WP-009 phase 1, with rows in
several tables and a moved sequence. The script dumps it in an exported snapshot, restores it into
a second, isolated PostgreSQL 18 cluster it starts itself (initdb, unix socket only), verifies,
encrypts with age (passphrase mode), decrypts again and deletes every plaintext file.

Negative cases (each FAILS, leaves no artifact and no plaintext): tampered ciphertext, wrong
passphrase, a table missing in the restore, a row-count mismatch, a content change, an owner or ACL
mismatch, a role-attribute mismatch, a roles dump carrying a password hash, a source object owned by
another role. The passphrase never reaches argv, a child's environment, stdout/stderr or the summary.
The source is read-only for the backup: its data and catalog are byte-for-byte unchanged.

Needs TEST_DATABASE_URL (PostgreSQL 18), FCB_REQUIRE_PSQL=1, the PostgreSQL 18 server binaries
(initdb, pg_ctl; FCB_PG18_BINDIR, default /usr/lib/postgresql/18/bin), age >= 1.1, and a non-root user
(initdb refuses root). No production credentials; every role, database and cluster is removed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "fcb_db_backup.py"
BINDIR = Path(os.environ.get("FCB_PG18_BINDIR", "/usr/lib/postgresql/18/bin"))

_spec = importlib.util.spec_from_file_location("wp009_rr_for_backup", Path(__file__).with_name("test_wp009_roles_runtime_postgres.py"))
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)


def _server_major():
    try:
        return rr._server_major_raw()
    except Exception:
        return None


pytestmark = [
    *rr.pytestmark,
    pytest.mark.skipif(not (BINDIR / "initdb").exists() or not (BINDIR / "pg_dump").exists(),
                       reason="requires the PostgreSQL 18 server binaries (FCB_PG18_BINDIR)"),
    pytest.mark.skipif(shutil.which("age") is None, reason="requires age >= 1.1"),
    pytest.mark.skipif(os.geteuid() == 0, reason="initdb refuses to run as root"),
]

PASSPHRASE = "Fcb-test-" + secrets.token_urlsafe(32)


def _module():
    spec = importlib.util.spec_from_file_location("fcb_db_backup_it", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module", autouse=True)
def _require_pg18():
    if _server_major() != 18:
        pytest.skip("TEST_DATABASE_URL is not PostgreSQL 18")


@pytest.fixture
def short_tmp():
    path = Path(tempfile.mkdtemp(prefix="fb-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _seed(env):
    """Rows (and a moved sequence) written by the owner, as the runtime would."""
    env.superuser(
        "INSERT INTO public.oauth_claude_dcr_clients (client_id_hash, redirect_uri, expires_at) VALUES "
        "('h1', 'https://example.invalid/a', '2030-01-01T00:00:00Z'), ('h2', 'https://example.invalid/b', '2030-01-02T00:00:00Z')",
        "INSERT INTO public.runtime_state (key, value) VALUES ('k1', '{\"a\": [1, 2.5, null]}'), ('k2', '\"x\"')",
        "INSERT INTO public.ingest_runs (source, source_sha256, rows_seen, rows_inserted) "
        "SELECT 'src' || g, pg_catalog.md5(g::text), g, g FROM pg_catalog.generate_series(1, 25) g",
        as_role=env.admin_role)
    # Database-level properties that live in database.sql, and default privileges (review #202).
    env.superuser(
        f'ALTER DATABASE "{env.dbname}" SET work_mem TO \'8MB\'',
        f'ALTER ROLE "{env.admin_role}" IN DATABASE "{env.dbname}" SET statement_timeout TO \'5min\'',
        f'REVOKE TEMPORARY ON DATABASE "{env.dbname}" FROM PUBLIC',
        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{env.admin_role}" IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC')


class AivenEnv(rr.Env):
    """The production shape: the administrator is called `avnadmin` (review #202 P1)."""

    def __init__(self):
        super().__init__()
        self.admin_role = "avnadmin"
        self.admin_dsn = rr._dsn_for(self.dbname, user=self.admin_role, password=self.admin_password)


def _aiven_monitor_schema(env):
    """Aiven's `_avnadmin_monitor`: owned by another role, unreadable by the administrator."""
    monitor = env.extra_role()
    env.superuser(f'CREATE SCHEMA _avnadmin_monitor AUTHORIZATION "{monitor}"',
                  "CREATE TABLE _avnadmin_monitor.probe (x int)", "INSERT INTO _avnadmin_monitor.probe VALUES (1)",
                  f'ALTER TABLE _avnadmin_monitor.probe OWNER TO "{monitor}"',
                  "REVOKE ALL ON SCHEMA _avnadmin_monitor FROM PUBLIC")


# FCB-WP009-AIVEN-MANAGED-ROLES-RESTORE-FIX: Aiven's service roles, memberships and role-level
# settings as observed read-only on the production service (run 43870519736 failed on them with
# ROLES_SQL_APPLY_FAILED class=INVALID_SETTING). Values are placeholders; the dotted names are SET-able
# placeholder GUCs on a stock server. avn_wal_sender_stop_timeout exists only in Aiven's build, so it
# is injected into the dumped roles.sql, exactly where pg_dumpall writes it (first line of the role's
# User Config block, sorted by name).
AIVEN_SERVICE_ROLES = (("_aiven", "SUPERUSER LOGIN"), ("_aiven_repl", "LOGIN"), ("_aiven_rewind", "LOGIN"),
                       ("_avnadmin_auditing", "NOLOGIN"), ("_avnadmin_managed", "NOLOGIN"),
                       ("_avnadmin_monitor", "LOGIN"), ("avnadmin_group", "NOLOGIN"))
AIVEN_SERVICE_SETTINGS = (("default_transaction_isolation", "read committed"), ("default_transaction_read_only", "off"),
                          ("pg_stat_monitor.pgsm_track", "none"), ("pg_stat_plans.track", "none"),
                          ("pg_stat_statements.track", "none"), ("pgaudit.log", "none"),
                          ("timescaledb.disable_load", "true"))
AIVEN_MONITOR_SETTINGS = (("extwlist.extensions", "pg_stat_statements"), ("pg_stat_monitor.pgsm_track", "none"),
                          ("pg_stat_plans.track", "none"), ("pg_stat_statements.track", "none"), ("pgaudit.log", "none"))
AVN_ONLY_PARAM = "avn_wal_sender_stop_timeout"


def _aiven_service_roles(env):
    """Create the Aiven service roles, memberships and settings on the test server (cluster-wide:
    removed again by _drop_aiven_service_roles). The bootstrap superuser must be `postgres`."""
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        if conn.execute("SELECT rolname FROM pg_roles WHERE oid = 10").fetchone()[0] != "postgres":
            pytest.skip("the Aiven shape needs a test server whose bootstrap superuser is postgres")
        taken = conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
                             ([r for r, _ in AIVEN_SERVICE_ROLES],)).fetchone()[0]
        preset = conn.execute("SELECT count(*) FROM pg_db_role_setting WHERE setrole = 10 AND setdatabase = 0").fetchone()[0]
        if taken or preset:
            pytest.skip("the test server already has Aiven-like roles or postgres settings")
        env.aiven_service_roles = True
        for role, attrs in AIVEN_SERVICE_ROLES:
            conn.execute(f"CREATE ROLE {role} {attrs}")
        conn.execute("ALTER ROLE _aiven_repl REPLICATION")
        conn.execute(f'GRANT avnadmin_group TO "{env.admin_role}" WITH ADMIN TRUE, INHERIT TRUE, SET TRUE GRANTED BY postgres')
        conn.execute("GRANT _avnadmin_managed TO avnadmin_group GRANTED BY postgres")
        conn.execute("GRANT _avnadmin_auditing, pg_create_subscription, pg_read_all_stats, pg_signal_autovacuum_worker, "
                     "pg_signal_backend, pg_stat_scan_tables TO _avnadmin_managed GRANTED BY postgres")
        conn.execute("GRANT pg_monitor, pg_use_reserved_connections TO _avnadmin_monitor GRANTED BY postgres")
        for role in ("_aiven", "_aiven_repl", "postgres"):
            for param, value in AIVEN_SERVICE_SETTINGS:
                conn.execute(f"ALTER ROLE {role} SET \"{param}\" TO '{value}'")
        for param, value in AIVEN_MONITOR_SETTINGS:
            conn.execute(f"ALTER ROLE _avnadmin_monitor SET \"{param}\" TO '{value}'")
    env.roles_sql_inject = {r: f"ALTER ROLE {r} SET {AVN_ONLY_PARAM} TO '10s';" for r in ("_aiven", "_aiven_repl", "postgres")}


def _drop_aiven_service_roles(env):
    if not getattr(env, "aiven_service_roles", False):
        return
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("ALTER ROLE postgres RESET ALL")  # there was none before (checked above)
        for role, _ in reversed(AIVEN_SERVICE_ROLES):
            conn.execute(f"DROP ROLE IF EXISTS {role}")


def _inject_into_roles_sql(path: Path, lines: dict) -> None:
    """Add `lines[role]` as the first statement of that role's pg_dumpall User Config block."""
    text = path.read_text()
    for role, line in lines.items():
        marker = f"ALTER ROLE {role} SET "
        assert text.count(f'-- User Config "{role}"') == 1 and marker in text, role
        i = text.index(marker)
        text = text[:i] + line + "\n" + text[i:]
    path.write_text(text)


@pytest.fixture(params=["fresh", "phase1", "aiven"])
def source(request, monkeypatch):
    if request.param == "aiven":
        monkeypatch.setattr(rr, "Env", AivenEnv)
    env = rr._create_env()
    monkeypatch.undo()
    try:
        if request.param == "aiven":
            _aiven_monitor_schema(env)
            _aiven_service_roles(env)
        if request.param == "phase1":
            rc, _, err = rr._roles_tx(env)
            assert rc == 0, err
        _seed(env)
        env.kind = request.param
        yield env
    finally:
        rr._destroy_env(env)
        _drop_aiven_service_roles(env)


def _url(env):
    p = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    return f"postgresql://{env.admin_role}:{env.admin_password}@{p.get('host', 'localhost')}:{p.get('port', '5432')}/{env.dbname}"


def _wf_env():
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-backup.yml").read_text(encoding="utf-8"))
    return wf["jobs"]["backup"]["env"]


def _args(env, tmp, **over):
    wf = _wf_env()
    ns = argparse.Namespace(
        workdir=str(tmp / "work"), outdir=str(tmp / "out"), bindir=str(BINDIR), expected_db=env.dbname,
        expected_counts=wf["EXPECTED_COUNTS"], expected_structure=wf["EXPECTED_STRUCTURE_0008"],
        source_commit="0" * 40, run_id="4242", summary=str(tmp / "summary.md"), publish_to=str(tmp / "public.md"),
        restore_port=54329,
        require_tls=False)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _proc_env(env, passphrase=PASSPHRASE):
    out = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "FCB_"))}
    out["FCB_DEFAULTDB_URL"] = _url(env)
    out["FCB_BACKUP_PASSPHRASE"] = passphrase
    return out


class Recorder:
    """Records argv and environment of every subprocess.run of the script."""

    def __init__(self, mod, monkeypatch):
        self.calls = []
        real = subprocess.run

        def fake(argv, *a, **kw):
            self.calls.append((list(argv), dict(kw.get("env") or {})))
            return real(argv, *a, **kw)
        monkeypatch.setattr(mod.subprocess, "run", fake)


def _age_wrapper(tmp):
    """A stand-in for age that records its own argv and environment, then execs the real age."""
    rec = tmp / "age-record.txt"
    wrapper = tmp / "age-recording"
    wrapper.write_text(f"#!/bin/sh\n{{ printf 'ARGV %s\\n' \"$@\"; env; }} >> '{rec}'\nexec {shutil.which('age')} \"$@\"\n")
    wrapper.chmod(0o700)
    return wrapper, rec


def _source_state(env):
    """Superuser view of the source: every table's content hash and the catalog fingerprint."""
    full = env.full_fingerprint()
    with psycopg.connect(rr._dsn_for(env.dbname), autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1")]
        content = {t: conn.execute(f'SELECT md5(coalesce(string_agg(x::text, \'|\' ORDER BY x::text), \'\')) FROM public."{t}" x').fetchone()[0]
                   for t in tables}
        seq = conn.execute("SELECT last_value, is_called FROM public.ingest_runs_id_seq").fetchone()
    return full, content, seq


def _run(mod, env, tmp, capsys, **over):
    inject = getattr(env, "roles_sql_inject", None)
    real = mod._run
    if inject:
        def run_injecting(argv, env_, code, timeout=3600):
            out = real(argv, env_, code, timeout)
            if "--roles-only" in argv:  # what Aiven's pg_dumpall writes and a stock server cannot
                _inject_into_roles_sql(Path(argv[argv.index("--file") + 1]), inject)
            return out
        mod._run = run_injecting
    try:
        rc = mod.run(_args(env, tmp, **over), env=_proc_env(env))
    finally:
        mod._run = real
    out = capsys.readouterr()
    return rc, out.out + out.err, (tmp / "summary.md").read_text(encoding="utf-8") if (tmp / "summary.md").exists() else ""


# ------------------------------------------------------------------ GREEN

def test_backup_restore_verify_encrypt_roundtrip(source, short_tmp, capsys, monkeypatch):
    mod = _module()
    before = _source_state(source)
    rec = Recorder(mod, monkeypatch)
    wrapper, age_rec = _age_wrapper(short_tmp)
    monkeypatch.setattr(mod, "AGE_BIN", str(wrapper))
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    monkeypatch.undo()
    assert rc == 0, out + summary
    assert "## FCB defaultdb encrypted backup: PASS" in summary and "| FAIL |" not in summary
    for check in ("source_guard_catalog_only", "sequences_stable_during_dump", "roles_sql_without_passwords",
                  "roles_names_attributes_match_source", "role_memberships_match_source", "pg_restore_exit_on_error",
                  "restore_catalog_counts", "restore_structure", "restore_schema_migrations_1_8", "restore_functions",
                  "restore_sequences_last_value_is_called", "restore_owners", "restore_acls_normalized",
                  "restore_rows_per_table", "restore_content_per_table", "restore_content_sha256",
                  "database_sql_applied", "restore_database_owner_acl_settings",
                  "decrypt_roundtrip_plaintext_sha256", "tampered_ciphertext_rejected",
                  "plaintext_deleted_before_upload", "artifact_dir_only_ciphertext_and_sha256"):
        assert f"| {check} | PASS |" in summary, check
    # No per-table counts and no schema/role names in the public summary (review #202).
    # The skipped Aiven-managed settings row names allowlisted roles and parameters only (by design,
    # FCB-WP009-AIVEN-MANAGED-ROLES-RESTORE-FIX); nothing else names a schema or a role.
    skipped_row = next(l for l in summary.splitlines()
                       if l.startswith("| roles_sql_aiven_managed_settings_skipped_in_restore_test | PASS |"))
    assert "rows ingest_runs" not in summary and "| 25 " not in summary
    assert "_avnadmin_monitor" not in summary.replace(skipped_row, "")
    if source.kind == "aiven":
        assert skipped_row == ("| roles_sql_aiven_managed_settings_skipped_in_restore_test | PASS | skipped=29 "
                               "_aiven[" + ",".join(sorted(["avn_wal_sender_stop_timeout", *[p for p, _ in AIVEN_SERVICE_SETTINGS]]))
                               + "] _aiven_repl[" + ",".join(sorted(["avn_wal_sender_stop_timeout", *[p for p, _ in AIVEN_SERVICE_SETTINGS]]))
                               + "] _avnadmin_monitor[" + ",".join(sorted(p for p, _ in AIVEN_MONITOR_SETTINGS))
                               + "] postgres[" + ",".join(sorted(["avn_wal_sender_stop_timeout", *[p for p, _ in AIVEN_SERVICE_SETTINGS]]))
                               + "] |")
        assert "'10s'" not in summary and "read committed" not in summary
    else:
        assert skipped_row.endswith("| skipped=0 |")
    assert f"- excluded_schemas: `{1 if source.kind == 'aiven' else 0}`" in summary
    # The summary passes its own leak check and is published (P1: user avnadmin vs _avnadmin_monitor).
    assert (short_tmp / "public.md").read_text() == summary

    # Only the ciphertext and its SHA-256; nothing else was produced or left behind.
    out_dir = short_tmp / "out"
    files = sorted(p.name for p in out_dir.iterdir())
    assert files == ["fcb-defaultdb-4242.tar.age", "fcb-defaultdb-4242.tar.age.sha256"]
    assert not (short_tmp / "work").exists()
    cipher = out_dir / files[0]
    assert cipher.read_bytes().startswith(b"age-encryption.org/v1\n-> scrypt ")
    assert (out_dir / files[1]).read_text() == f"{hashlib.sha256(cipher.read_bytes()).hexdigest()}  {files[0]}\n"
    assert mod.leak_problems(short_tmp / "summary.md", out_dir, _proc_env(source)) == []

    # Real decryption (the recovery path) gives exactly the four package members.
    plain = short_tmp / "plain.tar"
    assert mod.decrypt(cipher, plain, PASSPHRASE)
    assert f"plaintext_sha256: `{hashlib.sha256(plain.read_bytes()).hexdigest()}`" in summary
    with tarfile.open(plain) as tar:
        assert sorted(tar.getnames()) == ["database.sql", "defaultdb.dump", "manifest.json", "roles.sql"]
        manifest = json.loads(tar.extractfile("manifest.json").read())
        roles_sql = tar.extractfile("roles.sql").read().decode()
        dump_head = tar.extractfile("defaultdb.dump").read(5)
    assert dump_head == b"PGDMP" and "PASSWORD" not in roles_sql.upper() and "SCRAM" not in roles_sql
    assert manifest["bridge_state"] == "BRIDGE_0001_0008" and manifest["schema_migrations"] == [str(i) for i in range(1, 9)]
    assert manifest["tables"]["ingest_runs"]["rows"] == 25 and manifest["sequences"]["ingest_runs_id_seq"] == "25:true"
    assert manifest["restore_test"] == "PASS"
    assert any(r.startswith("fcb_app ") for r in manifest["roles"]) == (source.kind == "phase1")
    if source.kind == "aiven":
        # The packaged roles.sql is the complete dump (avn_* lines included); the restore test
        # skipped exactly the allowlisted settings, and the managed roles were still recreated.
        assert roles_sql.count(f"SET {AVN_ONLY_PARAM} TO") == 3 and roles_sql.count('SET "pgaudit.log" TO') == 4
        assert len(manifest["restore_test_skipped_role_settings"]) == 29
        assert "_aiven pgaudit.log" in manifest["restore_test_skipped_role_settings"]
        for role, _ in AIVEN_SERVICE_ROLES:
            assert any(r.startswith(role + " ") for r in manifest["roles"]), role
        assert any(r.startswith("_aiven ") and "pgaudit.log=none" in r for r in manifest["roles"])
        assert "_avnadmin_managed <- avnadmin_group grantor=postgres admin=false inherit=true set=true" in manifest["memberships"]
        assert "avnadmin_group <- avnadmin grantor=postgres admin=true inherit=true set=true" in manifest["memberships"]
    else:
        assert manifest["restore_test_skipped_role_settings"] == []

    # The passphrase never reached argv, a child environment, the output or the summary.
    assert rec.calls, "no subprocess recorded"
    for argv, child in rec.calls:
        assert all(PASSPHRASE not in a for a in argv)
        assert all(PASSPHRASE not in v for v in child.values()) and "FCB_BACKUP_PASSPHRASE" not in child
        assert "FCB_DEFAULTDB_URL" not in child and all(source.admin_password not in a for a in argv)
    recorded_age = age_rec.read_text()
    assert recorded_age.count("ARGV -p") == 1 and recorded_age.count("ARGV -d") >= 2
    assert PASSPHRASE not in recorded_age and "FCB_" not in recorded_age and "PGPASSWORD" not in recorded_age
    assert PASSPHRASE not in out and PASSPHRASE not in summary
    assert source.admin_password not in out and source.admin_password not in summary

    # The source was only read.
    assert _source_state(source) == before


def test_restore_of_the_package_by_hand_follows_the_runbook(source, short_tmp, capsys):
    """The runbook's recovery: age -d, tar x, roles.sql, CREATE DATABASE, pg_restore."""
    if source.kind != "fresh":
        pytest.skip("one state is enough for the manual procedure")
    mod = _module()
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    assert rc == 0, out + summary
    cipher = next((short_tmp / "out").glob("*.tar.age"))
    plain = short_tmp / "p.tar"
    assert mod.decrypt(cipher, plain, PASSPHRASE)
    unpack = short_tmp / "unpack"
    with tarfile.open(plain) as tar:
        tar.extractall(unpack, filter="data")
    cluster = mod.RestoreCluster(BINDIR, short_tmp / "manual", port=54330)
    try:
        cluster.start()
        env = mod.child_env(cluster.params("postgres"))
        subprocess.run([str(BINDIR / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(unpack / "roles.sql")],
                       env=env, check=True, capture_output=True)
        subprocess.run([str(BINDIR / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
                        f'CREATE DATABASE defaultdb TEMPLATE template0 OWNER "{source.admin_role}"'],
                       env=env, check=True, capture_output=True)
        # The runbook's grep of database.sql, renamed to defaultdb (the test source has another name).
        import re as _re
        replay = "\n".join(l.replace(f"DATABASE {source.dbname}", "DATABASE defaultdb")
                            for l in (unpack / "database.sql").read_text().splitlines()
                            if _re.match(r"^(ALTER DATABASE |ALTER ROLE [^ ]+ IN DATABASE |GRANT |REVOKE |COMMENT ON DATABASE )", l))
        assert "work_mem" in replay and "IN DATABASE defaultdb" in replay
        (unpack / "database_replay.sql").write_text(replay + "\n")
        subprocess.run([str(BINDIR / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-1", "-f", str(unpack / "database_replay.sql")],
                       env=env, check=True, capture_output=True)
        subprocess.run([str(BINDIR / "pg_restore"), "--exit-on-error", "--single-transaction", "--no-statistics",
                        "--dbname", "defaultdb", str(unpack / "defaultdb.dump")],
                       env=mod.child_env(cluster.params("defaultdb")), check=True, capture_output=True)
        with cluster.connect("defaultdb") as conn:
            assert conn.execute("SELECT count(*) FROM public.ingest_runs").fetchone()[0] == 25
            assert conn.execute("SELECT count(*) FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase "
                                "WHERE d.datname = 'defaultdb'").fetchone()[0] == 2
    finally:
        cluster.stop()
        cluster.cleanup()


# ------------------------------------------------------------------ negative: encryption

def test_tampered_ciphertext_and_wrong_passphrase_fail_authentication(short_tmp):
    mod = _module()
    plain = short_tmp / "p.bin"
    plain.write_bytes(os.urandom(200_000))
    cipher = short_tmp / "c.age"
    mod.encrypt(plain, cipher, PASSPHRASE)
    assert mod.decrypt(cipher, short_tmp / "ok.bin", PASSPHRASE)
    assert (short_tmp / "ok.bin").read_bytes() == plain.read_bytes()
    # Wrong passphrase.
    assert not mod.decrypt(cipher, short_tmp / "wrong.bin", "W" * 40)
    assert not (short_tmp / "wrong.bin").exists()
    # Tampered payload (last chunk), tampered first payload chunk, tampered header MAC.
    data = cipher.read_bytes()
    header_end = data.index(b"\n---") + 1
    mac_line_end = data.index(b"\n", header_end) + 1
    for offset in (len(data) - 20, mac_line_end + 40, header_end + 6):
        bad = bytearray(data)
        bad[offset] ^= 0x01
        t = short_tmp / "t.age"
        t.write_bytes(bytes(bad))
        assert not mod.decrypt(t, short_tmp / "t.out", PASSPHRASE), offset
        assert not (short_tmp / "t.out").exists()
    # Truncated file.
    (short_tmp / "trunc.age").write_bytes(data[:-100])
    assert not mod.decrypt(short_tmp / "trunc.age", short_tmp / "tr.out", PASSPHRASE)


# ------------------------------------------------------------------ negative: restore verification

def _tamper_restore(mod, monkeypatch, short_tmp, *statements):
    """Run `statements` on the RESTORED database right before it is verified."""
    real = mod.read_evidence

    def tampered(conn):
        if str(conn.info.host).startswith(tempfile.gettempdir()) and conn.info.dbname == "defaultdb":
            for s in statements:
                conn.execute("SET session_replication_role = replica")  # bypass the seal triggers
                conn.execute(s)
        return real(conn)
    monkeypatch.setattr(mod, "read_evidence", tampered)


def _assert_failed_clean(rc, out, summary, short_tmp, *expected_fail):
    assert rc == 1, out
    assert "## FCB defaultdb encrypted backup: FAIL" in summary
    for name in expected_fail:
        assert f"| {name} | FAIL |" in summary, (name, summary)
    assert not (short_tmp / "out").exists() or not any((short_tmp / "out").iterdir())
    assert not (short_tmp / "work").exists()
    assert "| plaintext_deleted_before_upload | PASS |" in summary
    assert "decrypt_roundtrip_plaintext_sha256" not in summary  # nothing was encrypted


@pytest.mark.parametrize("statements, failing", [
    (("DROP TABLE public.runtime_state",), ("restore_table_set", "restore_rows_per_table", "restore_catalog_counts")),
    (("DELETE FROM public.ingest_runs WHERE id = 3",), ("restore_rows_per_table", "restore_content_per_table")),
    (("UPDATE public.oauth_claude_dcr_clients SET redirect_uri = 'https://example.invalid/z' WHERE client_id_hash = 'h1'",),
     ("restore_content_per_table", "restore_content_sha256")),
    (("SELECT pg_catalog.setval('public.ingest_runs_id_seq', 99)",), ("restore_sequences_last_value_is_called",)),
    (("CREATE ROLE fcb_backup_intruder", "ALTER TABLE public.runtime_state OWNER TO fcb_backup_intruder"),
     ("restore_owners",)),
    (("GRANT SELECT ON public.runtime_state TO PUBLIC",), ("restore_acls_normalized",)),
    (("ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC",), ("restore_acls_normalized",)),
    (("ALTER DATABASE defaultdb SET work_mem TO '16MB'",), ("restore_database_owner_acl_settings",)),
    (("GRANT TEMPORARY ON DATABASE defaultdb TO PUBLIC",), ("restore_database_owner_acl_settings",)),
], ids=["missing-table", "row-count", "content", "sequence", "owner", "acl", "default-acl", "db-setting", "db-acl"])
def test_incomplete_or_different_restore_fails_the_job(source, short_tmp, capsys, monkeypatch, statements, failing):
    mod = _module()
    _tamper_restore(mod, monkeypatch, short_tmp, *statements)
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, *failing)


def test_role_attribute_mismatch_after_roles_sql_fails(source, short_tmp, capsys, monkeypatch):
    mod = _module()
    real = mod.read_globals
    calls = []

    def tampered(conn):
        calls.append(1)
        if len(calls) == 2:  # the restore cluster, after roles.sql
            conn.execute(f'ALTER ROLE "{source.admin_role}" NOCREATEROLE')
        return real(conn)
    monkeypatch.setattr(mod, "read_globals", tampered)
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, "roles_names_attributes_match_source")
    assert "pg_restore_exit_on_error" not in summary


@pytest.mark.parametrize("line, category", [
    ("ALTER ROLE x WITH LOGIN PASSWORD 'SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVk:c2VydmVy';", "SCRAM_VERIFIER"),
    ("ALTER ROLE x WITH PASSWORD 'md5" + "a" * 32 + "';", "MD5_HASH"),
], ids=["scram", "md5"])
def test_roles_dump_with_a_password_hash_fails(source, short_tmp, capsys, monkeypatch, line, category):
    mod = _module()
    real = mod._run

    def run(argv, env, code, timeout=3600):
        out = real(argv, env, code, timeout)
        if "--roles-only" in argv:
            path = Path(argv[argv.index("--file") + 1])
            path.write_text(path.read_text() + line + "\n")
        return out
    monkeypatch.setattr(mod, "_run", run)
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, "roles_sql_without_passwords")
    assert category in summary and "SCRAM-SHA-256$" not in summary and "a" * 32 not in summary


def test_source_object_owned_by_another_role_is_refused_before_any_row_is_read(source, short_tmp, capsys, monkeypatch):
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    mod = _module()
    intruder = source.extra_role()
    source.superuser('CREATE TABLE public.planted (x int)', f'ALTER TABLE public.planted OWNER TO "{intruder}"')
    rec = Recorder(mod, monkeypatch)
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, "source_guard_catalog_only")
    assert "PUBLIC_OBJECT_NOT_OWNED_BY_SESSION_USER" in summary
    assert rec.calls and all("--version" in argv for argv, _ in rec.calls)  # no dump, no restore


def test_source_schema_outside_public_is_refused_not_silently_skipped(source, short_tmp, capsys):
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    source.superuser("CREATE SCHEMA extra", as_role=source.admin_role)
    rc, out, summary = _run(_module(), source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, "source_guard_catalog_only")
    assert "UNEXPECTED_SCHEMA" in summary


def test_missing_or_short_passphrase_fails_before_connecting(source, short_tmp, capsys, monkeypatch):
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    mod = _module()
    rec = Recorder(mod, monkeypatch)
    for bad in ("", "x" * 31):
        rc = mod.run(_args(source, short_tmp), env=_proc_env(source, passphrase=bad))
        summary = (short_tmp / "summary.md").read_text()
        assert rc == 1 and ("PASSPHRASE_MISSING" in summary or "PASSPHRASE_TOO_SHORT" in summary)
        assert not (short_tmp / "out").exists() and not (short_tmp / "work").exists()
        (short_tmp / "summary.md").unlink()
    assert rec.calls == []
    capsys.readouterr()


def test_mismatch_lines_name_tables_but_never_print_counts_or_rows(source, short_tmp, capsys, monkeypatch):
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    mod = _module()
    _tamper_restore(mod, monkeypatch, short_tmp, "DELETE FROM public.ingest_runs WHERE id = 3")
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    assert rc == 1 and "mismatched=ingest_runs" in summary and "source=25" not in summary and "src3" not in summary


def test_p2_a_real_restore_failure_prints_a_code_and_class_only(source, short_tmp, capsys, monkeypatch):
    """pg_restore fails on the restored database; only `CODE class=CLASS` reaches the output."""
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    mod = _module()
    real = mod._run

    def run(argv, env, code, timeout=3600):
        if argv[0].endswith("pg_restore") and "--exit-on-error" in argv:
            subprocess.run([str(BINDIR / "psql"), "-X", "-q", "-c",
                            "CREATE TABLE public.runtime_state (secret_marker_col text)"], env=env, check=True,
                           capture_output=True)
        return real(argv, env, code, timeout)
    monkeypatch.setattr(mod, "_run", run)
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    assert rc == 1
    assert out.strip() == "::error::PG_RESTORE_FAILED class=ALREADY_EXISTS", out
    assert "secret_marker_col" not in out + summary and "runtime_state" not in out
    assert "| error | FAIL | PG_RESTORE_FAILED class=ALREADY_EXISTS |" in summary


def test_extension_outside_public_is_refused_by_the_guard(source, short_tmp, capsys):
    if source.kind != "aiven":
        pytest.skip("the Aiven shape only")
    source.superuser("CREATE EXTENSION pgcrypto SCHEMA _avnadmin_monitor")
    rc, out, summary = _run(_module(), source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, "source_guard_catalog_only")
    assert "EXTENSION_OUTSIDE_PUBLIC" in summary



# ------------------------------------------------------------------ PostgreSQL 18.7 / 16.16: pg_attver

def _attver_server():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        v = conn.info.server_version
    return v >= 180007 or 160016 <= v < 170000


def test_column_options_are_restored_and_pg_attver_never_causes_a_false_mismatch(source, short_tmp, capsys):
    """A real column option survives and is compared; the server-managed pg_attver (different on
    source and restore) is ignored, so the run is PASS on 18.6 and 18.7 alike."""
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    source.superuser("ALTER TABLE public.ingest_runs ALTER COLUMN source SET (n_distinct = 5)", as_role=source.admin_role)
    with psycopg.connect(rr._dsn_for(source.dbname)) as conn:
        opts = conn.execute("SELECT attoptions FROM pg_attribute WHERE attrelid = 'public.ingest_runs'::regclass "
                            "AND attname = 'source'").fetchone()[0]
    assert "n_distinct=5" in opts and any(o.startswith("pg_attver=") for o in opts) == _attver_server()
    rc, out, summary = _run(_module(), source, short_tmp, capsys)
    assert rc == 0, out + summary
    assert "| restore_column_options | PASS | options=1 (pg_attver ignored) |" in summary


def test_a_lost_column_option_fails_the_restore_verification(source, short_tmp, capsys, monkeypatch):
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    source.superuser("ALTER TABLE public.ingest_runs ALTER COLUMN source SET (n_distinct = 5)", as_role=source.admin_role)
    mod = _module()
    _tamper_restore(mod, monkeypatch, short_tmp, "ALTER TABLE public.ingest_runs ALTER COLUMN source RESET (n_distinct)")
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, "restore_column_options")


# ------------------------------------------------------------------ Aiven-managed role settings

@pytest.mark.parametrize("extra", [
    "ALTER ROLE avnadmin SET avn_wal_sender_stop_timeout TO '10s';",                           # application role
    "CREATE ROLE _aiven_x;\nALTER ROLE _aiven_x SET avn_wal_sender_stop_timeout TO '10s';",     # look-alike
    "CREATE ROLE _avnadmin_evil;\nALTER ROLE _avnadmin_evil SET avn_wal_sender_stop_timeout TO '10s';",
    "ALTER ROLE _aiven SET avn_unobserved_param TO '1';",                                      # param not allowlisted
    "ALTER ROLE \"_aiven\" SET avn_wal_sender_stop_timeout TO '10s';",                         # not pg_dumpall's form
    "ALTER ROLE _aiven_rewind SET avn_wal_sender_stop_timeout TO '10s';",                      # managed, no observed setting
], ids=["app-role", "aiven-x", "avnadmin-evil", "unlisted-param", "quoted-name", "rewind"])
def test_unknown_settings_outside_the_allowlist_still_fail_closed(source, short_tmp, capsys, extra):
    if source.kind != "aiven":
        pytest.skip("the Aiven shape only")
    mod = _module()
    real = mod._run

    def run(argv, env, code, timeout=3600):
        out = real(argv, env, code, timeout)
        if "--roles-only" in argv:
            path = Path(argv[argv.index("--file") + 1])
            text = path.read_text()
            end = next(l for l in text.splitlines() if l.startswith("\\unrestrict "))
            # inside the \restrict section, where pg_dumpall writes every statement
            path.write_text(text.replace(end + "\n", extra + "\n" + end + "\n", 1))
        return out
    mod._run = run
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp)
    assert out.strip() == "::error::ROLES_SQL_APPLY_FAILED class=INVALID_SETTING", out
    assert "| roles_sql_aiven_managed_settings_skipped_in_restore_test | PASS | skipped=29 " in summary
    assert "10s" not in out + summary


@pytest.mark.parametrize("statement, failing", [
    ("ALTER ROLE _aiven NOSUPERUSER", "roles_names_attributes_match_source"),
    ("ALTER ROLE _aiven_rewind SET \"pgaudit.log\" TO 'all'", "roles_names_attributes_match_source"),
    ("ALTER ROLE _avnadmin_monitor SET \"pgaudit.log.extra\" TO 'all'", "roles_names_attributes_match_source"),
    ("REVOKE _avnadmin_managed FROM avnadmin_group", "role_memberships_match_source"),
    ("DROP ROLE _avnadmin_auditing", "role_memberships_match_source"),
], ids=["managed-attribute", "unlisted-role-setting", "unlisted-param", "managed-membership", "managed-role-dropped"])
def test_managed_roles_stay_in_the_attribute_and_membership_comparison(source, short_tmp, capsys, monkeypatch,
                                                                       statement, failing):
    if source.kind != "aiven":
        pytest.skip("the Aiven shape only")
    mod = _module()
    real = mod.read_globals
    calls = []

    def tampered(conn, *a, **kw):
        calls.append(1)
        if len(calls) == 2:  # the restore cluster, after roles.sql
            conn.execute(statement)
        return real(conn, *a, **kw)
    monkeypatch.setattr(mod, "read_globals", tampered)
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp, failing)
    assert "pg_restore_exit_on_error" not in summary


# ------------------------------------------------------------------ review of PR #204: psql meta-commands

def test_f1_a_meta_command_in_roles_sql_is_refused_before_psql_runs(source, short_tmp, capsys):
    """A `\\!` line inside pg_dumpall's \\restrict section: psql would refuse it with exit code 0 and
    an ignored `error:` line (RED on 302ed83: the run PASSED); now the run fails before psql."""
    if source.kind != "fresh":
        pytest.skip("one state is enough")
    marker = short_tmp / "meta-marker"
    mod = _module()
    real = mod._run

    def run(argv, env, code, timeout=3600):
        out = real(argv, env, code, timeout)
        if "--roles-only" in argv:
            path = Path(argv[argv.index("--file") + 1])
            text = path.read_text()
            line = next(l for l in text.splitlines() if l.startswith("\\restrict "))
            path.write_text(text.replace(line + "\n", f"{line}\n\\! touch {marker}\n", 1))
        return out
    mod._run = run
    rc, out, summary = _run(mod, source, short_tmp, capsys)
    _assert_failed_clean(rc, out, summary, short_tmp)
    assert out.strip() == "::error::ROLES_SQL_UNPARSEABLE", out
    assert "roles_sql_applied" not in summary and not marker.exists()


def test_f4_a_refused_meta_command_fails_apply_roles_sql_on_real_psql(short_tmp):
    mod = _module()
    marker = short_tmp / "meta-marker"
    key = "Ab3" * 21
    script = short_tmp / "roles.sql"
    script.write_text(f"\\restrict {key}\nCREATE ROLE fcb_meta_probe;\n\\! touch {marker}\n\\unrestrict {key}\n")
    cluster = mod.RestoreCluster(BINDIR, short_tmp / "c", port=54331)
    try:
        cluster.start()
        with pytest.raises(mod.BackupError) as exc:
            mod.apply_roles_sql(str(BINDIR / "psql"), script, mod.child_env(cluster.params("postgres")),
                                mod.RESTORE_BOOTSTRAP)
        assert (exc.value.code, exc.value.detail) == ("ROLES_SQL_APPLY_FAILED", "class=META_COMMAND_REFUSED")
        assert not marker.exists()
    finally:
        cluster.stop()
        cluster.cleanup()
