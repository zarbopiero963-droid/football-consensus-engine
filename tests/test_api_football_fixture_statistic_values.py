"""Real statistic VALUES (xG, shots, corners, possession) captured by the probe.

Offline only: fake key, httpx.MockTransport. Missing and null stay distinct
from zero; labels are matched exactly; in-play values are never labelled final.
"""
import asyncio

import httpx
import pytest

from bridge.api_football.five_point_evidence import (
    extract_fixture_statistics, summarize_fixture_statistics)
from scripts.api_football_capability_probe import run


def _payload(stats_home, stats_away=None):
    return {"response": [
        {"team": {"id": 1}, "statistics": stats_home},
        {"team": {"id": 2}, "statistics": stats_away if stats_away is not None else stats_home},
    ]}


FULL = [
    {"type": "Shots on Goal", "value": 0},
    {"type": "Total Shots", "value": 11},
    {"type": "Corner Kicks", "value": 5},
    {"type": "Ball Possession", "value": "57%"},
    {"type": "expected_goals", "value": "1.42"},
]


def test_verified_values_kept_raw_and_zero_stays_zero():
    out = extract_fixture_statistics(_payload(FULL), 10, "FT")
    assert out["snapshot_kind"] == "FINAL"
    fields = out["teams"][0]["fields"]
    assert fields["shots_on_target"] == {"label": "Shots on Goal", "status": "VERIFIED_REAL", "value": 0}
    assert fields["possession"]["value"] == "57%"
    assert fields["xg"]["value"] == "1.42"
    assert all(f["status"] == "VERIFIED_REAL" for f in fields.values())
    assert out["teams"][0]["raw_statistics"][0] == {"type": "Shots on Goal", "value": 0}


def test_missing_and_null_are_not_zero():
    stats = [{"type": "Total Shots", "value": None}, {"type": "Corner Kicks", "value": 3}]
    fields = extract_fixture_statistics(_payload(stats), 10, "FT")["teams"][0]["fields"]
    assert fields["shots_total"] == {"label": "Total Shots", "status": "NULL_PUBLISHED", "value": None}
    assert fields["xg"]["status"] == "MISSING"
    assert fields["possession"]["status"] == "MISSING"
    assert fields["corners"]["status"] == "VERIFIED_REAL"


@pytest.mark.parametrize("label,value", [
    ("Ball Possession", "57"), ("Ball Possession", "140%"), ("Ball Possession", 57),
    ("Total Shots", -1), ("Total Shots", "11"), ("expected_goals", "abc"),
    ("expected_goals", "-0.1"),
])
def test_invalid_values_flagged_not_coerced(label, value):
    fields = extract_fixture_statistics(_payload([{"type": label, "value": value}]), 10, "FT")["teams"][0]["fields"]
    field = next(f for f in fields.values() if f["label"] == label)
    assert field["status"] == "INVALID_VALUE"
    assert field["value"] == value


def test_similar_labels_are_not_equivalent_and_duplicates_are_ambiguous():
    stats = [{"type": "Shots insidebox", "value": 7}, {"type": "Corner Kicks", "value": 2},
             {"type": "Corner Kicks", "value": 3}, {"type": "expected goals", "value": "1.0"}]
    fields = extract_fixture_statistics(_payload(stats), 10, "FT")["teams"][0]["fields"]
    assert fields["shots_total"]["status"] == "MISSING"
    assert fields["xg"]["status"] == "MISSING"
    assert fields["corners"]["status"] == "AMBIGUOUS_DUPLICATE_LABEL"


@pytest.mark.parametrize("status,kind", [("2H", "IN_PLAY"), ("HT", "IN_PLAY"), ("NS", "UNKNOWN_OR_NOT_STARTED"),
                                         (None, "UNKNOWN_OR_NOT_STARTED"), ("PEN", "FINAL")])
