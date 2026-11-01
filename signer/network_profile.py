"""Pinned SX Bet network profiles (mainnet / Toronto testnet) and a network-free cross-network guard.

No network code, no credentials, no I/O: every check is a pure function over values the caller already
holds (an intent, a signer config, a `GET /metadata/obv3` `data` object fetched elsewhere, a URL, an
env mapping). Its only job is to make it impossible for a Toronto profile to point at mainnet hosts,
chain id or contracts, and vice versa. Any mismatch raises ProfileMismatch(code) with a fixed code.

Pinned values: https://docs.sx.bet/developers/testnet-and-mainnet ("Configuration reference",
last-modified 31-10-2026) and https://docs.sx.bet/api-reference/references, confirmed by an
unauthenticated `GET /metadata/obv3` on both networks on 2026-11-01T16:00:46Z (see
docs/sxbet/testnet_toronto_readiness.md). The docs say "Read every value at runtime": these pins do
not replace the runtime read, they reject a runtime value that belongs to the OTHER network or to
neither (a changed contract is a stop-and-verify event, never silently accepted).

Profile names: "mainnet" and "toronto". The intent/signer `network` field keeps its existing values
("mainnet", "testnet"; signer/intent.py CHAIN_IDS), so profile "toronto" maps to network "testnet".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

DOMAIN_NAME, DOMAIN_VERSION = "OBv3 Escrow", "1"


@dataclass(frozen=True)
class NetworkProfile:
    name: str                 # profile name: "mainnet" | "toronto"
    network: str              # intent/signer network field: "mainnet" | "testnet"
    chain_id: int
    escrow: str               # EIP-712 verifyingContract == activeAsset.escrowAddress (lowercase)
    usdc: str                 # activeAsset.baseToken (lowercase)
    transfer_to_proxy_executor: str  # EIP-2612 permit spender (lowercase)
    api_base_url: str
    realtime_ws_url: str
    rpc_url: str
    explorer_api_url: str
    app_url: str              # owner's browser only; never a host the code calls

    @property
    def hosts(self) -> frozenset[str]:
        """Every host the code of this profile may ever address (the app host is excluded)."""
        return frozenset(_host(u) for u in (
            self.api_base_url, self.realtime_ws_url, self.rpc_url, self.explorer_api_url))

    @property
    def contracts(self) -> frozenset[str]:
        return frozenset({self.escrow, self.usdc, self.transfer_to_proxy_executor})


MAINNET = NetworkProfile(
    name="mainnet", network="mainnet", chain_id=4162,
    escrow="0x890482680c3a0116abb003b17e7d694d13a6c1eb",
    usdc="0x6629ce1cf35cc1329ebb4f63202f3f197b3f050b",
    transfer_to_proxy_executor="0xfc3dbb46d0703703fc1d1677e7e0babf971adcff",
    api_base_url="https://api.sx.bet",
    realtime_ws_url="wss://realtime.sx.bet/connection/websocket",
    rpc_url="https://rpc-rollup.sx.technology",
    explorer_api_url="https://explorerl2.sx.technology/api",
    app_url="https://sx.bet",
)

TORONTO = NetworkProfile(
    name="toronto", network="testnet", chain_id=79479957,
    escrow="0x0db3aba7de0bcb41ff99d35306f366cffeeede38",
    usdc="0x1bc6326ea6af2ab8e4b6bc83418044b1923b2956",
    transfer_to_proxy_executor="0xbb2828d990ec30b46715773b654ec4d44703177d",
    api_base_url="https://api.toronto.sx.bet",
    realtime_ws_url="wss://realtime.toronto.sx.bet/connection/websocket",
    rpc_url="https://rpc-rollup.toronto.sx.technology",
    explorer_api_url="https://explorerl2.toronto.sx.technology/api",
    app_url="https://toronto.sx.bet",
)

PROFILES: Mapping[str, NetworkProfile] = {"mainnet": MAINNET, "toronto": TORONTO}
_OTHER = {"mainnet": TORONTO, "toronto": MAINNET}

# Env names of a Toronto (testnet) environment. Deliberately distinct from every production name.
PROFILE_ENV = "SXBET_NETWORK_PROFILE"
TORONTO_ENV = {
    "api_key": "SXBET_TORONTO_API_KEY",
    "signer_data_dir": "SXBET_TORONTO_SIGNER_DATA_DIR",
    "database_url": "SXBET_TORONTO_DATABASE_URL",
    "allowed_hosts": "SXBET_TORONTO_ALLOWED_HOSTS",
}
# Production names that must never be present in a Toronto environment: their presence means the
# testnet process would share mainnet credentials, database or runtime.
PRODUCTION_ONLY_ENV = frozenset({"SXBET_API_KEY", "DATABASE_URL", "RENDER", "SXBET_LIVE_TRADING_FLAG"})

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Strict URL form (no urllib: the signer package bans it): scheme, lowercase host, optional path/query.
# No userinfo, no port, no fragment: anything else is PROFILE_URL_INVALID.
_URL = re.compile(r"^(https|wss)://([a-z0-9.-]{1,253})(/[!-~]*)?$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")


def _host(url: str) -> str:
    m = _URL.fullmatch(url)
    if m is None or "#" in url:
        raise ValueError("pinned URL malformed")
    return m.group(2)


class ProfileMismatch(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def get_profile(name: Any) -> NetworkProfile:
    if not isinstance(name, str) or name not in PROFILES:
        raise ProfileMismatch("PROFILE_UNKNOWN")
    return PROFILES[name]


def _profile(p: Any) -> NetworkProfile:
    return p if isinstance(p, NetworkProfile) and PROFILES.get(p.name) is p else get_profile(p)


def _addr(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ProfileMismatch(code)
    return value.lower()


def _expect_chain(p: NetworkProfile, chain_id: Any) -> None:
    if type(chain_id) is not int:
        raise ProfileMismatch("PROFILE_CHAIN_ID_INVALID")
    if chain_id == _OTHER[p.name].chain_id:
        raise ProfileMismatch("PROFILE_CHAIN_ID_OF_OTHER_NETWORK")
    if chain_id != p.chain_id:
        raise ProfileMismatch("PROFILE_CHAIN_ID_UNEXPECTED")


def _expect_addr(p: NetworkProfile, value: Any, attr: str, code: str) -> None:
    a = _addr(value, f"PROFILE_{code}_INVALID")
    if a in _OTHER[p.name].contracts:
        raise ProfileMismatch(f"PROFILE_{code}_OF_OTHER_NETWORK")
    if a != getattr(p, attr):
        raise ProfileMismatch(f"PROFILE_{code}_UNEXPECTED")


def _expect_network(p: NetworkProfile, network: Any) -> None:
    if network == _OTHER[p.name].network:
        raise ProfileMismatch("PROFILE_NETWORK_OF_OTHER_NETWORK")
    if network != p.network:
        raise ProfileMismatch("PROFILE_NETWORK_UNEXPECTED")


def check_intent(profile: Any, intent: Mapping[str, Any]) -> None:
    """The intent's network, chain id, verifyingContract and baseToken all belong to `profile`."""
    p = _profile(profile)
    if not isinstance(intent, Mapping):
        raise ProfileMismatch("PROFILE_INTENT_INVALID")
    _expect_network(p, intent.get("network"))
    _expect_chain(p, intent.get("chain_id"))
    _expect_addr(p, intent.get("verifying_contract"), "escrow", "ESCROW")
    _expect_addr(p, intent.get("base_token"), "usdc", "BASE_TOKEN")


