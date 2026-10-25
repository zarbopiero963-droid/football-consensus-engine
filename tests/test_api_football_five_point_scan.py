"""End-to-end offline acceptance of the owner five-point probe."""
import asyncio
import httpx
from scripts.api_football_capability_probe import run

ENV = {
    "APIFOOTBALL_ENABLED": "true",
    "APIFOOTBALL_LIVE_AUTHORIZED": "true",
    "APIFOOTBALL_API_KEY": "FAKE_TEST_ONLY",
    "APIFOOTBALL_MAX_DAILY_REQUESTS": "60",
    "APIFOOTBALL_MAX_RPM": "8",
}

async def no_sleep(_):
    return None


def test_full_12_page_bookmaker_scan_multiple_samples_and_live_delta():
    urls = []
    live_calls = 0

    def reply(request):
        nonlocal live_calls
        assert request.url.host == "v3.football.api-sports.io"
        assert request.headers["x-apisports-key"] == ENV["APIFOOTBALL_API_KEY"]
        path = request.url.path
        urls.append((path, dict(request.url.params)))
        page = int(request.url.params.get("page", "1"))
        if path == "/odds/bookmakers":
            data = [{"id": page, "name": "Book %d" % page}]
            paging = {"current": page, "total": 12}
        elif path == "/fixtures":
            data = [{"fixture": {"id": n, "status": {"short": "1H" if
                request.url.params.get("live") == "all" else "FT"}},
                "teams": {"home": {"id": 100+n}, "away": {"id": 200+n}}}
                for n in (11, 12, 13)]
            paging = {"current": 1, "total": 1}
        elif path == "/odds":
            data = [{"fixture": {"id": int(request.url.params["fixture"])},
                     "bookmakers": [{"id": 1, "name": "Book 1",
                     "bets": [{"id": 1, "name": "Totals",
                     "values": [{"value": "Over 2.5", "odd": "1.90"}]}]}]}]
            paging = {"current": 1, "total": 1}
        elif path == "/odds/live":
            live_calls += 1
            data = [{"fixture": {"id": int(request.url.params["fixture"])},
                     "status": {"stopped": False},
                     "bets": [{"id": 1, "name": "Live totals",
                     "values": [{"value": "Over", "odd": "1.91" if live_calls == 1 else "1.99",
                                 "suspended": live_calls > 1}]}]}]
            paging = {"current": 1, "total": 1}
        else:
            data = [{"team": {"id": int(request.url.params.get("team", "101"))},
                     "player": {"id": 99}, "statistics": [{"shots": None}]}]
            paging = {"current": 1, "total": 1}
        return httpx.Response(200, json={"response": data, "errors": {}, "paging": paging},
                              headers={"x-ratelimit-requests-remaining": "7400",
                                       "x-ratelimit-remaining": "100"})

    records = asyncio.run(run(ENV, transport=httpx.MockTransport(reply), sleep=no_sleep))
    pages = [r["page"] for r in records if r.get("path") == "/odds/bookmakers"
             and r.get("status") == "OBSERVED"]
    assert pages == list(range(1, 13))
    assert len({params["fixture"] for path, params in urls
                if path == "/fixtures/statistics"}) == 3
    assert len({params["team"] for path, params in urls
                if path == "/teams/statistics"}) == 3
    assert len({params["team"] for path, params in urls
                if path == "/players"}) == 3
    delta = [r for r in records if r.get("status") == "LIVE_DELTA_OBSERVED"]
    assert len(delta) == 1
    assert delta[0]["delta"]["status"] == "CHANGE_OBSERVED"
    assert {x["field"] for x in delta[0]["delta"]["changes"]} >= {"odd", "suspended"}
    assert sum(p == "/odds/live" for p, _ in urls) == 2
    assert not any(r.get("status") == "PAGINATION_CAP_REACHED" for r in records)


def test_live_blocked_with_no_observed_fixture():
    def reply(request):
        if request.url.path == "/fixtures":
            return httpx.Response(200, json={"response": [], "errors": {},
                                              "paging": {"current": 1, "total": 1}})
        return httpx.Response(200, json={"response": [], "errors": {},
                                          "paging": {"current": 1, "total": 1}})
    records = asyncio.run(run(ENV, transport=httpx.MockTransport(reply), sleep=no_sleep))
    assert any(r.get("status") == "BLOCKED_NO_LIVE_FIXTURE" for r in records)
    assert not any(r.get("status") == "LIVE_DELTA_OBSERVED" for r in records)
