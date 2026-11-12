"""Owner gate P2-B (issue #187): the signer's own market reads are complete, canonical and fail closed.

Offline: an injected GET function plays SX Bet (pure fakes, plus the SimExchange end to end). The signer
package still imports no network library; nothing here can sign for real, send or move funds.
"""
import copy
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import market_reads as mr  # noqa: E402
from signer import rest_adapter as ra  # noqa: E402
from signer.core import SignerRefused  # noqa: E402
from sxbet_e2e_kit import H, Stack  # noqa: E402
from sxbet_signer_kit import ESCROW, META_RAW, TOKEN  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
H2, H3, H4 = "0x" + "b2" * 32, "0x" + "c3" * 32, "0x" + "d4" * 32
P = 10 ** 18


def row(h, event, one, two, *, league=1, sport=5, game=2_000_000_000):
    return {"marketHash": h, "status": "ACTIVE", "sportXeventId": event, "teamOneName": one, "teamTwoName": two,
            "outcomeOneName": one, "outcomeTwoName": two, "sportId": sport, "leagueId": league,
            "leagueLabel": f"L{league}", "gameTime": game, "type": 52}


class Fake:
    """GET /markets/active paginated by `page`; other routes static. Records every call."""

    def __init__(self, rows, *, page=2, book=None, overrides=None):
        self.rows, self.page, self.calls, self.overrides = rows, page, [], overrides or {}
        self.book = book or {"marketHash": H, "outcomeOne": [{"percentageOdds": str(41 * P), "size": "5000000"},
                                                              {"percentageOdds": str(45 * P), "size": "1000000"}],
                             "outcomeTwo": []}

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path in self.overrides:
            return self.overrides[path](params)
        if path == "/markets/find":
            return [r for r in self.rows if r["marketHash"] in params["marketHashes"]]
        if path == "/sports":
            return [{"sportId": x, "label": str(x)} for x in sorted({r["sportId"] for r in self.rows})]
        if path == "/markets/active":
            rows = self.rows
            if params.get("onlyMainLine"):  # strict reading: rows without mainLine dropped (DOCS_UNVERIFIED)
                rows = [r for r in rows if r.get("mainLine") is True]
            if "eventId" in params:
                rows = [r for r in rows if r["sportXeventId"] == params["eventId"]]
            if "sportIds" in params:
                rows = [r for r in rows if r["sportId"] in params["sportIds"]]
            start = int(params.get("paginationKey", 0))
            return {"markets": rows[start:start + self.page],
                    "nextKey": str(start + self.page) if start < len(rows) else None}
        if path == "/orderbook-v3/snapshot/market":
            return copy.deepcopy(self.book)
        if path == "/metadata/obv3":
            return copy.deepcopy(META_RAW)
        if path == "/user/balance-v3":
            return {"balances": [{"tokenAddress": TOKEN, "escrowAddress": ESCROW, "availableAmount": "50000000"}]}
        if path == "/user/fees-v3":
            return {"refundFee": None, "makerPayoutFee": None, "takerPayoutFee": "0.01",
                    "makerParlayPayoutFee": None, "takerParlayPayoutFee": "0.05"}
        raise AssertionError(path)


def provider(fake, **kw):
    return mr.RestMarketDataProvider(fake, base_token=TOKEN, escrow=ESCROW, **kw)


def read(fake, h=H, **kw):
    return provider(fake, **kw).read(market_hash=h, outcome_one=True, percentage_odds=42 * P, now=1_800_000_000)


BASE = [row(H, "E1", "Home FC", "Away FC"), row(H2, "E1", "Home FC", "Away FC"),
        row(H3, "E2", "Other", "Team"), row(H4, "E3", "Third", "Side")]


# ---------------------------------------------------------------- completeness: every page, fail closed
def test_reads_every_page_and_returns_the_full_context():
    fake = Fake(BASE, page=1)
    ctx = read(fake)
    assert ctx["market"]["marketHash"] == H and {m["marketHash"] for m in ctx["event_markets"]} == {H, H2}
    assert [e["sportXeventId"] for e in ctx["same_team_events"]] == ["E1"]
    active = [p for p, q in fake.calls if p == "/markets/active"]
    assert len(active) >= len(BASE) + 2  # page size 1: one call per row plus the final empty pages
    assert ctx["quote"] == {"outcome": 1, "best_percentage_odds": str(41 * P),
                            "available_liquidity_base_units": "5000000", "observed_at": 1_800_000_000,
                            "perspective": "TAKER (showTakerPerspective=true)"}
    assert ctx["balance"] == {"available_base_units": "50000000", "observed_at": 1_800_000_000}
    assert ctx["exchange_limits"]["order_size_minimum_base_units"] == "1000000"
    assert ctx["fees"]["makerPayoutFee"] is None  # null stays null


