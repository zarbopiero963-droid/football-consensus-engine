import json

import app.persist as persist


def test_numeric_helpers_preserve_zero_values():
    assert persist._i(0) == 0
    assert persist._i("0") == 0
    assert persist._f(0) == 0.0
    assert persist._f("0.0") == 0.0


def test_numeric_helpers_keep_true_missing_as_none():
    assert persist._i(None) is None
    assert persist._i("") is None
    assert persist._i("   ") is None
    assert persist._f(None) is None
    assert persist._f("") is None
    assert persist._f("   ") is None


def test_upsert_result_persists_zero_score(monkeypatch):
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(persist, "connect", lambda: Connection())

    row = {
        "match_id": "fi:test-zero-2026-10-01",
        "version": 1,
        "status": "settled",
        "result_1x2": "2",
        "final_home_goals": 0,
        "final_away_goals": 2,
        "result_timestamp_utc": "2026-10-01T20:00:00Z",
        "result_source": "test",
    }
    persist.upsert_result(row)

    assert len(executed) == 1
    params = executed[0][1]
    assert params[4] == 0
    assert params[5] == 2
    payload = json.loads(params[8])
    assert payload["final_home_goals"] == 0
    assert payload["final_away_goals"] == 2
