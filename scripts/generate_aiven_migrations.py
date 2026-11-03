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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--target', choices=('aiven-0001-0007', 'bridge-0001-0008'), default='aiven-0001-0007')
    args = parser.parse_args()
    if args.target == 'bridge-0001-0008':
        text, target = render_bridge(), BRIDGE_TARGET
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
