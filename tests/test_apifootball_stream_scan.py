"""FIX-01 option B: replies above the 512 KiB read cap are scanned in a stream, never buffered.
Only ids, row sizes, counts, paging and a raw-body SHA-256 are kept; the whole document is
validated; finite bounds (bytes, time, rows, depth, token) refuse with a fixed code and no
partial claim. Synthetic payloads only, through the REAL transport, resilience client and tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random

import httpx
import pytest

from apifootball_continuation_kit import (
    KEY, PAD, Provider, boot, call, fixture_row, follow_all, validate_follow_up,
)
from bridge.api_football.config import OFFICIAL_BASE_URL
from bridge.api_football.continuation import READ_CAP_BYTES, ROW_FIELDS
from bridge.api_football.errors import ProviderDataError, ProviderDeclaredErrors
from bridge.api_football.stream_scan import DEFAULT_LIMITS, IdScanner, ScanBoundExceeded, ScanLimits
from bridge.api_football.transport import InjectedHTTPXTransport, ScannedReply

D = "2026-11-10"
FIX = ROW_FIELDS["/fixtures"]


def doc(rows, **extra):
    body = {"get": "fixtures", "parameters": {"date": D}, "errors": [], "results": len(rows),
            "paging": {"current": 1, "total": 1}, "response": rows}
    body.update(extra)
    return json.dumps(body).encode()


def scan(raw, *, chunk=7, fields=FIX, limits=DEFAULT_LIMITS, clock=None):
    s = IdScanner(fields, limits=limits, **({"clock": clock} if clock else {}))
    for i in range(0, len(raw), chunk):
        s.feed(raw[i:i + chunk])
    return s.finish()


def many(n, leagues=(39, 140, 71), pad=PAD):
    return [fixture_row(10_000 + i, D, leagues[i % len(leagues)], pad=pad) for i in range(n)]


# ---- end to end above the read cap -----------------------------------------------------------

def test_fixtures_by_date_above_512kib_is_scanned_and_retrieved_completely():
    provider = Provider(many(1000))
    tools = boot(provider)
    out = call(tools, "apifootball_fixtures", {"date": D})
    assert len(provider.bodies[0]) > READ_CAP_BYTES
    assert out["status"] == "PARTIAL_ID_MANIFEST" and out["network_calls_made"] == 1
    m = out["data"]
    assert m["mode"] == "STREAMED_SCAN" and m["hash_basis"] == "RAW_BODY_BYTES"
    assert m["reply_sha256"] == hashlib.sha256(provider.bodies[0]).hexdigest()
    assert m["bytes_scanned"] == len(provider.bodies[0]) and m["rows_in_reply"] == 1000 and m["results_match"]
    assert m["row_ids"] == [10_000 + i for i in range(1000)]          # provider order kept
    assert PAD not in json.dumps(out) and m["data_included"] is False
    assert m["complete_retrieval_available"] is True and m["completeness_basis"] == "MEASURED_RAW_ROW_BYTES"
    assert all(c["expect_bytes_max"] <= READ_CAP_BYTES for c in m["follow_up"]["calls"])
    rows, problems = follow_all(tools, m)
    assert problems == [] and sorted(rows) == sorted(m["row_ids"])


def test_above_2mib_still_bounded_and_complete_only_if_slices_fit():
    provider = Provider(many(3600))
    tools = boot(provider)
    out = call(tools, "apifootball_fixtures", {"date": D})
    assert len(provider.bodies[0]) > 2 * 1024 * 1024
    m = out["data"]
    assert m["rows_in_reply"] == 3600
    # 3 leagues of ~700 KB each: a league slice could exceed the read cap, so no completeness claim.
    assert m["complete_retrieval_available"] is False and m["follow_up"]["reason"] == "FOLLOW_UP_MAY_EXCEED_READ_CAP"
    assert any(c.get("size_provable") is False for c in m["follow_up"]["calls"])


def test_leagues_without_filter_above_the_cap_list_each_league():
    leagues = [{"league": {"id": i, "name": PAD}, "country": {"name": "C"}, "seasons": [{"year": 2026}]}
               for i in range(1, 1201)]
    provider = Provider(leagues=leagues)
    tools = boot(provider)
    out = call(tools, "apifootball_leagues", {})
    m = out["data"]
    assert m["mode"] == "STREAMED_SCAN" and m["rows_in_reply"] == 1200 and m["complete_retrieval_available"] is True
    calls = m["follow_up"]["calls"]
    assert len(calls) == 1200 and calls[0]["arguments"] == {"id": 1} and calls[0]["combination_status"] == "DOCUMENTED_VALID"
    validate_follow_up("apifootball_leagues", calls[0]["arguments"])
    assert len(json.dumps(out).encode()) <= 262_144


def test_continuation_after_a_restart_needs_nothing_from_the_scanning_process():
    provider = Provider(many(1000))
    manifest = call(boot(provider), "apifootball_fixtures", {"date": D})["data"]
    rows, problems = follow_all(boot(provider), manifest)
    assert problems == [] and len(rows) == 1000


def test_odds_above_2mib_give_fixture_ids_and_never_a_price():
    rows = [{"fixture": {"id": 900 + i}, "bookmakers": [{"id": b, "bets": [{"id": 5, "values": [
        {"value": f"Over {k}.5", "odd": "7.77"} for k in range(30)]}]} for b in range(30)]} for i in range(60)]
    body = json.dumps({"get": "odds", "parameters": {"league": "39", "season": "2026"}, "errors": [],
                       "results": 60, "paging": {"current": 1, "total": 2}, "response": rows}).encode()
    assert len(body) > 2 * 1024 * 1024

    class OddsProvider(Provider):
        def body(self, request):
            self.requests.append((request.url.path, dict(request.url.params)))
            self.bodies.append(body)
            return body
    tools = boot(OddsProvider())
    out = call(tools, "apifootball_odds", {"league": 39, "season": 2026})
    m = out["data"]
    assert out["status"] == "PARTIAL_ID_MANIFEST" and m["prices_included"] is False and "7.77" not in json.dumps(out)
    assert [c["arguments"] for c in m["follow_up"]["calls"]][:2] == [{"fixture": 900}, {"fixture": 901}]
    assert m["next_page_arguments"] == {"league": 39, "season": 2026, "page": 2}


# ---- validation and malformed payloads ----------------------------------------------------------

@pytest.mark.parametrize("raw,code", [
    (doc([fixture_row(1)])[:-1], "PROVIDER_INVALID_JSON"),                              # truncated
    (doc([fixture_row(1)])[:len(doc([fixture_row(1)])) // 2], "PROVIDER_INVALID_JSON"),  # truncated mid-way
    (b'{"get":"fixtures","response":[1,],"errors":[]}', "PROVIDER_INVALID_JSON"),        # trailing comma
    (b'{"get" "fixtures","response":[]}', "PROVIDER_INVALID_JSON"),                       # missing colon
    (b'{"response":[tru]}', "PROVIDER_INVALID_JSON"),
    (b'{"response":["a\x01b"]}', "PROVIDER_INVALID_JSON"),                                # control char
    (b'{"response":[]} x', "PROVIDER_INVALID_JSON"),                                      # trailing garbage
    (b'{"response":[]}{"response":[]}', "PROVIDER_INVALID_JSON"),                         # two documents
    (b'{"response":["\xff"]}', "PROVIDER_INVALID_JSON"),                                  # invalid UTF-8
    (b'{"response":[01]}', "PROVIDER_INVALID_JSON"),                                      # leading zero
    (b'{"response":[],"response":[]}', "PROVIDER_REPLY_CONTRACT_INVALID"),                # duplicate key
    (b'{"response":{"a":1}}', "PROVIDER_REPLY_CONTRACT_INVALID"),                         # not a list
    (b'{"response":5}', "PROVIDER_REPLY_CONTRACT_INVALID"),
    (b'[{"response":[]}]', "PROVIDER_JSON_NOT_OBJECT"),
    (b'{"results":0}', "PROVIDER_RESPONSE_FIELD_MISSING"),
    (b'', "PROVIDER_INVALID_JSON"),
])
def test_malformed_payloads_are_refused(raw, code):
    with pytest.raises(ProviderDataError) as info:
        scan(raw)
    assert info.value.code == code


def test_chunk_boundaries_do_not_change_the_result():
    raw = doc(many(40, pad="é\\\"ü" * 20), paging={"current": 2, "total": 9})
    whole = scan(raw, chunk=len(raw))
    rng = random.Random(7)
    for _ in range(20):
        size = rng.randint(1, 97)
        assert scan(raw, chunk=size) == whole
    assert [r["id"] for r in whole["rows"]] == [10_000 + i for i in range(40)]
    assert whole["paging"] == {"current": 2, "total": 9} and whole["body_sha256"] == hashlib.sha256(raw).hexdigest()


def test_row_sizes_are_the_raw_byte_spans():
    rows = many(3)
    raw = doc(rows)
    out = scan(raw)
    assert [r["size"] for r in out["rows"]] == [len(json.dumps(r).encode()) for r in rows]


def test_duplicate_and_missing_ids():
    rows = many(4)
    rows[1]["fixture"]["id"] = rows[0]["fixture"]["id"]
    del rows[2]["fixture"]["id"]
    rows[3]["fixture"]["id"] = {"nested": 1}
    out = scan(doc(rows))
    assert [r["id"] for r in out["rows"]] == [10_000, 10_000, None, "NOT_A_SCALAR"]
    twice = b'{"response":[{"fixture":{"id":1,"id":2}}]}'
    assert scan(twice)["rows"][0]["id"] == "DUPLICATE_FIELD"
    from bridge.api_football.continuation import scan_manifest
    m = scan_manifest("apifootball_fixtures", {"date": D}, out)
    assert m["rows_without_id"] == 2 and m["complete_retrieval_available"] is False


def test_declared_errors_in_a_streamed_body_keep_only_keys():
    raw = json.dumps({"get": "fixtures", "errors": {"requests": "SECRET TEXT"}, "results": 0,
                      "response": [fixture_row(i) for i in range(1000)]}).encode()

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL,
                                     transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw))) as http:
            return await InjectedHTTPXTransport(http, api_key=KEY).get("/fixtures", {"date": D})
    with pytest.raises(ProviderDeclaredErrors) as info:
        asyncio.run(go())
    assert info.value.error_keys == ("requests",) and "SECRET" not in repr(info.value.__dict__)


# ---- bounds ------------------------------------------------------------------------------------

def test_byte_bound_is_exact():
    raw = doc(many(5))
    assert scan(raw, limits=ScanLimits(max_bytes=len(raw)))["rows"]
    with pytest.raises(ScanBoundExceeded) as info:
        scan(raw, limits=ScanLimits(max_bytes=len(raw) - 1))
    assert info.value.bound == "MAX_BYTES" and info.value.code == "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN"


def test_row_bound_is_exact_and_memory_stays_bounded():
    raw = doc(many(50))
    assert len(scan(raw, limits=ScanLimits(max_rows=50))["rows"]) == 50
    with pytest.raises(ScanBoundExceeded) as info:
        scan(raw, limits=ScanLimits(max_rows=49))
    assert info.value.bound == "MAX_ROWS"
    s = IdScanner(FIX)
    big = doc(many(2000))
    peak = 0
    for i in range(0, len(big), 65_536):
        s.feed(big[i:i + 65_536])
        peak = max(peak, len(s.buf))
    s.finish()
    assert len(big) > 1_000_000 and peak <= 65_536  # only an incomplete token tail is held


def test_depth_bound_without_recursion():
    nested = b'{"response":[' + b"[" * 100_000 + b"]" * 100_000 + b"]}"
    with pytest.raises(ScanBoundExceeded) as info:
        scan(nested, chunk=65_536)
    assert info.value.bound == "MAX_DEPTH"
    ok = b'{"response":[' + b"[" * 29 + b"]" * 29 + b"]}"
    assert scan(ok)["rows"] == [{"id": None, "size": None}]


def test_token_bound():
    raw = b'{"response":[{"fixture":{"id":1,"x":"' + b"a" * 70_000 + b'"}}]}'
    with pytest.raises(ScanBoundExceeded) as info:
        scan(raw, chunk=4096)
    assert info.value.bound == "MAX_TOKEN"


def test_slow_stream_hits_the_time_bound():
    now = [0.0]

    def clock():
        now[0] += 1.0  # each read of the clock is one second later: a slow stream
        return now[0]
    with pytest.raises(ScanBoundExceeded) as info:
        scan(doc(many(30)), chunk=1024, clock=clock, limits=ScanLimits(max_seconds=5))
    assert info.value.bound == "MAX_SECONDS"


def test_a_bound_through_the_tool_is_a_fixed_code_with_no_partial_claim():
    provider = Provider(many(1000))
    tools = boot(provider, limits=ScanLimits(max_rows=999))
    out = call(tools, "apifootball_fixtures", {"date": D})
    assert out["status"] == "PROVIDER_ERROR"
    assert out["error"] == {"code": "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN", "bound": "MAX_ROWS"}
    nav = out["navigation"]
    assert nav["complete_retrieval_available"] is False and nav["reason"] == "SCAN_BOUND_MAX_ROWS"
    assert nav["row_ids"] is None and "league" in nav["narrowing"] and out["data"] is None
    assert out["network_calls_made"] == 1


def test_paths_without_row_ids_are_still_refused_at_the_read_cap():
    raw = json.dumps({"response": [{"blob": "x" * 600_000}]}).encode()

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL,
                                     transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw))) as http:
            return await InjectedHTTPXTransport(http, api_key=KEY).get("/fixtures/statistics", {"fixture": 1})
    with pytest.raises(ProviderDataError) as info:
        asyncio.run(go())
    assert info.value.code == "PROVIDER_RESPONSE_TOO_LARGE"


def test_a_reply_under_the_cap_is_not_scanned():
    async def go():
        raw = doc(many(2))
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL,
                                     transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw))) as http:
            return await InjectedHTTPXTransport(http, api_key=KEY).get("/fixtures", {"date": D})
    reply = asyncio.run(go())
    assert not isinstance(reply, ScannedReply) and len(reply.data["response"]) == 2