def check_signer_config(profile: Any, config: Any) -> None:
    """A signer.core.SignerConfig (duck-typed) whose network/chain/domain all belong to `profile`."""
    p = _profile(profile)
    try:
        network, chain_id = config.network, config.chain_id
        escrow, token = config.verifying_contract, config.base_token
    except AttributeError:
        raise ProfileMismatch("PROFILE_SIGNER_CONFIG_INVALID") from None
    _expect_network(p, network)
    _expect_chain(p, chain_id)
    _expect_addr(p, escrow, "escrow", "ESCROW")
    _expect_addr(p, token, "usdc", "BASE_TOKEN")


def check_metadata(profile: Any, data: Mapping[str, Any]) -> None:
    """The `data` object of `GET /metadata/obv3` (fetched elsewhere) belongs to `profile`.

    Checks chainId, the full EIP-712 domain, activeAsset (baseToken, escrowAddress == verifyingContract,
    decimals 6) and transferToProxyExecutorAddress. Other fields (limits, bettingDelay, ladder) are
    runtime values the caller reads as-is; they are not network identity.
    """
    p = _profile(profile)
    if not isinstance(data, Mapping):
        raise ProfileMismatch("PROFILE_METADATA_INVALID")
    domain, asset = data.get("domain"), data.get("activeAsset")
    if not isinstance(domain, Mapping) or not isinstance(asset, Mapping):
        raise ProfileMismatch("PROFILE_METADATA_INVALID")
    _expect_chain(p, data.get("chainId"))
    _expect_chain(p, domain.get("chainId"))
    if (domain.get("name"), domain.get("version")) != (DOMAIN_NAME, DOMAIN_VERSION):
        raise ProfileMismatch("PROFILE_DOMAIN_UNEXPECTED")
    _expect_addr(p, domain.get("verifyingContract"), "escrow", "ESCROW")
    _expect_addr(p, asset.get("escrowAddress"), "escrow", "ESCROW")
    _expect_addr(p, asset.get("baseToken"), "usdc", "BASE_TOKEN")
    _expect_addr(p, data.get("transferToProxyExecutorAddress"), "transfer_to_proxy_executor",
                 "PROXY_EXECUTOR")
    if asset.get("decimals") != 6 or type(asset.get("decimals")) is not int:
        raise ProfileMismatch("PROFILE_DECIMALS_UNEXPECTED")


