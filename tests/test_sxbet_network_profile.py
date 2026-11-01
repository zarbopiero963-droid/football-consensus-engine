"""Toronto/mainnet network profiles: a Toronto profile can never point at mainnet hosts, chain or contracts.

Offline only. The two METADATA objects below are the `data` of an unauthenticated public
`GET /metadata/obv3` on each network, recorded 2026-11-01T16:00:46Z (docs/sxbet/testnet_toronto_readiness.md).
"""
import ast
import copy
import pathlib

import pytest

from signer import intent as intent_mod
from signer import network_profile as np
from signer.network_profile import MAINNET, TORONTO, ProfileMismatch

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / "docs" / "sxbet" / "toronto.env.example"

METADATA = {
    "toronto": {"chainId": 79479957, "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 79479957,
                                                "verifyingContract": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38"},
                "activeAsset": {"symbol": "USDC", "baseToken": "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956",
                                "escrowAddress": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38", "decimals": 6},
                "transferToProxyExecutorAddress": "0xBB2828D990eC30B46715773b654eC4D44703177d",
                "oddsLadderStepSize": 125,
                "limits": {"orderSizeMinimum": "1", "orderSizeMinimumBaseUnits": "1000000",
                           "minRestingOrderSize": "1", "minRestingOrderSizeBaseUnits": "1000000",
                           "maxCreateOrders": 10, "maxCancelOrders": 100, "rfqRequestTtlSeconds": 60}},
    "mainnet": {"chainId": 4162, "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 4162,
                                            "verifyingContract": "0x890482680C3a0116aBB003B17e7D694D13a6c1eB"},
                "activeAsset": {"symbol": "USDC", "baseToken": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
                                "escrowAddress": "0x890482680C3a0116aBB003B17e7D694D13a6c1eB", "decimals": 6},
                "transferToProxyExecutorAddress": "0xfC3dbB46D0703703fC1D1677E7e0BaBF971adcfF",
                "oddsLadderStepSize": 125,
                "limits": {"orderSizeMinimum": "1", "orderSizeMinimumBaseUnits": "1000000",
                           "minRestingOrderSize": "1", "minRestingOrderSizeBaseUnits": "1000000",
                           "maxCreateOrders": 10, "maxCancelOrders": 100, "rfqRequestTtlSeconds": 60}},
}
OTHER = {"toronto": "mainnet", "mainnet": "toronto"}


def _intent(p):
    return {"network": p.network, "chain_id": p.chain_id, "verifying_contract": p.escrow, "base_token": p.usdc}


def _code(fn, *a, **kw):
    with pytest.raises(ProfileMismatch) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------------------ pinned values and consistency
def test_pins_match_official_docs_and_existing_code():
    from bridge.sxbet.client import NETWORKS
    from bridge.sxbet.simulation import EXPECTED_CHAIN_IDS
    for p in (MAINNET, TORONTO):
        assert intent_mod.CHAIN_IDS[p.network] == p.chain_id == EXPECTED_CHAIN_IDS[p.network]
        assert NETWORKS[p.network] == p.api_base_url
    assert TORONTO.chain_id == 79479957 and MAINNET.chain_id == 4162


def test_profiles_are_fully_disjoint():
    assert MAINNET.chain_id != TORONTO.chain_id
    assert not MAINNET.hosts & TORONTO.hosts
    assert not MAINNET.contracts & TORONTO.contracts
    assert all(".toronto." in h for h in TORONTO.hosts)
    assert not any("toronto" in h for h in MAINNET.hosts)
    assert len(TORONTO.contracts) == 3 and len(MAINNET.contracts) == 3


def test_get_profile_rejects_unknown_names():
    assert np.get_profile("toronto") is TORONTO
    for bad in ("testnet", "Toronto", "", None, "mainnet ", 1):
        assert _code(np.get_profile, bad) == "PROFILE_UNKNOWN"


# ------------------------------------------------------------------ metadata
@pytest.mark.parametrize("name", ["toronto", "mainnet"])
def test_recorded_runtime_metadata_matches_its_own_profile(name):
    np.check_metadata(name, METADATA[name])


@pytest.mark.parametrize("name", ["toronto", "mainnet"])
def test_metadata_of_other_network_is_refused(name):
    assert _code(np.check_metadata, name, METADATA[OTHER[name]]) == "PROFILE_CHAIN_ID_OF_OTHER_NETWORK"


