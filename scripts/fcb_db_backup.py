#!/usr/bin/env python3
"""FCB-WP-009 backup: encrypted, restore-tested backup of the production database `defaultdb`.

Owner-gated: run only by .github/workflows/fcb-aiven-defaultdb-backup.yml (protected Environment
`aiven-production`, owner dispatch from main, exact confirmation phrase). Runbook (Italian):
docs/certification/areas/defaultdb_backup_runbook.md.

Subcommands
  check-passphrase   fail closed unless $FCB_BACKUP_PASSPHRASE is set, 32..1024 characters and has
                     no control character (prints a category only, never the value)
  run                the whole backup (below); the workflow is a thin wrapper around it
  leak-check         scan the step summary and the artifact directory for secrets or plaintext
  sanitize FILE      one redacted line of a client stderr capture (scripts/fcb_db_roles.py sanitize
                     plus the backup passphrase)

`run`, in order (any failure stops it, deletes every plaintext file and exits non-zero):
  1. Source (connection from $FCB_DEFAULTDB_URL, never argv; children get libpq PG* variables):
     a READ ONLY REPEATABLE READ transaction with default_transaction_read_only=on and
     row_security=off exports its snapshot (pg_export_snapshot). Inside that transaction a
     catalog-only guard runs BEFORE any row is read: expected database, PostgreSQL 18, TLS when
     required, no schema outside public except Aiven's _avnadmin_monitor (excluded and reported),
     every object of public owned by the session user, no other non-superuser able to act as it,
     no large object. Then, in the SAME snapshot: pinned object counts and structure fingerprint
     (sql/aiven/count_bridge_catalog.sql, sql/aiven/fingerprint_bridge_structure.sql), the
     schema_migrations rows against the repository, functions, sequences, per-table row counts and
     per-table content hashes. `pg_dump --snapshot=<id> --schema=public --format=custom` (owners and
     ACLs included) runs while the transaction is open, plus `pg_dumpall --roles-only
     --no-role-passwords` (global, not snapshot-bound) and the database properties
     (`pg_dump --create --schema-only --exclude-schema=*`).
  2. Roles SQL and database SQL must contain no PASSWORD clause, SCRAM verifier or md5 hash.
  3. Isolated restore: a throwaway PostgreSQL 18 cluster on a private unix socket (no TCP listener,
     started with a scrubbed environment), the roles SQL applied with ON_ERROR_STOP, every role the
     dump references present, then `pg_restore --exit-on-error --single-transaction` into a
     database named defaultdb. The source connection is closed before the restore starts.
  4. Verification, source snapshot vs restore: counts and structure (also against the pinned
     0001-0008 values), migration rows 1..8, sequences (same last_value; sequences must not move
     during the dump), functions, per-table row counts and content hashes.
  5. Package (dump + roles SQL + database SQL + a non-sensitive manifest) into one deterministic tar,
     encrypt it with age in passphrase mode (scrypt + ChaCha20-Poly1305, authenticated), decrypt it
     again with age and compare the plaintext SHA-256, prove that a tampered copy is rejected, then
     delete every plaintext file (tar, dumps, restore cluster) before the workflow uploads only
     `<name>.tar.age` and `<name>.tar.age.sha256`.

The passphrase is read from the environment by this process only and written to age through a
pseudo-terminal with echo disabled: never argv, never a child's environment, never printed. Output
is PASS/FAIL per check, counts and hashes; never DSNs, credentials, row contents or exception text.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
AIVEN = ROOT / "sql" / "aiven"
COUNT_SQL = AIVEN / "count_bridge_catalog.sql"
STRUCTURE_SQL = AIVEN / "fingerprint_bridge_structure.sql"

URL_VAR = "FCB_DEFAULTDB_URL"
PASSPHRASE_VAR = "FCB_BACKUP_PASSPHRASE"
MIN_PASSPHRASE = 32
MAX_PASSPHRASE = 1024
BRIDGE_STATE = 8
AGE_MAGIC = b"age-encryption.org/v1\n"
RESTORE_DB = "defaultdb"
RESTORE_BOOTSTRAP = "fcb_restore_bootstrap"  # default; the restore uses the SOURCE's bootstrap name
ROLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
BOOTSTRAP_SQL = "SELECT r.rolname::pg_catalog.text FROM pg_catalog.pg_roles r WHERE r.oid OPERATOR(pg_catalog.=) 10::pg_catalog.oid"
ALLOWED_QUERY_KEYS = {"sslmode"}
EXCLUDABLE_SCHEMAS = ("_avnadmin_monitor",)
# Session settings for every verification read, identical on both sides so the text form of each
# row (hence its hash) does not depend on server or role defaults.
READ_GUCS = ("-c search_path=pg_catalog -c default_transaction_read_only=on -c row_security=off "
             "-c TimeZone=UTC -c DateStyle=ISO,YMD -c IntervalStyle=postgres -c extra_float_digits=1 "
             "-c bytea_output=hex -c client_encoding=UTF8 -c statement_timeout=0")
DUMP_GUCS = "-c default_transaction_read_only=on"
PASSWORD_PATTERNS = (
    re.compile(r"\bPASSWORD\b", re.I),
    re.compile(r"SCRAM-SHA-256\$", re.I),
    re.compile(r"\bmd5[0-9a-f]{32}\b", re.I),
)


class BackupError(Exception):
    """A fixed, non-sensitive failure code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------ helpers

