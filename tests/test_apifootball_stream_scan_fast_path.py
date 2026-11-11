"""F-B05 (real certification 2026-11-11): apifootball_leagues() with no filter ended as
PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN bound MAX_SECONDS on Render Free. The scan was CPU-bound: one
Python step per JSON token, most of them in `seasons[].coverage`, which the scan never reads.

The fast path consumes runs of short scalar members inside such subtrees with one regular
expression. These tests pin that it is a speed-up only: identical results, hashes, row sizes,
error codes and bounds with the fast path on or off, and every bound still enforced inside the
skipped subtrees. No timing assertion: the step count is the deterministic measure.
"""
from __future__ import annotations

import hashlib
import json
import random

import pytest

from bridge.api_football.continuation import ROW_FIELDS
from bridge.api_football.errors import ProviderDataError
from bridge.api_football.stream_scan import FAST_TOKEN_MAX, IdScanner, ScanBoundExceeded, ScanLimits

LEAGUES = ROW_FIELDS["/leagues"]


def league_row(lid, seasons=15, rng=None):
    rng = rng or random.Random(lid)
    flag = lambda: rng.choice([True, False])  # noqa: E731
    return {
        "league": {"id": lid, "name": f"League {lid} é", "type": "League",
                   "logo": f"https://media.api-sports.io/football/leagues/{lid}.png"},
        "country": {"name": "England", "code": "GB-ENG", "flag": None},
        "seasons": [{"year": 2010 + s, "start": f"{2010 + s}-08-14", "end": f"{2011 + s}-05-17",
                     "current": s == seasons - 1,
                     "coverage": {"fixtures": {"events": flag(), "lineups": flag(), "statistics_fixtures": flag(),
                                               "statistics_players": flag()},
                                  "standings": flag(), "players": flag(), "top_scorers": flag(),
                                  "top_assists": flag(), "top_cards": flag(), "injuries": flag(),
                                  "predictions": flag(), "odds": flag()}} for s in range(seasons)]}


def leagues_body(n, seasons=15, **extra):
    body = {"get": "leagues", "parameters": [], "errors": [], "results": n,
            "paging": {"current": 1, "total": 1}, "response": [league_row(i + 1, seasons) for i in range(n)]}
    body.update(extra)
    return json.dumps(body).encode()


def scan(raw, *, fast=True, chunk=65_536, fields=LEAGUES, limits=ScanLimits(), clock=None):
    s = IdScanner(fields, limits=limits, **({"clock": clock} if clock else {}))
    s._fast = fast and getattr(s, "_fast", False)
    for i in range(0, len(raw), chunk):
        s.feed(raw[i:i + chunk])
    return s.finish()


def outcome(raw, **kw):
    try:
        return ("OK", scan(raw, **kw))
    except ProviderDataError as exc:
        return ("ERR", exc.code, getattr(exc, "bound", None))


def test_leagues_scan_needs_far_fewer_python_steps(monkeypatch):
    """RED on 848c007: every token was one Python step (_punct/_string/_scalar)."""
    raw = leagues_body(120)
    steps = {"n": 0}
    for name in ("_punct", "_string", "_scalar"):
        original = getattr(IdScanner, name)

        def counted(self, *a, _original=original, **k):
            steps["n"] += 1
            return _original(self, *a, **k)
        monkeypatch.setattr(IdScanner, name, counted)
    out = scan(raw)
    tokens = len(raw) - len(raw.replace(b",", b"").replace(b":", b"").replace(b"{", b"").replace(b"}", b"")
                            .replace(b"[", b"").replace(b"]", b""))  # punctuation tokens only: a floor
    assert [r["id"] for r in out["rows"]] == list(range(1, 121))
    assert steps["n"] < 0.35 * tokens


def test_fast_and_token_paths_agree_on_a_leagues_payload():
    raw = leagues_body(150, seasons=7)
    fast, slow = scan(raw, chunk=4096), scan(raw, fast=False, chunk=4096)
    assert fast == slow
    assert fast["body_sha256"] == hashlib.sha256(raw).hexdigest()
    rows = json.loads(raw)["response"]
    assert [r["size"] for r in fast["rows"]] == [len(json.dumps(r).encode()) for r in rows]


@pytest.mark.parametrize("chunk", [1, 2, 5, 13, 64, 977])
def test_chunk_boundaries_inside_skipped_subtrees_do_not_matter(chunk):
    raw = leagues_body(3, seasons=3)
    assert scan(raw, chunk=chunk) == scan(raw, fast=False, chunk=len(raw))


def _season_with(fragment: bytes) -> bytes:
    """A leagues body whose first season's coverage ends with `fragment` (raw JSON bytes)."""
    raw = leagues_body(2, seasons=2)
    marker = b'"odds": '
    i = raw.index(marker)
    j = raw.index(b"}", i)
    return raw[:j] + b", " + fragment + raw[j:]


