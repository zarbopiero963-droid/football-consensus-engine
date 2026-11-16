"""Issue #187 R2: offline tests of the pure analysis used by the mainnet READ-ONLY audit (REST vs realtime,
recovery, on-chain balance). No network: the live run happens only in the owner-run workflow."""
import pytest

from scripts import sxbet_realtime_consistency as rc

PROXY = "0x" + "aa" * 20
USDC = rc.MAINNET["usdc"]
ESCROW = rc.MAINNET["escrow"]


def pubs(*offsets, ids=None):
    ids = ids or [f"m{o}" for o in offsets]
    return [{"offset": o, "message_id": i} for o, i in zip(offsets, ids)]


# ---------------------------------------------------------------- stream integrity
@pytest.mark.parametrize("stream, outcome, reason", [
    ([], "EMPTY_VALID", "NO_PUBLICATION_IN_WINDOW"),
    (pubs(5, 6, 7), "PASS", "UNIQUE_IDS_CONTIGUOUS_OFFSETS"),
    (pubs(5, 6, 6, ids=["a", "b", "b"]), "PASS", "UNIQUE_IDS_CONTIGUOUS_OFFSETS"),       # duplicate dropped
    (pubs(5, 7), "PARTIAL", "OFFSET_GAPS_IN_LIVE_STREAM"),
    (pubs(7, 6), "FAIL", "OFFSETS_OUT_OF_ORDER"),
    ([{"offset": 1, "message_id": None}], "FAIL", "PUBLICATION_WITHOUT_MESSAGE_ID"),
    ([{"offset": None, "message_id": "x"}], "PARTIAL", "OFFSETS_NOT_PROVIDED"),
])
def test_analyze_channel(stream, outcome, reason):
    out = rc.analyze_channel(stream)
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_duplicates_are_counted_and_never_applied_twice():
    out = rc.analyze_channel(pubs(1, 2, 2, 3, ids=["a", "b", "b", "c"]))
    assert out["duplicates_dropped"] == 1 and out["unique"] == 3


# ---------------------------------------------------------------- recovery across a reconnect
CH = "recent_trades_v3:global"


@pytest.mark.parametrize("resub, replayed, after, outcome, reason", [
    ({"was_recovering": True, "recovered": True}, pubs(11, 12), pubs(13), "PASS", "RECOVERED_DEDUPED_CONTIGUOUS"),
    ({"was_recovering": True, "recovered": True}, pubs(10, 11, ids=["m10", "m11"]), pubs(12), "PASS",
     "RECOVERED_DEDUPED_CONTIGUOUS"),                                                     # replay overlaps: deduped
    ({"was_recovering": True, "recovered": True}, [], pubs(14), "FAIL", "GAP_ACROSS_RECONNECT"),
    ({"was_recovering": True, "recovered": False}, [], pubs(40), "PARTIAL", "RECOVERY_NOT_GRANTED_RESEED_FROM_REST"),
    ({"was_recovering": False, "recovered": False}, [], [], "FAIL", "RECOVERY_NOT_ATTEMPTED"),
    (None, [], [], "FAIL", "NO_RESUBSCRIBE_RESULT"),
])
def test_analyze_recovery(resub, replayed, after, outcome, reason):
    out = rc.analyze_recovery(CH, before=pubs(9, 10), resubscribe=resub, replayed=replayed, after=after)
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_recovery_not_claimed_for_namespaces_without_history():
    out = rc.analyze_recovery("best_odds_v3:global", before=pubs(1), resubscribe={}, replayed=[], after=[])
    assert out["outcome"] == "NOT_TESTED"


def test_recovery_without_prior_publications_is_not_tested():
    out = rc.analyze_recovery(CH, before=[], resubscribe={"was_recovering": True, "recovered": True},
                              replayed=[], after=pubs(1))
    assert out["outcome"] == "NOT_TESTED"


# ---------------------------------------------------------------- REST vs realtime identifiers
@pytest.mark.parametrize("ws, rest, complete, outcome", [
    ([], ["t1"], True, "NOT_TESTED"),
    (["t1", "t2"], ["t0", "t1", "t2"], True, "PASS"),
    # Changed by the review of 9729fe1 (P2, declared): the REST tape is LOCKED-only, a miss is never FAIL.
    (["t1", "t9"], ["t1"], True, "PARTIAL"),
    (["t1", "t9"], ["t1"], False, "PARTIAL"),
])
def test_compare_trades(ws, rest, complete, outcome):
    assert rc.compare_trades(ws, rest, rest_complete=complete)["outcome"] == outcome