def _roles_helper():
    spec = importlib.util.spec_from_file_location("fcb_db_roles_for_backup", ROOT / "scripts" / "fcb_db_roles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sanitize(text: str, env=None) -> str:
    """scripts/fcb_db_roles.py sanitize (URI parts, verifiers, PASSWORD/statement lines) plus the
    backup passphrase."""
    env = os.environ if env is None else env
    out = _roles_helper().sanitize(text, env)
    secret = env.get(PASSPHRASE_VAR, "")
    if len(secret) >= 4:
        out = out.replace(secret, "<redacted>")
    return out


def passphrase_problem(env=None) -> str | None:
    env = os.environ if env is None else env
    value = env.get(PASSPHRASE_VAR)
    if value is None or value == "":
        return "PASSPHRASE_MISSING"
    if len(value) < MIN_PASSPHRASE:
        return "PASSPHRASE_TOO_SHORT"
    if len(value) > MAX_PASSPHRASE:
        return "PASSPHRASE_TOO_LONG"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "PASSPHRASE_CONTROL_CHARACTER"
    if value.strip() != value:
        return "PASSPHRASE_SURROUNDING_WHITESPACE"
    return None


def source_params(env=None, *, expected_db: str, require_tls: bool) -> dict:
    """libpq keywords from $FCB_DEFAULTDB_URL (never from argv). Codes only on failure."""
    env = os.environ if env is None else env
    raw = env.get(URL_VAR, "")
    if not raw:
        raise BackupError("SOURCE_URL_MISSING")
    try:
        u = urlsplit(raw)
        query = parse_qs(u.query, keep_blank_values=True)
        port = u.port
    except Exception:
        raise BackupError("SOURCE_URL_UNPARSEABLE") from None
    if u.scheme not in ("postgres", "postgresql"):
        raise BackupError("SOURCE_URL_SCHEME")
    if "," in u.netloc or not u.hostname or u.netloc.count("@") > 1:
        raise BackupError("SOURCE_URL_HOST")
    if unquote(u.path.lstrip("/")) != expected_db:
        raise BackupError("SOURCE_URL_DATABASE")
    if set(query) - ALLOWED_QUERY_KEYS or any(len(v) != 1 for v in query.values()):
        raise BackupError("SOURCE_URL_QUERY")
    sslmode = query.get("sslmode", [None])[0]
    if require_tls and sslmode != "verify-full":
        raise BackupError("SOURCE_SSLMODE_NOT_VERIFY_FULL")
    if require_tls and env.get("PGSSLMODE") != "verify-full":
        raise BackupError("SOURCE_PGSSLMODE_NOT_VERIFY_FULL")
    params = {"host": u.hostname, "port": str(port or 5432), "user": unquote(u.username or ""),
              "password": unquote(u.password or ""), "dbname": expected_db}
    if sslmode:
        params["sslmode"] = sslmode
    if require_tls:
        if not env.get("PGSSLROOTCERT"):
            raise BackupError("SOURCE_CA_MISSING")
        params["sslrootcert"] = env["PGSSLROOTCERT"]
    return params


_LIBPQ_ENV = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
              "dbname": "PGDATABASE", "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT"}


def child_env(params: dict | None = None, *, options: str = "") -> dict:
    """A scrubbed environment for a client or server child: no FCB_* secret, no passphrase, no
    inherited PG* variable; only the libpq keywords given here."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "HOME": os.environ.get("HOME", "/tmp"), "PGAPPNAME": "fcb-backup",
           "PGCONNECT_TIMEOUT": os.environ.get("PGCONNECT_TIMEOUT", "15"), "TZ": "UTC"}
    for key, value in (params or {}).items():
        env[_LIBPQ_ENV[key]] = value
    if options:
        env["PGOPTIONS"] = options
    return env


def _run(argv: list[str], env: dict, code: str, timeout: int = 3600) -> str:
    proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise BackupError(code, sanitize(proc.stderr or "") or "<no message>")
    return proc.stdout


def tool_major(bindir: Path, tool: str) -> int:
    out = _run([str(bindir / tool), "--version"], child_env(), "CLIENT_VERSION")
    m = re.search(r"\b(\d+)(?:\.\d+)?", out)
    if not m:
        raise BackupError("CLIENT_VERSION")
    return int(m.group(1))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def password_findings(text: str) -> list[str]:
    """Categories of password material found in a roles/database SQL file (empty = clean)."""
    found = []
    for pattern, name in zip(PASSWORD_PATTERNS, ("PASSWORD_CLAUSE", "SCRAM_VERIFIER", "MD5_HASH")):
        if pattern.search(text):
            found.append(name)
    return found


# ------------------------------------------------------------------ catalog / evidence SQL

GUARD_SQL = """
SELECT pg_catalog.current_database()::pg_catalog.text AS db,
       pg_catalog.current_setting('server_version_num')::pg_catalog.int4 AS ver,
       COALESCE((SELECT s.ssl FROM pg_catalog.pg_stat_ssl s
                 WHERE s.pid OPERATOR(pg_catalog.=) pg_catalog.pg_backend_pid()), false) AS ssl,
       (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER) AS superuser,
       (CURRENT_USER OPERATOR(pg_catalog.=) SESSION_USER) AS same_user,
       pg_catalog.current_setting('transaction_read_only') AS read_only,
       pg_catalog.current_setting('transaction_isolation') AS isolation,
       (SELECT pg_catalog.array_agg(n.nspname::pg_catalog.text ORDER BY n.nspname) FROM pg_catalog.pg_namespace n
         WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['public', 'pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[])
           AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text) AS other_schemas,
       (SELECT pg_catalog.array_agg(n.nspname::pg_catalog.text ORDER BY n.nspname) FROM pg_catalog.pg_namespace n
         WHERE n.nspowner OPERATOR(pg_catalog.=) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER)
           AND n.nspname OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name) AS own_other_schemas,
       ((SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
          WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
            AND c.relowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER))
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
          WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
            AND p.proowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER))
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace
          WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
            AND t.typowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER))
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_operator o JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) o.oprnamespace
          WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
            AND o.oprowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER))
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_collation k JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) k.collnamespace
          WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
            AND k.collowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER))
      OPERATOR(pg_catalog.+) (SELECT pg_catalog.count(*) FROM pg_catalog.pg_statistic_ext x JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) x.stxnamespace
          WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
            AND x.stxowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER))
       ) AS foreign_owned,
       (SELECT pg_catalog.count(*) FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) m.member
         WHERE m.roleid OPERATOR(pg_catalog.=) (SELECT r2.oid FROM pg_catalog.pg_roles r2 WHERE r2.rolname OPERATOR(pg_catalog.=) SESSION_USER)
           AND NOT r.rolsuper) AS acting_members,
       (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata) AS large_objects,
       (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
         WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relforcerowsecurity) AS forced_rls
"""

TABLES_SQL = """
SELECT c.relname::pg_catalog.text FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
 WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
 ORDER BY 1
"""

SEQUENCES_SQL = """
SELECT c.relname::pg_catalog.text FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
 WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relkind OPERATOR(pg_catalog.=) 'S'::pg_catalog."char"
 ORDER BY 1
"""

FUNCTIONS_SQL = """
SELECT p.oid::pg_catalog.regprocedure::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
       OPERATOR(pg_catalog.||) pg_catalog.md5(pg_catalog.pg_get_functiondef(p.oid))
  FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
 WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND p.prokind OPERATOR(pg_catalog.<>) 'a'::pg_catalog."char"
 ORDER BY 1
