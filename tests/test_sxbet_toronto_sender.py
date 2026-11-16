"""S8 (issue #187): the Toronto-only RestSender. Offline: the HTTP transport is a fake; nothing reaches SX Bet.

Mainnet must be unreachable by construction: pinned host / chain / escrow / token, signer domain and live metadata
checked at start, every request checked before it leaves, writes refused unless explicitly enabled (owner GO).
"""
import dataclasses
import json

import pytest

from signer import rest_adapter as ra
from signer.toronto_sender import (MAINNET_CHAIN_ID, TORONTO, TorontoGuardError, TorontoRestSender,
                                   verify_toronto_config, verify_toronto_metadata)
from sxbet_signer_kit import signer_config

MAKER = "0x" + "4c" * 20
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


class FakeTransport:
    def __init__(self, reply=(200, {"status": "success", "data": {}}), raise_=None):
        self.calls, self.reply, self.raise_ = [], reply, raise_

    def __call__(self, method, url, query, body, headers):
        self.calls.append((method, url, query, body, headers))
        if self.raise_:
            raise self.raise_
        return self.reply


def sender(transport=None, **kw):
    return TorontoRestSender(transport or FakeTransport(), config=toronto_config(), metadata=META, maker=MAKER,
                             api_key=KEY, **kw)


def order(**kw):
    o = {"marketHash": "0x" + "11" * 32, "maker": MAKER, "baseToken": TORONTO["usdc"], "totalBetSize": "1000000",
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


@pytest.mark.parametrize("kw, code", [({"maker": "0x123"}, "MAKER_INVALID"), ({"api_key": ""}, "API_KEY_MISSING"),
                                      ({"writes_enabled": "yes"}, "WRITES_FLAG_INVALID")])
def test_constructor_refuses_bad_settings(kw, code):
    args = dict(config=toronto_config(), metadata=META, maker=MAKER, api_key=KEY)
    args.update(kw)
    with pytest.raises(TorontoGuardError) as e:
        TorontoRestSender(FakeTransport(), **args)
    assert e.value.code == code


# ---------------------------------------------------------------- per-request guards
def test_reads_go_only_to_the_pinned_host_with_the_key_header():
    t = FakeTransport()
    s = sender(t)
    for req in (ra.RestRequest("GET", f"/orders-v3/{OID}", (), None),
                ra.RestRequest("GET", "/orders-v3/client/order-key-1", (), None),
                ra.RestRequest("GET", "/fills-v3", (("orderId", OID),), None)):
        assert s.send(req).http_status == 200
    assert {c[1].split("/")[2] for c in t.calls} == {"api.toronto.sx.bet"}
    assert all(c[4]["x-sx-api-key"] == KEY for c in t.calls) and KEY not in repr(s)


@pytest.mark.parametrize("req", [
    ra.RestRequest("POST", "/orders-v3", (), {"orders": [order()]}),
    ra.RestRequest("DELETE", "/orders-v3", (), {"orders": [{"orderId": OID}]}),
    ra.RestRequest("DELETE", "/orders-v3/market", (("marketHash", "0x" + "11" * 32),), None),
    ra.RestRequest("DELETE", "/orders-v3/event", (("eventId", "L1"),), None),
    ra.RestRequest("DELETE", "/orders-v3/all", (), None),
])
def test_writes_are_refused_by_default_and_nothing_leaves(req):
    t = FakeTransport()
    with pytest.raises(TorontoGuardError) as e:
        sender(t).send(req)
    assert e.value.code == "SENDER_WRITES_DISABLED" and t.calls == []


@pytest.mark.parametrize("req, code", [
    (ra.RestRequest("GET", "/user/balance-v3", (), None), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("POST", "/user/transfer-to-proxy", (), {}), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("POST", "/heartbeat/v3", (), {}), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("GET", "/orders-v3/../user/withdraw", (), None), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("GET", "//api.sx.bet/orders-v3", (), None), "ROUTE_NOT_ALLOWED"),
    (ra.RestRequest("GET", "/fills-v3", (("x", "https://api.sx.bet"),), None), "MAINNET_VALUE_IN_REQUEST"),
])
def test_anything_outside_the_documented_order_routes_or_pointing_to_mainnet_is_refused(req, code):
    t = FakeTransport()
    with pytest.raises(TorontoGuardError) as e:
        sender(t).send(req)
    assert e.value.code == code and t.calls == []


@pytest.mark.parametrize("o, code", [
    (order(baseToken="0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"), "MAINNET_VALUE_IN_REQUEST"),
    (order(baseToken="0x" + "99" * 20), "ORDER_TOKEN_NOT_TORONTO_USDC"),
    (order(maker="0x" + "77" * 20), "ORDER_MAKER_NOT_THE_TORONTO_MAKER"),
])
def test_even_with_writes_enabled_an_order_not_bound_to_toronto_never_leaves(o, code):
    t = FakeTransport()
    with pytest.raises(TorontoGuardError) as e:
        sender(t, writes_enabled=True).send(ra.RestRequest("POST", "/orders-v3", (), {"orders": [o]}))
    assert e.value.code == code and t.calls == []


def test_enabled_write_goes_to_toronto_only():
    t = FakeTransport()
    sender(t, writes_enabled=True).send(ra.RestRequest("POST", "/orders-v3", (), {"orders": [order()]}))
    assert t.calls[0][0] == "POST" and t.calls[0][1] == "https://api.toronto.sx.bet/orders-v3"


@pytest.mark.parametrize("raise_, reply", [(TimeoutError(), None), (ConnectionResetError(), None),
                                           (None, ("200", {}))])
def test_transport_failure_is_sender_timeout_never_not_sent(raise_, reply):
    t = FakeTransport(reply=reply, raise_=raise_)
    with pytest.raises(ra.SenderTimeout):
        sender(t, writes_enabled=True).send(ra.RestRequest("POST", "/orders-v3", (), {"orders": [order()]}))


def test_module_imports_no_network_library():
    import ast
    import pathlib
    import signer.toronto_sender as ts
    tree = ast.parse(pathlib.Path(ts.__file__).read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not mods & {"httpx", "requests", "urllib", "socket", "websockets", "aiohttp", "http"}