V1, V2, V3 = "00100000000000010015000", "00100000000000010015001", "00100000000000010015002"
L = [{"percentageOdds": "40000000000000000000", "size": "1000000"}]


@pytest.mark.parametrize("ws, rest, outcome, reason", [
    ([], {"version": V1}, "NOT_TESTED", "NO_BOOK_PUBLICATION_IN_WINDOW"),
    ([{"version": V1, "outcomeOne": L, "outcomeTwo": []}], {"version": V1, "outcomeOne": L, "outcomeTwo": []},
     "PASS", "SAME_VERSION_SAME_LEVELS"),
    ([{"version": V1, "outcomeOne": L, "outcomeTwo": []}], {"version": V1, "outcomeOne": [], "outcomeTwo": []},
     "FAIL", "SAME_VERSION_DIFFERENT_LEVELS"),
    ([{"version": V1}, {"version": V2}], {"version": V3}, "PASS", "REST_NEWER_THAN_LAST_SOCKET_VERSION"),
    ([{"version": V2}], {"version": V1}, "FAIL", "REST_SNAPSHOT_OLDER_THAN_SOCKET"),
    ([{"version": "x"}], {"version": V1}, "FAIL", "BOOK_VERSION_MALFORMED"),
    ([{"version": V1}], None, "PARTIAL", "REST_SNAPSHOT_NOT_READ"),
])
def test_compare_book_versions(ws, rest, outcome, reason):
    out = rc.compare_book_versions(ws, rest)
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_stale_socket_versions_are_counted():
    out = rc.compare_book_versions([{"version": V2}, {"version": V1}], {"version": V3})
    assert out["stale_publications"] == 1


# ---------------------------------------------------------------- on-chain balance
def row(available="14140000", escrowed="0", pa="0", pe="0", **kw):
    r = {"wallet": PROXY, "tokenAddress": USDC, "escrowAddress": ESCROW, "availableAmount": available,
         "escrowedAmount": escrowed, "pendingAvailableAmount": pa, "pendingEscrowAmount": pe}
    r.update(kw)
    return r


OK_CHAIN = {"chain_id": 4162, "usdc_decimals": 6}


@pytest.mark.parametrize("rows, proxy_usdc, outcome, reason", [
    ([row()], 14140000, "PASS", "PROXY_TOKEN_EQUALS_AVAILABLE"),
    ([row(available="10000000", escrowed="4140000")], 14140000, "PASS", "PROXY_TOKEN_EQUALS_AVAILABLE_PLUS_ESCROWED"),
    ([row(available="10000000", pa="4140000")], 14140000, "PASS", "PROXY_TOKEN_EQUALS_AVAILABLE_PLUS_PENDING"),
    # Changed by the review of 9729fe1 (P2, declared): proxy 0 is only explained with the escrow lower bound read.
    ([row()], 0, "PARTIAL", "EXPLANATION_UNVERIFIED"),
    ([row()], 9, "PARTIAL", "DIFFERENCE_NOT_EXPLAINED_BY_DOCUMENTED_FIELDS"),          # explained, never "anomaly"
    # Reason renamed by the review of 53a5b84 (P3-8, declared): rows are selected by the documented escrow.
    ([row(escrowAddress="0x" + "11" * 20)], 14140000, "FAIL", "NO_SINGLE_USDC_ROW_ON_THE_DOCUMENTED_ESCROW"),
    ([row(wallet="0x" + "22" * 20)], 14140000, "FAIL", "BALANCE_ROW_WALLET_IS_NOT_THE_PROXY"),
    ([], 0, "EMPTY_VALID", "NO_USDC_ROW"),
    ([row(tokenAddress="0x" + "33" * 20)], 0, "PARTIAL", "NO_ROW_FOR_THE_DOCUMENTED_USDC"),
    ([row(available="1.5")], 1, "PARTIAL", "AMOUNT_NOT_READABLE"),
])
def test_classify_balances(rows, proxy_usdc, outcome, reason):
    out = rc.classify_balances(api_rows=rows, proxy=PROXY, eoa=None,
                               onchain={**OK_CHAIN, "proxy_usdc": proxy_usdc, "eoa_usdc": None})
    assert (out["outcome"], out["reason"]) == (outcome, reason)


