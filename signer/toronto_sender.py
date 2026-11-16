"""Toronto-ONLY RestSender for the isolated signer (issue #187, S8; governance §2.6). Runs ONLY in the signer process.

This is the first sender in the repository, and it can reach the SX Bet TESTNET (Toronto) and nothing else.
Mainnet is unreachable by construction, not by configuration:
- the base URL, chain id, EIP-712 verifying contract (OBv3 escrow) and USDC are constants pinned from the
  official documentation (https://docs.sx.bet/api-reference/references, developers/testnet-and-mainnet, read
  16-11-2026); nothing can override them;
- `verify_toronto_config` refuses a SignerConfig that is not exactly that testnet domain, so the signer itself
  can never produce a mainnet signature behind this sender;
- `verify_toronto_metadata` refuses to start unless GET /metadata/obv3 of Toronto reports the same chain,
  domain, escrow and token (a changed contract stops the sender instead of being followed);
- every request is checked before it leaves: only the 7 documented order routes, only the pinned host (the
  transport receives an absolute URL built here), and every order body pinned to the Toronto token and maker;
- WRITES (POST/DELETE) are refused unless the sender was built with `writes_enabled=True`. The protected,
  manual workflow passes it only with the owner's GO (S8: no testnet order is authorized yet).

No network library is imported: the HTTP transport is injected (a callable), so this module stays importable in
the signer package and fully testable offline. The transport never sees anything but a URL under the pinned
host, a method, query pairs, a JSON body and the API key header it adds itself.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from .rest_adapter import RestRequest, RestResponse, SenderRefusedBeforeSend, SenderTimeout

TORONTO = {
    "network": "testnet",
    "base_url": "https://api.toronto.sx.bet",
    "chain_id": 79479957,
    "usdc": "0x1bc6326ea6af2ab8e4b6bc83418044b1923b2956",
    "escrow": "0x0db3aba7de0bcb41ff99d35306f366cffeeede38",
    "domain": ("OBv3 Escrow", "1"),
}
# Everything mainnet, refused wherever it appears (docs: references, testnet-and-mainnet).
MAINNET_MARKERS = ("api.sx.bet", "0x6629ce1cf35cc1329ebb4f63202f3f197b3f050b",
                   "0x890482680c3a0116abb003b17e7d694d13a6c1eb")
MAINNET_CHAIN_ID = 4162

_HASH = r"0x[0-9a-f]{64}"
_CLIENT_ID = r"[A-Za-z0-9_-]{1,64}"
# (method, path regex, write?) -- exactly the routes the adapter and the cancellation service build.
ROUTES = (
    ("POST", r"/orders-v3", True),
    ("DELETE", r"/orders-v3", True),
    ("DELETE", r"/orders-v3/market", True),
    ("DELETE", r"/orders-v3/event", True),
    ("DELETE", r"/orders-v3/all", True),
    ("GET", rf"/orders-v3/{_HASH}", False),
    ("GET", rf"/orders-v3/client/{_CLIENT_ID}", False),
    ("GET", r"/fills-v3", False),
    ("GET", r"/metadata/obv3", False),
)
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class TorontoGuardError(SenderRefusedBeforeSend):
    """A guard refused the request before anything left (subclass of SenderRefusedBeforeSend)."""


def verify_toronto_config(config: Any) -> None:
    """The signer's own domain must be exactly Toronto: then it can never sign a mainnet order for this sender."""
    if getattr(config, "network", None) != TORONTO["network"]:
        raise TorontoGuardError("SIGNER_NETWORK_NOT_TESTNET")
    if getattr(config, "chain_id", None) != TORONTO["chain_id"]:
        raise TorontoGuardError("SIGNER_CHAIN_NOT_TORONTO")
    if str(getattr(config, "verifying_contract", "")).lower() != TORONTO["escrow"]:
        raise TorontoGuardError("SIGNER_VERIFYING_CONTRACT_NOT_TORONTO_ESCROW")
    if str(getattr(config, "base_token", "")).lower() != TORONTO["usdc"]:
        raise TorontoGuardError("SIGNER_BASE_TOKEN_NOT_TORONTO_USDC")


