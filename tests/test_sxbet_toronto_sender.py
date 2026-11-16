"""S8 (issue #187): the Toronto-only RestSender. Offline: the HTTP transport is a fake; nothing reaches SX Bet.

Mainnet must be unreachable by construction: pinned host / chain / escrow / token, signer domain and live metadata
checked at start, every request checked before it leaves, writes refused unless explicitly enabled (owner GO).
"""
import dataclasses
import json

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import rest_adapter as ra  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.core import KEY_ENV, Signer  # noqa: E402
from signer.toronto_sender import (MAINNET_CHAIN_ID, TORONTO, TorontoGuardError, TorontoRestSender,  # noqa: E402
                                   verify_toronto_config, verify_toronto_metadata)
from sxbet_signer_kit import Clock, load_limits, open_or_create_ledger, pub, signer_config  # noqa: E402

KEY = "TORONTO-FAKE-KEY-0123456789"
OID = "0x" + "ab" * 32
META = {"chainId": TORONTO["chain_id"],
        "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": TORONTO["chain_id"],
                   "verifyingContract": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38"},
        "activeAsset": {"baseToken": "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956",
                        "escrowAddress": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38", "decimals": 6}}


def toronto_config(**kw):
    base = signer_config()
    return dataclasses.replace(base, **{"network": "testnet", "chain_id": TORONTO["chain_id"],
                                        "verifying_contract": TORONTO["escrow"], "base_token": TORONTO["usdc"], **kw})


@pytest.fixture
def toronto_signer(tmp_path):
    """A REAL Signer bound to the Toronto domain, with a THROWAWAY key and owner (never funded, never used)."""
    acct, owner, clock = Account.create(), Ed25519PrivateKey.generate(), Clock()
    return Signer(toronto_config(), owner_public_key=pub(owner),
                  ledger=open_or_create_ledger(tmp_path / "ledger.jsonl", acct.address, clock),
                  audit=AuditLog(str(tmp_path / "audit.jsonl")), risk_config=load_limits(tmp_path, owner, network="testnet"),
                  env={KEY_ENV: acct.key.hex()}, clock=clock)


class FakeTransport:
    def __init__(self, reply=(200, {"status": "success", "data": {}}), raise_=None, meta=None):
        self.calls, self.reply, self.raise_ = [], reply, raise_
        self.meta = META if meta is None else meta

    def __call__(self, method, url, query, body, headers):
        self.calls.append((method, url, query, body, headers))
        if url.endswith("/metadata/obv3"):
            return 200, {"status": "success", "data": self.meta}
        if self.raise_:
            raise self.raise_
        return self.reply


LIVE = "0x" + "5e" * 20     # stands for the owner's LIVE maker address (never the testnet signer)


def make(signer, transport=None, **kw):
    kw.setdefault("live_maker", LIVE)
    return TorontoRestSender(signer, transport or FakeTransport(), api_key=KEY, **kw)


def order(maker, **kw):
    o = {"marketHash": "0x" + "11" * 32, "maker": maker, "baseToken": TORONTO["usdc"], "totalBetSize": "1000000",
         "percentageOdds": "50000000000000000000", "salt": "0x" + "00" * 32, "expiry": 2_000_000_000,
         "isMakerBettingOutcomeOne": True, "timeInForce": "GTC", "orderSignature": "0x" + "aa" * 65}
    o.update(kw)
    return o


# ---------------------------------------------------------------- pinned facts (docs.sx.bet references)
def test_pinned_values_are_the_documented_toronto_ones_and_never_mainnet():
    assert TORONTO["base_url"] == "https://api.toronto.sx.bet" and TORONTO["chain_id"] == 79479957
    assert TORONTO["usdc"] == "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956".lower()
    assert TORONTO["escrow"] == "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38".lower()
    assert TORONTO["chain_id"] != MAINNET_CHAIN_ID
    assert TORONTO["base_url"].split("/")[2] == "api.toronto.sx.bet" != "api.sx.bet"
    assert "api.sx.bet" not in TORONTO["base_url"]                    # "api.toronto.sx.bet" does not contain it


# ---------------------------------------------------------------- start-up guards
@pytest.mark.parametrize("kw, code", [
    ({"network": "mainnet"}, "SIGNER_NETWORK_NOT_TESTNET"),
    ({"chain_id": 4162}, "SIGNER_CHAIN_NOT_TORONTO"),
    ({"verifying_contract": "0x890482680c3a0116abb003b17e7d694d13a6c1eb"}, "SIGNER_VERIFYING_CONTRACT_NOT_TORONTO_ESCROW"),
    ({"base_token": "0x6629ce1cf35cc1329ebb4f63202f3f197b3f050b"}, "SIGNER_BASE_TOKEN_NOT_TORONTO_USDC"),
])
def test_a_signer_not_bound_to_the_toronto_domain_is_refused(kw, code):
    with pytest.raises(TorontoGuardError) as e:
        verify_toronto_config(toronto_config(**kw))
    assert e.value.code == code


@pytest.mark.parametrize("mutate, code", [
    (lambda m: m.update(chainId=4162), "METADATA_CHAIN_NOT_TORONTO"),
    (lambda m: m["domain"].update(chainId=4162), "METADATA_CHAIN_NOT_TORONTO"),
    (lambda m: m["domain"].update(name="Other"), "METADATA_DOMAIN_UNEXPECTED"),
    (lambda m: m["domain"].update(verifyingContract="0x890482680C3a0116aBB003B17e7D694D13a6c1eB"),
     "METADATA_ESCROW_NOT_TORONTO"),
    (lambda m: m["activeAsset"].update(baseToken="0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"),
     "METADATA_TOKEN_NOT_TORONTO_USDC"),
])
def test_live_metadata_that_differs_from_the_pinned_toronto_domain_stops_the_sender(mutate, code):
    meta = json.loads(json.dumps(META))
    mutate(meta)
    with pytest.raises(TorontoGuardError) as e:
        verify_toronto_metadata(meta)
    assert e.value.code == code


def test_only_a_real_signer_bound_to_toronto_can_build_the_sender(toronto_signer, tmp_path):
    with pytest.raises(TorontoGuardError) as e:
        TorontoRestSender(toronto_config(), FakeTransport(), api_key=KEY, live_maker=LIVE)
    assert e.value.code == "NOT_A_SIGNER"
    s = make(toronto_signer)
    assert s.maker == toronto_signer.address.lower() and s.writes_enabled is False


@pytest.mark.parametrize("kw, code", [({"api_key": ""}, "API_KEY_MISSING"), ({"writes_enabled": "yes"},
                                      "WRITES_FLAG_INVALID")])
def test_constructor_refuses_bad_settings(toronto_signer, kw, code):
    with pytest.raises(TorontoGuardError) as e:
        TorontoRestSender(toronto_signer, FakeTransport(), **{"api_key": KEY, "live_maker": LIVE, **kw})
    assert e.value.code == code


def test_the_live_maker_can_never_be_the_testnet_signer(toronto_signer):
    with pytest.raises(TorontoGuardError) as e:
        make(toronto_signer, live_maker=toronto_signer.address.upper().replace("0X", "0x"))
    assert e.value.code == "TESTNET_SIGNER_IS_THE_LIVE_MAKER"


def test_metadata_is_fetched_by_the_sender_through_the_guarded_read(toronto_signer):
    t = FakeTransport()
    make(toronto_signer, t)
    assert t.calls[0][:2] == ("GET", "https://api.toronto.sx.bet/metadata/obv3")
    bad = json.loads(json.dumps(META))
    bad["chainId"] = 4162
    with pytest.raises(TorontoGuardError) as e:
        make(toronto_signer, FakeTransport(meta=bad))
    assert e.value.code == "METADATA_CHAIN_NOT_TORONTO"


def test_a_later_config_change_stops_the_sender(toronto_signer):
    s = make(toronto_signer)
    toronto_signer.config = dataclasses.replace(toronto_signer.config, chain_id=4162)
    with pytest.raises(TorontoGuardError):
        s.send(ra.RestRequest("GET", f"/orders-v3/{OID}", (), None))


# ---------------------------------------------------------------- per-request guards
def test_reads_go_only_to_the_pinned_host_with_the_key_header(toronto_signer):
    t = FakeTransport()
    s = make(toronto_signer, t)
    for req in (ra.RestRequest("GET", f"/orders-v3/{OID}", (), None),
                ra.RestRequest("GET", "/orders-v3/client/order-key-1", (), None),
                ra.RestRequest("GET", "/fills-v3", (("orderId", OID),), None)):
        assert s.send(req).http_status == 200
    assert {c[1].split("/")[2] for c in t.calls} == {"api.toronto.sx.bet"}
    assert all(c[4]["x-sx-api-key"] == KEY for c in t.calls) and KEY not in repr(s)


@pytest.mark.parametrize("req", [
    ra.RestRequest("POST", "/orders-v3", (), {"orders": [{"x": 1}]}),
    ra.RestRequest("DELETE", "/orders-v3", (), {"orders": [{"orderId": OID}]}),
    ra.RestRequest("DELETE", "/orders-v3/market", (("marketHash", "0x" + "11" * 32),), None),
    ra.RestRequest("DELETE", "/orders-v3/event", (("eventId", "L1"),), None),
    ra.RestRequest("DELETE", "/orders-v3/all", (), None),
])
def test_writes_are_refused_by_default_and_nothing_leaves(toronto_signer, req):
    t = FakeTransport()
    s = make(toronto_signer, t)
    n = len(t.calls)
    with pytest.raises(TorontoGuardError) as e:
        s.send(req)
    assert e.value.code == "SENDER_WRITES_DISABLED" and len(t.calls) == n


@pytest.mark.parametrize("req, code", [
    (ra.RestRequest("GET", "/user/balance-v3", (), None), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("POST", "/user/transfer-to-proxy", (), {}), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("POST", "/heartbeat/v3", (), {}), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("GET", "/orders-v3/../user/withdraw", (), None), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("GET", "//api.sx.bet/orders-v3", (), None), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("GET", "/fills-v3", (("x", "https://api.sx.bet"),), None), "MAINNET_VALUE_IN_REQUEST"),
    (ra.RestRequest("GET", "/fills-v3", (("x", "api%2Esx%2Ebet"),), None), "MAINNET_VALUE_IN_REQUEST"),
    (ra.RestRequest("GET", "/fills-v3", (("x", "6629ce1cf35cc1329ebb4f63202f3f197b3f050b"),), None),
     "MAINNET_VALUE_IN_REQUEST"),
    (ra.RestRequest("GET", "/fills-v3", (("x", 1),), None), "REQUEST_FIELDS_NOT_PLAIN_STRINGS"),
])
def test_anything_outside_the_documented_order_routes_or_pointing_to_mainnet_is_refused(toronto_signer, req, code):
    t = FakeTransport()
    s = make(toronto_signer, t)
    n = len(t.calls)
    with pytest.raises(TorontoGuardError) as e:
        s.send(req)
    assert e.value.code == code and len(t.calls) == n


def test_str_subclass_tricks_are_refused(toronto_signer):
    class Evil(str):
        def __radd__(self, other):
            return "https://evil.example" + str(self)
    s = make(toronto_signer)
    with pytest.raises(TorontoGuardError) as e:
        s.send(ra.RestRequest("GET", Evil("/metadata/obv3"), (), None))
    assert e.value.code == "REQUEST_FIELDS_NOT_PLAIN_STRINGS"


def test_even_with_writes_enabled_an_order_not_bound_to_toronto_never_leaves(toronto_signer):
    t = FakeTransport()
    s = make(toronto_signer, t, writes_enabled=True)
    m = s.maker
    for o, code in ((order(m, baseToken="0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"), "MAINNET_VALUE_IN_REQUEST"),
                    (order(m, baseToken="0x" + "99" * 20), "ORDER_TOKEN_NOT_TORONTO_USDC"),
                    (order("0x" + "77" * 20), "ORDER_MAKER_NOT_THE_TORONTO_MAKER"),
                    (order(m, chainId=4162), "MAINNET_VALUE_IN_REQUEST")):
        n = len(t.calls)
        with pytest.raises(TorontoGuardError) as e:
            s.send(ra.RestRequest("POST", "/orders-v3", (), {"orders": [o]}))
        assert e.value.code == code and len(t.calls) == n


def test_enabled_write_goes_to_toronto_only_with_the_body_serialized_once(toronto_signer):
    t = FakeTransport()
    s = make(toronto_signer, t, writes_enabled=True)
    s.send(ra.RestRequest("POST", "/orders-v3", (), {"orders": [order(s.maker)]}))
    method, url, _, body, _ = t.calls[-1]
    assert (method, url) == ("POST", "https://api.toronto.sx.bet/orders-v3") and isinstance(body, bytes)
    assert json.loads(body)["orders"][0]["maker"] == s.maker


@pytest.mark.parametrize("raise_, reply", [(TimeoutError(), None), (ConnectionResetError(), None),
                                           (None, ("200", {})), (None, (True, {}))])
def test_transport_failure_is_sender_timeout_never_not_sent(toronto_signer, raise_, reply):
    s = make(toronto_signer, FakeTransport(reply=reply, raise_=raise_), writes_enabled=True)
    with pytest.raises(ra.SenderTimeout):
        s.send(ra.RestRequest("POST", "/orders-v3", (), {"orders": [order(s.maker)]}))


def test_module_imports_no_network_library():
    import ast
    import pathlib
    import signer.toronto_sender as ts
    tree = ast.parse(pathlib.Path(ts.__file__).read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not mods & {"httpx", "requests", "urllib", "socket", "websockets", "aiohttp", "http"}


# ---------------------------------------------------------------- review d9ca299 P3 + owner decision 2 (16-11)
@pytest.mark.parametrize("live", [None, "", "0x123"])
def test_live_maker_is_mandatory(toronto_signer, live):
    with pytest.raises(TorontoGuardError) as e:
        TorontoRestSender(toronto_signer, FakeTransport(), api_key=KEY, live_maker=live)
    assert e.value.code == "LIVE_MAKER_REQUIRED"


@pytest.mark.parametrize("req, code", [
    (ra.RestRequest("GET", "/fills-v3", (("a", "b", "c"),), None), "QUERY_MALFORMED"),
    (ra.RestRequest("POST", "/orders-v3", (), {"orders": [{"x": float("nan")}]}), "BODY_NOT_SERIALIZABLE"),
    (ra.RestRequest("POST", "/orders-v3", (), {"orders": [{"x": {1, 2}}]}), "BODY_NOT_SERIALIZABLE"),
])
def test_refusal_codes_are_fixed_guard_codes(toronto_signer, req, code):
    s = make(toronto_signer, writes_enabled=True)
    with pytest.raises(TorontoGuardError) as e:
        s.preflight(req)
    assert e.value.code == code


def test_deeply_nested_body_is_body_too_deep(toronto_signer):
    s = make(toronto_signer, writes_enabled=True)
    deep = {}
    cur = deep
    for _ in range(30):
        cur["a"] = {}
        cur = cur["a"]
    with pytest.raises(TorontoGuardError) as e:
        s.preflight(ra.RestRequest("DELETE", "/orders-v3", (), {"orders": [], "x": deep}))
    assert e.value.code == "BODY_TOO_DEEP"


def test_cancel_body_with_a_foreign_maker_is_refused(toronto_signer):
    s = make(toronto_signer, writes_enabled=True)
    with pytest.raises(TorontoGuardError) as e:
        s.preflight(ra.RestRequest("DELETE", "/orders-v3", (), {"orders": [{"orderId": OID}],
                                                                "maker": "0x" + "77" * 20}))
    assert e.value.code == "CANCEL_MAKER_NOT_THE_TORONTO_MAKER"


def test_guard_errors_are_refusals_before_send(toronto_signer):
    assert issubclass(TorontoGuardError, ra.SenderRefusedBeforeSend)
    t = FakeTransport()
    s = make(toronto_signer, t)
    n = len(t.calls)
    with pytest.raises(ra.SenderRefusedBeforeSend):
        s.preflight(ra.RestRequest("POST", "/orders-v3", (), {"orders": [order(s.maker)]}))
    assert len(t.calls) == n                                           # preflight never does I/O


# ---------------------------------------------------------------- owner decision 2: gateway / cancel preflight
class _RefusingSender:
    """Wraps the simulated exchange; its preflight refuses everything (a local guard), its send would deliver."""
    PREFLIGHT_IS_PURE = True

    def __init__(self, sim):
        self.sim = sim

    def preflight(self, request):
        raise ra.SenderRefusedBeforeSend("TEST_LOCAL_GUARD")

    def send(self, request):
        return self.sim.send(request)


def test_gateway_records_a_local_refusal_as_signed_never_sent_not_unknown(tmp_path):
    from signer.gateway import GatewayRefused
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path)
    i = s.intent("order-key-PF01")
    signed = s.sign(i)
    s.adapter._sender = _RefusingSender(s.sim)
    with pytest.raises(GatewayRefused) as e:
        s.gw.submit(signed, i)
    assert e.value.code == "SENDER_REFUSED_BEFORE_SEND_TEST_LOCAL_GUARD"
    assert s.state("order-key-PF01") == "SIGNED"                      # not UNKNOWN: provably not sent
    assert [r for r in s.sim.writes() if r.method == "POST"] == []


def test_cancel_refused_locally_does_not_consume_the_owner_command(tmp_path):
    from signer import cancellation as cx
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path)
    _, sg, _ = s.place("order-key-PF02")
    auth = s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[sg["order_id"]])
    s.adapter._sender = _RefusingSender(s.sim)
    with pytest.raises(cx.CancelRefused) as e:
        s.cx.cancel_orders([sg["order_id"]], auth)
    assert e.value.code == "SENDER_REFUSED_BEFORE_SEND_TEST_LOCAL_GUARD"
    assert not s.ledger.command_consumed(auth.command_id)              # the owner's command stays usable
    assert [r for r in s.sim.writes() if r.method == "DELETE"] == []


