from app.api import strategy_ids


def test_high_probability_favorite_without_edge():
    payload = {
        "model_pick_probability": "0.71",
        "picked_edge": "-0.05",
        "picked_odds": "1.30",
        "analysis_eligible": "1",
        "same_as_favorite": "1",
    }
    assert strategy_ids(payload) == ["S1", "S2", "S3", "S4"]


def test_positive_edge_strategy_stack():
    payload = {
        "model_pick_probability": "0.61",
        "picked_edge": "0.09",
        "picked_odds": "2.10",
        "analysis_eligible": "1",
        "same_as_favorite": "0",
    }
    assert strategy_ids(payload) == ["S1", "S2", "S5", "S6", "S7", "S8", "S9"]


def test_unpriced_signal_does_not_invent_market_strategies():
    payload = {
        "model_pick_probability": "0.62",
        "picked_edge": "",
        "picked_odds": "",
        "analysis_eligible": "0",
        "same_as_favorite": "0",
    }
    assert strategy_ids(payload) == ["S1", "S2"]