def verify_toronto_metadata(meta: Any) -> None:
    """GET /metadata/obv3 (Toronto) must report the pinned chain, domain, escrow and token, else the sender stops."""
    if not isinstance(meta, Mapping):
        raise TorontoGuardError("METADATA_MISSING")
    domain = meta.get("domain") if isinstance(meta.get("domain"), Mapping) else {}
    asset = meta.get("activeAsset") if isinstance(meta.get("activeAsset"), Mapping) else {}
    if meta.get("chainId") != TORONTO["chain_id"] or domain.get("chainId") != TORONTO["chain_id"]:
        raise TorontoGuardError("METADATA_CHAIN_NOT_TORONTO")
    if (domain.get("name"), domain.get("version")) != TORONTO["domain"]:
        raise TorontoGuardError("METADATA_DOMAIN_UNEXPECTED")
    if str(domain.get("verifyingContract", "")).lower() != TORONTO["escrow"] or \
            str(asset.get("escrowAddress", "")).lower() != TORONTO["escrow"]:
        raise TorontoGuardError("METADATA_ESCROW_NOT_TORONTO")
    if str(asset.get("baseToken", "")).lower() != TORONTO["usdc"]:
        raise TorontoGuardError("METADATA_TOKEN_NOT_TORONTO_USDC")


def _route(request: RestRequest) -> tuple[str, str, bool]:
    for method, pattern, write in ROUTES:
        if request.method == method and re.fullmatch(pattern, request.path):
            return method, pattern, write
    raise TorontoGuardError("ROUTE_NOT_ALLOWED")


def _no_mainnet(value: Any) -> None:
    """Defence in depth only (the host and the order token are pinned): mainnet host / addresses, also without
    0x or URL-encoded, and the mainnet chain id anywhere in the serialized request."""
    text = _unquote(repr(value)).lower()
    if any(m in text or m.removeprefix("0x") in text for m in MAINNET_MARKERS):
        raise TorontoGuardError("MAINNET_VALUE_IN_REQUEST")


def _unquote(text: str) -> str:
    return re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)