@pytest.mark.parametrize("chain, outcome, reason", [
    ({"chain_id": 1, "usdc_decimals": 6}, "FAIL", "RPC_CHAIN_ID_NOT_4162"),
    ({"chain_id": 4162, "usdc_decimals": 18}, "FAIL", "USDC_DECIMALS_NOT_6"),
])
def test_classify_balances_pins_the_documented_chain_and_token(chain, outcome, reason):
    out = rc.classify_balances(api_rows=[row()], proxy=PROXY, eoa=None, onchain={**chain, "proxy_usdc": 1})
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_classify_balances_without_api_read_is_not_tested():
    assert rc.classify_balances(api_rows=None, proxy=PROXY, eoa=None,
                                onchain={**OK_CHAIN, "proxy_usdc": 1})["outcome"] == "NOT_TESTED"


def test_signed_pending_deltas_are_parsed():
    out = rc.classify_balances(api_rows=[row(available="15000000", pa="-860000")], proxy=PROXY, eoa=None,
                               onchain={**OK_CHAIN, "proxy_usdc": 14140000})
    assert out["reason"] == "PROXY_TOKEN_EQUALS_AVAILABLE_PLUS_PENDING"
    assert out["api"]["pending_available"] == "-860000"


def test_module_does_no_io():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(rc.__file__).read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not mods & {"httpx", "requests", "urllib", "socket", "websockets", "psycopg", "asyncio", "os"}


# ---------------------------------------------------------------- review of 9729fe1 (NOT SAFE): regressions
REC = {"was_recovering": True, "recovered": True}


@pytest.mark.parametrize("before, replayed, after, outcome, reason", [
    (pubs(9, 10), [], [], "NOT_TESTED", "RECOVERED_NOTHING_DELIVERED_AFTER_RECONNECT"),          # P1 scenario 1
    (pubs(9, 10), [{"offset": None, "message_id": None}], [], "FAIL", "PUBLICATION_WITHOUT_MESSAGE_ID"),  # 2
    (pubs(9, 10), [{"offset": None, "message_id": "x"}], [], "PARTIAL", "NO_OFFSET_AFTER_RECONNECT"),
    ([{"offset": None, "message_id": "a"}], pubs(11), [], "PARTIAL", "NO_OFFSET_BEFORE_DISCONNECT"),     # 3
])
def test_review_9729fe1_p1_recovery_never_passes_without_evidence(before, replayed, after, outcome, reason):
    out = rc.analyze_recovery(CH, before=before, resubscribe=REC, replayed=replayed, after=after)
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_review_9729fe1_p2_proxy_zero_explained_only_with_the_escrow_lower_bound():
    base = dict(api_rows=[row()], proxy=PROXY, eoa=None)
    ok = rc.classify_balances(**base, onchain={**OK_CHAIN, "proxy_usdc": 0, "escrow_usdc": 10 ** 12})
    # Changed by the review of 53a5b84 (P3-7, declared): the shared escrow gives a lower bound only, never PASS.
    assert (ok["outcome"], ok["reason"]) == ("PARTIAL", "PROXY_ZERO_ESCROW_LOWER_BOUND_SATISFIED")
    low = rc.classify_balances(**base, onchain={**OK_CHAIN, "proxy_usdc": 0, "escrow_usdc": 5})
    assert (low["outcome"], low["reason"]) == ("PARTIAL", "EXPLANATION_UNVERIFIED")


def test_review_9729fe1_p2_all_zero_is_empty_and_funded_proxy_without_row_is_partial():
    zero = rc.classify_balances(api_rows=[row(available="0")], proxy=PROXY, eoa=None,
                                onchain={**OK_CHAIN, "proxy_usdc": 0})
    assert (zero["outcome"], zero["reason"]) == ("EMPTY_VALID", "ALL_ZERO_NOTHING_TO_COMPARE")
    funded = rc.classify_balances(api_rows=[], proxy=PROXY, eoa=None, onchain={**OK_CHAIN, "proxy_usdc": 7})
    assert (funded["outcome"], funded["reason"]) == ("PARTIAL", "PROXY_FUNDED_BUT_NO_API_USDC_ROW")


