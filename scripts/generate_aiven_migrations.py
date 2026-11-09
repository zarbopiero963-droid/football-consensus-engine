"""Generate (or --check) sql/aiven/aiven_migrations_0001_0007.sql from sql/migrations/bridge 0001-0007 only.

Bridge 0008+ (and the separate signer series) are deliberately excluded: the script is built from a temporary
directory that contains only byte-identical copies of 0001-0007.
"""
import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.migrations import migration_statements  # noqa: E402

SOURCE = ROOT / 'sql' / 'migrations' / 'bridge'
TARGET = ROOT / 'sql' / 'aiven' / 'aiven_migrations_0001_0007.sql'
HEADER = '-- FCB migrations 0001-0007 only, regenerated from main\n'
INCLUDED = [f'{n:04d}' for n in range(1, 8)]


def render():
    with tempfile.TemporaryDirectory() as tmp:
        for prefix in INCLUDED:
            matches = sorted(SOURCE.glob(f'{prefix}_*.sql'))
            if len(matches) != 1:
                raise SystemExit(f'expected exactly one migration {prefix}, found {len(matches)}')
            shutil.copyfile(matches[0], Path(tmp) / matches[0].name)
        statements = migration_statements(tmp)
    text = HEADER + ';\n'.join(statements) + ';\n'
    if '0008' in text or 'signer_ledger' in text:
        raise SystemExit('0008 content leaked into the 0001-0007 script')
    return text


# FCB-WP-005: the full bridge series for the defaultdb restore (owner-gated workflow).
# Built from the whole bridge directory, never from the signer series.
BRIDGE_TARGET = ROOT / 'sql' / 'aiven' / 'aiven_migrations_bridge_0001_0008.sql'
BRIDGE_HEADER = '-- FCB bridge migrations 0001-0008 only (no signer series), regenerated from main\n'
BRIDGE_VERSIONS = list(range(1, 9))


def render_bridge():
    from app.migrations import BRIDGE, load_migrations
    migrations = load_migrations(BRIDGE.directory)
    if [m.version for m in migrations] != BRIDGE_VERSIONS:
        raise SystemExit(f'expected bridge versions {BRIDGE_VERSIONS}')
    text = BRIDGE_HEADER + ';\n'.join(migration_statements(series=BRIDGE)) + ';\n'
    for marker in ('signer_ledger_anchors', 'signer_ledger_writer_leases', 'signer_schema_migrations'):
        if marker in text:
            raise SystemExit('signer content leaked into the bridge script')
    return text


# FCB-WP-005: the precheck, re-evaluated INSIDE the restore transaction (no TOCTOU window).
# Generated from the two read-only precheck files so the logic cannot diverge.
GUARD_TARGET = ROOT / 'sql' / 'aiven' / 'defaultdb_restore_guard.sql'
ALLOWED_STATES = ('EMPTY', 'RUNTIME_ONLY', 'BRIDGE_0001_0007', 'BRIDGE_0001_0008')
# int8 overload explicitly (audit F4): an int4 literal would prefer an exact-match
# public.pg_advisory_xact_lock(integer) over pg_catalog's bigint function.
ADVISORY_LOCK = 'SELECT pg_catalog.pg_advisory_xact_lock(460244120::pg_catalog.int8);\n'

