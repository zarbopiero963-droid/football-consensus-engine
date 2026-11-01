"""Official SX Bet REST adapter: request BUILDER + response PARSER, behind an abstract sender.

There is NO network code here and no credential handling. A `RestSender` is an abstract Protocol: it
receives a fully built `RestRequest` (method, path, query, JSON body) and returns a `RestResponse`
(HTTP status + parsed JSON body) or raises `SenderTimeout`. Authentication, TLS, base URL, rate
limiting and timeouts belong to the sender. This repository ships no sender implementation (tests use an
in-memory fake): a real sender is trading code -- separate PR, owner review, owner merge (§2.6).

Contract (fetched 01-11-2026 from https://docs.sx.bet/<path>.md and /openapi.json):
- api-reference/post-orders-v3: body {"orders": [CreateOrderItem], "waitForOutcome"?: bool,
  "maxWaitTime"?: int ms}. "1 to `limits.maxCreateOrders` signed orders -- 10 today. An empty array is a
  400. All orders in a batch must share one `maker`." maxWaitTime: "Omitted uses the server default
  (10s today); capped server-side (15s today). Ignored when `waitForOutcome` is not true." (never sent
  then). CreateOrderItem: marketHash, maker, totalBetSize (string), percentageOdds (string), salt,
  expiry (integer, unix SECONDS), baseToken, isMakerBettingOutcomeOne, timeInForce (GTC|IOC|FOK,
  case-sensitive, no default), orderSignature, clientOrderId? (max 64, ^[A-Za-z0-9_-]+$, unique per
  address: "one collision fails the whole batch"), postOnly? ("Add liquidity only. `GTC` only").
- 200: {"status", "data": {"orders": [CreateOrderResult]}} -- "One result per submitted order, in the
  order you sent them." CreateOrderResult: orderId, status (SUBMITTED | FAILED: "rejected before
  submitting"), commandId ("always present, including on FAILED"), clientOrderId?, outcome? ("Present
  only when waitForOutcome was requested and the order was accepted").
- outcome.state: RESTED | FULLY_FILLED | PARTIAL_FILL_DONE | PARTIAL_FILL_RESTED | CANCELLED | TIMEOUT;
  cancelReason (only when CANCELLED): NO_LIQUIDITY | INTERNAL_ERROR | ENGINE_SHUTDOWN | EXPIRED |
  INSUFFICIENT_BALANCE | POST_ONLY_WOULD_CROSS ("These are the only values"). "`outcome` is the
  *matching* result, not on-chain finality."
- developers/posting-orders: "Batches are not atomic. A partial outcome is normal. Always read the
  status of every entry in the array."
- developers/taking-liquidity: "Taking liquidity is the same request as posting an order. There is no
  separate endpoint. Submit a signed limit order ... with timeInForce IOC or FOK". So a TAKER order is an
  IOC/FOK order (the signed fields are identical; percentageOdds is the worst price accepted), a MAKER
  order is GTC (resting; `postOnly` guarantees it never takes). A GTC that crosses on arrival matches
  immediately and rests only the remainder (developers/time-in-force).
- Reads: GET /orders-v3/{orderId}, GET /orders-v3/client/{clientOrderId} -> 200 {"data": {"order":
  OrderV3}} or 404 (also ~10-20 min after INACTIVE); GET /fills-v3?orderId=&perPage=&nextKey= ->
  {"data": {"fills": [FillV3], "nextKey"?}} ("Absent -- not null -- on the last page").

salt: the OpenAPI schema says "Random 32-byte hex string" and the request example sends
"0x8fa0...70ef"; the JS samples send `hexlify(randomBytes(32))`; the Python samples
(developers/posting-orders, developers/taking-liquidity) send `str(int.from_bytes(...))`, the DECIMAL
string of the same uint256. The documented SCHEMA is hex, so the default here is SALT_HEX32 (0x + 64
lowercase hex digits, zero-padded); SALT_DECIMAL is an explicit config choice. Either way the value is
the same uint256 that was signed. DOCS_UNVERIFIED until a testnet POST confirms the accepted format(s).

Reply policy (fail closed; the parser never releases exposure by itself):
- SUBMITTED row -> SUBMITTED. With waitForOutcome: RESTED -> OPEN; every other outcome -> SUBMITTED
  with the whole stake reserved: FULLY_FILLED, PARTIAL_FILL_RESTED, PARTIAL_FILL_DONE and CANCELLED with a
  fillAmount (reason *_RECONCILE_FOR_FILL), CANCELLED without fill (stays reserved until a read confirms),
  TIMEOUT. A reply NEVER sets the matched amount: only reads (GET /orders-v3, /fills-v3) do, because
  outcome.fillAmount units are DOCS_UNVERIFIED and `filled` can never go down again. Any outcome whose
  amounts are inconsistent with the order is ignored (SUBMITTED, OUTCOME_MALFORMED_IGNORED).
  DOCS_UNVERIFIED: that outcome.fillAmount is in the same base units as totalBetSize/remainingSize.
- FAILED row -> REJECTED (the ledger keeps the stake reserved until expiry: the signature was released).
- HTTP 400 for a ONE-order request -> REJECTED; for a batch -> UNKNOWN for every order (not documented
  that every 400 rejects every order of a batch).
- Anything else (timeout, 401/403/5xx, status != success, row count != sent, row at another position or
  with another orderId / clientOrderId, unknown status, missing commandId) -> UNKNOWN.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, REJECTED, UNKNOWN = (
    "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "FILLED", "REJECTED", "UNKNOWN")

SALT_HEX32, SALT_DECIMAL = "HEX32", "DECIMAL"
SALT_FORMATS = frozenset({SALT_HEX32, SALT_DECIMAL})
TIME_IN_FORCE = frozenset({"GTC", "IOC", "FOK"})
MAX_WAIT_TIME_MS = 15_000  # "capped server-side (15s today)"
OUTCOME_STATES = frozenset({"RESTED", "FULLY_FILLED", "PARTIAL_FILL_DONE", "PARTIAL_FILL_RESTED", "CANCELLED",
                            "TIMEOUT"})
CANCEL_REASONS = frozenset({"NO_LIQUIDITY", "INTERNAL_ERROR", "ENGINE_SHUTDOWN", "EXPIRED", "INSUFFICIENT_BALANCE",
                            "POST_ONLY_WOULD_CROSS"})
ORDER_KEYS = frozenset({"marketHash", "maker", "totalBetSize", "percentageOdds", "salt", "expiry", "baseToken",
                        "isMakerBettingOutcomeOne", "timeInForce", "orderSignature", "clientOrderId"})
FILLS_PER_PAGE = 100

_HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIG = re.compile(r"^0x[0-9a-fA-F]{130}$")
_HEX_SALT = re.compile(r"^0x[0-9a-f]{64}$")


class AdapterRefused(RuntimeError):
    """A request that would not match the documented contract is never built (fixed code)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SenderTimeout(Exception):
    """Raised by a sender when it cannot tell whether the exchange received the request."""


