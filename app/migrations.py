"""Immutable SQL migrations, applied atomically under a transaction-scoped lock."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from psycopg.sql import Literal

MIGRATIONS = Path(__file__).resolve().parents[1] / 'sql' / 'migrations'
LOCK_ID = 460244120
TRACKING_SQL = '''CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)'''


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations(directory=MIGRATIONS):
    migrations = []
    for path in sorted(Path(directory).glob('*.sql')):
        match = re.fullmatch(r'(\d{4})_([a-z0-9_]+)\.sql', path.name)
        if not match:
            raise ValueError(f'Invalid migration filename: {path.name}')
        raw = path.read_bytes()
        sql = raw.decode('utf-8')
        if not sql.strip():
            raise ValueError(f'Empty migration: {path.name}')
        migrations.append(Migration(int(match[1]), path.name, sql, sha256(raw).hexdigest()))
    if not migrations or [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise ValueError('Migrations must be nonempty and consecutively numbered from 0001')
    return migrations


def migration_block(migration):
    version = migration.version
    name = Literal(migration.name).as_string()
    checksum = Literal(migration.checksum).as_string()
    body = Literal(migration.sql).as_string()
    block = f'''
    DECLARE applied schema_migrations%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM schema_migrations WHERE version={version};
        IF FOUND THEN
            IF applied.name <> {name} OR applied.sha256 <> {checksum} THEN
                RAISE EXCEPTION 'Migration {version} checksum/name drift';
            END IF;
        ELSE
            EXECUTE {body};
            INSERT INTO schema_migrations(version,name,sha256)
            VALUES ({version},{name},{checksum});
        END IF;
    END;'''
    return 'DO ' + Literal(block).as_string()


def migration_statements(directory=MIGRATIONS):
    migrations = load_migrations(directory)
    versions = ','.join(str(m.version) for m in migrations)
    return [
        f'SELECT pg_advisory_xact_lock({LOCK_ID})',
        TRACKING_SQL,
        f'''DO $guard$ BEGIN
        IF EXISTS (SELECT 1 FROM schema_migrations WHERE version NOT IN ({versions})) THEN
            RAISE EXCEPTION 'Database has migrations absent from this checkout';
        END IF;
        END; $guard$''',
        *(migration_block(m) for m in migrations),
    ]


def apply_migrations(connect, directory=MIGRATIONS):
    # Validate all files before opening the DB; the whole upgrade is one transaction.
    statements = migration_statements(directory)
    with connect() as conn, conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
        conn.commit()