# Unexpected (non-system) schemas, as sql/aiven/pg18_occupancy.sql: anchored temp-schema regex,
# `_avnadmin_monitor` excluded only in Aiven's real shape (audit F1). The workflow's $GUARD
# carries the same text (tests/test_defaultdb_restore_workflow.py keeps them equal).
UNEXPECTED_SCHEMA_EXISTS = (
    "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n "
    "JOIN pg_catalog.pg_roles r_owner ON r_owner.oid OPERATOR(pg_catalog.=) n.nspowner "
    "WHERE n.nspname OPERATOR(pg_catalog.<>) ALL (ARRAY['public', 'pg_catalog', 'information_schema', 'pg_toast']::pg_catalog.name[]) "
    "AND n.nspname OPERATOR(pg_catalog.!~) '^pg_(toast_)?temp_[0-9]+$'::pg_catalog.text "
    "AND NOT (n.nspname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name "
    "AND n.nspowner OPERATOR(pg_catalog.<>) (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) SESSION_USER) "
    "AND (r_owner.rolsuper OR r_owner.rolname OPERATOR(pg_catalog.=) '_avnadmin_monitor'::pg_catalog.name) "
    "AND NOT pg_catalog.pg_has_role(SESSION_USER, n.nspowner, 'MEMBER') "
    "AND NOT pg_catalog.has_schema_privilege(SESSION_USER, n.oid, 'CREATE')))"
)
# Login-time settings other than search_path (database, role, database+role): audit F5.
FOREIGN_SETTINGS_EXISTS = (
    "EXISTS (SELECT 1 FROM pg_catalog.pg_settings g "
    "WHERE g.source OPERATOR(pg_catalog.=) ANY (ARRAY['database', 'user', 'database user']::pg_catalog.text[]) "
    "AND g.name OPERATOR(pg_catalog.<>) 'search_path'::pg_catalog.text)"
)
# The session conditions of the workflow's $GUARD that code running inside the transaction
# could change (search_path, role, schemas), re-asserted with qualified names only (audit F3/F5).
SESSION_TAMPERED = (
    "pg_catalog.current_schema() IS NULL "
    "OR pg_catalog.current_schema() OPERATOR(pg_catalog.<>) 'public'::pg_catalog.name "
    "OR (pg_catalog.current_schemas(true))[1] OPERATOR(pg_catalog.<>) 'pg_catalog'::pg_catalog.name "
    "OR pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) 'public'::pg_catalog.text "
    "OR CURRENT_USER OPERATOR(pg_catalog.<>) SESSION_USER "
    f"OR {FOREIGN_SETTINGS_EXISTS} "
    f"OR {UNEXPECTED_SCHEMA_EXISTS}"
)


def _session_check(tag):
    return (
        f'DO ${tag}$\n'
        'BEGIN\n'
        f'    IF {SESSION_TAMPERED} THEN\n'
        "        RAISE EXCEPTION 'FCB_RESTORE_SESSION_GUARD_FAILED';\n"
        '    END IF;\n'
        f'END ${tag}$;\n'
        # Pinned again for the rest of the transaction (belt and braces: everything is qualified).
        'SET LOCAL search_path = public;\n'
    )


def _query(name):
    text = (ROOT / 'sql' / 'aiven' / name).read_text(encoding='utf-8')
    body = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('--')).strip()
    if not body.endswith(';') or '$fcbq$' in body:
        raise SystemExit(f'unexpected precheck file shape: {name}')
    return body[:-1]


def _state_block(indent='    '):
    return (
        f'{indent}EXECUTE $fcbq$\n{_query("defaultdb_precheck.sql")}\n$fcbq$ INTO s;\n'
        f"{indent}IF s OPERATOR(pg_catalog.=) 'TRACKED'::pg_catalog.text THEN\n"
        f'{indent}    EXECUTE $fcbq$\n{_query("defaultdb_precheck_tracked.sql")}\n$fcbq$ INTO s;\n'
        f'{indent}END IF;\n'
    )


def _allowed_array():
    return 'ARRAY[' + ', '.join(f"'{s}'" for s in ALLOWED_STATES) + ']::pg_catalog.text[]'