# Kept for the gateway's legacy transports: same meaning as SenderTimeout.
TransportTimeout = SenderTimeout


@dataclass(frozen=True)
class RestRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    body: Any


@dataclass(frozen=True)
class RestResponse:
    http_status: int
    body: Any


class RestSender(Protocol):
    """Abstract. No implementation exists in this repository (see module docstring)."""

    def send(self, request: RestRequest) -> RestResponse: ...


@dataclass(frozen=True)
class ExchangeLimits:
    """`limits` of GET /metadata/obv3 (maxCreateOrders 10, maxCancelOrders 100 "today")."""
    max_create_orders: int
    max_cancel_orders: int

    def __post_init__(self):
        for v in (self.max_create_orders, self.max_cancel_orders):
            if type(v) is not int or not 1 <= v <= 1000:
                raise AdapterRefused("EXCHANGE_LIMITS_INVALID")

    @classmethod
    def from_metadata(cls, limits: Any) -> "ExchangeLimits":
        if not isinstance(limits, Mapping):
            raise AdapterRefused("EXCHANGE_LIMITS_INVALID")
        return cls(limits.get("maxCreateOrders"), limits.get("maxCancelOrders"))


@dataclass(frozen=True)
class SubmitOptions:
    """Owner/deployment configuration of the create request. Never an MCP tool argument."""
    wait_for_outcome: bool = False
    max_wait_time_ms: int | None = None
    post_only: bool = False          # applied to GTC orders; an IOC/FOK order is refused locally
    salt_format: str = SALT_HEX32    # DOCS_UNVERIFIED (see module docstring)

    def __post_init__(self):
        if type(self.wait_for_outcome) is not bool or type(self.post_only) is not bool:
            raise AdapterRefused("SUBMIT_OPTIONS_INVALID")
        if self.salt_format not in SALT_FORMATS:
            raise AdapterRefused("SALT_FORMAT_INVALID")
        if self.max_wait_time_ms is not None:
            if not self.wait_for_outcome:
                raise AdapterRefused("MAX_WAIT_TIME_WITHOUT_WAIT_FOR_OUTCOME")
            if type(self.max_wait_time_ms) is not int or not 1 <= self.max_wait_time_ms <= MAX_WAIT_TIME_MS:
                raise AdapterRefused("MAX_WAIT_TIME_INVALID")


