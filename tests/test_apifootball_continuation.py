"""FIX-01: replies above the 256 KiB MCP cap become a data-free id manifest whose follow-up
calls are plain, re-validated provider parameters (no token, no server state: they survive a
restart). Verified end to end through the REAL transport, resilience client and MCP tools over
httpx.MockTransport: every row is retrieved exactly once, order and ids preserved, nothing
mixed between requests, a change between reads is detected, the read cap stays."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.continuation import STRATEGIES, id_manifest, response_sha256
from bridge.api_football.contracts import validate
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools

KEY = "LOCAL_FAKE_KEY_CONTINUATION_ONLY"
PAD = "Stadio-" + "v" * 420  # a listing row of ~600 bytes: 600 rows sit between 256 and 512 KiB


def fixture_row(fid, day="2026-11-10"):
    return {"fixture": {"id": fid, "date": day + "T15:00:00+00:00", "timestamp": 1_794_000_000 + fid,
                        "venue": {"id": None, "name": PAD}, "status": {"short": "NS", "elapsed": None}},
            "league": {"id": 39, "season": 2026}, "teams": {"home": {"id": 1}, "away": {"id": 2}},
            "goals": {"home": None, "away": None}}


class Provider:
    """A fake API-FOOTBALL holding fixtures by date; answers date listings and ids lookups."""

    def __init__(self, days):
        self.days = days  # {date: [fixture ids in provider order]}
        self.requests = []

    def rows_for(self, params):
        if "date" in params:
            return [fixture_row(f, params["date"]) for f in self.days.get(params["date"], [])]
        if "ids" in params or "id" in params:
            wanted = [int(x) for x in (params.get("ids") or params["id"]).split("-")]
            known = {f: d for d, ids in self.days.items() for f in ids}
            return [fixture_row(f, known[f]) for f in wanted if f in known]
        if "team" in params:  # players page shape
            return [{"player": {"id": i, "name": PAD + PAD}, "statistics": []} for i in range(1, 501)]
        if "country" in params or "season" in params:
            return [{"league": {"id": 1000 + i, "name": PAD}, "country": {"name": "C%d" % (i % 7)}}
                    for i in range(500)]
        return []

    def handler(self, request):
        params = dict(request.url.params)
        self.requests.append((request.url.path, params))
        rows = self.rows_for(params)
        paging = {"current": int(params.get("page", 1)), "total": 3 if "team" in params else 1}
        return httpx.Response(200, json={"get": request.url.path.strip("/"), "parameters": params, "errors": [],
                                         "results": len(rows), "paging": paging, "response": rows})


class Quota:
    async def acquire(self, secret):
        return QuotaLease("l", "h")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None


class Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def boot(provider):
    """A fresh bridge process: new client, new cache, no state shared with an earlier boot."""
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(provider.handler))
    from bridge.api_football.transport import InjectedHTTPXTransport
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     retry=RetryPolicy(base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    return server.tools


def call(tools, name, args):
    return asyncio.run(tools[name](**{("from_date" if k == "from" else "to_date" if k == "to" else k): v
                                      for k, v in args.items()}))


def follow_all(tools, manifest):
    """The client side: run every follow-up and check it against the manifest."""
    rows, problems = [], []
    for c in manifest["follow_up"]["calls"]:
        out = call(tools, manifest["follow_up"]["tool"], c["arguments"])
        assert out["status"] == "REAL_API_RESPONSE", out.get("error")
        got = [r["fixture"]["id"] for r in out["data"]["response"]]
        if got != c["expect_ids"] or len(got) != c["expect_rows"]:
            problems.append({"expected": c["expect_ids"], "got": got})
        rows.extend(got)
    return rows, problems


def test_fixtures_by_date_over_the_cap_is_retrieved_completely_in_order_without_duplicates():
    ids = list(range(5000, 5600))
    provider = Provider({"2026-11-10": ids})
    tools = boot(provider)
    first = call(tools, "apifootball_fixtures", {"date": "2026-11-10"})
    assert first["status"] == "PARTIAL_ID_MANIFEST" and first["network_calls_made"] == 1
    m = first["data"]
    assert m["data_included"] is False and PAD not in json.dumps(first)
    assert m["rows_in_reply"] == m["results_declared"] == 600 and m["row_ids"] == ids
    assert m["follow_up"]["strategy"] == "IDS_SLICES" and m["complete_retrieval_available"] is True
    assert all(len(c["expect_ids"]) <= 20 for c in m["follow_up"]["calls"]) and len(m["follow_up"]["calls"]) == 30
    assert len(json.dumps(first).encode()) <= 262_144
    rows, problems = follow_all(tools, m)
    assert problems == [] and rows == ids                  # no loss, no duplicate, same order
    assert len(provider.requests) == 31                    # each follow-up is one real, counted call


def test_follow_ups_survive_a_restart_and_carry_no_token():
    ids = list(range(1, 601))
    provider = Provider({"2026-11-10": ids})
    manifest = call(boot(provider), "apifootball_fixtures", {"date": "2026-11-10"})["data"]
    blob = json.dumps(manifest["follow_up"])
    assert "token" not in blob and "signature" not in blob
    for c in manifest["follow_up"]["calls"]:
        assert set(c["arguments"]) <= {"ids", "id", "timezone"}
        validate("apifootball_fixtures", c["arguments"])   # plain, re-validated parameters
    rows, problems = follow_all(boot(provider), manifest)  # a brand-new process
    assert problems == [] and rows == ids


def test_two_requests_never_mix_their_rows():
    provider = Provider({"2026-11-10": list(range(1, 601)), "2026-11-11": list(range(10_001, 10_601))})
    tools = boot(provider)
    a = call(tools, "apifootball_fixtures", {"date": "2026-11-10"})["data"]
    b = call(tools, "apifootball_fixtures", {"date": "2026-11-11"})["data"]
    assert a["response_sha256"] != b["response_sha256"]

    async def both():
        async def run(m):
            got = []
            for c in m["follow_up"]["calls"]:
                out = await tools["apifootball_fixtures"](**c["arguments"])
                got += [r["fixture"]["id"] for r in out["data"]["response"]]
            return got
        return await asyncio.gather(run(a), run(b))
    rows_a, rows_b = asyncio.run(both())
    assert rows_a == list(range(1, 601)) and rows_b == list(range(10_001, 10_601))
    assert not set(rows_a) & set(rows_b)


def test_a_change_between_reads_is_detected():
    ids = list(range(1, 601))
    provider = Provider({"2026-11-10": ids})
    tools = boot(provider)
    m = call(tools, "apifootball_fixtures", {"date": "2026-11-10"})["data"]
    provider.days["2026-11-10"] = [i for i in ids if i != 55]   # fixture 55 removed upstream
    rows, problems = follow_all(tools, m)
    assert len(problems) == 1 and 55 not in rows               # the gap is visible, never silent
    reread = call(tools, "apifootball_fixtures", {"date": "2026-11-10"})["data"]
    assert reread["response_sha256"] != m["response_sha256"] and reread["results_declared"] == 599


def test_an_oversized_ids_request_is_halved_and_a_single_id_is_not_splittable():
    ids = list(range(1, 601))
    m = id_manifest("apifootball_fixtures", {"ids": "-".join(map(str, ids[:20]))},
                    {"response": [fixture_row(i) for i in ids[:20]], "results": 20})
    calls = m["follow_up"]["calls"]
    assert [c["expect_ids"] for c in calls] == [ids[:10], ids[10:20]]
    one = id_manifest("apifootball_fixtures", {"id": 7}, {"response": [fixture_row(7)], "results": 1})
    assert one["follow_up"]["splittable"] is False and one["complete_retrieval_available"] is False
    assert one["follow_up"]["reason"] == "NOT_SPLITTABLE_SINGLE_FIXTURE_ID"


def test_native_pagination_and_per_id_split_for_players():
    provider = Provider({})
    tools = boot(provider)
    out = call(tools, "apifootball_players", {"team": 33, "season": 2026, "page": 2})
    assert out["status"] == "PARTIAL_ID_MANIFEST"
    m = out["data"]
    assert m["next_page_arguments"] == {"team": 33, "season": 2026, "page": 3}
    validate("apifootball_players", m["next_page_arguments"])
    assert len(m["follow_up"]["calls"]) == 500
    for c in m["follow_up"]["calls"][:5]:
        assert c["arguments"] == {"team": 33, "season": 2026, "id": c["expect_ids"][0]}
        validate("apifootball_players", c["arguments"])


@pytest.mark.parametrize("tool,request_args,rows", [
    ("apifootball_leagues", {"season": 2026}, [{"league": {"id": i}} for i in range(1, 30)]),
    ("apifootball_teams", {"league": 39, "season": 2026}, [{"team": {"id": i}} for i in range(1, 30)]),
    ("apifootball_venues", {"country": "Italy"}, [{"id": i} for i in range(1, 30)]),
    ("apifootball_coaches", {"search": "smith"}, [{"id": i} for i in range(1, 30)]),
    ("apifootball_player_profiles", {"page": 101}, [{"player": {"id": i}} for i in range(1, 30)]),
    ("apifootball_injuries", {"league": 39, "season": 2026}, [{"fixture": {"id": i // 2 + 1}} for i in range(30)]),
    ("apifootball_transfers", {"team": 33}, [{"player": {"id": i}} for i in range(1, 30)]),
    ("apifootball_fixtures", {"league": 39, "season": 2026}, [{"fixture": {"id": i}} for i in range(1, 30)]),
])
def test_every_strategy_yields_valid_plain_calls_covering_each_row_once(tool, request_args, rows):
    m = id_manifest(tool, request_args, {"response": rows, "results": len(rows)})
    assert m["complete_retrieval_available"] is True
    covered = []
    for c in m["follow_up"]["calls"]:
        validate(tool, c["arguments"])
        covered += c["expect_ids"] * (c["expect_rows"] // len(c["expect_ids"]))
    path = STRATEGIES[tool].id_path
    original = []
    for r in rows:
        node = r
        for k in path:
            node = node[k]
        original.append(node)
    assert sorted(covered) == sorted(original)


def test_rows_without_ids_or_unknown_endpoints_are_never_claimed_complete():
    m = id_manifest("apifootball_fixtures", {"date": "2026-11-10"},
                    {"response": [fixture_row(1), {"fixture": {"id": None}}], "results": 2})
    assert m["complete_retrieval_available"] is False and m["rows_without_id"] == 1
    assert m["follow_up"]["reason"] == "ROWS_WITHOUT_IDENTIFIER"
    n = id_manifest("apifootball_trophies", {"player": 1}, {"response": [{"league": "x"}] * 3, "results": 3})
    assert n["complete_retrieval_available"] is False
    assert n["follow_up"]["reason"] == "NO_DOCUMENTED_SPLIT_FOR_THIS_ENDPOINT"
    with pytest.raises(ValueError):
        id_manifest("apifootball_fixtures", {}, {"response": {"not": "a list"}})


def test_duplicate_ids_in_a_reply_are_not_silently_merged():
    m = id_manifest("apifootball_fixtures", {"date": "2026-11-10"},
                    {"response": [fixture_row(1), fixture_row(1)], "results": 2})
    assert m["complete_retrieval_available"] is False and m["follow_up"]["reason"] == "DUPLICATE_FIXTURE_IDS_IN_REPLY"


def test_hash_is_order_sensitive_and_value_exact():
    a = [fixture_row(1), fixture_row(2)]
    assert response_sha256(a) != response_sha256(list(reversed(a)))
    b = json.loads(json.dumps(a))
    b[0]["goals"]["home"] = 0  # null -> 0 is a change
    assert response_sha256(a) != response_sha256(b)


def test_above_the_read_cap_nothing_is_parsed_and_the_answer_says_not_complete():
    provider = Provider({"2026-11-10": list(range(1, 1001))})  # ~600 KB > 512 KiB read cap
    tools = boot(provider)
    out = call(tools, "apifootball_fixtures", {"date": "2026-11-10"})
    assert out["status"] == "PROVIDER_ERROR" and out["error"] == {"code": "PROVIDER_RESPONSE_TOO_LARGE"}
    assert out["navigation"]["complete_retrieval_available"] is False
    assert out["navigation"]["reason"] == "READ_CAP_EXCEEDED_BEFORE_PARSE" and out["navigation"]["row_ids"] is None
    assert out["network_calls_made"] == 1 and out["data"] is None


def test_e2e_runner_treats_an_id_manifest_as_partial_never_pass():
    from scripts import mcp_e2e_render as e2e
    m = id_manifest("apifootball_fixtures", {"date": "2026-11-10"},
                    {"response": [fixture_row(1), fixture_row(2)], "results": 2})
    p = {"status": "PARTIAL_ID_MANIFEST", "data": m, "http_status": 200, "endpoint": "/fixtures"}
    assert e2e._classify(p) == "PARTIAL"
    facts = {}
    assert e2e.check_id_manifest(p, facts) == [] and facts["follow_up_calls"] == 1
    assert e2e.check_id_manifest({"status": "PARTIAL_ID_MANIFEST", "data": {"partial": True}}, {}) == \
        ["ID_MANIFEST_NOT_EXPLICIT"]
    assert e2e.stop_reason_for(p) is None
