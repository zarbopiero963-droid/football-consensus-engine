from datetime import datetime, timedelta, timezone

from prospective import settle


def _team(name):
    return {"TeamName": [{"Description": name}]}


def _match(mid, when, home="Lithuania", away="Andorra"):
    return {
        "IdMatch": mid,
        "Date": when,
        "Home": _team(home),
        "Away": _team(away),
        "HomeTeamScore": 1,
        "AwayTeamScore": 0,
        "MatchStatus": 0,
        "OfficialityStatus": 2,
        "ResultType": 1,
    }


def test_match_fifa_rejects_old_six_hour_window_false_match():
    row = {
        "kickoff_utc": "2026-10-01T18:00:00+00:00",
        "home_team": "Lithuania",
        "away_team": "Andorra",
    }
    too_far = _match("far", "2026-10-01T20:00:00Z")
    found, reason = settle.match_fifa(row, [too_far])
    assert found is None
    assert reason == "no_unique_source_match"


def test_match_fifa_accepts_unique_close_exact_pair():
    row = {
        "kickoff_utc": "2026-10-01T18:00:00+00:00",
        "home_team": "Lithuania",
        "away_team": "Andorra",
    }
    close = _match("ok", "2026-10-01T18:45:00Z")
    found, reason = settle.match_fifa(row, [close])
    assert found["IdMatch"] == "ok"
    assert reason is None


def test_match_fifa_rejects_category_mismatch():
    row = {
        "kickoff_utc": "2026-10-01T18:00:00+00:00",
        "home_team": "Italy U21",
        "away_team": "Germany U21",
    }
    senior = _match("senior", "2026-10-01T18:00:00Z", "Italy", "Germany")
    found, reason = settle.match_fifa(row, [senior])
    assert found is None
    assert reason == "no_unique_source_match"


def test_match_fifa_rejects_ambiguous_close_candidates():
    row = {
        "kickoff_utc": "2026-10-01T18:00:00+00:00",
        "home_team": "Lithuania",
        "away_team": "Andorra",
    }
    matches = [
        _match("a", "2026-10-01T17:30:00Z"),
        _match("b", "2026-10-01T18:30:00Z"),
    ]
    found, reason = settle.match_fifa(row, matches)
    assert found is None
    assert reason == "ambiguous"


def test_fetch_fifa_chunks_large_window_and_deduplicates(monkeypatch):
    calls = []

    def fake_chunk(start, end):
        calls.append((start, end))
        # Duplicate ID across adjacent chunks must not duplicate output.
        return [
            _match("shared", start.isoformat().replace("+00:00", "Z")),
            _match(str(len(calls)), start.isoformat().replace("+00:00", "Z")),
        ], None

    monkeypatch.setattr(settle, "_fetch_fifa_chunk", fake_chunk)
    start = datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc)
    end = start + timedelta(hours=25)
    rows, err = settle.fetch_fifa(start, end)

    assert err is None
    assert len(calls) == 3
    assert len({r["IdMatch"] for r in rows}) == len(rows)
    assert sum(r["IdMatch"] == "shared" for r in rows) == 1