"""

REFERENCED_ROLES_SQL = """
WITH acl(a) AS (
  SELECT c.relacl FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND c.relacl IS NOT NULL
  UNION ALL SELECT a.attacl FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) a.attrelid
   JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND a.attacl IS NOT NULL
  UNION ALL SELECT p.proacl FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND p.proacl IS NOT NULL
  UNION ALL SELECT t.typacl FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) t.typnamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND t.typacl IS NOT NULL
  UNION ALL SELECT n.nspacl FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND n.nspacl IS NOT NULL
  UNION ALL SELECT d.defaclacl FROM pg_catalog.pg_default_acl d
), oids(o) AS (
  SELECT e.grantee FROM acl, LATERAL pg_catalog.aclexplode(acl.a) e
  UNION SELECT e.grantor FROM acl, LATERAL pg_catalog.aclexplode(acl.a) e
  UNION SELECT d.defaclrole FROM pg_catalog.pg_default_acl d
  UNION SELECT c.relowner FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
  UNION SELECT p.proowner FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
  UNION SELECT d.datdba FROM pg_catalog.pg_database d WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()
)
SELECT r.rolname::pg_catalog.text FROM oids JOIN pg_catalog.pg_roles r ON r.oid OPERATOR(pg_catalog.=) oids.o
 WHERE r.rolname OPERATOR(pg_catalog.!~) '^pg_'::pg_catalog.text ORDER BY 1
"""


# Owner of every table, sequence, view and function of public (role names, not OIDs).
OWNERS_SQL = """
SELECT 'rel '::pg_catalog.text OPERATOR(pg_catalog.||) c.relkind::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
       OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) ' '::pg_catalog.text
       OPERATOR(pg_catalog.||) pg_catalog.pg_get_userbyid(c.relowner)::pg_catalog.text
  FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
 WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
   AND c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'S', 'v', 'm', 'f']::pg_catalog."char"[])
UNION ALL
SELECT 'fn '::pg_catalog.text OPERATOR(pg_catalog.||) p.oid::pg_catalog.regprocedure::pg_catalog.text
       OPERATOR(pg_catalog.||) ' '::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_userbyid(p.proowner)::pg_catalog.text
  FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
 WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
ORDER BY 1
"""

# Normalized ACLs: NULL replaced by the built-in default for the object kind (acldefault), each
# ACL exploded to grantor>grantee:privilege:grantable with role names, sorted.
ACLS_SQL = """
WITH obj(k, acl, owner) AS (
  SELECT 'rel '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text,
         COALESCE(c.relacl, pg_catalog.acldefault(CASE WHEN c.relkind OPERATOR(pg_catalog.=) 'S'::pg_catalog."char"
                                                       THEN 's'::pg_catalog."char" ELSE 'r'::pg_catalog."char" END, c.relowner)),
         c.relowner
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
     AND c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p', 'S', 'v', 'm', 'f']::pg_catalog."char"[])
  UNION ALL
  SELECT 'col '::pg_catalog.text OPERATOR(pg_catalog.||) c.relname::pg_catalog.text OPERATOR(pg_catalog.||) '.'::pg_catalog.text
         OPERATOR(pg_catalog.||) a.attname::pg_catalog.text, a.attacl, c.relowner
    FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) a.attrelid
    JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name AND a.attacl IS NOT NULL
  UNION ALL
  SELECT 'fn '::pg_catalog.text OPERATOR(pg_catalog.||) p.oid::pg_catalog.regprocedure::pg_catalog.text,
         COALESCE(p.proacl, pg_catalog.acldefault('f'::pg_catalog."char", p.proowner)), p.proowner
    FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace
   WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
  UNION ALL
  SELECT 'schema public'::pg_catalog.text, COALESCE(n.nspacl, pg_catalog.acldefault('n'::pg_catalog."char", n.nspowner)), n.nspowner
    FROM pg_catalog.pg_namespace n WHERE n.nspname OPERATOR(pg_catalog.=) 'public'::pg_catalog.name
)
SELECT obj.k,
       CASE WHEN e.grantor IS NULL THEN NULL
            ELSE CASE WHEN e.grantor OPERATOR(pg_catalog.=) 0::pg_catalog.oid THEN 'PUBLIC'::pg_catalog.text
                      ELSE pg_catalog.pg_get_userbyid(e.grantor)::pg_catalog.text END
                 OPERATOR(pg_catalog.||) '>'::pg_catalog.text OPERATOR(pg_catalog.||)
                 CASE WHEN e.grantee OPERATOR(pg_catalog.=) 0::pg_catalog.oid THEN 'PUBLIC'::pg_catalog.text
                      ELSE pg_catalog.pg_get_userbyid(e.grantee)::pg_catalog.text END
                 OPERATOR(pg_catalog.||) ':'::pg_catalog.text OPERATOR(pg_catalog.||) e.privilege_type
                 OPERATOR(pg_catalog.||) CASE WHEN e.is_grantable THEN '*'::pg_catalog.text ELSE ''::pg_catalog.text END
       END
  FROM obj LEFT JOIN LATERAL pg_catalog.aclexplode(obj.acl) e ON true
"""


def read_acls(conn) -> list[str]:
    """One sorted line per object: `<kind name> <grantor>grantee:privilege[*]>,...`."""
    by_obj: dict[str, list[str]] = {}
    for k, item in conn.execute(ACLS_SQL).fetchall():
        by_obj.setdefault(k, [])
        if item is not None:
            by_obj[k].append(item)
    return [f"{k} {','.join(sorted(v)) or '-'}" for k, v in sorted(by_obj.items())]

# Role names, attributes and settings (no password column: rolpassword is never read), and
# memberships with their options and grantor. Predefined pg_* roles have no attributes to compare.
ROLES_SQL = """
SELECT r.rolname::pg_catalog.text OPERATOR(pg_catalog.||) ' super='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolsuper::pg_catalog.text
       OPERATOR(pg_catalog.||) ' inherit='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolinherit::pg_catalog.text
       OPERATOR(pg_catalog.||) ' createrole='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolcreaterole::pg_catalog.text
       OPERATOR(pg_catalog.||) ' createdb='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolcreatedb::pg_catalog.text
       OPERATOR(pg_catalog.||) ' login='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolcanlogin::pg_catalog.text
       OPERATOR(pg_catalog.||) ' replication='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolreplication::pg_catalog.text
       OPERATOR(pg_catalog.||) ' bypassrls='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolbypassrls::pg_catalog.text
       OPERATOR(pg_catalog.||) ' connlimit='::pg_catalog.text OPERATOR(pg_catalog.||) r.rolconnlimit::pg_catalog.text
       OPERATOR(pg_catalog.||) ' validuntil='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(r.rolvaliduntil::pg_catalog.text, '-'::pg_catalog.text)
       OPERATOR(pg_catalog.||) ' config='::pg_catalog.text OPERATOR(pg_catalog.||) COALESCE(r.rolconfig::pg_catalog.text, '-'::pg_catalog.text)
  FROM pg_catalog.pg_roles r
 WHERE r.rolname OPERATOR(pg_catalog.!~) '^pg_'::pg_catalog.text AND r.rolname OPERATOR(pg_catalog.<>) %(bootstrap)s
 ORDER BY 1