def render_guard():
    return (
        '-- FCB-WP-005: generated from defaultdb_precheck.sql + defaultdb_precheck_tracked.sql\n'
        '-- (+ fingerprint_bridge_schema.sql). Runs in the SAME transaction as the restore: the session\n'
        '-- conditions are re-asserted, the bridge advisory lock is taken (int8 overload), and if the\n'
        '-- state is not allowed at that instant the whole transaction aborts before any write. A\n'
        "-- BRIDGE_0001_0008 database must already carry the pinned catalog fingerprint\n"
        "-- (:'expected_fingerprint'), so nothing foreign can run inside the no-op restore (audit F2).\n"
        '-- The state is kept in fcb.prior_state for defaultdb_restore_post_guard.sql (audit F7).\n'
        + _session_check('fcbsession')
        + ADVISORY_LOCK
        + "SELECT pg_catalog.set_config('fcb.expected_fingerprint', :'expected_fingerprint', true) IS NOT NULL AS expected_set;\n"
        'DO $fcbguard$\n'
        'DECLARE s pg_catalog.text; f pg_catalog.text;\n'
        'BEGIN\n'
        + _state_block()
        + f'    IF s IS NULL OR s OPERATOR(pg_catalog.<>) ALL ({_allowed_array()}) THEN\n'
        "        RAISE EXCEPTION 'FCB_RESTORE_STATE_REFUSED %', s;\n"
        '    END IF;\n'
        "    IF s OPERATOR(pg_catalog.=) 'BRIDGE_0001_0008'::pg_catalog.text THEN\n"
        f'        EXECUTE $fcbq$\n{_query("fingerprint_bridge_schema.sql")}\n$fcbq$ INTO f;\n'
        "        IF f IS NULL OR f OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_fingerprint') THEN\n"
        "            RAISE EXCEPTION 'FCB_RESTORE_PRE_FINGERPRINT_MISMATCH %', f;\n"
        '        END IF;\n'
        '    END IF;\n'
        "    PERFORM pg_catalog.set_config('fcb.prior_state', s, true);\n"
        'END $fcbguard$;\n'
    )


POST_GUARD_TARGET = ROOT / 'sql' / 'aiven' / 'defaultdb_restore_post_guard.sql'

# Audit F7 (EMPTY prior state): every object in public and every database-wide object kind the
# bridge never creates must come from THIS transaction (as sql/aiven/pg18_post_apply_guard.sql):
# a catalog row with another transaction's xmin was committed concurrently (READ COMMITTED gap
# between the state guard and the script) and skipped by IF NOT EXISTS.
_XMIN_KINDS = (
    ('pg_class', 'relnamespace'), ('pg_proc', 'pronamespace'), ('pg_type', 'typnamespace'),
    ('pg_constraint', 'connamespace'), ('pg_collation', 'collnamespace'), ('pg_operator', 'oprnamespace'),
    ('pg_opclass', 'opcnamespace'), ('pg_opfamily', 'opfnamespace'), ('pg_conversion', 'connamespace'),
    ('pg_ts_config', 'cfgnamespace'), ('pg_ts_dict', 'dictnamespace'),
)


def _empty_prior_foreign_rows():
    terms = [
        f'(SELECT pg_catalog.count(*) FROM pg_catalog.{rel} x JOIN pg_catalog.pg_namespace n '
        f'ON n.oid OPERATOR(pg_catalog.=) x.{col} WHERE n.nspname OPERATOR(pg_catalog.=) \'public\'::pg_catalog.name '
        'AND x.xmin::pg_catalog.text OPERATOR(pg_catalog.<>) me_xid)'
        for rel, col in _XMIN_KINDS
    ] + [
        "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_extension x WHERE x.extname OPERATOR(pg_catalog.<>) 'plpgsql'::pg_catalog.name)",
        '(SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata)',
        '(SELECT pg_catalog.count(*) FROM pg_catalog.pg_publication)',
        '(SELECT pg_catalog.count(*) FROM pg_catalog.pg_subscription x WHERE x.subdbid OPERATOR(pg_catalog.=) '
        '(SELECT d.oid FROM pg_catalog.pg_database d WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()))',
        '(SELECT pg_catalog.count(*) FROM pg_catalog.pg_prepared_xacts x '
        'WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())',
        '(SELECT pg_catalog.count(*) FROM pg_catalog.pg_replication_slots x '
        'WHERE x.database OPERATOR(pg_catalog.=) pg_catalog.current_database())',
        '(SELECT pg_catalog.count(*) FROM pg_catalog.pg_default_acl)',
    ]
    return '\n            OPERATOR(pg_catalog.+) '.join(terms)