def test_in_play_values_are_never_labelled_final(status, kind):
    assert extract_fixture_statistics(_payload(FULL), 10, status)["snapshot_kind"] == kind


@pytest.mark.parametrize("payload", [
    {"response": None}, {"response": [{"team": {"id": "1"}, "statistics": []}]},
    {"response": [{"team": {"id": 1}, "statistics": [{"type": 5, "value": 1}]}]},
    {"response": [{"team": {"id": 1}, "statistics": [{"type": "Total Shots", "value": True}]}]},
    {"response": [{"team": {"id": 1}, "statistics": [{"type": "Total Shots", "value": [1]}]}]},
])
def test_malformed_payload_fails_closed(payload):
    with pytest.raises(ValueError):
        extract_fixture_statistics(payload, 10, "FT")


def test_summary_counts_and_final_only_counter():
    obs = [extract_fixture_statistics(_payload(FULL), 10, "FT"),
           extract_fixture_statistics(_payload([{"type": "Total Shots", "value": 4}]), 11, "2H")]
    summary = summarize_fixture_statistics(obs)
    assert summary["global_completeness_claim"] is False
    assert summary["fields"]["shots_total"]["status"] == "VERIFIED_REAL"
    assert summary["fields"]["shots_total"]["verified_on_final_snapshot"] == 2
    assert summary["fields"]["xg"]["status"] == "PARTIAL"
    assert summary["fields"]["xg"]["counts"] == {"VERIFIED_REAL": 2, "MISSING": 2}
    assert summarize_fixture_statistics([])["fields"]["corners"]["status"] == "NOT_TESTED"


ENV = {"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "true",
       "APIFOOTBALL_API_KEY": "FAKE_TEST_ONLY", "APIFOOTBALL_MAX_DAILY_REQUESTS": "60",
       "APIFOOTBALL_MAX_RPM": "8"}


async def _no_sleep(_):
    return None


def _probe(stats_body):
    calls = []

    def reply(request):
        path = request.url.path
        calls.append(path)
        if path == "/fixtures":
            live = request.url.params.get("live") == "all"
            data = [{"fixture": {"id": n, "status": {"short": "2H" if live else "FT"}},
                     "teams": {"home": {"id": 100 + n}, "away": {"id": 200 + n}},
                     "league": {"id": 39, "season": 2026}} for n in ((11,) if live else (21, 22))]
        elif path == "/fixtures/statistics":
            data = stats_body
        else:
            data = []
        return httpx.Response(200, json={"response": data, "errors": {}, "paging": {"current": 1, "total": 1}})

    records = asyncio.run(run(ENV, transport=httpx.MockTransport(reply), sleep=_no_sleep))
    return records, calls


def test_probe_captures_values_without_extra_calls_and_no_secret():
    records, calls = _probe(_payload(FULL)["response"])
    _, baseline_calls = _probe([])
    assert len(calls) == len(baseline_calls)  # values come from the already-fetched response
    assert calls.count("/fixtures/statistics") == 3
    summary = next(r for r in records if r["path"] == "_fixture_statistic_values")
    kinds = {o["fixture_id"]: o["snapshot_kind"] for o in summary["observations"]}
    assert kinds == {11: "IN_PLAY", 21: "FINAL", 22: "FINAL"}
    assert summary["summary"]["fields"]["possession"]["status"] == "VERIFIED_REAL"
    assert summary["summary"]["fields"]["possession"]["verified_on_final_snapshot"] == 4
    assert "FAKE_TEST_ONLY" not in repr(records)


def test_probe_records_malformed_statistics_without_crashing():
    records, _ = _probe([{"team": {"id": 1}, "statistics": "bad"}])
    summary = next(r for r in records if r["path"] == "_fixture_statistic_values")
    assert {o.get("status") for o in summary["observations"]} == {"INVALID_STATISTICS_TEAM"}
    assert summary["summary"]["fields"]["xg"]["status"] == "NOT_TESTED"