"""

MEMBERSHIPS_SQL = """
SELECT pg_catalog.pg_get_userbyid(m.roleid)::pg_catalog.text OPERATOR(pg_catalog.||) ' <- '::pg_catalog.text
       OPERATOR(pg_catalog.||) pg_catalog.pg_get_userbyid(m.member)::pg_catalog.text
       OPERATOR(pg_catalog.||) ' grantor='::pg_catalog.text OPERATOR(pg_catalog.||) pg_catalog.pg_get_userbyid(m.grantor)::pg_catalog.text
       OPERATOR(pg_catalog.||) ' admin='::pg_catalog.text OPERATOR(pg_catalog.||) m.admin_option::pg_catalog.text
       OPERATOR(pg_catalog.||) ' inherit='::pg_catalog.text OPERATOR(pg_catalog.||) m.inherit_option::pg_catalog.text
       OPERATOR(pg_catalog.||) ' set='::pg_catalog.text OPERATOR(pg_catalog.||) m.set_option::pg_catalog.text
  FROM pg_catalog.pg_auth_members m
 WHERE pg_catalog.pg_get_userbyid(m.member) OPERATOR(pg_catalog.<>) %(bootstrap)s
   AND pg_catalog.pg_get_userbyid(m.roleid) OPERATOR(pg_catalog.<>) %(bootstrap)s
   -- built-in memberships between predefined roles (initdb's, e.g. pg_monitor): not dumped, not compared
   AND NOT (pg_catalog.pg_get_userbyid(m.member) OPERATOR(pg_catalog.~) '^pg_'::pg_catalog.text
            AND pg_catalog.pg_get_userbyid(m.roleid) OPERATOR(pg_catalog.~) '^pg_'::pg_catalog.text)
 ORDER BY 1
"""


def read_globals(conn, exclude: str = "") -> dict:
    """Role names/attributes and memberships of the cluster (`exclude`: a role left out, none by default)."""
    roles = [r[0] for r in conn.execute(ROLES_SQL, {"bootstrap": exclude}).fetchall()]
    members = [r[0] for r in conn.execute(MEMBERSHIPS_SQL, {"bootstrap": exclude}).fetchall()]
    return {"roles": roles, "memberships": members,
            "roles_sha256": hashlib.sha256("\n".join(roles + ["--"] + members).encode()).hexdigest()}


def _table_hash_sql(table: str):
    from psycopg import sql
    # Per row: SHA-256 of its text form (fixed session settings); per table: SHA-256 of the
    # sorted row hashes (a multiset hash: order- and collation-independent, duplicates kept).
    return sql.SQL(
        "SELECT pg_catalog.count(*), pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
        "COALESCE(pg_catalog.string_agg(h.x, ''::pg_catalog.text ORDER BY h.x COLLATE \"C\"), ''::pg_catalog.text), "
        "'UTF8'::pg_catalog.name)), 'hex'::pg_catalog.text) FROM (SELECT pg_catalog.encode(pg_catalog.sha256("
        "pg_catalog.convert_to(t::pg_catalog.text, 'UTF8'::pg_catalog.name)), 'hex'::pg_catalog.text) AS x "
        "FROM ONLY {}.{} t) h").format(sql.Identifier("public"), sql.Identifier(table))


def _sequence_sql(seq: str):
    from psycopg import sql
    return sql.SQL("SELECT s.last_value, s.is_called FROM {}.{} s").format(sql.Identifier("public"), sql.Identifier(seq))


def read_sequences(conn) -> dict:
    out = {}
    for (seq,) in conn.execute(SEQUENCES_SQL).fetchall():
        last, called = conn.execute(_sequence_sql(seq)).fetchone()
        out[seq] = f"{last}:{str(called).lower()}"
    return out


def read_evidence(conn) -> dict:
    """Everything compared between the source snapshot and the restore (counts and hashes only)."""
    catalog = conn.execute(COUNT_SQL.read_text(encoding="utf-8")).fetchone()[0]
    structure = conn.execute(STRUCTURE_SQL.read_text(encoding="utf-8")).fetchone()[0]
    migrations = ["=".join(str(v) for v in row) for row in conn.execute(
        "SELECT m.version, m.name, m.sha256 FROM public.schema_migrations m ORDER BY m.version").fetchall()]
    functions = [r[0] for r in conn.execute(FUNCTIONS_SQL).fetchall()]
    owners = [r[0] for r in conn.execute(OWNERS_SQL).fetchall()]
    acls = read_acls(conn)
    tables = {}
    for (table,) in conn.execute(TABLES_SQL).fetchall():
        rows, digest = conn.execute(_table_hash_sql(table)).fetchone()
        tables[table] = {"rows": int(rows), "sha256": digest}
    return {
        "catalog_counts": catalog,
        "structure": structure,
        "migrations": migrations,
        "functions": functions,
        "functions_sha256": hashlib.sha256("\n".join(functions).encode()).hexdigest(),
        "owners": owners,
        "owners_sha256": hashlib.sha256("\n".join(owners).encode()).hexdigest(),
        "acls": acls,
        "acls_sha256": hashlib.sha256("\n".join(acls).encode()).hexdigest(),
        "sequences": read_sequences(conn),
        "tables": tables,
        "content_sha256": hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest(),
    }


def check_guard(row: dict, *, expected_db: str, require_tls: bool) -> tuple[list[str], list[str]]:
    """(problems, excluded schemas) of the catalog-only source guard."""
    problems = []
    if row["db"] != expected_db:
        problems.append("WRONG_DATABASE")
    if not 180000 <= int(row["ver"]) < 190000:
        problems.append("SERVER_NOT_POSTGRESQL_18")
    if require_tls and not row["ssl"]:
        problems.append("SOURCE_NOT_TLS")
    if not row["same_user"]:
        problems.append("CURRENT_USER_IS_NOT_SESSION_USER")
    if row["read_only"] != "on":
        problems.append("TRANSACTION_NOT_READ_ONLY")
    if row["isolation"] not in ("repeatable read", "serializable"):
        problems.append("TRANSACTION_NOT_SNAPSHOT")
    others = list(row["other_schemas"] or [])
    excluded = [s for s in others if s in EXCLUDABLE_SCHEMAS and s not in (row["own_other_schemas"] or [])]
    if set(others) - set(excluded):
        problems.append("UNEXPECTED_SCHEMA")  # not dumped by --schema=public: refuse, never truncate silently
    if row["foreign_owned"]:
        problems.append("PUBLIC_OBJECT_NOT_OWNED_BY_SESSION_USER")
    if row["acting_members"]:
        problems.append("ANOTHER_ROLE_CAN_ACT_AS_SESSION_USER")
    if row["large_objects"]:
        problems.append("LARGE_OBJECTS_PRESENT")
    if row["forced_rls"]:
        problems.append("FORCED_ROW_LEVEL_SECURITY")
    return problems, excluded


# ------------------------------------------------------------------ age (passphrase mode, via a pty)

_PROMPT = re.compile(rb"(Enter|Confirm) passphrase")


def _age(argv: list[str], passphrase: str, prompts: int, timeout: float = 600.0) -> int:
    """Run age with a pseudo-terminal as its controlling terminal (age reads passphrases only from
    the terminal). Echo is disabled on the pty before anything is written; the passphrase goes only
    to the pty, once per expected prompt. The child gets PATH only. age's output is discarded."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execve(argv[0], argv, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        finally:
            os._exit(127)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        buf, sent, deadline = b"", 0, time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if fd not in ready:
                done, _status = os.waitpid(pid, os.WNOHANG)
                if done:
                    return os.waitstatus_to_exitcode(_status)
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf = (buf + chunk)[-4096:]
            seen = len(_PROMPT.findall(buf))
            while sent < prompts and seen > 0:
                os.write(fd, passphrase.encode("utf-8") + b"\n")
                sent += 1
                seen -= 1
                buf = b""
        else:
            os.kill(pid, 9)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


AGE_BIN: str | None = None  # tests only (a recording stand-in); the workflow uses the installed age


def age_binary() -> str:
    path = AGE_BIN or shutil.which("age", path="/usr/bin:/bin:/usr/local/bin")
    if not path:
        raise BackupError("AGE_NOT_INSTALLED")
    return path


def age_version() -> str:
    out = _run([age_binary(), "--version"], child_env(), "AGE_VERSION")
    version = out.strip().splitlines()[0] if out.strip() else ""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\S*", version)
    if not m or (int(m.group(1)), int(m.group(2))) < (1, 1):
        raise BackupError("AGE_VERSION_UNSUPPORTED")
    return version


def encrypt(plain: Path, out: Path, passphrase: str) -> None:
    if out.exists():
        out.unlink()
    rc = _age([age_binary(), "-p", "-o", str(out), str(plain)], passphrase, prompts=2)
    if rc != 0 or not out.exists():
        raise BackupError("ENCRYPT_FAILED")
    with open(out, "rb") as f:
        head = f.read(256)
    if not head.startswith(AGE_MAGIC) or b"\n-> scrypt " not in head:
        raise BackupError("ENCRYPT_NOT_AGE_SCRYPT")


def decrypt(cipher: Path, out: Path, passphrase: str) -> bool:
    """True when age authenticated and decrypted the file (a wrong passphrase, a tampered header or
    a tampered payload chunk all fail authentication)."""
    if out.exists():
        out.unlink()
    rc = _age([age_binary(), "-d", "-o", str(out), str(cipher)], passphrase, prompts=1)
    if rc != 0:
        if out.exists():
            out.unlink()
        return False
    return out.exists()


def tamper_copy(cipher: Path, dest: Path, offset_from_end: int = 40) -> None:
    data = bytearray(cipher.read_bytes())
    data[len(data) - offset_from_end] ^= 0x01
    dest.write_bytes(bytes(data))


# ------------------------------------------------------------------ isolated restore cluster

class RestoreCluster:
    """A throwaway PostgreSQL cluster: unix socket in a private directory, no TCP listener, trust
    only for the local socket, started with a scrubbed environment (no secret reaches the server)."""

    def __init__(self, bindir: Path, root: Path, port: int = 54329, bootstrap: str = RESTORE_BOOTSTRAP):
        if not ROLE_NAME_RE.fullmatch(bootstrap):
            raise BackupError("BOOTSTRAP_ROLE_NAME_UNSUPPORTED")
        self.bindir, self.root, self.port, self.bootstrap = bindir, root, port, bootstrap
        self.data = root / "pgdata"
        self.sock: Path | None = None

    def start(self) -> None:
        # Short private socket directory (unix socket paths are limited to ~107 bytes).
        self.sock = Path(tempfile.mkdtemp(prefix="fcb-restore-sock-"))
        os.chmod(self.sock, 0o700)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = child_env()
        _run([str(self.bindir / "initdb"), "-D", str(self.data), "-U", self.bootstrap, "--auth=trust",
              "-E", "UTF8", "--locale=C.UTF-8", "--no-instructions"], env, "RESTORE_INITDB_FAILED", timeout=300)
        opts = (f"-c listen_addresses='' -c unix_socket_directories={self.sock} -c port={self.port} "
                "-c fsync=off -c full_page_writes=off -c synchronous_commit=off")
        _run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "-w", "-t", "120", "-o", opts,
              "-l", str(self.root / "server.log"), "start"], env, "RESTORE_SERVER_START_FAILED", timeout=180)

    def stop(self) -> None:
        if (self.data / "postmaster.pid").exists():
            subprocess.run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "-m", "immediate", "-w", "stop"],
                           env=child_env(), capture_output=True, timeout=120)

    def cleanup(self) -> None:
        if self.sock is not None:
            shutil.rmtree(self.sock, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def params(self, dbname: str) -> dict:
        return {"host": str(self.sock), "port": str(self.port), "user": self.bootstrap, "dbname": dbname}

    def connect(self, dbname: str):
        import psycopg
        return psycopg.connect(**self.params(dbname), options=READ_GUCS.replace(
            "-c default_transaction_read_only=on ", ""), autocommit=True)


# ------------------------------------------------------------------ run

class Checks:
    def __init__(self):
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, info: str = "") -> bool:
        self.items.append((name, bool(ok), info))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(ok for _, ok, _ in self.items)


