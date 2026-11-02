"""Immutable SQL migrations, applied atomically under a transaction-scoped lock.

Two independent series (FCB-WP-004, option A):

* ``BRIDGE``: ``sql/migrations/bridge``, tracked in ``schema_migrations``. Files 0001-0007
  are byte-identical to the former ``sql/migrations/000N_*.sql`` (same names, same SHA-256),
  so databases initialized before the split verify unchanged.
* ``SIGNER``: ``sql/migrations/signer``, tracked in ``signer_schema_migrations`` with its own
  advisory lock. It holds financial (signer) schema and is NEVER applied by the bridge
  runner; applying it to the signer database is an explicit owner step (CLAUDE.md §2.6).
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from psycopg.sql import Literal

ROOT_MIGRATIONS = Path(__file__).resolve().parents[1] / 'sql' / 'migrations'


@dataclass(frozen=True)
class Series:
    name: str
    directory: Path
    tracking_table: str
    lock_id: int


BRIDGE = Series('bridge', ROOT_MIGRATIONS / 'bridge', 'schema_migrations', 460244120)
SIGNER = Series('signer', ROOT_MIGRATIONS / 'signer', 'signer_schema_migrations', 460244121)
SERIES = (BRIDGE, SIGNER)

# Backwards-compatible names: the bridge series is the default everywhere.
MIGRATIONS = BRIDGE.directory
LOCK_ID = BRIDGE.lock_id


def tracking_sql(table='schema_migrations'):
    if table not in {s.tracking_table for s in SERIES}:
        raise ValueError('unknown tracking table')
    return f'''CREATE TABLE IF NOT EXISTS {table} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)'''


TRACKING_SQL = tracking_sql(BRIDGE.tracking_table)


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


def migration_block(migration, table='schema_migrations'):
    tracking_sql(table)  # allowlist check: the table name is interpolated below
    version = migration.version
    name = Literal(migration.name).as_string()
    checksum = Literal(migration.checksum).as_string()
    body = Literal(migration.sql).as_string()
    block = f'''
    DECLARE applied {table}%ROWTYPE;
    BEGIN
        SELECT * INTO applied FROM {table} WHERE version={version};
        IF FOUND THEN
            IF applied.name <> {name} OR applied.sha256 <> {checksum} THEN
                RAISE EXCEPTION 'Migration {version} checksum/name drift';
            END IF;
        ELSE
            EXECUTE {body};
            INSERT INTO {table}(version,name,sha256)
            VALUES ({version},{name},{checksum});
        END IF;
    END;'''
    return 'DO ' + Literal(block).as_string()


PRE_SPLIT_SIGNER_NAME = '0008_signer_ledger_anchor.sql'


def _directory_for(series, directory):
    if directory is None:
        return series.directory
    resolved = Path(directory).resolve()
    for other in SERIES:
        # A shipped series directory may only be applied through its own series:
        # never record signer DDL in schema_migrations (or bridge DDL in the signer table).
        if other is not series and resolved == other.directory.resolve():
            raise ValueError(f'{other.name} migrations must be applied with series={other.name.upper()}')
    return directory


def migration_statements(directory=None, series=BRIDGE):
    migrations = load_migrations(_directory_for(series, directory))
    versions = ','.join(str(m.version) for m in migrations)
    table = series.tracking_table
    pre_split = ''
    if series is BRIDGE and any(m.version == 8 for m in migrations):
        # Only a checkout that ships its own bridge v8 can collide with the pre-split one; the
        # pinned Aiven 0001-0007 script therefore stays byte-identical.
        pre_split = f'''
        IF EXISTS (SELECT 1 FROM {table} WHERE version=8 AND name='{PRE_SPLIT_SIGNER_NAME}') THEN
            RAISE EXCEPTION 'PRE_SPLIT_SIGNER_0008: schema_migrations v8 is the signer anchor from the pre-split layout; see docs/certification/FCB_PR_WORKPACKAGES.md (WP-004). Owner decision required.';
        END IF;'''
    return [
        f'SELECT pg_advisory_xact_lock({int(series.lock_id)})',
        tracking_sql(table),
        f'''DO $guard$ BEGIN{pre_split}
        IF EXISTS (SELECT 1 FROM {table} WHERE version NOT IN ({versions})) THEN
            RAISE EXCEPTION 'Database has migrations absent from this checkout';
        END IF;
        END; $guard$''',
        *(migration_block(m, table) for m in migrations),
    ]


def apply_migrations(connect, directory=None, series=BRIDGE):
    # Validate all files before opening the DB; the whole upgrade is one transaction.
    statements = migration_statements(directory, series)
    with connect() as conn, conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
        conn.commit()
