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


@pytest.fixture(params=["fresh", "phase1"])
def source(request):
    env = rr._create_env()
    try:
        if request.param == "phase1":
            rc, _, err = rr._roles_tx(env)
            assert rc == 0, err
        _seed(env)
        env.kind = request.param
        yield env
    finally:
        rr._destroy_env(env)


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
        source_commit="0" * 40, run_id="4242", summary=str(tmp / "summary.md"), restore_port=54329,
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
    rc = mod.run(_args(env, tmp, **over), env=_proc_env(env))
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
                  "rows ingest_runs", "content ingest_runs", "rows runtime_state", "restore_content_sha256",
                  "decrypt_roundtrip_plaintext_sha256", "tampered_ciphertext_rejected",
                  "plaintext_deleted_before_upload", "artifact_dir_only_ciphertext_and_sha256"):
        assert f"| {check} | PASS |" in summary, check
    assert "source=25 restore=25" in summary

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
        subprocess.run([str(BINDIR / "pg_restore"), "--exit-on-error", "--single-transaction", "--no-statistics",
                        "--dbname", "defaultdb", str(unpack / "defaultdb.dump")],
                       env=mod.child_env(cluster.params("defaultdb")), check=True, capture_output=True)
        with cluster.connect("defaultdb") as conn:
            assert conn.execute("SELECT count(*) FROM public.ingest_runs").fetchone()[0] == 25
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
    (("DROP TABLE public.runtime_state",), ("restore_table_set", "rows runtime_state", "restore_catalog_counts")),
    (("DELETE FROM public.ingest_runs WHERE id = 3",), ("rows ingest_runs", "content ingest_runs")),
    (("UPDATE public.oauth_claude_dcr_clients SET redirect_uri = 'https://example.invalid/z' WHERE client_id_hash = 'h1'",),
     ("content oauth_claude_dcr_clients", "restore_content_sha256")),
    (("SELECT pg_catalog.setval('public.ingest_runs_id_seq', 99)",), ("restore_sequences_last_value_is_called",)),
    (("CREATE ROLE fcb_backup_intruder", "ALTER TABLE public.runtime_state OWNER TO fcb_backup_intruder"),
     ("restore_owners",)),
    (("GRANT SELECT ON public.runtime_state TO PUBLIC",), ("restore_acls_normalized",)),
], ids=["missing-table", "row-count", "content", "sequence", "owner", "acl"])
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
