from pathlib import Path
import pytest
from app import migrations


def _write(directory, version, sql):
    p=directory / f'{version:04d}_test.sql'
    p.write_text(sql)
    return p


def test_current_baseline_is_versioned_and_stable():
    items=migrations.load_migrations()
    assert items[0].version==1 and items[0].name=='0001_baseline.sql'
    assert 'CREATE TABLE IF NOT EXISTS model_snapshots' in items[0].sql
    assert items[0].checksum=='a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287'
    assert items==migrations.load_migrations()


def test_later_alter_is_ordered_and_checksum_changes_are_detectable(tmp_path):
    _write(tmp_path,1,'CREATE TABLE test_upgrade(id INTEGER);')
    second=_write(tmp_path,2,'ALTER TABLE test_upgrade ADD COLUMN note TEXT;')
    original=migrations.load_migrations(tmp_path)
    assert [m.version for m in original]==[1,2]
    assert 'ALTER TABLE' in original[1].sql
    second.write_text(second.read_text()+'\n')
    assert migrations.load_migrations(tmp_path)[1].checksum!=original[1].checksum
    sql=migrations.migration_block(original[1])
    assert 'checksum/name drift' in sql and 'IF FOUND' in sql
    assert 'INSERT INTO schema_migrations' in sql


@pytest.mark.parametrize('names', [[],['0002_gap.sql'],['0001_a.sql','0001_b.sql'],['bad.sql']])
def test_missing_duplicate_gap_or_invalid_version_fails_before_connect(tmp_path,names):
    for name in names: (tmp_path/name).write_text('SELECT 1;')
    with pytest.raises(ValueError): migrations.apply_migrations(lambda:pytest.fail('must not connect'),tmp_path)


def test_empty_sql_fails(tmp_path):
    _write(tmp_path,1,'   ')
    with pytest.raises(ValueError,match='Empty'): migrations.load_migrations(tmp_path)


def test_sql_literals_preserve_quotes_and_delimiter_in_migration(tmp_path):
    _write(tmp_path,1,"SELECT '$migration$', 'owner''s data';")
    block=migrations.migration_block(migrations.load_migrations(tmp_path)[0])
    assert block.startswith("DO '")
    assert '$migration$' in block


def test_runner_commits_once_and_sql_failure_propagates(tmp_path):
    _write(tmp_path,1,'SELECT 1;')
    class Connection:
        def __init__(self,fail=False): self.sql=[]; self.commits=0; self.error=None; self.fail=fail
        def __enter__(self): return self
        def __exit__(self,typ,exc,tb): self.error=exc; return False
        def cursor(self): return self
        def execute(self,sql):
            self.sql.append(sql)
            if self.fail and sql.startswith('DO '): raise RuntimeError('migration SQL failed')
        def commit(self): self.commits+=1
    conn=Connection()
    migrations.apply_migrations(lambda:conn,tmp_path)
    assert conn.commits==1
    assert conn.sql[0].startswith('SELECT pg_advisory_xact_lock')
    assert conn.sql[1]==migrations.TRACKING_SQL
    assert 'absent from this checkout' in conn.sql[2]
    broken=Connection(fail=True)
    with pytest.raises(RuntimeError,match='migration SQL failed'):
        migrations.apply_migrations(lambda:broken,tmp_path)
    assert broken.commits==0 and broken.error is not None


def test_sxbet_rate_limits_migration_is_versioned_consecutive_and_additive():
    items=migrations.load_migrations()
    assert [m.version for m in items]==list(range(1,len(items)+1))
    m7=items[6]
    assert m7.version==7 and m7.name=='0007_sxbet_rate_limits.sql'
    assert 'CREATE TABLE IF NOT EXISTS sxbet_rate_attempts' in m7.sql
    assert 'CREATE TABLE IF NOT EXISTS sxbet_rate_cooldowns' in m7.sql
    for word in ('DROP ','TRUNCATE','DELETE ','ALTER ','UPDATE '):
        assert word not in m7.sql.upper()


def test_signer_ledger_anchor_migration_is_additive_and_append_only():
    # WP-004: the signer schema is its own series (former bridge 0008, same bytes).
    items=migrations.load_migrations(migrations.SIGNER.directory)
    m8=items[0]
    assert m8.version==1 and m8.name=='0001_signer_ledger_anchor.sql'
    assert m8.checksum=='8b193c18f9c7f6009dfab3b4a5af165e3ec3cfcc399fefbc205f5ac33ee3c566'
    sql=m8.sql.upper()
    assert 'CREATE TABLE IF NOT EXISTS SIGNER_LEDGER_ANCHORS' in sql
    assert 'CREATE TABLE IF NOT EXISTS SIGNER_LEDGER_WRITER_LEASES' in sql
    assert 'PRIMARY KEY (LEDGER_ID, SEQ)' in sql
    # Append-only is enforced in the database, not only by the signer.
    assert 'CREATE OR REPLACE TRIGGER' in sql and 'BEFORE UPDATE OR DELETE' in sql and 'BEFORE TRUNCATE' in sql
    for word in ('DROP ','ALTER TABLE','DELETE FROM','TRUNCATE TABLE','UPDATE SIGNER','INSERT INTO'):
        assert word not in sql, word


