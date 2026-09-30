from app.market_engine import project_from_1x2, project_from_source_row, settle_market


def test_double_chance_is_derived_from_complete_1x2():
    picks = project_from_1x2({
        "p1": 0.51, "px": 0.29, "p2": 0.20,
        "pinnacle_odds": {"1": 2.0, "X": 3.2, "2": 4.0},
        "pinnacle_fair": {"1": 0.48, "X": 0.30, "2": 0.22},
    })
    dc = next(p for p in picks if p.market_id == "FT_DOUBLE_CHANCE")
    assert dc.selection == "1X"
    assert round(dc.probability, 4) == 0.80
    assert dc.derivation == "DERIVED_FROM_1X2"


def test_source_projection_full_time_markets():
    picks = project_from_source_row({
        "source": "demo",
        "p1": 48, "px": 30, "p2": 22,
        "over25": 64, "under25": 36,
        "btts_yes": 58, "btts_no": 42,
        "correct_score": "2-1",
        "payload": {"correct_score_probability": 14},
    })
    by_market = {p.market_id: p for p in picks}
    assert by_market["FT_1X2"].selection == "1"
    assert by_market["FT_DOUBLE_CHANCE"].selection == "1X"
    assert by_market["FT_OU_2.5"].selection == "OVER"
    assert by_market["FT_BTTS"].selection == "GG"
    assert by_market["FT_CORRECT_SCORE"].selection == "2-1"
    assert by_market["FT_CORRECT_SCORE"].probability == 0.14


def test_source_projection_optional_ht_and_corners():
    picks = project_from_source_row({
        "source": "demo",
        "p1": None, "px": None, "p2": None,
        "over25": None, "under25": None,
        "btts_yes": None, "btts_no": None,
        "correct_score": "",
        "payload": {
            "ht_p1": 55, "ht_px": 30, "ht_p2": 15,
            "ht_over05": 70, "ht_under05": 30,
            "ht_over15": 40, "ht_under15": 60,
            "ht_btts_yes": 25, "ht_btts_no": 75,
            "ht_correct_score": "1-0",
            "ht_correct_score_probability": 18,
            "corners_line": 9.5, "corners_over": 62, "corners_under": 38,
            "ht_corners_line": 4.5, "ht_corners_over": 44, "ht_corners_under": 56,
        },
    })
    by_market = {p.market_id: p for p in picks}
    assert by_market["HT_1X2"].selection == "1"
    assert by_market["HT_OU_0.5"].selection == "OVER"
    assert by_market["HT_OU_1.5"].selection == "UNDER"
    assert by_market["HT_BTTS"].selection == "NG"
    assert by_market["HT_CORRECT_SCORE"].selection == "1-0"
    assert by_market["FT_CORNERS_OU"].selection == "OVER"
    assert by_market["FT_CORNERS_OU"].line == 9.5
    assert by_market["HT_CORNERS_OU"].selection == "UNDER"


def test_settlement_all_supported_markets():
    result = {
        "final_home_goals": 2, "final_away_goals": 1,
        "ht_home_goals": 1, "ht_away_goals": 0,
        "corners_home": 6, "corners_away": 5,
        "ht_corners_home": 2, "ht_corners_away": 2,
    }
    cases = [
        ("FT_1X2", "1", None, ("1", True)),
        ("FT_DOUBLE_CHANCE", "1X", None, ("1", True)),
        ("FT_DOUBLE_CHANCE", "X2", None, ("1", False)),
        ("FT_OU_2.5", "OVER", 2.5, ("OVER", True)),
        ("FT_BTTS", "GG", None, ("GG", True)),
        ("FT_CORRECT_SCORE", "2-1", None, ("2-1", True)),
        ("HT_1X2", "1", None, ("1", True)),
        ("HT_OU_0.5", "OVER", 0.5, ("OVER", True)),
        ("HT_OU_1.5", "UNDER", 1.5, ("UNDER", True)),
        ("HT_BTTS", "NG", None, ("NG", True)),
        ("HT_CORRECT_SCORE", "1-0", None, ("1-0", True)),
        ("FT_CORNERS_OU", "OVER", 9.5, ("OVER", True)),
        ("HT_CORNERS_OU", "UNDER", 4.5, ("UNDER", True)),
    ]
    for market, selection, line, expected in cases:
        assert settle_market(market, selection, line, result) == expected


