import pytest
from app import persist


class Cursor:
    def __init__(self):
        self.keys = set()
        self.rowcount = -1
        self.payloads = {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params):
        assert 'ON CONFLICT (match_id,snapshot_timestamp_utc) DO NOTHING' in sql
        key = (params['match_id'], params['snapshot_timestamp_utc'])
        self.rowcount = int(key not in self.keys)
        if self.rowcount:
            self.keys.add(key)
            self.payloads[key] = params['payload']


class Connection:
    def __init__(self):
        self.cur = Cursor()
        self.commits = 0
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self.cur
    def commit(self): self.commits += 1


def test_count_tracks_new_duplicate_and_mixed_batch_and_preserves_payload(monkeypatch):
    conn = Connection()
    monkeypatch.setenv('DATABASE_URL', 'test-placeholder')
    monkeypatch.setattr(persist, 'connect', lambda: conn)
    first = {'match_id': 'test:first', 'snapshot_timestamp_utc': '2026-10-01T08:00:00Z', 'model_pick': '1'}
    second = {'match_id': 'test:second', 'snapshot_timestamp_utc': first['snapshot_timestamp_utc']}
    assert persist.upsert_snapshots([first]) == 1
    assert persist.upsert_snapshots([dict(first, model_pick='2')]) == 0
    assert persist.upsert_snapshots([first, second, second]) == 1
    assert persist.upsert_snapshots([first, second]) == 0
    assert len(conn.cur.keys) == 2
    assert '"model_pick":"1"' in conn.cur.payloads[('test:first', first['snapshot_timestamp_utc'])]
    assert conn.commits == 4


def test_empty_or_unconfigured_does_not_connect(monkeypatch):
    monkeypatch.setattr(persist, 'connect', lambda: pytest.fail('unexpected database access'))
    monkeypatch.setenv('DATABASE_URL', 'test-placeholder')
    assert persist.upsert_snapshots([]) == 0
    monkeypatch.delenv('DATABASE_URL')
    assert persist.upsert_snapshots([{'match_id': 'test'}]) == 0


def test_insert_failure_is_not_reported_as_success(monkeypatch):
    conn = Connection()
    def fail(*args): raise RuntimeError('database failure')
    conn.cur.execute = fail
    monkeypatch.setenv('DATABASE_URL', 'test-placeholder')
    monkeypatch.setattr(persist, 'connect', lambda: conn)
    with pytest.raises(RuntimeError, match='database failure'):
        persist.upsert_snapshots([{'match_id': 'test'}])
    assert conn.commits == 0