# ---------------------------------------------------------------- review 9ddcfbf (SAFE, P3 hardening)
class _ImpurePreflightSender:
    """A sender whose preflight SENDS and then fails, and that does not declare a pure preflight."""

    def __init__(self, sim):
        self.sim = sim

    def preflight(self, request):
        self.sim.send(request)
        raise ValueError("boom")

    def send(self, request):
        return self.sim.send(request)


def test_review_9ddcfbf_p3_1_an_undeclared_preflight_is_never_trusted_as_not_sent(tmp_path):
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path)
    i = s.intent("order-key-PF03")
    signed = s.sign(i)
    s.adapter._sender = _ImpurePreflightSender(s.sim)
    s.gw.submit(signed, i)                                   # preflight ignored: normal write-ahead + send path
    assert s.state("order-key-PF03") != "SIGNED"             # never recorded as "signed, never sent"


def test_review_9ddcfbf_p3_5_query_must_be_a_tuple(toronto_signer):
    s = make(toronto_signer)
    with pytest.raises(TorontoGuardError) as e:
        s.preflight(ra.RestRequest("GET", "/fills-v3", iter([("orderId", OID)]), None))
    assert e.value.code == "QUERY_MALFORMED"


def test_review_9ddcfbf_p3_3_cancel_refusal_is_audited_with_the_exact_code(tmp_path):
    from signer import cancellation as cx
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path)
    _, sg, _ = s.place("order-key-PF04")
    auth = s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[sg["order_id"]])
    s.adapter._sender = _RefusingSender(s.sim)
    with pytest.raises(cx.CancelRefused):
        s.cx.cancel_orders([sg["order_id"]], auth)
    audit = (tmp_path / "gw-audit.jsonl").read_text()
    assert "CANCEL_NOT_SENT" in audit and "SENDER_REFUSED_BEFORE_SEND_TEST_LOCAL_GUARD" in audit
