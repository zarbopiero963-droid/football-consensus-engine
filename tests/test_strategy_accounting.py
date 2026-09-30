from app.strategy_catalog import STRATEGY_CATALOG, sample_reliability


def test_strategy_catalog_complete():
    assert list(STRATEGY_CATALOG) == [f"S{i}" for i in range(1, 10)]


def test_sample_reliability_thresholds():
    assert sample_reliability(0) == "INSUFFICIENT_SAMPLE"
    assert sample_reliability(30) == "EARLY_SAMPLE"
    assert sample_reliability(100) == "DEVELOPING_SAMPLE"
    assert sample_reliability(250) == "MATURE_SAMPLE"