@pytest.mark.parametrize("path,code", [
    (("domain", "verifyingContract"), "PROFILE_ESCROW_OF_OTHER_NETWORK"),
    (("activeAsset", "escrowAddress"), "PROFILE_ESCROW_OF_OTHER_NETWORK"),
    (("activeAsset", "baseToken"), "PROFILE_BASE_TOKEN_OF_OTHER_NETWORK"),
    (("transferToProxyExecutorAddress",), "PROFILE_PROXY_EXECUTOR_OF_OTHER_NETWORK"),
    (("domain", "chainId"), "PROFILE_CHAIN_ID_OF_OTHER_NETWORK"),
])
def test_toronto_metadata_with_one_mainnet_field_is_refused(path, code):
    m = copy.deepcopy(METADATA["toronto"])
    src = METADATA["mainnet"]
    tgt, s = m, src
    for k in path[:-1]:
        tgt, s = tgt[k], s[k]
    tgt[path[-1]] = s[path[-1]]
    assert _code(np.check_metadata, "toronto", m) == code


def test_metadata_unknown_values_are_refused():
    m = copy.deepcopy(METADATA["toronto"])
    m["domain"]["verifyingContract"] = "0x" + "11" * 20
    assert _code(np.check_metadata, "toronto", m) == "PROFILE_ESCROW_UNEXPECTED"
    m = copy.deepcopy(METADATA["toronto"])
    m["chainId"] = 1
    assert _code(np.check_metadata, "toronto", m) == "PROFILE_CHAIN_ID_UNEXPECTED"
    m = copy.deepcopy(METADATA["toronto"])
    m["chainId"] = "79479957"
    assert _code(np.check_metadata, "toronto", m) == "PROFILE_CHAIN_ID_INVALID"
    m = copy.deepcopy(METADATA["toronto"])
    m["domain"]["version"] = "1.0"
    assert _code(np.check_metadata, "toronto", m) == "PROFILE_DOMAIN_UNEXPECTED"
    m = copy.deepcopy(METADATA["toronto"])
    m["activeAsset"]["decimals"] = 18
    assert _code(np.check_metadata, "toronto", m) == "PROFILE_DECIMALS_UNEXPECTED"
    m = copy.deepcopy(METADATA["toronto"])
    del m["activeAsset"]
    assert _code(np.check_metadata, "toronto", m) == "PROFILE_METADATA_INVALID"
    assert _code(np.check_metadata, "toronto", None) == "PROFILE_METADATA_INVALID"


# ------------------------------------------------------------------ intents and signer config
@pytest.mark.parametrize("name", ["toronto", "mainnet"])
def test_intent_of_own_profile_passes_and_other_is_refused(name):
    p, o = np.get_profile(name), np.get_profile(OTHER[name])
    np.check_intent(p, _intent(p))
    assert _code(np.check_intent, p, _intent(o)) == "PROFILE_NETWORK_OF_OTHER_NETWORK"
    for k, code in (("chain_id", "PROFILE_CHAIN_ID_OF_OTHER_NETWORK"),
                    ("verifying_contract", "PROFILE_ESCROW_OF_OTHER_NETWORK"),
                    ("base_token", "PROFILE_BASE_TOKEN_OF_OTHER_NETWORK")):
        mixed = dict(_intent(p), **{k: _intent(o)[k]})
        assert _code(np.check_intent, p, mixed) == code


def test_toronto_intent_with_proxy_executor_as_escrow_is_refused():
    i = dict(_intent(TORONTO), verifying_contract=MAINNET.transfer_to_proxy_executor)
    assert _code(np.check_intent, "toronto", i) == "PROFILE_ESCROW_OF_OTHER_NETWORK"
    i = dict(_intent(TORONTO), verifying_contract=TORONTO.usdc)
    assert _code(np.check_intent, "toronto", i) == "PROFILE_ESCROW_UNEXPECTED"


def test_intent_with_checksummed_addresses_is_compared_case_insensitively():
    i = dict(_intent(TORONTO), verifying_contract=METADATA["toronto"]["domain"]["verifyingContract"])
    np.check_intent("toronto", i)