def test_sport_scan_uses_official_params_only():
    fake = Fake(BASE)
    read(fake)
    scans = [q for p, q in fake.calls if p == "/markets/active" and "sportIds" in q]
    # Review 98e7525 P1-1 (intentional change): no onlyMainLine, its effect is not stated by the contract.
    assert scans and all("onlyMainLine" not in q and q["pageSize"] == 100 for q in scans)
    allowed = {"eventId", "sportIds", "pageSize", "paginationKey"}
    assert all(set(q) <= allowed for p, q in fake.calls if p == "/markets/active")


def test_page_budget_exhausted_is_a_refusal_never_a_partial_scan():
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(BASE, page=1), max_pages=2)
    assert e.value.code == "MARKETS_PAGINATION_INCOMPLETE"


def test_repeated_cursor_is_a_refusal():
    def loop(params):
        return {"markets": [row(H, "E1", "Home FC", "Away FC")], "nextKey": "same"}
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(BASE, overrides={"/markets/active": loop}))
    assert e.value.code == "MARKETS_PAGINATION_LOOP"


@pytest.mark.parametrize("page,code", [
    ({"nextKey": None}, "MARKETS_PAGE_MALFORMED"),
    ({"markets": "x", "nextKey": None}, "MARKETS_PAGE_MALFORMED"),
    ({"markets": [{"status": "ACTIVE"}], "nextKey": None}, "MARKETS_ROW_MALFORMED"),
    ({"markets": [row(H, "E1", "Home FC", "Away FC"), row(H, "E9", "Home FC", "Away FC")], "nextKey": None},
     "MARKETS_ROW_CONFLICT"),
])
def test_malformed_or_conflicting_pages_are_refusals(page, code):
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(BASE, overrides={"/markets/active": lambda p: page}))
    assert e.value.code == code


def test_a_missing_page_is_a_refusal():
    calls = {"n": 0}

    def flaky(params):
        calls["n"] += 1
        if params.get("paginationKey") == "1":
            raise RuntimeError("timeout")
        return {"markets": BASE[:1], "nextKey": "1"}
    with pytest.raises(RuntimeError):
        read(Fake(BASE, overrides={"/markets/active": flaky}))


def test_event_scan_must_contain_the_order_market():
    rows = [row(H2, "E1", "Home FC", "Away FC")]
    fake = Fake(rows, overrides={"/markets/find": lambda p: [row(H, "E1", "Home FC", "Away FC")]})
    with pytest.raises(mr.MarketReadError) as e:
        read(fake)
    assert e.value.code == "EVENT_MARKETS_INCOMPLETE"


# ---------------------------------------------------------------- canonical normalisation and ambiguity
@pytest.mark.parametrize("one,two", [("HOME FC", "away fc"), ("Hómé FC", "Áwáy FC"), ("Home  FC", " Away FC "),
                                     ("Away FC", "Home FC")])
def test_same_team_events_are_found_canonically_in_any_league_or_date(one, two):
    rows = BASE + [row("0x" + "e5" * 32, "E7", one, two, league=99, game=2_100_000_000)]
    ctx = read(Fake(rows))
    assert [e["sportXeventId"] for e in ctx["same_team_events"]] == ["E1", "E7"]
    assert ctx["same_team_events"][1]["leagueId"] == 99


@pytest.mark.parametrize("one", ["Home FC Women", "Home FC U21", "Home"])
def test_different_names_are_not_fuzzy_matched(one):
    rows = BASE + [row("0x" + "e5" * 32, "E7", one, "Away FC")]
    assert [e["sportXeventId"] for e in read(Fake(rows))["same_team_events"]] == ["E1"]


def test_rows_without_team_names_are_a_refusal_not_ignored():
    rows = BASE + [dict(row("0x" + "e5" * 32, "E7", "x", "y"), teamOneName=None)]
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(rows))
    assert e.value.code == "MARKETS_TEAMS_MISSING"