def _compare(checks: Checks, src: dict, dst: dict, expected_catalog: str, expected_structure: str,
             expected_rows: list[str]) -> None:
    checks.add("source_catalog_counts_pinned_0008", src["catalog_counts"] == expected_catalog, src["catalog_counts"])
    checks.add("source_structure_pinned_0008", src["structure"] == expected_structure, src["structure"])
    checks.add("source_schema_migrations_1_8", src["migrations"] == expected_rows, f"rows={len(src['migrations'])}")
    checks.add("restore_catalog_counts", dst["catalog_counts"] == src["catalog_counts"] == expected_catalog,
               dst["catalog_counts"])
    checks.add("restore_structure", dst["structure"] == src["structure"] == expected_structure, dst["structure"])
    checks.add("restore_schema_migrations_1_8", dst["migrations"] == src["migrations"] == expected_rows,
               f"rows={len(dst['migrations'])}")
    checks.add("restore_functions", dst["functions"] == src["functions"],
               f"functions={len(dst['functions'])} sha256={dst['functions_sha256']}")
    checks.add("restore_sequences_last_value_is_called", dst["sequences"] == src["sequences"],
               f"sequences={len(dst['sequences'])}")
    checks.add("restore_owners", dst["owners"] == src["owners"],
               f"objects={len(dst['owners'])} sha256={dst['owners_sha256']}")
    checks.add("restore_acls_normalized", dst["acls"] == src["acls"],
               f"objects={len(dst['acls'])} sha256={dst['acls_sha256']}")
    checks.add("restore_table_set", sorted(dst["tables"]) == sorted(src["tables"]),
               f"tables={len(dst['tables'])}")
    for table in sorted(set(src["tables"]) | set(dst["tables"])):
        s, d = src["tables"].get(table), dst["tables"].get(table)
        checks.add(f"rows {table}", s is not None and d is not None and s["rows"] == d["rows"],
                   f"source={s['rows'] if s else 'missing'} restore={d['rows'] if d else 'missing'}")
        checks.add(f"content {table}", s is not None and d is not None and s["sha256"] == d["sha256"])
    checks.add("restore_content_sha256", dst["content_sha256"] == src["content_sha256"], dst["content_sha256"])


