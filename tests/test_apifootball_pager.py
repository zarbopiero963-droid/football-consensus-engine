"""WP3: resumable, quota-bounded multi-page scan (bridge/api_football/pager.py). Library only.

Offline: the REAL ResilientFootballClient and the REAL InjectedHTTPXTransport run over
httpx.MockTransport (in memory) with the in-memory stand-in of the shared PostgreSQL coordinator.
Sockets are disabled for the whole module, so a real HTTP request would fail the test.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import socket

import httpx
import pytest

from bridge.api_football import pager
from bridge.api_football.client import MockFootballClient
from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.contracts import ENDPOINTS
from bridge.api_football.pager import COMPLETE, PARTIAL, REFUSED, ResumeToken, scan_pages
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.transport import InjectedHTTPXTransport
from tests.apifootball_fake_coordinator import FakeCoordinator

KEY = "LOCAL_FAKE_KEY_PAGER_ONLY"
PROFILES = "apifootball_player_profiles"
PRICE_SENTINEL = "9.876543"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("real network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _rows(page: int, per_page: int, *, start: int | None = None) -> list[dict]:
    first = (page - 1) * per_page + 1 if start is None else start
    return [{"player": {"id": first + i, "name": f"Player {first + i}"}} for i in range(per_page)]


class Provider:
    """Deterministic fake provider: `pages[page] -> (status, body)`; default: a clean page."""

    def __init__(self, total: int, per_page: int = 20, path: str = "players/profiles"):
        self.total, self.per_page, self.path = total, per_page, path
        self.requests: list[dict] = []
        self.overrides: dict[int, tuple[int, object]] = {}

    def body(self, page: int, **changes) -> dict:
        rows = _rows(page, self.per_page) if page <= self.total else []
        body = {"get": self.path, "parameters": {"page": str(page)}, "errors": [], "results": len(rows),
                "paging": {"current": page, "total": self.total}, "response": rows}
        body.update(changes)
        return body

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        page = int(params.get("page", "1"))
        status, body = self.overrides.get(page, (200, self.body(page)))
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers={"content-type": "application/json"})
        return httpx.Response(status, json=body)


def _client(provider: Provider, coordinator: FakeCoordinator | None = None, *, live: bool = True,
            cache: bool = False, attempts: int = 1) -> ResilientFootballClient:
    settings = Settings(enabled=True, api_key=KEY, live_authorized=live, cache_enabled=cache)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(provider.handler))
    return ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY),
                                   quota=coordinator or FakeCoordinator(),
                                   retry=RetryPolicy(max_attempts=attempts, base_delay=0.0, max_delay=0.0))


def scan(client, tool=PROFILES, params=None, **kw):
    return asyncio.run(scan_pages(client, tool, {} if params is None else params, **kw))


def _pages_requested(provider: Provider) -> list[int]:
    return [int(p["page"]) for p in provider.requests]


# ------------------------------------------------------------------ complete scans, no page cap

@pytest.mark.parametrize("total", [1, 25, 150])
def test_all_pages_are_fetched_with_one_coordinated_call_per_page(total):
    provider, coordinator = Provider(total, per_page=3), FakeCoordinator()
    result = scan(_client(provider, coordinator), max_pages=100_000)
    assert result.status == COMPLETE and result.reason is None
    assert result.pages_fetched == total and result.paging_total == total and result.next_page is None
    assert _pages_requested(provider) == list(range(1, total + 1))  # in order, never twice
    assert coordinator.used == total and len(coordinator.released) == total and not coordinator.active
    assert [e["page"] for e in result.pages] == list(range(1, total + 1))
    assert len(result.rows) == 3 * total
    assert len({r["player"]["id"] for r in result.rows}) == 3 * total
    assert result.network_calls_made == total


def test_twenty_five_pages_prove_there_is_no_ten_page_cap():
    provider = Provider(25)
    result = scan(_client(provider), max_pages=25)
    assert result.status == COMPLETE and result.pages_fetched == 25 and len(result.rows) == 500


def test_page_budget_is_required_and_has_no_hidden_default():
    signature = inspect.signature(scan_pages)
    assert signature.parameters["max_pages"].default is inspect.Parameter.empty
    assert signature.parameters["max_pages"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(pager)
    assert "max_pages=10" not in source.replace(" ", "") and "[:10]" not in source


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, None, "5", pager.MAX_PAGE + 1])
def test_invalid_page_budget_is_refused_before_any_call(budget):
    provider, coordinator = Provider(3), FakeCoordinator()
    result = scan(_client(provider, coordinator), max_pages=budget)
    assert result.status == REFUSED and result.error == {"code": "INVALID_PAGE_BUDGET"}
    assert provider.requests == [] and coordinator.used == 0 and result.network_calls_made == 0


def test_per_page_evidence_records_page_results_and_response_hash():
    provider = Provider(2, per_page=4)
    result = scan(_client(provider), max_pages=5)
    first = result.pages[0]
    assert first["page"] == 1 and first["results"] == 4 and first["rows"] == 4
    assert first["paging"] == {"current": 1, "total": 2}
    expected = pager.response_sha256(_rows(1, 4))
    assert first["response_sha256"] == expected and first["hash_basis"] == "CANONICAL_JSON_OF_RESPONSE"
    assert first["cache_status"] == "MISS" and first["provider_attempts"] == 1


def test_empty_listing_is_complete_with_zero_rows():
    provider = Provider(0)
    provider.overrides[1] = (200, provider.body(1, paging={"current": 1, "total": 0}))
    result = scan(_client(provider), max_pages=3)
    assert result.status == COMPLETE and result.rows == [] and result.paging_total == 0
    assert result.pages_fetched == 1


# ------------------------------------------------------------------ budget stop and resume

def test_budget_stop_is_partial_with_the_next_page_and_resume_completes_without_duplicates():
    provider, coordinator = Provider(25), FakeCoordinator()
    client = _client(provider, coordinator)
    first = scan(client, max_pages=7)
    assert first.status == PARTIAL and first.reason == "PAGE_BUDGET_EXHAUSTED"
    assert first.pages_fetched == 7 and first.next_page == 8 and first.paging_total == 25
    assert len(first.rows) == 140 and coordinator.used == 7
    token = first.resume_token()
    assert token.next_page == 8 and token.total == 25 and token.covered_from_page == 1
    second = scan(client, max_pages=1000, resume=token)
    assert second.status == COMPLETE and second.covered_from_page == 1 and second.start_page == 8
    assert [e["page"] for e in second.pages] == list(range(8, 26))
    assert _pages_requested(provider) == list(range(1, 26))  # no page fetched twice
    ids = [r["player"]["id"] for r in first.rows + second.rows]
    assert len(ids) == len(set(ids)) == 500 and coordinator.used == 25


def test_resume_from_a_bare_page_is_never_complete():
    provider = Provider(12)
    result = scan(_client(provider), max_pages=100, resume=ResumeToken.from_page(PROFILES, {}, 10))
    assert _pages_requested(provider) == [10, 11, 12]
    assert result.status == PARTIAL and result.reason == "PAGES_BEFORE_START_NOT_COVERED"
    assert result.next_page is None and result.covered_from_page == 10 and len(result.rows) == 60


def test_resume_token_for_other_parameters_is_refused():
    provider = Provider(5)
    client = _client(provider)
    params = {"player": 276}
    first = scan(client, params=params, max_pages=2)
    other = scan(client, params={"player": 277}, max_pages=2, resume=first.resume_token())
    assert other.status == REFUSED and other.error == {"code": "RESUME_REQUEST_MISMATCH"}
    assert len(provider.requests) == 2


def test_resume_with_a_changed_total_is_refused():
    provider = Provider(25)
    client = _client(provider)
    first = scan(client, max_pages=3)
    provider.total = 26
    second = scan(client, max_pages=3, resume=first.resume_token())
    assert second.status == REFUSED and second.error["code"] == "PAGING_TOTAL_CHANGED"
    assert second.rows is None and second.next_page is None


def test_duplicate_across_a_resume_is_refused():
    provider = Provider(4, per_page=2)
    client = _client(provider)
    first = scan(client, max_pages=2)
    provider.overrides[3] = (200, provider.body(3, response=_rows(1, 2)))  # page 1 rows again
    second = scan(client, max_pages=5, resume=first.resume_token())
    assert second.status == REFUSED and second.error == {"code": "DUPLICATE_NATURAL_ID", "page": 3}


def test_no_resume_token_once_complete_or_refused():
    provider = Provider(2)
    done = scan(_client(provider), max_pages=5)
    with pytest.raises(ValueError):
        done.resume_token()


# ------------------------------------------------------------------ quota, rate limit, circuit

def test_quota_refusal_mid_scan_is_partial_and_keeps_the_pages_already_fetched():
    provider, coordinator = Provider(25), FakeCoordinator(budget=5)
    result = scan(_client(provider, coordinator), max_pages=25)
    assert result.status == PARTIAL and result.reason == "LOCAL_DAILY_QUOTA_EXHAUSTED"
    assert result.error["code"] == "LOCAL_DAILY_QUOTA_EXHAUSTED"
    assert result.pages_fetched == 5 and result.next_page == 6 and len(result.rows) == 100
    assert _pages_requested(provider) == [1, 2, 3, 4, 5] and coordinator.used == 5
    assert result.network_calls_made == 5


def test_quota_refusal_on_the_first_page_sends_nothing():
    provider, coordinator = Provider(3), FakeCoordinator(budget=0)
    result = scan(_client(provider, coordinator), max_pages=3)
    assert result.status == PARTIAL and result.reason == "LOCAL_DAILY_QUOTA_EXHAUSTED"
    assert result.pages_fetched == 0 and result.next_page == 1 and provider.requests == []
    assert result.network_calls_made == 0


def test_provider_rate_limit_stops_the_scan_resumably():
    provider = Provider(6)
    provider.overrides[3] = (429, {"message": "too many"})
    result = scan(_client(provider), max_pages=6)
    assert result.status == PARTIAL and result.reason == "PROVIDER_RATE_LIMITED"
    assert result.next_page == 3 and result.pages_fetched == 2 and len(result.rows) == 40


def test_circuit_breaker_refusal_stops_the_scan():
    provider = Provider(6)
    provider.overrides[2] = (500, {"message": "x"})
    provider.overrides[3] = (500, {"message": "x"})
    client = _client(provider)
    first = scan(client, max_pages=6)
    assert first.status == PARTIAL and first.reason == "PROVIDER_SERVER_ERROR" and first.next_page == 2
    second = scan(client, max_pages=6, resume=first.resume_token())
    assert second.status == PARTIAL and second.reason == "PROVIDER_SERVER_ERROR"  # breaker opens now
    third = scan(client, max_pages=6, resume=second.resume_token())
    assert third.status == PARTIAL and third.reason == "CIRCUIT_OPEN" and third.network_calls_made == 0
    assert _pages_requested(provider) == [1, 2, 2]


def test_retries_are_coordinated_too():
    provider, coordinator = Provider(2), FakeCoordinator()
    provider.overrides[2] = (503, {"message": "x"})
    result = scan(_client(provider, coordinator, attempts=3), max_pages=2)
    assert result.status == PARTIAL and result.network_calls_made == 1 + 3
    assert coordinator.used == len(provider.requests) == 4


# ------------------------------------------------------------------ inconsistencies fail closed

def _refused(provider: Provider, code: str, page: int, **kw):
    result = scan(_client(provider), max_pages=50, **kw)
    assert result.status == REFUSED, (result.status, result.error)
    assert result.error["code"] == code and result.error.get("page") == page
    assert result.rows is None and result.next_page is None
    # The evidence of the pages already fetched is kept; nothing is presented as a dataset.
    assert [e["page"] for e in result.pages] == list(range(1, page))
    payload = result.to_payload()
    assert payload["rows"] is None and payload["status"] == REFUSED
    return result


def test_page_echo_mismatch_is_refused():
    provider = Provider(6)
    provider.overrides[4] = (200, provider.body(1))  # page 1 served for page 4
    _refused(provider, "PAGE_ECHO_MISMATCH", 4)


def test_total_change_during_the_scan_is_refused():
    provider = Provider(6)
    provider.overrides[3] = (200, provider.body(3, paging={"current": 3, "total": 7}))
    _refused(provider, "PAGING_TOTAL_CHANGED", 3)


def test_page_beyond_total_is_refused():
    provider = Provider(6)
    provider.overrides[3] = (200, provider.body(3, paging={"current": 3, "total": 2}, response=[], results=0))
    _refused(provider, "PAGE_BEYOND_TOTAL", 3)


def test_empty_page_before_total_is_refused():
    # F-A10: the provider echoes total = requested page for a page beyond the data.
    provider = Provider(6)
    provider.overrides[4] = (200, provider.body(4, response=[], results=0))
    _refused(provider, "EMPTY_PAGE", 4)


@pytest.mark.parametrize("paging", [{"current": "3", "total": 6}, {"current": 3, "total": -1},
                                    {"current": True, "total": 6}, {"current": 3, "total": 6.0}, [3, 6]])
def test_non_integer_or_negative_paging_is_refused(paging):
    provider = Provider(6)
    provider.overrides[3] = (200, provider.body(3, paging=paging))
    _refused(provider, "PROVIDER_INVALID_PAGINATION", 3)  # the transport's own fixed code


def test_missing_paging_is_refused():
    provider = Provider(6)
    body = provider.body(2)
    del body["paging"]
    provider.overrides[2] = (200, body)
    _refused(provider, "PAGING_NOT_PUBLISHED", 2)


def test_results_not_matching_the_rows_is_refused():
    provider = Provider(6)
    provider.overrides[2] = (200, provider.body(2, results=19))
    _refused(provider, "RESULTS_ROWS_MISMATCH", 2)


def test_missing_results_stays_missing_and_is_refused():
    provider = Provider(6)
    body = provider.body(2)
    del body["results"]
    provider.overrides[2] = (200, body)
    _refused(provider, "RESULTS_NOT_PUBLISHED", 2)


def test_response_not_a_list_is_refused():
    provider = Provider(6)
    provider.overrides[2] = (200, provider.body(2, response={"a": 1}, results=1))
    _refused(provider, "RESPONSE_NOT_LIST", 2)


def test_provider_errors_are_refused_with_the_existing_classification():
    provider = Provider(6)
    provider.overrides[3] = (200, provider.body(3, errors={"requests": "limit reached"}, response=[], results=0))
    result = _refused(provider, "PROVIDER_DECLARED_ERRORS", 3)
    assert result.error["keys"] == ["requests"] and result.error["category"] == "DAILY_QUOTA"
    assert "limit reached" not in json.dumps(result.to_payload())


def test_duplicate_natural_id_across_pages_is_refused():
    provider = Provider(6, per_page=3)
    provider.overrides[3] = (200, provider.body(3, response=_rows(3, 3, start=6)))  # id 6 is on page 2
    _refused(provider, "DUPLICATE_NATURAL_ID", 3)


def test_duplicate_natural_id_within_a_page_is_refused():
    provider = Provider(6, per_page=3)
    provider.overrides[2] = (200, provider.body(2, response=_rows(2, 2) + _rows(2, 1)))
    provider.overrides[2][1]["results"] = 3
    _refused(provider, "DUPLICATE_NATURAL_ID", 2)


@pytest.mark.parametrize("bad", [None, "7", 0, -3, True, {"x": 1}])
def test_row_without_a_valid_natural_id_is_refused(bad):
    provider = Provider(6, per_page=2)
    rows = _rows(2, 2)
    rows[1]["player"]["id"] = bad
    provider.overrides[2] = (200, provider.body(2, response=rows))
    _refused(provider, "NATURAL_ID_MISSING", 2)


def test_invalid_json_page_is_refused():
    provider = Provider(6)
    provider.overrides[2] = (200, b"{not json")
    _refused(provider, "PROVIDER_INVALID_JSON", 2)


# ------------------------------------------------------------------ endpoints without a natural id

def test_endpoint_without_a_natural_id_counts_duplicate_rows_without_removing_them():
    provider = Provider(3, per_page=2, path="odds/mapping")
    row = {"league": {"id": 39, "season": 2026}, "fixture": {"id": 5, "date": "2026-01-01T00:00:00+00:00"}}
    provider.overrides[1] = (200, provider.body(1, response=[row, {**row, "fixture": {"id": 6}}]))
    provider.overrides[2] = (200, provider.body(2, response=[row, {**row, "fixture": {"id": 7}}]))
    result = scan(_client(provider), tool="apifootball_odds_mapping", max_pages=10)
    assert result.status == COMPLETE and result.natural_id is None
    assert len(result.rows) == 6 and result.duplicate_rows_by_hash == 1  # reported, never dropped
    assert result.to_payload()["duplicate_rows_by_hash"] == 1


# ------------------------------------------------------------------ refusals before any call

@pytest.mark.parametrize("tool", sorted(n for n, e in ENDPOINTS.items() if not e.pagination))
def test_endpoint_without_a_documented_page_parameter_is_refused(tool):
    provider, coordinator = Provider(3), FakeCoordinator()
    result = scan(_client(provider, coordinator), tool=tool, max_pages=3)
    assert result.status == REFUSED and result.error == {"code": "PAGINATION_NOT_DOCUMENTED"}
    assert provider.requests == [] and coordinator.used == 0


def test_paged_endpoints_are_exactly_those_whose_contract_declares_page():
    assert sorted(pager.pageable_tools()) == sorted(n for n, e in ENDPOINTS.items() if "page" in e.fields)


def test_page_parameter_is_owned_by_the_pager():
    provider = Provider(3)
    result = scan(_client(provider), params={"page": 2}, max_pages=3)
    assert result.status == REFUSED and result.error == {"code": "PAGE_PARAMETER_RESERVED"}
    assert provider.requests == []


def test_invalid_parameters_reuse_the_contract_codes():
    provider = Provider(3)
    result = scan(_client(provider), tool="apifootball_players", params={"team": 33}, max_pages=3)
    assert result.status == REFUSED and result.error == {"code": "PLAYER_FILTER_REQUIRED"}
    result = scan(_client(provider), tool="apifootball_players", params={"bogus": 1}, max_pages=3)
    assert result.error == {"code": "PARAMETER_NOT_SUPPORTED"}
    assert provider.requests == []


def test_client_outside_the_quota_coordinator_is_refused():
    mock = MockFootballClient({"/players/profiles": Provider(3).body(1)})
    result = scan(mock, max_pages=3)
    assert result.status == REFUSED and result.error == {"code": "PAGER_CLIENT_NOT_COORDINATED"}
    assert mock.calls == []


def test_live_gate_closed_sends_nothing():
    provider, coordinator = Provider(3), FakeCoordinator()
    result = scan(_client(provider, coordinator, live=False), max_pages=3)
    assert result.status == REFUSED and result.error == {"code": "LIVE_NOT_AUTHORIZED"}
    assert provider.requests == [] and coordinator.used == 0


# ------------------------------------------------------------------ large payloads and the MCP cap

def _big_rows(page: int, per_page: int, size: int) -> list[dict]:
    rows = _rows(page, per_page)
    for r in rows:
        r["player"]["odd"] = PRICE_SENTINEL
        r["player"]["bio"] = "x" * size
    return rows


def test_large_pages_complete_and_the_payload_is_a_manifest_under_the_mcp_cap():
    provider = Provider(4, per_page=20)
    for p in range(1, 5):  # ~400 KiB per page: under the 512 KiB read cap, above the 256 KiB MCP cap
        provider.overrides[p] = (200, provider.body(p, response=_big_rows(p, 20, 20_000)))
    result = scan(_client(provider), max_pages=10)
    assert result.status == COMPLETE and len(result.rows) == 80  # library keeps the rows
    payload = result.to_payload()
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    assert len(encoded) <= pager.MCP_PAYLOAD_CAP_BYTES
    assert payload["rows"] is None and payload["rows_status"] == "WITHHELD_ABOVE_MCP_PAYLOAD_CAP"
    assert PRICE_SENTINEL not in encoded.decode()
    assert payload["row_ids_by_page"]["1"] == list(range(1, 21))
    assert payload["status"] == COMPLETE and payload["pages_fetched"] == 4


def test_small_scan_payload_carries_the_rows():
    provider = Provider(2, per_page=2)
    payload = scan(_client(provider), max_pages=5).to_payload()
    assert payload["rows_status"] == "INCLUDED" and len(payload["rows"]) == 4
    assert payload["label"] == "PROVIDER_ROWS_WITH_DERIVED_SCAN_STATE"


def test_payload_manifest_degrades_to_counts_when_ids_do_not_fit():
    provider = Provider(300, per_page=250)
    result = scan(_client(provider), max_pages=300)
    assert result.status == COMPLETE and len(result.rows) == 75_000
    payload = result.to_payload()
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    assert len(encoded) <= pager.MCP_PAYLOAD_CAP_BYTES
    assert payload["rows"] is None and payload["row_ids_by_page"] is None
    assert payload["rows_status"] == "WITHHELD_ABOVE_MCP_PAYLOAD_CAP"
    assert payload["rows_count"] == 75_000


def test_page_above_the_read_cap_keeps_ids_only_and_is_never_complete():
    provider = Provider(3, per_page=20)
    provider.overrides[2] = (200, provider.body(2, response=_big_rows(2, 20, 30_000)))  # ~600 KiB: streamed
    result = scan(_client(provider), max_pages=10)
    assert result.status == PARTIAL and result.reason == "PAGE_ROWS_NOT_RETAINED_ABOVE_READ_CAP"
    assert result.next_page is None and result.pages_fetched == 3
    assert result.pages[1]["rows_mode"] == "STREAMED_SCAN_IDS_ONLY"
    assert result.pages[1]["hash_basis"] == "RAW_BODY_BYTES"
    assert result.row_ids_by_page[2] == list(range(21, 41))
    assert result.rows_not_retained_pages == [2]
    assert PRICE_SENTINEL not in json.dumps(result.to_payload())


def test_streamed_page_duplicates_are_still_refused():
    provider = Provider(3, per_page=20)
    provider.overrides[2] = (200, provider.body(2, response=_big_rows(1, 20, 30_000)))  # page 1 ids again
    _refused(provider, "DUPLICATE_NATURAL_ID", 2)


# ------------------------------------------------------------------ library only: no tool exposure

def test_pager_is_imported_by_no_tool_server_or_wiring_module():
    """WP3 is library-only (exposure is an owner decision): no MCP reply can change, which keeps
    the REUSED_EQUIVALENT rows of the certification matrix valid (CODE_PATH_CHANGES)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / "bridge" / "api_football").glob("*.py")), root / "bridge" / "server.py"]
    importers = [p.name for p in sources if p.name != "pager.py" and "pager" in p.read_text(encoding="utf-8")]
    assert importers == []


def test_tool_registry_is_unchanged():
    from bridge.api_football.tools import register_tools

    class Server:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    names = register_tools(Server(), Settings(enabled=True))
    assert len(names) == 42 and not any("pag" in n for n in names)