def test_inconsistent_rows_of_one_event_are_a_refusal():
    rows = BASE + [row("0x" + "e5" * 32, "E1", "Home FC", "Away FC", game=2_000_000_999)]
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(rows))
    assert e.value.code == "MARKETS_EVENT_INCONSISTENT"


def test_sport_scan_without_the_order_market_is_a_refusal():
    """Intentional change (review 98e7525): the full sport scan must contain the order's own market."""
    def scan(params):
        rows = [r for r in BASE if r["sportXeventId"] != "E1"] if "sportIds" in params else \
            [r for r in BASE if r["sportXeventId"] == params.get("eventId")]
        return {"markets": rows, "nextKey": None}
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(BASE, overrides={"/markets/active": scan}))
    assert e.value.code == "SPORT_SCAN_INCOMPLETE"


def test_review_p1_1_an_event_without_main_line_markets_is_still_found():
    """RED on 98e7525: with a strict onlyMainLine reading, the line-less twin event vanished from the scan."""
    rows = [dict(row(H, "E1", "Home FC", "Away FC"), mainLine=True),
            row("0x" + "e5" * 32, "E7", "Home FC", "Away FC", league=50)]  # no mainLine field at all
    assert [e["sportXeventId"] for e in read(Fake(rows))["same_team_events"]] == ["E1", "E7"]


def test_review_p2_1_every_sport_is_scanned_by_default():
    rows = BASE + [row("0x" + "e5" * 32, "E7", "Home FC", "Away FC", sport=7)]
    fake = Fake(rows)
    assert [e["sportXeventId"] for e in read(fake)["same_team_events"]] == ["E1", "E7"]
    assert {tuple(q["sportIds"]) for p, q in fake.calls if p == "/markets/active" and "sportIds" in q} == {(5,), (7,)}
    with pytest.raises(mr.MarketReadError) as e:  # an owner list that excludes the market's own sport
        read(Fake(rows), scan_sport_ids=[7])
    assert e.value.code == "MARKET_SPORT_NOT_SCANNED"


def test_review_p3_2_same_hash_with_different_labels_is_a_conflict():
    page = {"markets": [row(H, "E1", "Home FC", "Away FC"), dict(row(H, "E1", "Home FC", "Away FC"),
                                                                 outcomeOneName="Over 2.5")], "nextKey": None}
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(BASE, overrides={"/markets/active": lambda p: page}))
    assert e.value.code == "MARKETS_ROW_CONFLICT"


def test_review_p3_3_find_and_active_rows_must_agree():
    fake = Fake(BASE, overrides={"/markets/find": lambda p: [dict(row(H, "E1", "Home FC", "Away FC"), line=2.5)]})
    with pytest.raises(mr.MarketReadError) as e:
        read(fake)
    assert e.value.code == "MARKET_READS_DISAGREE"


# ---------------------------------------------------------------- quote, balance, metadata: missing never 0
def test_empty_book_side_has_no_best_price_and_liquidity_only_at_or_better_than_limit():
    ctx = read(Fake(BASE, book={"marketHash": H, "outcomeOne": [], "outcomeTwo": []}))
    assert ctx["quote"]["best_percentage_odds"] is None and ctx["quote"]["available_liquidity_base_units"] == "0"


@pytest.mark.parametrize("path,data,code", [
    ("/user/balance-v3", {"balances": []}, "BALANCE_ROW_NOT_UNIQUE"),
    ("/user/balance-v3", {"balances": [{"tokenAddress": TOKEN, "escrowAddress": ESCROW}]}, "BALANCE_MALFORMED"),
    ("/user/fees-v3", {"takerPayoutFee": "0.01"}, "FEES_MALFORMED"),
    ("/metadata/obv3", dict(META_RAW, activeAsset=dict(META_RAW["activeAsset"], baseToken="0x" + "99" * 20)),
     "METADATA_BASE_TOKEN_MISMATCH"),
    ("/orderbook-v3/snapshot/market", {"marketHash": H2, "outcomeOne": [], "outcomeTwo": []},
     "ORDER_BOOK_MARKET_MISMATCH"),
    ("/orderbook-v3/snapshot/market", {"marketHash": H, "outcomeOne": [{"percentageOdds": 1, "size": "1"}],
                                       "outcomeTwo": []}, "ORDER_BOOK_LEVEL_MALFORMED"),
    ("/markets/find", [], "MARKET_NOT_FOUND"),
])
def test_missing_or_malformed_reads_are_refusals(path, data, code):
    with pytest.raises(mr.MarketReadError) as e:
        read(Fake(BASE, overrides={path: lambda p: copy.deepcopy(data)}))
    assert e.value.code == code