def _mainnet_chain(obj: Any, depth: int = 0) -> bool:
    """A `chainId` field equal to the mainnet chain anywhere in the body (structured: no false hits in hex)."""
    if depth > 16:
        raise TorontoGuardError("BODY_TOO_DEEP")
    if isinstance(obj, Mapping):
        return any((k == "chainId" and v in (MAINNET_CHAIN_ID, str(MAINNET_CHAIN_ID))) or _mainnet_chain(v, depth + 1)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return any(_mainnet_chain(v, depth + 1) for v in obj)
    return False


def _check_body(method: str, body: Any, maker: str) -> None:
    if method == "DELETE" and isinstance(body, Mapping) and "maker" in body and \
            str(body.get("maker", "")).lower() != maker:
        raise TorontoGuardError("CANCEL_MAKER_NOT_THE_TORONTO_MAKER")   # review d9ca299 P3-3
    if method == "POST":
        body = body if isinstance(body, Mapping) else None
        orders = body.get("orders") if body else None
        if not isinstance(orders, list) or not orders:
            raise TorontoGuardError("ORDER_BODY_MALFORMED")
        for o in orders:
            if not isinstance(o, Mapping):
                raise TorontoGuardError("ORDER_BODY_MALFORMED")
            if str(o.get("baseToken", "")).lower() != TORONTO["usdc"]:
                raise TorontoGuardError("ORDER_TOKEN_NOT_TORONTO_USDC")
            if str(o.get("maker", "")).lower() != maker:
                raise TorontoGuardError("ORDER_MAKER_NOT_THE_TORONTO_MAKER")


class TorontoRestSender:
    """Built ONLY from the signer process's own `Signer` (review 151b2ed P2): the domain checked is the one the signer
    signs with (re-checked on every send, so a later config change stops the sender), the maker is the signer's
    own EOA (SX Bet: the maker is the signing EOA), and /metadata/obv3 is fetched by the sender itself through the
    guarded read path, never handed in by the caller.

    Transport contract `transport(method, url, query, body_bytes, headers) -> (http_status, parsed_json_or_None)`:
    - `url` is always "https://api.toronto.sx.bet" + a path that passed the route allowlist;
    - `body_bytes` is the JSON body serialized ONCE here and checked in that exact form (or None);
    - the transport must NOT follow redirects (the API key travels in a custom header) and must raise when it
      cannot tell whether the request arrived (-> SenderTimeout, never "not sent").
    Python cannot stop code running inside the signer process from mutating private attributes; the writes flag is
    name-mangled and documented as such (review 151b2ed P3)."""

    def __init__(self, signer: Any, transport: Callable[..., tuple[int, Any]], *, api_key: str,
                 live_maker: str, writes_enabled: bool = False):
        from .core import Signer, SignerConfig
        if not isinstance(signer, Signer) or not isinstance(signer.config, SignerConfig):
            raise TorontoGuardError("NOT_A_SIGNER")
        verify_toronto_config(signer.config)
        maker = str(signer.address).lower()
        if not _ADDRESS.fullmatch(maker):
            raise TorontoGuardError("MAKER_INVALID")
        if not isinstance(live_maker, str) or not _ADDRESS.fullmatch(live_maker.lower()):
            # Mandatory (review d9ca299 P3-1): the owner's LIVE maker address, so the check can never be skipped.
            raise TorontoGuardError("LIVE_MAKER_REQUIRED")
        if live_maker.lower() == maker:
            # §2.6: the testnet key is never the live key (review 151b2ed P3).
            raise TorontoGuardError("TESTNET_SIGNER_IS_THE_LIVE_MAKER")
        if not isinstance(api_key, str) or not api_key.strip():
            raise TorontoGuardError("API_KEY_MISSING")
        if writes_enabled is not True and writes_enabled is not False:
            raise TorontoGuardError("WRITES_FLAG_INVALID")
        self._signer = signer
        self._transport = transport
        self._maker = maker
        self._api_key = api_key
        self.__writes = False
        reply = self.send(RestRequest("GET", "/metadata/obv3", (), None))   # guarded read, writes still off
        body = reply.body if isinstance(reply.body, Mapping) else {}
        if reply.http_status != 200 or body.get("status") != "success":
            raise TorontoGuardError("METADATA_UNAVAILABLE")
        verify_toronto_metadata(body.get("data"))
        self.__writes = writes_enabled

    @property
    def writes_enabled(self) -> bool:
        return self.__writes

    @property
    def maker(self) -> str:
        return self._maker

    def __repr__(self) -> str:  # never the key
        return f"TorontoRestSender(base={TORONTO['base_url']!r}, writes_enabled={self.__writes})"

    def _prepare(self, request: RestRequest) -> tuple[str, tuple, bytes | None, dict[str, str]]:
        """Every guard, no I/O. Raises TorontoGuardError (a SenderRefusedBeforeSend): provably nothing sent."""
        if type(request.method) is not str or type(request.path) is not str:
            raise TorontoGuardError("REQUEST_FIELDS_NOT_PLAIN_STRINGS")   # no str subclass tricks (P3)
        verify_toronto_config(self._signer.config)                       # still the Toronto domain
        _, _, write = _route(request)
        if write and not self.__writes:
            raise TorontoGuardError("SENDER_WRITES_DISABLED")        # nothing leaves: no testnet order authorized
        try:
            query = tuple(request.query)
            ok = all(type(k) is str and type(v) is str for k, v in query)
        except (TypeError, ValueError):
            raise TorontoGuardError("QUERY_MALFORMED") from None
        if not ok:
            raise TorontoGuardError("REQUEST_FIELDS_NOT_PLAIN_STRINGS")
        try:
            body_bytes = None if request.body is None else json.dumps(
                request.body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                allow_nan=False).encode("ascii")
        except (TypeError, ValueError, RecursionError):
            raise TorontoGuardError("BODY_NOT_SERIALIZABLE") from None
        _no_mainnet((request.path, query, body_bytes))
        parsed = json.loads(body_bytes) if body_bytes is not None else None
        if _mainnet_chain(parsed):
            raise TorontoGuardError("MAINNET_VALUE_IN_REQUEST")
        _check_body(request.method, parsed, self._maker)
        url = f"{TORONTO['base_url']}{request.path}"                     # only the pinned host, ever
        headers = {"x-sx-api-key": self._api_key, "Accept": "application/json",
                   "Content-Type": "application/json"}
        return url, query, body_bytes, headers

    def preflight(self, request: RestRequest) -> None:
        """The same guards as send(), without any I/O: lets the gateway refuse BEFORE its write-ahead."""
        self._prepare(request)

    def send(self, request: RestRequest) -> RestResponse:
        url, query, body_bytes, headers = self._prepare(request)
        try:
            status, body = self._transport(request.method, url, query, body_bytes, headers)
        except Exception:
            # Cannot tell whether the exchange received it: never "not sent" (signer reconciles first).
            raise SenderTimeout() from None
        if type(status) is not int:
            raise SenderTimeout()
        return RestResponse(status, body)


__all__ = ["MAINNET_CHAIN_ID", "MAINNET_MARKERS", "ROUTES", "TORONTO", "TorontoGuardError", "TorontoRestSender",
           "verify_toronto_config", "verify_toronto_metadata"]