def render_post_guard():
    return (
        '-- FCB-WP-005: generated. Runs in the SAME transaction, AFTER the bridge script: the session\n'
        '-- conditions must be unchanged (audit F3), the end state must be BRIDGE_0001_0008 with every\n'
        '-- check of the precheck passing, the catalog fingerprint must equal :\'expected_fingerprint\'\n'
        '-- (pinned in the workflow, PostgreSQL 18), and after an EMPTY prior state every object must\n'
        '-- carry this transaction\'s xmin (audit F7). Otherwise the transaction aborts and nothing is\n'
        '-- committed. Residual: an object a concurrent session commits after this statement.\n'
        + _session_check('fcbsession')
        + "SELECT pg_catalog.set_config('fcb.expected_fingerprint', :'expected_fingerprint', true) IS NOT NULL AS expected_set;\n"
        'DO $fcbpost$\n'
        'DECLARE\n'
        '    s pg_catalog.text; f pg_catalog.text; foreign_rows pg_catalog.int8;\n'
        "    prior pg_catalog.text := pg_catalog.current_setting('fcb.prior_state', true);\n"
        '    me_xid pg_catalog.text := pg_catalog.pg_current_xact_id()::pg_catalog.xid::pg_catalog.text;\n'
        'BEGIN\n'
        + _state_block()
        + "    IF s IS NULL OR s OPERATOR(pg_catalog.<>) 'BRIDGE_0001_0008'::pg_catalog.text THEN\n"
        "        RAISE EXCEPTION 'FCB_RESTORE_POST_STATE %', s;\n"
        '    END IF;\n'
        f'    EXECUTE $fcbq$\n{_query("fingerprint_bridge_schema.sql")}\n$fcbq$ INTO f;\n'
        "    IF f IS NULL OR f OPERATOR(pg_catalog.<>) pg_catalog.current_setting('fcb.expected_fingerprint') THEN\n"
        "        RAISE EXCEPTION 'FCB_RESTORE_FINGERPRINT_MISMATCH %', f;\n"
        '    END IF;\n'
        f'    IF prior IS NULL OR prior OPERATOR(pg_catalog.<>) ALL ({_allowed_array()}) THEN\n'
        "        RAISE EXCEPTION 'FCB_RESTORE_PRIOR_STATE_UNKNOWN';\n"
        '    END IF;\n'
        "    IF prior OPERATOR(pg_catalog.=) 'EMPTY'::pg_catalog.text THEN\n"
        '        SELECT ' + _empty_prior_foreign_rows() + '\n'
        '        INTO foreign_rows;\n'
        '        IF foreign_rows IS NULL OR foreign_rows OPERATOR(pg_catalog.<>) 0::pg_catalog.int8 THEN\n'
        "            RAISE EXCEPTION 'FCB_RESTORE_FOREIGN_OBJECTS_AFTER_EMPTY';\n"
        '        END IF;\n'
        '    END IF;\n'
        'END $fcbpost$;\n'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--target', choices=('aiven-0001-0007', 'bridge-0001-0008', 'restore-guard', 'restore-post-guard'),
                        default='aiven-0001-0007')
    args = parser.parse_args()
    if args.target == 'bridge-0001-0008':
        text, target = render_bridge(), BRIDGE_TARGET
    elif args.target == 'restore-guard':
        text, target = render_guard(), GUARD_TARGET
    elif args.target == 'restore-post-guard':
        text, target = render_post_guard(), POST_GUARD_TARGET
    else:
        text, target = render(), TARGET
    digest = hashlib.sha256(text.encode()).hexdigest()
    if args.check:
        if target.read_text(encoding='utf-8') != text:
            raise SystemExit('committed script differs from regenerated script')
    else:
        target.write_text(text, encoding='utf-8')
    print(digest)


if __name__ == '__main__':
    main()