@pytest.mark.parametrize("fragment", [
    b'"x": 1,', b'"x": 01', b'"x": 1.', b'"x": tru', b'"x" 1', b'"x": "a\x01b"', b'"x": [1,]',
    b'"x": -', b'"x": 1e', b'"x": "\\q"', b'"x": .5', b'"x": +1', b'"x": "\\u12G4"', b'"x":: 1', b'x: 1',
])
def test_invalid_json_inside_a_skipped_subtree_is_refused_identically(fragment):
    raw = _season_with(fragment)
    for chunk in (3, 4096):
        fast, slow = outcome(raw, chunk=chunk), outcome(raw, fast=False, chunk=chunk)
        assert fast == slow and fast[:2] == ("ERR", "PROVIDER_INVALID_JSON")


def test_token_bound_still_applies_inside_a_skipped_subtree():
    raw = _season_with(b'"x": "' + b"a" * 70_000 + b'"')
    assert outcome(raw, chunk=4096) == ("ERR", "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN", "MAX_TOKEN")
    # A string just above what the fast path takes is read token by token, and stays valid.
    raw = _season_with(b'"x": "' + b"a" * (FAST_TOKEN_MAX + 10) + b'"')
    assert outcome(raw) == outcome(raw, fast=False) and outcome(raw)[0] == "OK"


def test_depth_bound_still_applies_inside_a_skipped_subtree():
    raw = _season_with(b'"x": ' + b"[" * 40 + b"1" + b"]" * 40)
    assert outcome(raw) == ("ERR", "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN", "MAX_DEPTH")
    assert outcome(raw, fast=False) == outcome(raw)


def test_row_and_byte_bounds_are_unchanged():
    raw = leagues_body(30, seasons=3)
    assert outcome(raw, limits=ScanLimits(max_rows=29)) == ("ERR", "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN", "MAX_ROWS")
    assert outcome(raw, limits=ScanLimits(max_bytes=len(raw) - 1))[2] == "MAX_BYTES"
    assert outcome(raw, limits=ScanLimits(max_bytes=len(raw)))[0] == "OK"


def test_the_in_loop_time_check_still_fires_on_the_fast_path():
    now = [0.0]

    def clock():
        now[0] += 1.0
        return now[0]
    raw = leagues_body(400)
    with pytest.raises(ScanBoundExceeded) as info:
        scan(raw, chunk=len(raw), clock=clock, limits=ScanLimits(max_seconds=2))
    assert info.value.bound == "MAX_SECONDS"


def test_fast_path_is_off_when_the_token_bound_is_below_its_longest_token():
    s = IdScanner(LEAGUES, limits=ScanLimits(max_token=FAST_TOKEN_MAX - 1))
    assert s._fast is False
    raw = _season_with(b'"x": "' + b"a" * 600 + b'"')
    assert outcome(raw, limits=ScanLimits(max_token=500)) == ("ERR", "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN", "MAX_TOKEN")


def test_fields_errors_and_paging_are_never_skipped():
    # A row field after a skipped subtree, declared errors and paging are still read exactly.
    row = {"seasons": [{"coverage": {"odds": True}}], "league": {"id": 77, "name": "x"}}
    raw = json.dumps({"get": "leagues", "parameters": {"a": [1, 2]}, "errors": {"season": "x", "id": "y"},
                      "results": 1, "paging": {"current": 1, "total": 1}, "response": [row]}).encode()
    out = scan(raw, chunk=5)
    assert out["rows"][0]["id"] == 77 and out["error_keys"] == ["id", "season"] and out["errors_nonempty"]
    assert out["paging"] == {"current": 1, "total": 1} and out == scan(raw, fast=False, chunk=5)
    # A duplicated row field is still flagged, never copied.
    raw = b'{"response":[{"seasons":[1,2],"league":{"id":1,"id":2}}]}'
    assert scan(raw)["rows"][0]["invalid"] == ["id"] == scan(raw, fast=False)["rows"][0]["invalid"]


def test_randomised_documents_agree_with_the_token_path():
    rng = random.Random(20261111)

    def value(d=0):
        r = rng.random()
        if d > 3 or r < 0.55:
            return rng.choice([0, -7, 3.25, 1e21, True, False, None, "", "a,b:c", "é\"\\/", "x" * 700])
        if r < 0.8:
            return {rng.choice(["id", "league", "k", "seasons"]): value(d + 1) for _ in range(rng.randint(0, 4))}
        return [value(d + 1) for _ in range(rng.randint(0, 4))]
    for _ in range(300):
        rows = [{"league": {"id": rng.choice([1, "2", 3.0]), "z": value()}, "seasons": value(), "k": value()}
                for _ in range(rng.randint(0, 4))]
        raw = json.dumps({"get": "leagues", "parameters": value(), "errors": rng.choice([[], {}, {"a": value()}]),
                          "results": len(rows), "paging": {"current": 1, "total": 1}, "response": rows},
                         ensure_ascii=rng.random() < 0.5).encode()
        if rng.random() < 0.5:
            i = rng.randrange(len(raw))
            raw = raw[:i] + bytes([rng.choice(b'{}[],:"\\01-.e tx\x01')]) + raw[i + rng.randint(0, 1):]
        chunk = rng.choice([7, 64, 4096])
        assert outcome(raw, chunk=chunk) == outcome(raw, fast=False, chunk=chunk)