def test_no_network_library_and_no_key_in_the_module():
    src = (ROOT / "signer/market_reads.py").read_text()
    for banned in ("import httpx", "import requests", "import urllib", "import socket", "aiohttp", "API_KEY",
                   "x-sx-api-key", "psycopg"):
        assert banned not in src


# ---------------------------------------------------------------- end to end: the signer on these reads
def _sim_get(sim):
    def get(path, params):
        query = tuple((k, ",".join(map(str, v)) if isinstance(v, list) else
                       ("true" if v is True else "false" if v is False else str(v))) for k, v in params.items())
        resp = sim.send(ra.RestRequest("GET", path, query, None))
        assert resp.http_status == 200, (path, resp.http_status)
        return resp.body["data"]
    return get


@pytest.fixture
def s(tmp_path):
    st = Stack(tmp_path)
    object.__setattr__(st.signer.config, "market_data",
                       mr.RestMarketDataProvider(_sim_get(st.sim), base_token=TOKEN, escrow=ESCROW))
    return st


def _sign(s, key, text=None):
    i = s.intent(key)
    ctx = s.context(i)  # only to build the owner's command text; the signer reads through the provider
    return s.signer.sign_order(i, s.command(i, ctx, text=text))


def test_e2e_signer_signs_on_its_own_complete_reads(s):
    out = _sign(s, "order-key-S5E01")
    assert out["order_id"].startswith("0x")


def test_e2e_same_teams_in_another_league_make_the_command_ambiguous(s):
    m = s.sim.add_market("0x" + "f6" * 32, "OTHER-EVENT", "HOME FC", "Away FC")
    m.league_id = 777
    with pytest.raises(SignerRefused) as e:
        _sign(s, "order-key-S5E02")
    assert e.value.code == "COMMAND_AMBIGUOUS_EVENT"


def test_e2e_many_pages_still_complete(s):
    for n in range(250):  # 3 pages of 100 in the sport scan
        s.sim.add_market("0x" + f"{n:064x}", f"EV{n}", f"Team {n}", f"Rival {n}")
    assert _sign(s, "order-key-S5E03")["order_id"].startswith("0x")


def test_e2e_provider_failure_is_a_fixed_refusal(s):
    object.__setattr__(s.signer.config, "market_data",
                       mr.RestMarketDataProvider(_sim_get(s.sim), base_token=TOKEN, escrow=ESCROW, max_pages=1))
    for n in range(150):
        s.sim.add_market("0x" + f"{n:064x}", f"EV{n}", f"Team {n}", f"Rival {n}")
    with pytest.raises(SignerRefused) as e:
        _sign(s, "order-key-S5E04")
    assert e.value.code == "SIGNER_MARKET_READS_FAILED"


def test_review_p2_2_reversed_team_order_in_another_event_makes_the_command_ambiguous(s):
    s.sim.add_market("0x" + "f7" * 32, "REVERSED-EVENT", "Away FC", "Home FC")
    with pytest.raises(SignerRefused) as e:
        _sign(s, "order-key-S5E05")
    assert e.value.code == "COMMAND_AMBIGUOUS_EVENT"


def test_review_p2_1_same_teams_in_another_sport_make_the_command_ambiguous(s):
    m = s.sim.add_market("0x" + "f8" * 32, "ESOCCER-EVENT", "Home FC", "Away FC")
    m.sport_id = 77
    with pytest.raises(SignerRefused) as e:
        _sign(s, "order-key-S5E06")
    assert e.value.code == "COMMAND_AMBIGUOUS_EVENT"


def test_review_p3_4_a_missing_page_is_a_fixed_signer_refusal(s):
    for n in range(150):
        s.sim.add_market("0x" + f"{n:064x}", f"EV{n}", f"Team {n}", f"Rival {n}")
    real = s.signer.config.market_data._get

    def flaky(path, params):
        if path == "/markets/active" and params.get("paginationKey"):
            raise RuntimeError("SXBET_TIMEOUT")
        return real(path, params)
    s.signer.config.market_data._get = flaky
    with pytest.raises(SignerRefused) as e:
        _sign(s, "order-key-S5E07")
    assert e.value.code == "SIGNER_MARKET_READS_FAILED"