def test_signer_config_cross_network_is_refused():
    from signer.core import SignerConfig
    ok = SignerConfig("toronto-signer", "testnet", 79479957, TORONTO.escrow, TORONTO.usdc, 125)
    np.check_signer_config("toronto", ok)
    bad = SignerConfig("toronto-signer", "testnet", 79479957, MAINNET.escrow, TORONTO.usdc, 125)
    assert _code(np.check_signer_config, "toronto", bad) == "PROFILE_ESCROW_OF_OTHER_NETWORK"
    bad = SignerConfig("toronto-signer", "mainnet", 4162, MAINNET.escrow, MAINNET.usdc, 125)
    assert _code(np.check_signer_config, "toronto", bad) == "PROFILE_NETWORK_OF_OTHER_NETWORK"
    assert _code(np.check_signer_config, "mainnet", ok) == "PROFILE_NETWORK_OF_OTHER_NETWORK"
    assert _code(np.check_signer_config, "toronto", object()) == "PROFILE_SIGNER_CONFIG_INVALID"


def test_forged_profile_object_is_not_trusted():
    import dataclasses
    forged = dataclasses.replace(TORONTO, escrow=MAINNET.escrow)
    assert _code(np.check_intent, forged, _intent(TORONTO)) == "PROFILE_UNKNOWN"


# ------------------------------------------------------------------ hosts
@pytest.mark.parametrize("url", [TORONTO.api_base_url, TORONTO.api_base_url + "/metadata/obv3",
                                 TORONTO.realtime_ws_url, TORONTO.rpc_url, TORONTO.explorer_api_url])
def test_toronto_urls_pass(url):
    assert ".toronto." in np.check_url("toronto", url)


@pytest.mark.parametrize("url", [MAINNET.api_base_url, MAINNET.api_base_url + "/orders-v3", MAINNET.realtime_ws_url,
                                 MAINNET.rpc_url, MAINNET.explorer_api_url, MAINNET.app_url])
def test_toronto_profile_refuses_every_mainnet_host(url):
    assert _code(np.check_url, "toronto", url) == "PROFILE_HOST_OF_OTHER_NETWORK"


@pytest.mark.parametrize("url", [TORONTO.api_base_url, TORONTO.realtime_ws_url, TORONTO.app_url])
def test_mainnet_profile_refuses_every_toronto_host(url):
    assert _code(np.check_url, "mainnet", url) == "PROFILE_HOST_OF_OTHER_NETWORK"


