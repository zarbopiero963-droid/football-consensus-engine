from app import result_validation


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.rows = [{
            "match_id": "m1",
            "kickoff_utc": "2026-10-01T18:00:00+00:00",
            "payload": {
                "status": "FT",
                "final_home_goals": 2,
                "final_away_goals": 1,
            },
        }]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_sync_fifa_reads_kickoff_from_model_snapshots(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(result_validation, "connect", lambda: FakeConn(cur))
    seen = []
    monkeypatch.setattr(
        result_validation,
        "upsert_observation",
        lambda match_id, provider, observed_at, kickoff, payload: seen.append(
            (match_id, provider, kickoff, payload)
        ),
    )
    monkeypatch.setattr(result_validation, "rebuild_all", lambda: [])

    assert result_validation.sync_fifa_from_match_results() == 1
    sql = cur.executed[0][0]
    assert "model_snapshots" in sql
    assert "mr.kickoff_utc" not in sql
    assert seen[0][0] == "m1"
    assert seen[0][2] == "2026-10-01T18:00:00+00:00"