def check_url(profile: Any, url: Any, *, allowed_hosts: frozenset[str] | None = None) -> str:
    """Return the host of `url` if it is an https/wss URL to a host of `profile` (and of `allowed_hosts`)."""
    p = _profile(profile)
    if not isinstance(url, str) or not url.isascii():  # before lower(): no Unicode case folding to ASCII
        raise ProfileMismatch("PROFILE_URL_INVALID")
    m = _URL.fullmatch(url.lower())
    if m is None or "#" in url or "@" in url:
        raise ProfileMismatch("PROFILE_URL_INVALID")
    host = m.group(2)
    if host in _OTHER[p.name].hosts or host == _host(_OTHER[p.name].app_url):
        raise ProfileMismatch("PROFILE_HOST_OF_OTHER_NETWORK")
    if host not in p.hosts:
        raise ProfileMismatch("PROFILE_HOST_NOT_ALLOWED")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ProfileMismatch("PROFILE_HOST_NOT_ALLOWED")
    return host


def parse_allowed_hosts(profile: Any, raw: Any) -> frozenset[str]:
    """A comma-separated host list; every entry must be a host of `profile`. Empty is refused."""
    p = _profile(profile)
    if not isinstance(raw, str):
        raise ProfileMismatch("PROFILE_ALLOWED_HOSTS_MISSING")
    hosts = [h.strip() for h in raw.split(",")]
    if not raw.strip() or any(not h for h in hosts):
        raise ProfileMismatch("PROFILE_ALLOWED_HOSTS_MISSING")
    for h in hosts:
        if not _HOST.fullmatch(h):
            raise ProfileMismatch("PROFILE_ALLOWED_HOSTS_INVALID")
        if h in _OTHER[p.name].hosts or h == _host(_OTHER[p.name].app_url):
            raise ProfileMismatch("PROFILE_HOST_OF_OTHER_NETWORK")
        if h not in p.hosts:
            raise ProfileMismatch("PROFILE_HOST_NOT_ALLOWED")
    return frozenset(hosts)


def check_toronto_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Validate a Toronto environment mapping (see docs/sxbet/toronto.env.example).

    Returns only presence flags and the allowlist, never a credential value.
    """
    if not isinstance(env, Mapping):
        raise ProfileMismatch("PROFILE_ENV_INVALID")
    if env.get(PROFILE_ENV, "").strip() != "toronto":
        raise ProfileMismatch("PROFILE_ENV_NOT_TORONTO")
    present = sorted(k for k in PRODUCTION_ONLY_ENV if k in env)
    if present:
        raise ProfileMismatch("PROFILE_ENV_HAS_PRODUCTION_VARIABLE")
    if "SXBET_NETWORK" in env and env["SXBET_NETWORK"].strip().lower() != "testnet":
        raise ProfileMismatch("PROFILE_NETWORK_OF_OTHER_NETWORK")
    for k in env:
        if k.startswith("SXBET_MAINNET_"):
            raise ProfileMismatch("PROFILE_ENV_HAS_PRODUCTION_VARIABLE")
    hosts = parse_allowed_hosts(TORONTO, env.get(TORONTO_ENV["allowed_hosts"]))
    data_dir = env.get(TORONTO_ENV["signer_data_dir"], "").strip()
    if data_dir and (not data_dir.startswith("/") or "mainnet" in data_dir.lower()
                     or "/../" in data_dir + "/"):
        raise ProfileMismatch("PROFILE_SIGNER_DATA_DIR_INVALID")
    return {
        "profile": "toronto",
        "allowed_hosts": sorted(hosts),
        "api_key_present": bool(env.get(TORONTO_ENV["api_key"], "").strip()),
        "signer_data_dir_present": bool(data_dir),
        "database_url_present": bool(env.get(TORONTO_ENV["database_url"], "").strip()),
    }