@pytest.mark.parametrize("url,code", [
    ("http://api.toronto.sx.bet", "PROFILE_URL_INVALID"),
    ("ws://realtime.toronto.sx.bet/connection/websocket", "PROFILE_URL_INVALID"),
    ("https://user@api.toronto.sx.bet", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet:8443", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet:443", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet:bad", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet /x", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet#frag", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet/x@api.sx.bet", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.bet\\@api.sx.bet", "PROFILE_URL_INVALID"),
    ("https://api.toronto.sx.\u212aet", "PROFILE_URL_INVALID"),
    ("HTTPS://API.SX.BET", "PROFILE_HOST_OF_OTHER_NETWORK"),
    ("https://api.toronto.sx.bet.evil.example", "PROFILE_HOST_NOT_ALLOWED"),
    ("https://toronto.sx.bet", "PROFILE_HOST_NOT_ALLOWED"),
    ("https://127.0.0.1", "PROFILE_HOST_NOT_ALLOWED"),
    (None, "PROFILE_URL_INVALID"),
])
def test_url_negative_cases(url, code):
    assert _code(np.check_url, "toronto", url) == code


def test_allowlist_narrows_hosts():
    allowed = np.parse_allowed_hosts("toronto", "api.toronto.sx.bet")
    np.check_url("toronto", TORONTO.api_base_url, allowed_hosts=allowed)
    assert _code(np.check_url, "toronto", TORONTO.realtime_ws_url, allowed_hosts=allowed) == "PROFILE_HOST_NOT_ALLOWED"


@pytest.mark.parametrize("raw,code", [
    ("", "PROFILE_ALLOWED_HOSTS_MISSING"), (None, "PROFILE_ALLOWED_HOSTS_MISSING"),
    ("api.toronto.sx.bet,", "PROFILE_ALLOWED_HOSTS_MISSING"),
    ("api.toronto.sx.bet,api.sx.bet", "PROFILE_HOST_OF_OTHER_NETWORK"),
    ("sx.bet", "PROFILE_HOST_OF_OTHER_NETWORK"),
    ("realtime.sx.bet", "PROFILE_HOST_OF_OTHER_NETWORK"),
    ("api.toronto.sx.bet,*.sx.bet", "PROFILE_ALLOWED_HOSTS_INVALID"),
    ("API.TORONTO.SX.BET", "PROFILE_ALLOWED_HOSTS_INVALID"),
    ("toronto.sx.bet", "PROFILE_HOST_NOT_ALLOWED"),
])
def test_allowlist_negative_cases(raw, code):
    assert _code(np.parse_allowed_hosts, "toronto", raw) == code


# ------------------------------------------------------------------ env template and env validator
def _parse_env(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def test_env_example_has_no_values_for_credentials_and_only_toronto_hosts():
    env = _parse_env(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert env[np.PROFILE_ENV] == "toronto" and env["SXBET_NETWORK"] == "testnet"
    assert set(np.TORONTO_ENV.values()) <= set(env)
    for k in ("api_key", "signer_data_dir", "database_url"):
        assert env[np.TORONTO_ENV[k]] == ""
    assert not np.PRODUCTION_ONLY_ENV & set(env)
    assert np.parse_allowed_hosts("toronto", env[np.TORONTO_ENV["allowed_hosts"]]) <= TORONTO.hosts
    assert "PRIVATE_KEY" not in "".join(env)


def test_env_example_validates_and_never_echoes_credentials():
    env = _parse_env(ENV_EXAMPLE.read_text(encoding="utf-8"))
    env.update({np.TORONTO_ENV["api_key"]: "dummy-toronto-key-not-real",
                np.TORONTO_ENV["database_url"]: "postgresql://dummy-not-real",
                np.TORONTO_ENV["signer_data_dir"]: "/srv/sxbet-toronto-signer"})
    out = np.check_toronto_env(env)
    assert out == {"profile": "toronto", "allowed_hosts": ["api.toronto.sx.bet", "realtime.toronto.sx.bet"],
                   "api_key_present": True, "signer_data_dir_present": True, "database_url_present": True}
    assert "dummy" not in repr(out)


@pytest.mark.parametrize("extra,code", [
    ({"SXBET_API_KEY": ""}, "PROFILE_ENV_HAS_PRODUCTION_VARIABLE"),
    ({"DATABASE_URL": "postgresql://x"}, "PROFILE_ENV_HAS_PRODUCTION_VARIABLE"),
    ({"RENDER": "true"}, "PROFILE_ENV_HAS_PRODUCTION_VARIABLE"),
    ({"SXBET_LIVE_TRADING_FLAG": "x"}, "PROFILE_ENV_HAS_PRODUCTION_VARIABLE"),
    ({"SXBET_MAINNET_API_KEY": ""}, "PROFILE_ENV_HAS_PRODUCTION_VARIABLE"),
    ({"SXBET_NETWORK": "mainnet"}, "PROFILE_NETWORK_OF_OTHER_NETWORK"),
    ({np.PROFILE_ENV: "mainnet"}, "PROFILE_ENV_NOT_TORONTO"),
    ({np.TORONTO_ENV["allowed_hosts"]: "api.sx.bet"}, "PROFILE_HOST_OF_OTHER_NETWORK"),
    ({np.TORONTO_ENV["allowed_hosts"]: ""}, "PROFILE_ALLOWED_HOSTS_MISSING"),
    ({np.TORONTO_ENV["signer_data_dir"]: "relative/dir"}, "PROFILE_SIGNER_DATA_DIR_INVALID"),
    ({np.TORONTO_ENV["signer_data_dir"]: "/srv/sxbet-mainnet-signer"}, "PROFILE_SIGNER_DATA_DIR_INVALID"),
    ({np.TORONTO_ENV["signer_data_dir"]: "/srv/toronto/../mainnet"}, "PROFILE_SIGNER_DATA_DIR_INVALID"),
    ({np.TORONTO_ENV["signer_data_dir"]: "/srv/toronto/.."}, "PROFILE_SIGNER_DATA_DIR_INVALID"),
])
def test_toronto_env_negative_cases(extra, code):
    env = _parse_env(ENV_EXAMPLE.read_text(encoding="utf-8"))
    env.update(extra)
    assert _code(np.check_toronto_env, env) == code


def test_missing_profile_selector_is_refused():
    assert _code(np.check_toronto_env, {}) == "PROFILE_ENV_NOT_TORONTO"
    assert _code(np.check_toronto_env, None) == "PROFILE_ENV_INVALID"


# ------------------------------------------------------------------ network-free module
def test_network_profile_module_is_network_free():
    src = (ROOT / "signer" / "network_profile.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "re", "dataclasses", "typing"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"urlopen", "socket", "httpx", "requests", "open", "connect", "environ"}