def _write_tar(dest: Path, members: dict[str, Path], mtime: int) -> None:
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(members):
            path = members[name]
            info = tarfile.TarInfo(name)
            info.size = path.stat().st_size
            info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = mtime, 0o600, 0, 0, "", ""
            with open(path, "rb") as f:
                tar.addfile(info, f)


def _write_summary(path: str | None, checks: Checks, facts: dict) -> None:
    lines = ["## FCB defaultdb encrypted backup: " + ("PASS" if checks.ok else "FAIL"), "",
             "| check | result | evidence (counts/hashes only) |", "|---|---|---|"]
    for name, ok, info in checks.items:
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {info} |")
    lines.append("")
    for key in ("encrypted_file", "encrypted_sha256", "encrypted_bytes", "plaintext_sha256", "plaintext_bytes",
                "excluded_schemas", "pg_dump", "server_version_num", "age", "source_commit", "created_at_utc"):
        if key in facts:
            lines.append(f"- {key}: `{facts[key]}`")
    text = "\n".join(lines) + "\n"
    if path:  # a private file: the workflow publishes it only after the leak check
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


def run(args, env=None) -> int:
    import psycopg
    from psycopg.rows import dict_row, tuple_row

    env = os.environ if env is None else env
    checks, facts = Checks(), {}
    workdir = Path(args.workdir)
    outdir = Path(args.outdir)
    bindir = Path(args.bindir)
    cluster = None
    success = False
    try:
        problem = passphrase_problem(env)
        if problem:
            raise BackupError(problem)
        passphrase = env[PASSPHRASE_VAR]
        facts["age"] = age_version()
        for tool in ("pg_dump", "pg_dumpall", "pg_restore", "psql", "initdb", "pg_ctl"):
            if tool_major(bindir, tool) != 18:
                raise BackupError("CLIENT_NOT_POSTGRESQL_18")
        workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
        outdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(outdir.iterdir()):
            raise BackupError("OUTDIR_NOT_EMPTY")
        expected_rows = _roles_helper().expected_migrations(BRIDGE_STATE)
        expected_catalog = " ".join(c for c in args.expected_counts.split()
                                    if not c.startswith(("constraints_information_schema=", "schema_migrations_rows=")))
        if f"schema_migrations_rows={BRIDGE_STATE}" not in args.expected_counts.split():
            raise BackupError("EXPECTED_COUNTS_NOT_0008")

        params = source_params(env, expected_db=args.expected_db, require_tls=args.require_tls)
        dump = workdir / "defaultdb.dump"
        roles_sql = workdir / "roles.sql"
        database_sql = workdir / "database.sql"
        bin_ = lambda t: str(bindir / t)  # noqa: E731
        dump_env = child_env(params, options=DUMP_GUCS)

        # ---- 1. source: one READ ONLY snapshot transaction, guard first, dump inside it.
        try:
            conn = psycopg.connect(**params, options=READ_GUCS, row_factory=dict_row, autocommit=False,
                                   application_name="fcb-backup")
        except psycopg.Error:
            raise BackupError("SOURCE_CONNECT_FAILED") from None
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_catalog.pg_export_snapshot() AS s").fetchone()["s"]
            guard = conn.execute(GUARD_SQL).fetchone()
            problems, excluded = check_guard(guard, expected_db=args.expected_db, require_tls=args.require_tls)
            facts["server_version_num"] = guard["ver"]
            facts["excluded_schemas"] = ",".join(excluded) or "none"
            if not checks.add("source_guard_catalog_only", not problems, ",".join(problems) or "ok"):
                raise BackupError("SOURCE_GUARD_FAILED", ",".join(problems))
            conn.row_factory = tuple_row
            referenced_roles = [r[0] for r in conn.execute(REFERENCED_ROLES_SQL).fetchall()]
            src_globals = read_globals(conn)
            bootstrap = conn.execute(BOOTSTRAP_SQL).fetchone()[0]
            seq_before = read_sequences(conn)
            # The whole database (public's ACL relative to the default, as pg_dump does), minus the
            # schemas the guard admitted as Aiven-managed and excluded (reported); any other schema
            # was refused above.
            dump_argv = [bin_("pg_dump"), "--format=custom", f"--snapshot={snapshot}", "--no-statistics",
                         "--lock-wait-timeout=60s", "--no-password", "--file", str(dump),
                         *[f"--exclude-schema={s}" for s in excluded]]
            _run(dump_argv, dump_env, "PG_DUMP_FAILED")
            src = read_evidence(conn)  # same snapshot as the dump (sequences: see below)
            seq_after = read_sequences(conn)
            if not checks.add("sequences_stable_during_dump", seq_before == seq_after == src["sequences"],
                              f"sequences={len(seq_after)}"):
                raise BackupError("SEQUENCE_MOVED_DURING_DUMP")
            _run([bin_("pg_dump"), "--create", "--schema-only", "--exclude-schema=*", "--no-statistics",
                  f"--snapshot={snapshot}", "--no-password", "--file", str(database_sql)], dump_env,
                 "PG_DUMP_DATABASE_FAILED")
            conn.rollback()
        finally:
            conn.close()
        _run([bin_("pg_dumpall"), "--roles-only", "--no-role-passwords", "-l", args.expected_db, "--no-password",
              "--file", str(roles_sql)], dump_env, "PG_DUMPALL_ROLES_FAILED")
        facts["pg_dump"] = _run([bin_("pg_dump"), "--version"], child_env(), "CLIENT_VERSION").strip()
        checks.add("pg_dump_snapshot_custom_format", dump.read_bytes()[:5] == b"PGDMP", f"bytes={dump.stat().st_size}")

        # ---- 2. no password material in the globals.
        for label, path in (("roles_sql_without_passwords", roles_sql), ("database_sql_without_passwords", database_sql)):
            found = password_findings(path.read_text(encoding="utf-8", errors="replace"))
            if not checks.add(label, not found, ",".join(found) or "ok"):
                raise BackupError("PASSWORD_MATERIAL_IN_DUMP", ",".join(found))

        # ---- 3. isolated restore.
        # The cluster's bootstrap superuser gets the SOURCE's bootstrap name, so grants recorded
        # "GRANTED BY <bootstrap>" replay exactly; pg_dumpall's own CREATE ROLE of that name is the
        # one error that is expected (pg_dumpall emits CREATE ROLE knowing it may already exist).
        cluster = RestoreCluster(bindir, workdir / "restore", port=args.restore_port, bootstrap=bootstrap)
        cluster.start()
        apply_roles_sql(bin_("psql"), roles_sql, child_env(cluster.params("postgres")), bootstrap)
        checks.add("roles_sql_applied", True, "only expected error: bootstrap role already exists")
        with cluster.connect("postgres") as lc:
            present = {r[0] for r in lc.execute("SELECT rolname::text FROM pg_catalog.pg_roles").fetchall()}
            missing = [r for r in referenced_roles if r not in present]
            if not checks.add("roles_cover_dump", not missing, f"referenced={len(referenced_roles)} missing={len(missing)}"):
                raise BackupError("ROLES_SQL_INCOMPLETE")
            dst_globals = read_globals(lc)
            checks.add("roles_names_attributes_match_source", dst_globals["roles"] == src_globals["roles"],
                       f"roles={len(dst_globals['roles'])}")
            if not checks.add("role_memberships_match_source", dst_globals["memberships"] == src_globals["memberships"],
                              f"memberships={len(dst_globals['memberships'])} sha256={dst_globals['roles_sha256']}") \
                    or not checks.ok:
                raise BackupError("ROLES_MISMATCH")
            from psycopg import sql
            stmt = sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(RESTORE_DB))
            db_owner = _db_owner_from(database_sql)
            if db_owner and db_owner in present:  # the source database owner (public: pg_database_owner)
                stmt = stmt + sql.SQL(" OWNER {}").format(sql.Identifier(db_owner))
            lc.execute(stmt)
        _run([bin_("pg_restore"), "--exit-on-error", "--single-transaction", "--no-statistics",
              "--dbname", RESTORE_DB, str(dump)], child_env(cluster.params(RESTORE_DB)), "PG_RESTORE_FAILED")
        checks.add("pg_restore_exit_on_error", True)

        # ---- 4. verification.
        with cluster.connect(RESTORE_DB) as rc:
            dst = read_evidence(rc)
        _compare(checks, src, dst, expected_catalog, args.expected_structure, expected_rows)
        if not checks.ok:
            raise BackupError("RESTORE_VERIFICATION_FAILED")
        cluster.stop()

        # ---- 5. package, encrypt, decrypt-verify, tamper-verify.
        created = datetime.now(timezone.utc).replace(microsecond=0)
        facts["created_at_utc"] = created.isoformat()
        facts["source_commit"] = args.source_commit
        manifest = {
            "format": "fcb-defaultdb-backup/1",
            "created_at_utc": facts["created_at_utc"],
            "source_commit": args.source_commit,
            "run_id": args.run_id,
            "database": args.expected_db,
            "bridge_state": "BRIDGE_0001_0008",
            "server_version_num": guard["ver"],
            "pg_dump": facts["pg_dump"],
            "age": facts["age"],
            "excluded_schemas": excluded,
            "files": {"defaultdb.dump": sha256_file(dump), "roles.sql": sha256_file(roles_sql),
                      "database.sql": sha256_file(database_sql)},
            "catalog_counts": src["catalog_counts"],
            "structure_fingerprint": src["structure"],
            "schema_migrations": [r.split("=")[0] for r in src["migrations"]],
            "functions_sha256": src["functions_sha256"],
            "owners_sha256": src["owners_sha256"],
            "acls_sha256": src["acls_sha256"],
            "roles": src_globals["roles"],
            "memberships": src_globals["memberships"],
            "sequences": src["sequences"],
            "tables": src["tables"],
            "content_sha256": src["content_sha256"],
            "restore_test": "PASS",
        }
        manifest_path = workdir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        name = f"fcb-defaultdb-{args.run_id}"
        tar_path = workdir / f"{name}.tar"
        _write_tar(tar_path, {"defaultdb.dump": dump, "roles.sql": roles_sql, "database.sql": database_sql,
                              "manifest.json": manifest_path}, int(created.timestamp()))
        plain_sha = sha256_file(tar_path)
        facts["plaintext_sha256"] = plain_sha
        facts["plaintext_bytes"] = tar_path.stat().st_size
        cipher = outdir / f"{name}.tar.age"
        encrypt(tar_path, cipher, passphrase)
        roundtrip = workdir / "roundtrip.tar"
        ok = decrypt(cipher, roundtrip, passphrase) and sha256_file(roundtrip) == plain_sha
        if not checks.add("decrypt_roundtrip_plaintext_sha256", ok, plain_sha):
            raise BackupError("DECRYPT_ROUNDTRIP_FAILED")
        roundtrip.unlink()
        tampered = workdir / "tampered.age"
        tamper_copy(cipher, tampered)
        rejected = not decrypt(tampered, workdir / "tampered.out", passphrase)
        if not checks.add("tampered_ciphertext_rejected", rejected):
            raise BackupError("TAMPER_NOT_DETECTED")
        tampered.unlink()
        enc_sha = sha256_file(cipher)
        (outdir / f"{name}.tar.age.sha256").write_text(f"{enc_sha}  {cipher.name}\n", encoding="ascii")
        facts["encrypted_file"] = cipher.name
        facts["encrypted_sha256"] = enc_sha
        facts["encrypted_bytes"] = cipher.stat().st_size
        success = True
    except BackupError as exc:
        checks.add("error", False, exc.code)
        if exc.detail:
            print(f"::error::{exc.code}: {exc.detail}", file=sys.stderr)
        else:
            print(f"::error::{exc.code}", file=sys.stderr)
    except Exception as exc:  # never print exception text (could carry connection details)
        checks.add("error", False, f"UNEXPECTED_{type(exc).__name__}")
        print(f"::error::UNEXPECTED_{type(exc).__name__}", file=sys.stderr)
    finally:
        if cluster is not None:
            try:
                cluster.stop()
            except Exception:
                pass
            cluster.cleanup()
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
        deleted = not workdir.exists()
        checks.add("plaintext_deleted_before_upload", deleted)
        if not (success and deleted):
            shutil.rmtree(outdir, ignore_errors=True)
        else:
            names = sorted(p.name for p in outdir.iterdir())
            checks.add("artifact_dir_only_ciphertext_and_sha256", names == sorted(
                [facts["encrypted_file"], facts["encrypted_file"] + ".sha256"]), f"files={len(names)}")
        _write_summary(args.summary, checks, facts)
        publish_summary(args.summary, getattr(args, "publish_to", None), env)
    return 0 if checks.ok else 1


