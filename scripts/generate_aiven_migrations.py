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


def _query(name):
    text = (ROOT / 'sql' / 'aiven' / name).read_text(encoding='utf-8')
    body = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('--')).strip()
    if not body.endswith(';') or '$fcbq$' in body:
        raise SystemExit(f'unexpected precheck file shape: {name}')
    return body[:-1]


def render_guard():
    allowed = ', '.join(f"'{s}'" for s in ALLOWED_STATES)
    return (
        '-- FCB-WP-005: generated from defaultdb_precheck.sql + defaultdb_precheck_tracked.sql.\n'
        '-- Runs in the SAME transaction as the restore, after the bridge advisory lock: if the\n'
        '-- state is not allowed at that instant, the whole transaction aborts before any write.\n'
        'SELECT pg_advisory_xact_lock(460244120);\n'
        'DO $fcbguard$\n'
        'DECLARE s text;\n'
        'BEGIN\n'
        f'    EXECUTE $fcbq$\n{_query("defaultdb_precheck.sql")}\n$fcbq$ INTO s;\n'
        "    IF s = 'TRACKED' THEN\n"
        f'        EXECUTE $fcbq$\n{_query("defaultdb_precheck_tracked.sql")}\n$fcbq$ INTO s;\n'
        '    END IF;\n'
        f'    IF s IS NULL OR s NOT IN ({allowed}) THEN\n'
        "        RAISE EXCEPTION 'FCB_RESTORE_STATE_REFUSED %', s;\n"
        '    END IF;\n'
        'END $fcbguard$;\n'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--target', choices=('aiven-0001-0007', 'bridge-0001-0008', 'restore-guard'),
                        default='aiven-0001-0007')
    args = parser.parse_args()
    if args.target == 'bridge-0001-0008':
        text, target = render_bridge(), BRIDGE_TARGET
    elif args.target == 'restore-guard':
        text, target = render_guard(), GUARD_TARGET
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