def test_signer_ledger_writer_lease_token_only_increases_in_the_database():
    # Review finding E: the fencing token must not be lowered (UPDATE) or reset (DELETE + re-insert, TRUNCATE).
    sql=migrations.load_migrations(migrations.SIGNER.directory)[0].sql.upper()
    assert 'CREATE OR REPLACE FUNCTION SIGNER_LEDGER_WRITER_LEASES_GUARD_UPDATE()' in sql
    assert 'NEW.FENCING_TOKEN <= OLD.FENCING_TOKEN' in sql and 'NEW.LEDGER_ID <> OLD.LEDGER_ID' in sql
    assert 'CREATE OR REPLACE TRIGGER SIGNER_LEDGER_WRITER_LEASES_UPDATE_GUARD' in sql
    assert 'BEFORE UPDATE ON SIGNER_LEDGER_WRITER_LEASES' in sql
    assert 'BEFORE DELETE ON SIGNER_LEDGER_WRITER_LEASES' in sql
    assert 'BEFORE TRUNCATE ON SIGNER_LEDGER_WRITER_LEASES' in sql


LEGACY_SHA256={
    1:'a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287',
    2:'a4a889aaf87ef74a40fb8453b96cbea451b6673ab5dda3eed2e127d906b21012',
    3:'4cb5e76853a41fad80ca8f5cb11f5415e3f7dec70a7e351548e00b51bd3e172e',
    4:'483b2391d9b4af2823f3d74784562777a16e41c4e11c0d9415aea2cafa715a8f',
    5:'ee7ad29edc404f38608404216fd55da795eba597fcd6d6eddb7e7f80001fbd56',
    6:'08253596be53ccc1273a7f956793b6c20ae28333c5dc7553c67d8d530acb13fb',
    7:'3f3f06d34cf7e83e01a096bd7366eeeecac2edd5d05b1039c491ea64183b74e4',
}


def test_bridge_series_keeps_0001_0007_bytes_and_adds_runtime_tables():
    items=migrations.load_migrations()
    assert [m.version for m in items]==list(range(1,9))
    assert {m.version:m.checksum for m in items[:7]}==LEGACY_SHA256
    m8=items[7]
    assert m8.name=='0008_runtime_tables.sql'
    sql=m8.sql.upper()
    for table in ('API_FOOTBALL_QUOTA_COUNTERS','API_FOOTBALL_QUOTA_LEASES','API_FOOTBALL_QUOTA_ATTEMPTS',
                  'OAUTH_CLAUDE_DCR_CLIENTS'):
        assert f'CREATE TABLE IF NOT EXISTS {table}' in sql
    code='\n'.join(l for l in sql.splitlines() if not l.strip().startswith('--'))
    for word in ('DROP ','TRUNCATE','DELETE ','ALTER ','UPDATE ','INSERT ','SIGNER'):
        assert word not in code, word


def test_runtime_ddl_is_contained_verbatim_in_the_migration():
    from bridge.api_football.quota import _SCHEMA
    sql=migrations.load_migrations()[7].sql
    for statement in _SCHEMA:
        assert statement + ';' in sql
    oauth=Path('bridge/oauth_store.py').read_text()
    block=oauth[oauth.index('CREATE TABLE IF NOT EXISTS oauth_claude_dcr_clients'):]
    block=block[:block.index(')"""')+1]
    assert block + ';' in sql


def test_bridge_and_signer_series_never_mix():
    assert migrations.BRIDGE.tracking_table!=migrations.SIGNER.tracking_table
    assert migrations.BRIDGE.lock_id!=migrations.SIGNER.lock_id
    assert migrations.BRIDGE.directory!=migrations.SIGNER.directory
    bridge=' '.join(migrations.migration_statements())
    assert 'signer_ledger' not in bridge and 'signer_schema_migrations' not in bridge
    signer=' '.join(migrations.migration_statements(series=migrations.SIGNER))
    assert 'INSERT INTO signer_schema_migrations' in signer and 'INSERT INTO schema_migrations' not in signer
    assert not list(migrations.ROOT_MIGRATIONS.glob('*.sql'))  # no file left in the old flat layout


def test_tracking_table_is_allowlisted():
    with pytest.raises(ValueError):
        migrations.tracking_sql('pg_catalog.pg_authid')
