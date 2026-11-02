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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    text = render()
    digest = hashlib.sha256(text.encode()).hexdigest()
    if args.check:
        if TARGET.read_text(encoding='utf-8') != text:
            raise SystemExit('committed script differs from regenerated script')
    else:
        TARGET.write_text(text, encoding='utf-8')
    print(digest)


if __name__ == '__main__':
    main()