def publish_summary(private: str | None, public: str | None, env=None) -> bool:
    """Append the private summary to the public one (GITHUB_STEP_SUMMARY) only if the leak check of
    its text passes; works for PASS and FAIL runs alike."""
    if not private or not public or not Path(private).exists():
        return False
    problems = leak_problems(Path(private), None, env)
    if problems:
        print("::error::summary not published: " + ",".join(problems), file=sys.stderr)
        return False
    with open(public, "a", encoding="utf-8") as f:
        f.write(Path(private).read_text(encoding="utf-8"))
    return True


def apply_roles_sql(psql: str, roles_sql: Path, env: dict, bootstrap: str) -> None:
    """psql -f roles.sql on the isolated cluster. Every error fails, except exactly the expected
    `role "<bootstrap>" already exists`."""
    proc = subprocess.run([psql, "-X", "-q", "-v", "ON_ERROR_STOP=0", "-f", str(roles_sql)], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    errors = [line for line in proc.stderr.splitlines() if re.search(r"\b(ERROR|FATAL|PANIC):", line)]
    allowed = f'ERROR:  role "{bootstrap}" already exists'
    unexpected = [e for e in errors if not e.endswith(allowed)]
    if proc.returncode != 0 or unexpected or len(errors) > 1:
        raise BackupError("ROLES_SQL_APPLY_FAILED", sanitize("\n".join(unexpected or errors) or proc.stderr))


def _db_owner_from(database_sql: Path) -> str | None:
    m = re.search(r"^ALTER DATABASE \S+ OWNER TO (\"(?:[^\"]|\"\")+\"|[a-z_][a-z0-9_$]*);$",
                  database_sql.read_text(encoding="utf-8", errors="replace"), re.M)
    if not m:
        return None
    name = m.group(1)
    return name[1:-1].replace('""', '"') if name.startswith('"') else name


# ------------------------------------------------------------------ leak check

def _secret_values(env) -> list[str]:
    values = []
    raw = env.get(URL_VAR, "")
    if raw:
        values.append(raw)
        try:
            u = urlsplit(raw)
            for v in (u.hostname, u.username, u.password, u.password and unquote(u.password)):
                if v and len(v) >= 4:
                    values.append(v)
        except Exception:
            pass
    secret = env.get(PASSPHRASE_VAR, "")
    if secret:
        values.append(secret)
    return values


FORBIDDEN_TEXT = (re.compile(r"postgres(?:ql)?://", re.I), re.compile(r"SCRAM-SHA-256\$", re.I),
                  re.compile(r"PGDMP"), re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
                  re.compile(r"\b(?:access|refresh)_token\b", re.I))
PLAINTEXT_MARKERS = (b"PGDMP", b"ustar", b"CREATE ROLE", b"schema_migrations", b"manifest.json")


def leak_problems(summary: Path | None, artifact_dir: Path | None, env=None) -> list[str]:
    env = os.environ if env is None else env
    secrets_ = [v.encode("utf-8") for v in _secret_values(env)]
    problems = []
    if summary is not None and summary.exists():
        text = summary.read_bytes()
        if any(s in text for s in secrets_):
            problems.append("SUMMARY_CONTAINS_SECRET")
        decoded = text.decode("utf-8", errors="replace")
        if any(p.search(decoded) for p in FORBIDDEN_TEXT):
            problems.append("SUMMARY_CONTAINS_FORBIDDEN_TEXT")
    if artifact_dir is None:
        return problems
    files = sorted(p for p in artifact_dir.iterdir()) if artifact_dir.is_dir() else []
    ages = [p for p in files if p.name.endswith(".tar.age")]
    if len(files) != 2 or len(ages) != 1 or not (artifact_dir / (ages[0].name + ".sha256")).is_file():
        return problems + ["ARTIFACT_DIR_NOT_EXACTLY_CIPHERTEXT_AND_SHA256"]
    cipher, digest = ages[0], artifact_dir / (ages[0].name + ".sha256")
    if any(p.is_symlink() or not p.is_file() for p in files):
        problems.append("ARTIFACT_NOT_REGULAR_FILE")
    data = cipher.read_bytes()
    if not data.startswith(AGE_MAGIC):
        problems.append("ARTIFACT_NOT_AGE")
    body = data[data.find(b"\n---") + 1:] if b"\n---" in data else data
    if any(m in body for m in PLAINTEXT_MARKERS) or any(s in data for s in secrets_):
        problems.append("ARTIFACT_CONTAINS_PLAINTEXT_OR_SECRET")
    line = digest.read_text(encoding="ascii", errors="replace")
    if line != f"{hashlib.sha256(data).hexdigest()}  {cipher.name}\n":
        problems.append("ARTIFACT_SHA256_MISMATCH")
    if any(s in digest.read_bytes() for s in secrets_):
        problems.append("ARTIFACT_CONTAINS_PLAINTEXT_OR_SECRET")
    return problems


# ------------------------------------------------------------------ CLI

def _parser():
    import argparse
    p = argparse.ArgumentParser(prog="fcb_db_backup.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-passphrase")
    r = sub.add_parser("run")
    r.add_argument("--workdir", required=True)
    r.add_argument("--outdir", required=True)
    r.add_argument("--bindir", default="/usr/lib/postgresql/18/bin")
    r.add_argument("--expected-db", default="defaultdb")
    r.add_argument("--expected-counts", required=True)
    r.add_argument("--expected-structure", required=True)
    r.add_argument("--source-commit", required=True)
    r.add_argument("--run-id", required=True)
    r.add_argument("--summary", default=None)
    r.add_argument("--publish-to", default=None, help="GITHUB_STEP_SUMMARY: the summary, after its leak check")
    r.add_argument("--restore-port", type=int, default=54329)
    r.add_argument("--require-tls", action="store_true")
    lc = sub.add_parser("leak-check")
    lc.add_argument("--summary", default=None)
    lc.add_argument("--artifact-dir", default=None)
    s = sub.add_parser("sanitize")
    s.add_argument("file")
    return p


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv[1:])
    if args.cmd == "check-passphrase":
        problem = passphrase_problem()
        if problem:
            print(f"::error::{problem}", file=sys.stderr)
            return 1
        print("backup passphrase: present, length policy met")
        return 0
    if args.cmd == "run":
        if not re.fullmatch(r"[0-9]{1,20}(?:-[0-9]{1,4})?", args.run_id):
            print("::error::RUN_ID_INVALID", file=sys.stderr)
            return 1
        return run(args)
    if args.cmd == "leak-check":
        problems = leak_problems(Path(args.summary) if args.summary else None,
                                 Path(args.artifact_dir) if args.artifact_dir else None)
        if problems:
            print("::error::leak check failed: " + ",".join(problems), file=sys.stderr)
            return 1
        print("leak check: PASS (summary" + (" and artifact directory)" if args.artifact_dir else ")"))
        return 0
    if args.cmd == "sanitize":
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        print(sanitize(text) or "<empty message>")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
