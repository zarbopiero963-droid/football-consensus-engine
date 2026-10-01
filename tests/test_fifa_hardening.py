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


def test_containment_names_cannot_settle():
    row = {'kickoff_utc': '2026-10-01T18:00:00Z', 'home_team': 'United', 'away_team': 'Chile'}
    assert settle.match_fifa(row, [_match('wrong', row['kickoff_utc'], 'United States', 'Chile')])[0] is None


def test_legitimate_registered_alias_is_exact():
    row = {'kickoff_utc': '2026-10-01T18:00:00Z', 'home_team': 'USA', 'away_team': 'Chile'}
    assert settle.match_fifa(row, [_match('ok', row['kickoff_utc'], 'United States', 'Chile')])[0]['IdMatch'] == 'ok'


def test_age_group_and_fifa_metadata_do_not_collapse():
    assert not settle._category_compatible('Italy U21', 'Italy U23')
    assert not settle._category_compatible('Italy B', 'Italy U21')
    assert not settle._category_compatible('Italy', 'Italy', {'Gender': 2, 'AgeType': 7})
    assert not settle._category_compatible('Italy', 'Italy', {'Gender': 1, 'AgeType': 4})
    assert settle._category_compatible('Italy', 'Italy', {'Gender': 1, 'AgeType': 7})


def test_naive_source_and_anchor_kickoffs_fail_closed():
    row = {'kickoff_utc': '2026-10-01T18:00:00Z', 'home_team': 'Lithuania', 'away_team': 'Andorra'}
    assert settle.match_fifa(row, [_match('bad', '2026-10-01T18:00:00')])[0] is None
    row['kickoff_utc'] = '2026-10-01T18:00:00'
    assert settle.match_fifa(row, [_match('ok', '2026-10-01T18:00:00Z')])[1] == 'kickoff_timezone_unresolved'


def test_known_competition_mismatch_rejected():
    row = {'kickoff_utc': '2026-10-01T18:00:00Z', 'home_team': 'Lithuania', 'away_team': 'Andorra', 'league': 'International Friendlies'}
    match = _match('wrong', row['kickoff_utc'])
    match['CompetitionName'] = [{'Description': 'World Cup'}]
    assert settle.match_fifa(row, [match])[0] is None
    match['CompetitionName'] = [{'Description': 'Friendlies'}]
    assert settle.match_fifa(row, [match])[0]['IdMatch'] == 'wrong'


def test_fetch_adaptively_splits_saturated_window(monkeypatch):
    calls = []
    def fake(start, end):
        calls.append((start, end))
        if (end - start).total_seconds() > 3600:
            return [], 'saturated_window'
        return [_match(start.isoformat(), start.isoformat())], None
    monkeypatch.setattr(settle, '_fetch_fifa_chunk', fake)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows, err = settle.fetch_fifa(start, start + timedelta(hours=3))
    assert err is None and len(rows) == 4
    assert len(calls) == 7


def test_fetch_saturation_at_minimum_and_partial_errors_fail_closed(monkeypatch):
    monkeypatch.setattr(settle, '_fetch_fifa_chunk', lambda *args: ([], 'saturated_window'))
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert settle._fetch_fifa_window(start, start + timedelta(seconds=60)) == ([], 'saturated_window_at_minimum_resolution')
    calls = []
    def fake(*args):
        calls.append(1)
        return ([_match('ok', start.isoformat())], None) if len(calls) == 1 else ([], 'transport_failure')
    monkeypatch.setattr(settle, '_fetch_fifa_chunk', fake)
    rows, err = settle.fetch_fifa(start, start + timedelta(hours=15))
    assert rows == [] and 'transport_failure' in err


def test_chunk_payload_and_continuation_validation(monkeypatch):
    import json
    from io import BytesIO
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for payload, expected in [
        ({}, 'invalid_results_payload'),
        ({'Results': {}}, 'invalid_results_payload'),
        ({'Results': [None]}, 'invalid_results_payload'),
        ({'Results': [], 'ContinuationToken': 'more'}, 'saturated_window'),
        ({'Results': [_match(str(i), start.isoformat()) for i in range(200)]}, 'saturated_window'),
        ({'Results': []}, None),
    ]:
        monkeypatch.setattr(settle, 'urlopen', lambda *args, **kwargs: BytesIO(json.dumps(payload).encode()))
        rows, err = settle._fetch_fifa_chunk(start, start + timedelta(hours=1))
        assert err == expected
        assert rows == []


def test_malformed_match_sides_are_rejected_without_crash():
    row = {'kickoff_utc': '2026-10-01T18:00:00Z', 'home_team': 'Lithuania', 'away_team': 'Andorra'}
    malformed = _match('bad', row['kickoff_utc'])
    malformed['Home'] = ['Lithuania']
    assert settle.match_fifa(row, [None, malformed])[0] is None


def test_ghana_competition_alias_requires_both_country_ids():
    row = {'league': 'Ghana Premier League'}
    match = {'CompetitionName': [{'Description': 'Premier League'}], 'Home': {'IdCountry': 'GHA'}, 'Away': {'IdCountry': 'GHA'}}
    assert settle._competition_compatible(row, match)
    match['Away']['IdCountry'] = 'ENG'
    assert not settle._competition_compatible(row, match)
