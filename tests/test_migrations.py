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
