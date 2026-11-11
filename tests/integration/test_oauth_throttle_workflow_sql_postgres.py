"""OAuth persistent throttle: the workflow SQL on REAL PostgreSQL with the real psql client.

A throwaway DATABASE per test (the workflow SQL addresses `public.*`) and throwaway login roles
standing in for fcb_migrator / fcb_app (role names are substituted; roles are cluster-wide and
the WP-009 tests own the real names). Covers: the generated 0001-0009 script + grant file in one
transaction on a database already at 0001-0008 (history kept, second run a no-op, exact
privileges), refusal for a non-migrator identity, and the audited emergency unlock (global /
one key, evidence kept, other buckets untouched).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.sql import SQL, Identifier, Literal  # noqa: E402

from app import migrations  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires TEST_DATABASE_URL"),
    pytest.mark.skipif(shutil.which("psql") is None and os.environ.get("FCB_REQUIRE_PSQL") != "1",
                       reason="psql client not installed"),
]

AIVEN = Path(__file__).resolve().parents[2] / "sql" / "aiven"


def _dsn(base, db, user=None, password=None):
    u = urlsplit(base)
    host = u.hostname + (f":{u.port}" if u.port else "")
    auth = f"{user}:{password}@" if user else (u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else "")
    return urlunsplit((u.scheme, auth + host, "/" + db, u.query, ""))


@pytest.fixture
def env():
    base = os.environ["TEST_DATABASE_URL"]
    tag = uuid.uuid4().hex[:8]
    db, mig, app = f"thrwf_{tag}", f"thr_mig_{tag}", f"thr_app_{tag}"
    pw = uuid.uuid4().hex                      # throwaway test-only login password
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(SQL("CREATE DATABASE {}").format(Identifier(db)))
        conn.execute(SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(Identifier(mig), Literal(pw)))
        conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(app)))
    admin = _dsn(base, db)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(SQL("GRANT ALL ON SCHEMA public TO {}").format(Identifier(mig)))
    mig_dsn = _dsn(base, db, mig, pw)
    # The database is at 0001-0008, owned by the migrator (as after WP-009 phase 2).
    with tempfile.TemporaryDirectory() as tmp:
        for p in sorted(migrations.BRIDGE.directory.glob("000[1-8]_*.sql")):
            shutil.copyfile(p, Path(tmp) / p.name)
        migrations.apply_migrations(lambda: psycopg.connect(mig_dsn, options="-c search_path=public"), tmp)
    with psycopg.connect(admin) as conn:
        conn.execute("INSERT INTO oauth_claude_dcr_clients (client_id_hash, redirect_uri, expires_at) "
                     "VALUES ('pre-existing', 'https://claude.ai/cb', now() + interval '1 day')")
    yield {"admin": admin, "mig": mig_dsn, "mig_role": mig, "app_role": app}
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(db)))
        conn.execute(SQL("DROP ROLE {}").format(Identifier(mig)))
        conn.execute(SQL("DROP ROLE {}").format(Identifier(app)))


def _sql(name, env):
    text = (AIVEN / name).read_text(encoding="utf-8")
    return text.replace("fcb_migrator", env["mig_role"]).replace("fcb_app", env["app_role"])


def _psql(dsn, *args, stdin=None):
    out = subprocess.run(["psql", dsn, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", *args],
                         env={**os.environ, "PGOPTIONS": "-c search_path=public"},
                         input=stdin, capture_output=True, text=True, timeout=120)
    return out.returncode, out.stdout.strip(), out.stderr


def _apply(env, tmp_path):
    grant = tmp_path / "grant.sql"
    grant.write_text(_sql("oauth_throttle_0009_grant.sql", env))
    return _psql(env["mig"], "-1", "-f", str(AIVEN / "aiven_migrations_bridge_0001_0009.sql"), "-f", str(grant))


def _rows(dsn):
    return _psql(dsn, "-c", "select version, name, sha256, applied_at from schema_migrations order by version")[1]


def test_0009_apply_on_an_initialised_database_twice_with_exact_grants(env, tmp_path):
    before = _rows(env["mig"])
    rc, _, err = _apply(env, tmp_path)
    assert rc == 0, err
    first = _rows(env["mig"])
    rc, _, err = _apply(env, tmp_path)
    assert rc == 0, err
    assert _rows(env["mig"]) == first                              # second run is a no-op
    assert first.splitlines()[:8] == before.splitlines()           # 0001-0008 history untouched
    assert first.splitlines()[8].startswith("9|0009_oauth_throttle_buckets.sql|")
    rc, out, _ = _psql(env["admin"], "-c",
                       f"select has_table_privilege('{env['app_role']}', 'public.oauth_throttle_buckets', 'SELECT,INSERT,UPDATE,DELETE'),"
                       f" has_table_privilege('{env['app_role']}', 'public.oauth_throttle_buckets', 'TRUNCATE'),"
                       f" has_table_privilege('{env['app_role']}', 'public.oauth_throttle_unlock_audit', 'SELECT'),"
                       " (select count(*) from oauth_claude_dcr_clients)")
    assert out == "t|f|f|1"                                       # existing OAuth rows survive


def test_0009_apply_refuses_a_non_migrator_identity_and_commits_nothing(env, tmp_path):
    grant = tmp_path / "grant.sql"
    grant.write_text(_sql("oauth_throttle_0009_grant.sql", env))
    rc, _, err = _psql(env["admin"], "-1", "-f", str(AIVEN / "aiven_migrations_bridge_0001_0009.sql"), "-f", str(grant))
    assert rc != 0 and "FCB_THROTTLE_NOT_MIGRATOR" in err
    assert len(_rows(env["mig"]).splitlines()) == 8
    rc, out, _ = _psql(env["admin"], "-c", "select to_regclass('public.oauth_throttle_buckets') is null")
    assert out == "t"


def _unlock(env, tmp_path, scope, target, key, run_id="4242"):
    f = tmp_path / "unlock.sql"
    f.write_text(_sql("oauth_throttle_unlock.sql", env))
    return _psql(env["mig"], "-1", "-v", f"unlock_scope={scope}", "-v", f"unlock_target={target}",
                 "-v", f"unlock_key_hash={key}", "-v", f"github_run_id={run_id}", "-v", "github_actor=owner",
                 "-f", str(f))


def test_unlock_is_scoped_audited_and_keeps_evidence(env, tmp_path):
    rc, _, err = _apply(env, tmp_path)
    assert rc == 0, err
    k1, k2 = "t1:" + "a" * 64, "t1:" + "b" * 64
    rc, _, err = _psql(env["mig"], "-c",
                       "insert into oauth_throttle_buckets (scope, key_hash, tat) values "
                       f"('consent', '*', now() + interval '1 hour'), ('consent', '{k1}', now() + interval '15 min'),"
                       f" ('consent', '{k2}', now() + interval '15 min'), ('registration', '{k1}', now() + interval '1 hour')")
    assert rc == 0, err
    rc, _, err = _unlock(env, tmp_path, "consent", "key", k1, run_id="1")
    assert rc == 0, err
    rc, out, _ = _psql(env["mig"], "-c", "select scope || ':' || key_hash from oauth_throttle_buckets order by 1")
    assert out.splitlines() == ["consent:*", f"consent:{k2}", f"registration:{k1}"]   # only that one bucket
    rc, _, err = _unlock(env, tmp_path, "consent", "global", "*", run_id="2")
    assert rc == 0, err
    rc, out, _ = _psql(env["mig"], "-c",
                       "select github_run_id || '|' || target || '|' || coalesce(removed_key_hash, '-') "
                       "from oauth_throttle_unlock_audit order by audit_id")
    assert out.splitlines() == ["1|key|-", f"1|key|{k1}", "2|global|-", "2|global|*"]
    rc, _, err = _unlock(env, tmp_path, "consent", "key", "t1:" + "c" * 64, run_id="3")   # nothing matches
    assert rc == 0, err
    rc, out, _ = _psql(env["mig"], "-c", "select count(*) from oauth_throttle_unlock_audit where github_run_id = '3'")
    assert out == "1"                                             # the attempt itself is recorded


def test_unlock_refuses_a_non_migrator_and_quotes_its_inputs(env, tmp_path):
    rc, _, err = _apply(env, tmp_path)
    assert rc == 0, err
    f = tmp_path / "unlock.sql"
    f.write_text(_sql("oauth_throttle_unlock.sql", env))
    rc, _, err = _psql(env["admin"], "-1", "-v", "unlock_scope=consent", "-v", "unlock_target=global",
                       "-v", "unlock_key_hash=*", "-v", "github_run_id=9", "-v", "github_actor=x", "-f", str(f))
    assert rc != 0 and "FCB_THROTTLE_NOT_MIGRATOR" in err
    # An injection attempt in a variable is just a literal that matches nothing.
    rc, _, err = _unlock(env, tmp_path, "consent", "key", "x' OR true --")
    assert rc == 0, err
    rc, out, _ = _psql(env["mig"], "-c", "select count(*) from oauth_throttle_unlock_audit where removed_key_hash is not null")
    assert out == "0"