@dataclass(frozen=True)
class ExpectedOrder:
    order_id: str
    client_order_id: str
    stake: int


@dataclass(frozen=True)
class CreateResult:
    order_id: str
    client_order_id: str
    state: str
    reason: str
    filled: int | None = None
    command_id: str | None = None
    outcome_state: str | None = None
    cancel_reason: str | None = None
    evidence_sha256: str | None = None


def _sha(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


def _dec(v: Any) -> int | None:
    return int(v) if isinstance(v, str) and _DECIMAL.fullmatch(v) else None


def wire_salt(salt: Any, salt_format: str) -> str:
    """The signed uint256 salt (decimal string, as in the intent) in the configured wire format."""
    if not isinstance(salt, str) or not _DECIMAL.fullmatch(salt) or int(salt) >= 2 ** 256:
        raise AdapterRefused("ORDER_SALT_INVALID")
    if salt_format == SALT_DECIMAL:
        return salt
    return "0x" + format(int(salt), "064x")


# ---------------------------------------------------------------- POST /orders-v3
def build_create_orders(orders: Sequence[Mapping[str, Any]], *, limits: ExchangeLimits,
                        options: SubmitOptions) -> RestRequest:
    if not isinstance(orders, (list, tuple)) or not 1 <= len(orders) <= limits.max_create_orders:
        raise AdapterRefused("BATCH_SIZE_INVALID")
    out, makers, client_ids = [], set(), set()
    for o in orders:
        if not isinstance(o, Mapping) or set(o) != ORDER_KEYS:
            raise AdapterRefused("ORDER_KEYS_INVALID")
        if not isinstance(o["marketHash"], str) or not _HEX32.fullmatch(o["marketHash"]):
            raise AdapterRefused("ORDER_MARKET_HASH_INVALID")
        for k in ("maker", "baseToken"):
            if not isinstance(o[k], str) or not _ADDRESS.fullmatch(o[k]):
                raise AdapterRefused("ORDER_ADDRESS_INVALID")
        for k in ("totalBetSize", "percentageOdds"):
            if not isinstance(o[k], str) or not _DECIMAL.fullmatch(o[k]) or int(o[k]) <= 0:
                raise AdapterRefused("ORDER_AMOUNT_INVALID")
        if type(o["expiry"]) is not int or o["expiry"] <= 0:
            raise AdapterRefused("ORDER_EXPIRY_INVALID")  # unix SECONDS, an integer
        if not isinstance(o["isMakerBettingOutcomeOne"], bool):
            raise AdapterRefused("ORDER_SIDE_INVALID")
        if not isinstance(o["timeInForce"], str) or o["timeInForce"] not in TIME_IN_FORCE:
            raise AdapterRefused("ORDER_TIME_IN_FORCE_INVALID")
        if not isinstance(o["orderSignature"], str) or not _SIG.fullmatch(o["orderSignature"]):
            raise AdapterRefused("ORDER_SIGNATURE_INVALID")
        if not isinstance(o["clientOrderId"], str) or not _CLIENT_ID.fullmatch(o["clientOrderId"]):
            raise AdapterRefused("ORDER_CLIENT_ID_INVALID")
        if o["clientOrderId"] in client_ids:
            raise AdapterRefused("BATCH_DUPLICATE_CLIENT_ORDER_ID")
        client_ids.add(o["clientOrderId"])
        makers.add(o["maker"].lower())
        item = dict(o)
        item["salt"] = wire_salt(o["salt"], options.salt_format)
        if options.post_only:
            if o["timeInForce"] != "GTC":
                raise AdapterRefused("POST_ONLY_REQUIRES_GTC")
            item["postOnly"] = True
        out.append(item)
    if len(makers) != 1:
        raise AdapterRefused("BATCH_MIXED_MAKERS")
    body: dict[str, Any] = {"orders": out}
    if options.wait_for_outcome:
        body["waitForOutcome"] = True
        if options.max_wait_time_ms is not None:
            body["maxWaitTime"] = options.max_wait_time_ms
    return RestRequest("POST", "/orders-v3", (), body)


def _status_body(response: Any) -> tuple[Any, Any]:
    if isinstance(response, RestResponse):
        return response.http_status, response.body
    if isinstance(response, Mapping):  # legacy transport shape {"http_status", "body"}
        return response.get("http_status"), response.get("body")
    return None, None


def _outcome(row: Mapping[str, Any], exp: ExpectedOrder, base: dict[str, Any]) -> CreateResult:
    out = row.get("outcome")
    st = out.get("state") if isinstance(out, Mapping) else None
    malformed = CreateResult(**{**base, "state": SUBMITTED, "reason": "OUTCOME_MALFORMED_IGNORED",
                                "outcome_state": st if isinstance(st, str) else None})
    if not isinstance(out, Mapping) or st not in OUTCOME_STATES:
        return malformed
    raw_fill = out.get("fillAmount")
    fill = 0 if raw_fill is None else _dec(raw_fill)
    if fill is None or fill > exp.stake:
        return malformed
    res = {**base, "outcome_state": st}
    if st == "TIMEOUT":
        return CreateResult(**res, state=SUBMITTED, reason="OUTCOME_TIMEOUT")
    if st == "RESTED":
        if fill:
            return malformed
        return CreateResult(**res, state=OPEN, filled=0, reason="OUTCOME_RESTED")
    # outcome.fillAmount units are DOCS_UNVERIFIED: a reply never sets `filled` (an overstated value would
    # make every later read fail as FILLED_AMOUNT_INVALID, since filled never decreases). An outcome with a
    # fill keeps the order SUBMITTED, i.e. the whole stake reserved; only exchange READS record the fill.
    if st == "FULLY_FILLED":
        if fill != exp.stake:
            return malformed
        return CreateResult(**res, state=SUBMITTED, reason="OUTCOME_FULLY_FILLED_RECONCILE_FOR_FILL")
    if st == "PARTIAL_FILL_RESTED":
        if not 0 < fill < exp.stake:
            return malformed
        return CreateResult(**res, state=SUBMITTED, reason="OUTCOME_PARTIAL_FILL_RESTED_RECONCILE_FOR_FILL")
    if st == "PARTIAL_FILL_DONE":
        if not 0 < fill < exp.stake:
            return malformed
        return CreateResult(**res, state=SUBMITTED, reason="OUTCOME_PARTIAL_FILL_DONE_RECONCILE_FOR_FILL")
    # CANCELLED: off the book per the engine, but only a read releases the reservation (or records a fill).
    cr = out.get("cancelReason")
    res["cancel_reason"] = cr if cr in CANCEL_REASONS else None
    if fill:
        return CreateResult(**res, state=SUBMITTED, reason="OUTCOME_CANCELLED_RECONCILE_FOR_FILL")
    return CreateResult(**res, state=SUBMITTED, reason="OUTCOME_CANCELLED_RECONCILE_TO_RELEASE")


def parse_create_orders(response: Any, expected: Sequence[ExpectedOrder], *,
                        wait_for_outcome: bool) -> list[CreateResult]:
    """One CreateResult per expected order, in order. `response` None = the sender timed out."""
    def all_(state: str, reason: str) -> list[CreateResult]:
        return [CreateResult(e.order_id, e.client_order_id, state, reason) for e in expected]

    if response is None:
        return all_(UNKNOWN, "TRANSPORT_TIMEOUT")
    status, body = _status_body(response)
    if status == 400 and isinstance(body, Mapping) and isinstance(body.get("message"), (str, list)):
        return all_(REJECTED, "EXCHANGE_REJECTED_400") if len(expected) == 1 else \
            all_(UNKNOWN, "BATCH_REJECTED_400_AMBIGUOUS")
    if status != 200 or not isinstance(body, Mapping) or body.get("status") != "success":
        return all_(UNKNOWN, "REPLY_NOT_DEFINITIVE")
    data = body.get("data")
    rows = data.get("orders") if isinstance(data, Mapping) else None
    if not isinstance(rows, list) or len(rows) != len(expected):
        return all_(UNKNOWN, "REPLY_ROWS_AMBIGUOUS")
    results = []
    for exp, row in zip(expected, rows):
        def unknown(reason: str) -> CreateResult:
            return CreateResult(exp.order_id, exp.client_order_id, UNKNOWN, reason)

        if not isinstance(row, Mapping) or not isinstance(row.get("orderId"), str) \
                or row["orderId"].lower() != exp.order_id:
            results.append(unknown("REPLY_OTHER_ORDER_ID"))
            continue
        if "clientOrderId" in row and row["clientOrderId"] != exp.client_order_id:
            results.append(unknown("REPLY_CLIENT_ORDER_ID_MISMATCH"))
            continue
        cmd = row.get("commandId")
        if not isinstance(cmd, str) or not cmd:
            results.append(unknown("REPLY_COMMAND_ID_MISSING"))
            continue
        base = {"order_id": exp.order_id, "client_order_id": exp.client_order_id, "command_id": cmd,
                "evidence_sha256": _sha({"create_result": row})}
        if row.get("status") == "FAILED":
            results.append(CreateResult(**base, state=REJECTED, reason="EXCHANGE_FAILED"))
        elif row.get("status") != "SUBMITTED":
            results.append(unknown("REPLY_STATUS_UNKNOWN"))
        elif "outcome" not in row:
            results.append(CreateResult(**base, state=SUBMITTED, reason="EXCHANGE_SUBMITTED"))
        elif not wait_for_outcome:
            results.append(CreateResult(**base, state=SUBMITTED, reason="OUTCOME_UNREQUESTED_IGNORED"))
        else:
            results.append(_outcome(row, exp, base))
    return results


# ---------------------------------------------------------------- reads
def build_get_order(order_id: str) -> RestRequest:
    if not isinstance(order_id, str) or not _HEX32.fullmatch(order_id):
        raise AdapterRefused("ORDER_ID_INVALID")
    return RestRequest("GET", "/orders-v3/" + order_id, (), None)


def build_get_order_by_client_id(client_order_id: str) -> RestRequest:
    if not isinstance(client_order_id, str) or not _CLIENT_ID.fullmatch(client_order_id):
        raise AdapterRefused("ORDER_CLIENT_ID_INVALID")
    return RestRequest("GET", "/orders-v3/client/" + client_order_id, (), None)


def build_get_fills(order_id: str, *, next_key: str | None = None, per_page: int = FILLS_PER_PAGE) -> RestRequest:
    if not isinstance(order_id, str) or not _HEX32.fullmatch(order_id):
        raise AdapterRefused("ORDER_ID_INVALID")
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise AdapterRefused("PER_PAGE_INVALID")
    q = [("orderId", order_id), ("perPage", str(per_page))]
    if next_key is not None:
        if not isinstance(next_key, str) or not 0 < len(next_key) <= 1024:
            raise AdapterRefused("NEXT_KEY_INVALID")
        q.append(("nextKey", next_key))
    return RestRequest("GET", "/fills-v3", tuple(q), None)


def parse_lookup(response: Any) -> dict[str, Any]:
    status, body = _status_body(response)
    if status == 404:
        return {"kind": "NOT_FOUND"}
    if status == 200 and isinstance(body, Mapping) and body.get("status") == "success":
        data = body.get("data")
        row = data.get("order") if isinstance(data, Mapping) else None
        if isinstance(row, Mapping):
            return {"kind": "FOUND", "order": dict(row)}
    return {"kind": "ERROR"}


class OfficialRestAdapter:
    """SubmissionTransport for signer/gateway.py built on the documented REST contract and an abstract
    `RestSender`. It builds and parses; the sender (not shipped here) does the I/O."""

    def __init__(self, sender: RestSender, *, limits: ExchangeLimits, options: SubmitOptions | None = None,
                 max_fill_pages: int = 20):
        if not isinstance(limits, ExchangeLimits):
            raise AdapterRefused("EXCHANGE_LIMITS_INVALID")
        options = SubmitOptions() if options is None else options
        if not isinstance(options, SubmitOptions):
            raise AdapterRefused("SUBMIT_OPTIONS_INVALID")
        if type(max_fill_pages) is not int or not 1 <= max_fill_pages <= 1000:
            raise AdapterRefused("MAX_FILL_PAGES_INVALID")
        self._sender = sender
        self.limits = limits
        self.options = options
        self._max_fill_pages = max_fill_pages

    @property
    def wait_for_outcome(self) -> bool:
        return self.options.wait_for_outcome

    def send(self, request: RestRequest) -> RestResponse | None:
        """None = timeout or transport failure (the caller must treat it as 'unknown')."""
        try:
            resp = self._sender.send(request)
        except Exception:
            return None
        if not isinstance(resp, RestResponse) or type(resp.http_status) is not int:
            return RestResponse(0, None)
        return resp

    # ---- create ----
    def build_create(self, payload: Mapping[str, Any]) -> RestRequest:
        orders = payload.get("orders") if isinstance(payload, Mapping) else None
        return build_create_orders(orders, limits=self.limits, options=self.options)

    def submit_orders(self, payload: Mapping[str, Any]) -> RestResponse:
        """SubmissionTransport compatibility: build + send; raises SenderTimeout when unknown."""
        resp = self.send(self.build_create(payload))
        if resp is None:
            raise SenderTimeout()
        return resp

    # ---- reads (SubmissionTransport) ----
    def _lookup(self, build, arg) -> dict[str, Any]:
        try:
            req = build(arg)
        except AdapterRefused:
            return {"kind": "ERROR"}
        return parse_lookup(self.send(req))

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._lookup(build_get_order, order_id)

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        return self._lookup(build_get_order_by_client_id, client_order_id)

    def get_fills(self, order_id: str) -> dict[str, Any]:
        fills: list[Any] = []
        next_key = None
        for _ in range(self._max_fill_pages):
            try:
                req = build_get_fills(order_id, next_key=next_key)
            except AdapterRefused:
                return {"kind": "ERROR"}
            status, body = _status_body(self.send(req))
            data = body.get("data") if status == 200 and isinstance(body, Mapping) \
                and body.get("status") == "success" else None
            page = data.get("fills") if isinstance(data, Mapping) else None
            if not isinstance(page, list):
                return {"kind": "ERROR"}
            fills.extend(page)
            if "nextKey" not in data:
                return {"kind": "OK", "fills": fills, "complete": True}
            next_key = data["nextKey"]
            if not isinstance(next_key, str) or not next_key:
                return {"kind": "ERROR"}
        return {"kind": "OK", "fills": fills, "complete": False}  # page cap: never presented as complete