def test_review_9729fe1_p3_versions_compare_as_plain_strings():
    out = rc.compare_book_versions([{"version": "9"}], {"version": "0010"})
    assert out["outcome"] == "FAIL" and out["reason"] == "REST_SNAPSHOT_OLDER_THAN_SOCKET"


def test_review_9729fe1_p3_equal_versions_without_levels_are_not_a_vacuous_pass():
    out = rc.compare_book_versions([{"version": V1}], {"version": V1})
    assert (out["outcome"], out["reason"]) == ("PARTIAL", "LEVELS_MISSING")


def test_review_9729fe1_p3_book_channels_do_not_require_message_ids():
    assert not rc.requires_message_id("orderbook_v3:0xab") and rc.requires_message_id(CH)
    out = rc.analyze_channel([{"offset": 1, "message_id": None}, {"offset": 2, "message_id": None}],
                             require_message_id=False)
    assert out["outcome"] == "PASS"


# ---------------------------------------------------------------- review of 53a5b84 (NOT SAFE): regressions
@pytest.mark.parametrize("kw, proxy_usdc, escrow, outcome, reason", [
    (dict(available="0", escrowed="50000000"), 0, 5, "PARTIAL", "EXPLANATION_UNVERIFIED"),          # P2: no 0 == 0
    (dict(available="0", escrowed="50000000"), 0, 0, "PARTIAL", "EXPLANATION_UNVERIFIED"),
    (dict(available="0", escrowed="0", pe="-5"), 0, 0, "PARTIAL", "ZERO_BALANCES_WITH_PENDING_MOVEMENT"),
])
def test_review_53a5b84_p2_zero_available_is_never_a_trivial_pass(kw, proxy_usdc, escrow, outcome, reason):
    out = rc.classify_balances(api_rows=[row(**kw)], proxy=PROXY, eoa=None,
                               onchain={**OK_CHAIN, "proxy_usdc": proxy_usdc, "escrow_usdc": escrow})
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_review_53a5b84_p3_8_extra_usdc_rows_on_other_escrows_are_information():
    rows = [row(), row(escrowAddress="0x" + "11" * 20, available="7")]
    out = rc.classify_balances(api_rows=rows, proxy=PROXY, eoa=None, onchain={**OK_CHAIN, "proxy_usdc": 14140000})
    assert out["outcome"] == "PASS" and out["other_usdc_rows"] == 1


@pytest.mark.parametrize("replayed, after, outcome, reason", [
    (pubs(10, ids=["m10"]), [], "NOT_TESTED", "ONLY_DUPLICATES_AFTER_RECONNECT"),                    # P3-1
    (pubs(11, ids=["b"]), [{"offset": None, "message_id": "c"}], "PARTIAL", "OFFSETS_NOT_PROVIDED"),  # P3-2
])
def test_review_53a5b84_recovery_needs_new_publications_with_offsets(replayed, after, outcome, reason):
    out = rc.analyze_recovery(CH, before=pubs(9, 10), resubscribe=REC, replayed=replayed, after=after)
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def _book(v, m="0xab"):
    return {"offset": None, "message_id": None, "data": {"version": v, "marketHash": m}}


def test_review_53a5b84_p3_3_book_channels_are_judged_by_version():
    out = rc.analyze_book_channel([_book(V1), _book(V1), _book(V2), _book(V1)])
    assert (out["outcome"], out["applied"], out["ignored_not_newer"]) == ("PASS", 2, 2)
    assert rc.analyze_book_channel([_book("x")])["outcome"] == "FAIL"
    assert rc.analyze_book_channel([])["outcome"] == "EMPTY_VALID"


@pytest.mark.parametrize("levels", [[1, 2], [{"percentageOdds": None, "size": None}], [{"size": "1"}]])
def test_review_53a5b84_p3_4_junk_levels_are_never_equal(levels):
    out = rc.compare_book_versions([{"version": V1, "outcomeOne": levels, "outcomeTwo": []}],
                                   {"version": V1, "outcomeOne": levels, "outcomeTwo": []})
    assert (out["outcome"], out["reason"]) == ("PARTIAL", "LEVELS_MISSING")