def test_missing_ht_or_corners_stays_pending():
    result = {"final_home_goals": 1, "final_away_goals": 0}
    assert settle_market("HT_1X2", "1", None, result) is None
    assert settle_market("FT_CORNERS_OU", "OVER", 9.5, result) is None


def test_push_on_integer_total():
    result = {"final_home_goals": 1, "final_away_goals": 1}
    assert settle_market("FT_OU_2.0", "OVER", 2.0, result) == ("PUSH", True)


def test_prosoccer_payload_projects_multiple_real_markets():
    picks = project_from_source_row({
        "source": "prosoccer",
        "p1": 54, "px": 24, "p2": 22,
        "over25": 51, "under25": 49,
        "btts_yes": None, "btts_no": None,
        "correct_score": "1-1",
        "payload": {
            "correct_score_probability": 11.3,
            "goal_lines": [
                {"selection": "OVER", "line": 1.5, "probability": 76},
                {"selection": "OVER", "line": 2.5, "probability": 51},
                {"selection": "UNDER", "line": 3.5, "probability": 71},
            ],
            "ht_p1": 34, "ht_px": 48, "ht_p2": 18,
            "ht_correct_score": "0-0",
            "ht_correct_score_probability": 40.3,
            "corner_lines": [
                {"selection": "UNDER", "line": 10.5, "probability": 92},
                {"selection": "UNDER", "line": 9.5, "probability": 86},
            ],
            "htft_selection": "1/1",
            "htft_probability": 28,
        },
    })
    by_id = {p.market_id: p for p in picks}
    assert by_id["FT_OU_1.5"].selection == "OVER"
    assert by_id["FT_OU_2.5"].selection == "OVER"
    assert by_id["FT_OU_3.5"].selection == "UNDER"
    assert by_id["HT_1X2"].selection == "X"
    assert by_id["HT_CORRECT_SCORE"].selection == "0-0"
    assert by_id["FT_CORNERS_OU_10.5"].selection == "UNDER"
    assert by_id["HTFT_1X2"].selection == "1/1"


def test_htft_settlement():
    result = {
        "final_home_goals": 2, "final_away_goals": 1,
        "ht_home_goals": 1, "ht_away_goals": 0,
    }
    assert settle_market("HTFT_1X2", "1/1", None, result) == ("1/1", True)



def test_freeze_market_tickets_never_reads_pilot_tables(monkeypatch):
    import app.market_engine as market_engine

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

    monkeypatch.setattr(market_engine, "connect", lambda: Connection())

    frozen = [{
        "match_id": "fi:test-a-vs-b-2026-10-01",
        "kickoff_utc": "2026-10-01T18:00:00+00:00",
        "snapshot_timestamp_utc": "2026-10-01T12:00:00+00:00",
        "home": "Test A",
        "away": "Test B",
        "league": "Test League",
        "p1": 0.60,
        "px": 0.25,
        "p2": 0.15,
        "pinnacle_odds": {"1": 1.80, "X": 3.50, "2": 5.00},
        "pinnacle_fair": {"1": 0.55, "X": 0.27, "2": 0.18},
    }]

    count = market_engine.freeze_market_tickets(
        "decision-test-m01",
        frozen,
        "2026-10-01T12:00:00+00:00",
    )

    sql_text = "\n".join(sql for sql, _params in executed).lower()
    assert "source_pilot_predictions" not in sql_text
    assert "source_pilot_runs" not in sql_text
    assert "source_pilot_matches" not in sql_text
    assert count == 2
    assert sum("insert into market_decision_tickets" in sql.lower() for sql, _ in executed) == 2
