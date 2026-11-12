"""SIMULATED SX Bet exchange for offline end-to-end tests. TESTS ONLY: never imported by signer/ or bridge/.

`SimExchange` implements the abstract `signer.rest_adapter.RestSender` protocol IN MEMORY: it answers
`RestRequest`s built by `OfficialRestAdapter` / `signer.cancellation` with `RestResponse`s shaped like the
official contract. It opens no socket, reads no API key, signs nothing and holds no key. Every number it
produces is SIMULATED: it reproduces the documented contract, it does not prove how the real exchange
behaves. Where the docs are silent the choice made here is listed in SIM_ASSUMPTIONS (DOCS_UNVERIFIED).

Documented contract reproduced (docs.sx.bet, as summarised in docs/sxbet/magic_and_proxy_wallet.md §5,
signer/rest_adapter.py, signer/cancellation.py, bridge/sxbet/positions.py, bridge/sxbet/hedging.py):
- binary markets; percentageOdds = implied probability x 1e20 of the party holding the order; a maker resting
  at p offers the taker q = 1e20 - p, with taker capacity size * (1e20 - p) / p; return = stake * 1e20 / odds
  (stake included); taking = the same POST /orders-v3 with IOC / FOK, percentageOdds = the highest implied
  probability accepted; GTC rests the remainder, IOC discards it, FOK executes nothing unless complete;
  postOnly never takes; the engine never matches an account against its own resting orders;
- POST /orders-v3: 1..maxCreateOrders orders of one maker, non-atomic, one row per order in order
  (SUBMITTED | FAILED, commandId always present), optional waitForOutcome outcome; a clientOrderId collision
  fails the whole batch (HTTP 400);
- lifecycle PENDING -> ACTIVE -> INACTIVE with inactiveReason FILLED / USER_REQUESTED / EXPIRED / NO_LIQUIDITY /
  INSUFFICIENT_BALANCE / EVENT_LIFECYCLE / HEARTBEAT_TIMEOUT / POST_ONLY_WOULD_CROSS; an INACTIVE row is
  returned for a while (purge_after_seconds, docs: ~10-20 min) then 404;
- DELETE /orders-v3 (by id: cancelled / notCancelled{NOT_FOUND|NOT_ACTIVE|CANCEL_PENDING} / unconfirmed),
  DELETE /orders-v3/market|event|all (cancelledSubmitted only, never proof);
- GET /orders-v3/{id}, /orders-v3/client/{cid}, /orders-v3 (open orders), /fills-v3 with nextKey pagination
  ("absent -- not null -- on the last page"), /user/balance-v3, /user/fees-v3, /markets/find,
  /orderbook-v3/snapshot/market (maker frame or showTakerPerspective), /orders-v3/odds/best;
- bets MATCHED -> LOCKED -> SETTLED; settlement outcome 1 / 2 / 0 (void); payout fee on profit only, maker or
  taker rate of the fill; posting does not lock funds, a match moves the stake to escrow;
- POST /heartbeat/v3 {timeoutSeconds}: a lapse cancels ALL orders with HEARTBEAT_TIMEOUT.

Injectable faults (`inject`): timeout / HTTP 500 before or after the exchange processed the request, HTTP 429,
caller crash (SimCrash, a BaseException) before or after processing, delayed visibility of a new order,
read lag (stale replica), fill lag, a third-party fill racing a cancel, unconfirmed or late-landing cancels.
There is NO cashout route: SX Bet documents none; closing a position is an opposite-outcome order (ours).
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Callable

from eth_account import Account

from signer import eip712
from signer.rest_adapter import RestRequest, RestResponse, SenderTimeout

ODDS = 10 ** 20
LIQUIDITY_MAKER = "0x" + "ee" * 20
SIM_LABEL = "SIMULATED_NOT_NATIVE_EVIDENCE"
SIM_ASSUMPTIONS = (
    "MATCH_ROUNDING: taker return = floor(stake * 1e20 / q); maker stake consumed = ceil(stake * p / q) capped at "
    "the resting size; maker return = floor(maker stake * 1e20 / p) (exchange rounding DOCS_UNVERIFIED)",
    "FILL_AMOUNT_UNITS: fillAmount = the account's own stake, so sum(fillAmount) = totalBetSize - remainingSize "
    "(the signer's DOCS_UNVERIFIED assumption holds by construction here)",
    "SETTLE_RETURN_NET: settleReturnAmount = return - floor(profit * payout fee) on a win, 0 on a loss, the stake "
    "on a void; settleFeeAmount reports the fee (gross/net DOCS_UNVERIFIED)",
    "VALIDATION_ERRORS: odds off ladder, size below orderSizeMinimumBaseUnits, bad signature, postOnly with "
    "IOC/FOK, clientOrderId collision -> HTTP 400 for the whole request; state-dependent refusals (market not "
    "ACTIVE, insufficient balance, expiry too soon) -> a FAILED row",
    "IOC_REMAINDER: an IOC / FOK remainder ends INACTIVE NO_LIQUIDITY",
    "BALANCE: maker check resting + new <= available per market:outcome at POST; a match moves stake to escrow",
)
# FILL_AMOUNT_UNITS and SETTLE_RETURN_NET are the SAME DOCS_UNVERIFIED assumptions signer/orders.py relies on
# (settlement condition and realized P&L). The simulator satisfies them by construction, so simulated tests
# CANNOT validate them: only a testnet (Toronto) partial fill + settlement can.
CIRCULAR_ASSUMPTIONS = ("FILL_AMOUNT_UNITS", "SETTLE_RETURN_NET")


class SimCrash(BaseException):
    """Simulated death of the CALLER process at a crash point (BaseException: no `except Exception` catches it)."""


def iso(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def ok(data: Any) -> RestResponse:
    return RestResponse(200, {"status": "success", "data": data})


def err(status: int, message: str) -> RestResponse:
    return RestResponse(status, {"message": message, "statusCode": status})


@dataclass
class SimMarket:
    market_hash: str
    event_id: str
    outcome_one_name: str
    outcome_two_name: str
    status: str = "ACTIVE"
    game_time: int = 0
    outcome: int | None = None  # 1 / 2 / 0 once settled


@dataclass
class SimOrder:
    id: str
    maker: str
    market_hash: str
    outcome_one: bool
    odds: int
    total: int
    remaining: int
    expiry: int
    tif: str
    client_order_id: str | None
    created_at: int
    post_only: bool = False
    status: str = "PENDING"
    inactive_reason: str | None = None
    inactive_at: int | None = None
    visible_from: int = 0
    seq: int = 0
    versions: list = field(default_factory=list)   # [(ts, row)] for read lag
    pending_cancel_at: int | None = None


@dataclass
class SimFill:
    id: str
    match_id: str
    order_id: str
    trade_id: str
    market_hash: str
    stake: int
    odds: int
    ret: int
    outcome_one: bool
    is_maker: bool
    created_at: int
    visible_from: int
    status: str = "MATCHED"
    settlement: dict | None = None
    updated_at: int = 0


class SimExchange:
    """In-memory SIMULATED exchange for ONE account (the throwaway EOA of the test). A `RestSender`."""

    def __init__(self, *, clock: Callable[[], float], account: str, meta: dict, fees: dict,
                 balance_base_units: int, purge_after_seconds: int = 900):
        self.clock = clock
        self.account = account.lower()
        self.meta = meta
        self.fees = dict(fees)
        self.escrow = meta["activeAsset"]["escrowAddress"].lower()
        self.token = meta["activeAsset"]["baseToken"].lower()
        self.step = meta["oddsLadderStepSize"] * 10 ** 15
        self.min_order = int(meta["limits"]["orderSizeMinimumBaseUnits"])
        self.max_create = meta["limits"]["maxCreateOrders"]
        self.max_cancel = meta["limits"]["maxCancelOrders"]
        self.purge_after = purge_after_seconds
        self.available = balance_base_units
        self.escrowed = 0
        self.markets: dict[str, SimMarket] = {}
        self.orders: dict[str, SimOrder] = {}
        self.fills: list[SimFill] = []
        self.requests: list[RestRequest] = []
        self.accepted: list[str] = []        # order ids accepted by POST /orders-v3 (exactly-once checks)
        self.faults: list[dict] = []
        self.read_lag = 0
        self.fill_lag = 0
        self.heartbeat: tuple[int, int] | None = None   # (last_beat_at, timeout_seconds)
        self.lock = threading.RLock()
        self._n = 0

    # Cross-process handoff (tests only, e.g. a "spawn" child): the in-memory world is copied, the lock is not.
    def __getstate__(self):
        with self.lock:
            state = dict(self.__dict__)
        del state["lock"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.lock = threading.RLock()

    # ------------------------------------------------------------ setup helpers (the "world", not the API)
    def add_market(self, market_hash: str, event_id: str, one: str, two: str, game_time: int = 0) -> SimMarket:
        m = SimMarket(market_hash.lower(), event_id, one, two, game_time=game_time or int(self.clock()) + 86_400)
        self.markets[m.market_hash] = m
        return m

    def add_liquidity(self, market_hash: str, *, outcome_one: bool, maker_odds: int, size: int,
                      expiry: int | None = None) -> str:
        """A third-party MAKER order resting on `outcome_one` at maker price `maker_odds`."""
        with self.lock:
            oid = "0x" + hashlib.sha256(f"liq:{self._next()}".encode()).hexdigest()
            now = int(self.clock())
            o = SimOrder(oid, LIQUIDITY_MAKER, market_hash.lower(), outcome_one, maker_odds, size, size,
                         expiry or now + 10 ** 6, "GTC", None, now, status="ACTIVE", seq=self._next())
            self.orders[oid] = o
            return oid

    def clear_liquidity(self, market_hash: str) -> None:
        with self.lock:
            for o in self.orders.values():
                if o.maker == LIQUIDITY_MAKER and o.market_hash == market_hash.lower() and o.status == "ACTIVE":
                    self._inactivate(o, "USER_REQUESTED")

    def inject(self, method: str, path: str, kind: str, **params: Any) -> None:
        """One-shot fault for the next request with this method and path prefix. kind: timeout_before,
        timeout_after, http500_before, http500_after, http429, crash_before, crash_after, get_error,
        fill_before_cancel(taker_stake), unconfirmed(land_after), async_cancel(land_after), or "none" (only
        params). POST /orders-v3 also takes visibility_delay=<seconds> with any kind."""
        self.faults.append({"method": method, "path": path, "kind": kind, **params})

    def advance(self, seconds: int) -> None:
        self.clock.t += seconds
        with self.lock:
            self._tick()

    def take(self, market_hash: str, *, outcome_one: bool, taker_stake: int, max_taker_odds: int = ODDS - 1) -> int:
        """A third-party TAKER backs `outcome_one` (consumes makers on the other outcome). Returns stake taken."""
        with self.lock:
            self._tick()
            now = int(self.clock())
            t = SimOrder("0x" + hashlib.sha256(f"taker:{self._next()}".encode()).hexdigest(), LIQUIDITY_MAKER,
                         market_hash.lower(), outcome_one, max_taker_odds, taker_stake, taker_stake, now + 60, "IOC",
                         None, now, status="ACTIVE", seq=self._next())
            self._match(t)
            return taker_stake - t.remaining

    def fill_maker_order(self, order_id: str, maker_stake: int) -> None:
        """A third-party taker consumes exactly `maker_stake` of our resting order (taker stake derived)."""
        with self.lock:
            o = self.orders[order_id.lower()]
            q = ODDS - o.odds
            taker = maker_stake * q // o.odds
            assert taker * o.odds == maker_stake * q, "choose a maker_stake that converts exactly"
            got = self.take(o.market_hash, outcome_one=not o.outcome_one, taker_stake=taker, max_taker_odds=q)
            assert got == taker

    def late_fill(self, order_id: str, maker_stake: int) -> None:
        """SIMULATED inconsistency: the engine reports a match of our maker order AFTER its INACTIVE row was
        already served (e.g. a stale index). Whether SX Bet can do this is DOCS_UNVERIFIED; the signer must never
        drop such a fill. A fill row is added and the order row's remainingSize drops; the status is unchanged."""
        with self.lock:
            o = self.orders[order_id.lower()]
            assert 0 < maker_stake <= o.remaining and o.maker == self.account
            now = int(self.clock())
            match_id = "0x" + hashlib.sha256(f"late:{self._next()}".encode()).hexdigest()
            o.remaining -= maker_stake
            self.available -= maker_stake
            self.escrowed += maker_stake
            self.fills.append(SimFill(f"{match_id}:{o.id}:{o.market_hash}", match_id, o.id,
                                      "0x" + hashlib.sha256(f"trade:{match_id}".encode()).hexdigest(), o.market_hash,
                                      maker_stake, o.odds, maker_stake * ODDS // o.odds, o.outcome_one, True, now,
                                      now + self.fill_lag, updated_at=now))
            self._snapshot(o)

    def close_market(self, market_hash: str) -> None:
        """Kickoff / suspension: market INACTIVE, resting orders swept (EVENT_LIFECYCLE)."""
        with self.lock:
            m = self.markets[market_hash.lower()]
            m.status = "INACTIVE"
            for o in self.orders.values():
                if o.market_hash == m.market_hash and o.status in ("PENDING", "ACTIVE"):
                    self._inactivate(o, "EVENT_LIFECYCLE")

    def lock_fills(self, market_hash: str) -> None:
        with self.lock:
            for f in self.fills:
                if f.market_hash == market_hash.lower() and f.status == "MATCHED":
                    f.status, f.updated_at = "LOCKED", int(self.clock())

    def settle(self, market_hash: str, outcome: int) -> None:
        """Settlement outcome 1 / 2 / 0 (void). Payout fee on profit, maker or taker rate of the fill."""
        assert outcome in (0, 1, 2)
        with self.lock:
            m = self.markets[market_hash.lower()]
            if m.status == "ACTIVE":
                self.close_market(market_hash)
            m.status, m.outcome = "SETTLED", outcome
            now = int(self.clock())
            for f in self.fills:
                if f.market_hash != m.market_hash or f.status not in ("MATCHED", "LOCKED"):
                    continue
                won = outcome != 0 and (outcome == 1) == f.outcome_one
                fee = 0
                if outcome == 0:
                    back = f.stake
                elif won:
                    rate = self.fees["makerPayoutFee" if f.is_maker else "takerPayoutFee"]
                    fee = int((Decimal(f.ret - f.stake) * Decimal(rate or "0")).to_integral_value(ROUND_FLOOR))
                    back = f.ret - fee
                else:
                    back = 0
                f.status, f.updated_at = "SETTLED", now
                f.settlement = {"outcome": outcome, "settleReturnAmount": str(back), "settleFeeAmount": str(fee),
                                "settledAt": iso(now)}
                if self._ours(f.order_id):
                    self.escrowed -= f.stake
                    self.available += back

    def accepted_count(self, client_order_id: str | None = None) -> int:
        if client_order_id is None:
            return len(self.accepted)
        return sum(1 for i in self.accepted if self.orders[i].client_order_id == client_order_id)

    def writes(self) -> list[RestRequest]:
        return [r for r in self.requests if r.method != "GET"]

    # ------------------------------------------------------------ RestSender
    def send(self, request: RestRequest) -> RestResponse:
        with self.lock:
            self.requests.append(request)
            self._tick()
            fault = self._fault(request)
            kind = fault["kind"] if fault else None
            if kind == "timeout_before":
                raise SenderTimeout()
            if kind == "crash_before":
                raise SimCrash("crash before the exchange processed " + request.path)
            if kind == "http429":
                return err(429, "Too Many Requests")
            if kind == "http500_before":
                return err(500, "Internal Server Error")
            if kind == "get_error":
                return err(503, "Service Unavailable")
            resp = self._route(request, fault or {})
            if kind == "timeout_after":
                raise SenderTimeout()
            if kind == "crash_after":
                raise SimCrash("crash after the exchange processed " + request.path)
            if kind == "http500_after":
                return err(500, "Internal Server Error")
            return resp

    def _fault(self, req: RestRequest) -> dict | None:
        for i, f in enumerate(self.faults):
            if f["method"] == req.method and req.path.startswith(f["path"]):
                return self.faults.pop(i)
        return None

    def _route(self, req: RestRequest, fault: dict) -> RestResponse:
        q = dict(req.query)
        if req.method == "POST" and req.path == "/orders-v3":
            return self._post_orders(req.body, fault)
        if req.method == "POST" and req.path == "/heartbeat/v3":
            return self._heartbeat(req.body)
        if req.method == "DELETE" and req.path == "/orders-v3":
            return self._cancel_ids(req.body, fault)
        if req.method == "DELETE" and req.path in ("/orders-v3/market", "/orders-v3/event", "/orders-v3/all"):
            return self._cancel_scope(req.path.rsplit("/", 1)[1], q, fault)
        if req.method == "GET":
            if req.path.startswith("/orders-v3/client/"):
                return self._lookup(self._by_client(req.path.rsplit("/", 1)[1]))
            if req.path == "/orders-v3/odds/best":
                return self._best(q.get("marketHashes", ""))
            if req.path.startswith("/orders-v3/"):
                return self._lookup(self.orders.get(req.path.rsplit("/", 1)[1].lower()))
            if req.path == "/orders-v3":
                return ok({"orders": [self._row(o) for o in self._mine() if o.status in ("PENDING", "ACTIVE")
                                      and self._visible(o)]})
            if req.path == "/fills-v3":
                return self._fills(q)
            if req.path == "/user/balance-v3":
                return ok({"balances": [{"tokenAddress": self.token, "escrowAddress": self.escrow,
                                         "availableAmount": str(self.available), "pendingAvailableAmount": "0",
                                         "escrowedAmount": str(self.escrowed), "pendingEscrowAmount": "0"}]})
            if req.path == "/user/fees-v3":
                return ok(dict(self.fees))
            if req.path == "/markets/find":
                hs = [h.lower() for h in q.get("marketHashes", "").split(",") if h]
                return ok([self._market_row(self.markets[h]) for h in hs if h in self.markets])
            if req.path == "/metadata/obv3":
                return ok(dict(self.meta))
            if req.path == "/markets/active":
                return self._markets_active(q)
            if req.path == "/orderbook-v3/snapshot/market":
                return self._book(q.get("marketHash", "").lower(), q.get("showTakerPerspective") == "true")
        return err(404, "Not Found")  # e.g. there is no cashout route

    # ------------------------------------------------------------ engine
    def _next(self) -> int:
        self._n += 1
        return self._n

    def _ours(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        return o is not None and o.maker == self.account

    def _mine(self) -> list[SimOrder]:
        return [o for o in self.orders.values() if o.maker == self.account]

    def _by_client(self, cid: str) -> SimOrder | None:
        rows = [o for o in self._mine() if o.client_order_id == cid and not self._purged(o)]
        return max(rows, key=lambda o: o.created_at) if rows else None

    def _purged(self, o: SimOrder) -> bool:
        return o.inactive_at is not None and int(self.clock()) >= o.inactive_at + self.purge_after

    def _visible(self, o: SimOrder) -> bool:
        return int(self.clock()) >= o.visible_from

    def _snapshot(self, o: SimOrder) -> None:
        if o.maker == self.account:
            o.versions.append((int(self.clock()), self._row_now(o)))

    def _row_now(self, o: SimOrder) -> dict:
        m = self.markets.get(o.market_hash)
        return {"id": o.id, "marketHash": o.market_hash, "maker": o.maker, "isBettingOutcomeOne": o.outcome_one,
                "percentageOdds": str(o.odds), "totalBetSize": str(o.total), "remainingSize": str(o.remaining),
                "status": o.status, "inactiveReason": o.inactive_reason, "clientOrderId": o.client_order_id,
                "eventId": m.event_id if m else None, "timeInForce": o.tif, "expiry": o.expiry,
                "createdAt": iso(o.created_at), "updatedAt": iso(int(self.clock()))}

    def _row(self, o: SimOrder) -> dict:
        """What a READ returns now (read lag: the latest version at least `read_lag` seconds old)."""
        cutoff = int(self.clock()) - self.read_lag
        rows = [r for ts, r in o.versions if ts <= cutoff]
        return dict(rows[-1]) if rows else None

    def _inactivate(self, o: SimOrder, reason: str) -> None:
        o.status, o.inactive_reason, o.inactive_at = "INACTIVE", reason, int(self.clock())
        self._snapshot(o)

    def _tick(self) -> None:
        now = int(self.clock())
        for o in list(self.orders.values()):
            if o.status == "PENDING" and now >= o.visible_from:
                self._activate(o)
            if o.status in ("PENDING", "ACTIVE") and o.pending_cancel_at is not None and now >= o.pending_cancel_at:
                o.pending_cancel_at = None
                self._inactivate(o, "USER_REQUESTED")
            if o.status in ("PENDING", "ACTIVE") and now >= o.expiry:
                self._inactivate(o, "EXPIRED")
        if self.heartbeat is not None and self.heartbeat[1] > 0 and now > self.heartbeat[0] + self.heartbeat[1]:
            for o in self._mine():
                if o.status in ("PENDING", "ACTIVE"):
                    self._inactivate(o, "HEARTBEAT_TIMEOUT")
            self.heartbeat = None

    def _activate(self, o: SimOrder) -> None:
        crosses = self._crossing(o)
        if o.post_only and crosses:
            self._inactivate(o, "POST_ONLY_WOULD_CROSS")
            return
        if o.tif == "FOK" and sum(c for _, c in crosses) < o.remaining:
            self._inactivate(o, "NO_LIQUIDITY")
            return
        o.status = "ACTIVE"
        self._match(o)
        if o.status != "ACTIVE":
            return
        if o.remaining == 0:
            self._inactivate(o, "FILLED")
        elif o.tif in ("IOC", "FOK"):
            self._inactivate(o, "NO_LIQUIDITY")
        else:
            self._snapshot(o)

    def _crossing(self, o: SimOrder) -> list[tuple[SimOrder, int]]:
        """Resting makers on the other outcome that cross `o` (taker price 1e20 - p <= o.odds), best first."""
        out = []
        for r in self.orders.values():
            if (r is o or r.market_hash != o.market_hash or r.outcome_one == o.outcome_one or r.status != "ACTIVE"
                    or r.remaining <= 0 or r.maker == o.maker or ODDS - r.odds > o.odds):
                continue
            out.append((r, r.remaining * (ODDS - r.odds) // r.odds))
        out.sort(key=lambda x: (ODDS - x[0].odds, x[0].seq))
        return out

    def _match(self, o: SimOrder) -> None:
        for r, cap in self._crossing(o):
            if o.remaining <= 0:
                break
            q = ODDS - r.odds
            s = min(o.remaining, cap)
            if s <= 0:
                continue
            mc = min(r.remaining, ceil_div(s * r.odds, q))
            if r.maker == self.account and mc > self.available:
                self._inactivate(r, "INSUFFICIENT_BALANCE")
                continue
            if o.maker == self.account and s > self.available:
                break
            match_id = "0x" + hashlib.sha256(f"match:{self._next()}".encode()).hexdigest()
            now = int(self.clock())
            for order, stake, odds, maker in ((o, s, q, False), (r, mc, r.odds, True)):
                order.remaining -= stake
                if order.maker == self.account:
                    self.available -= stake
                    self.escrowed += stake
                    self.fills.append(SimFill(f"{match_id}:{order.id}:{order.market_hash}", match_id, order.id,
                                              "0x" + hashlib.sha256(f"trade:{match_id}:{order.id}".encode()).hexdigest(),
                                              order.market_hash, stake, odds, stake * ODDS // odds, order.outcome_one,
                                              maker, now, now + self.fill_lag, updated_at=now))
            if r.remaining == 0:
                self._inactivate(r, "FILLED")
            else:
                self._snapshot(r)

    # ------------------------------------------------------------ POST /orders-v3
    def _post_orders(self, body: Any, fault: dict) -> RestResponse:
        if not isinstance(body, dict) or not isinstance(body.get("orders"), list):
            return err(400, "BAD_REQUEST")
        items = body["orders"]
        if not 1 <= len(items) <= self.max_create:
            return err(400, "BATCH_SIZE_INVALID")
        if len({i.get("maker", "").lower() for i in items}) != 1:
            return err(400, "BATCH_MIXED_MAKERS")
        now = int(self.clock())
        live_cids = {o.client_order_id for o in self._mine() if o.client_order_id and not self._purged(o)}
        cids = [i.get("clientOrderId") for i in items if i.get("clientOrderId")]
        if len(set(cids)) != len(cids) or live_cids & set(cids):
            return err(400, "DUPLICATE_CLIENT_ORDER_ID")  # "one collision fails the whole batch"
        parsed = []
        for it in items:
            o, why = self._parse(it, now)
            if why:
                return err(400, why)
            if o.id in self.orders:
                return err(400, "ORDER_ALREADY_EXISTS")
            parsed.append(o)
        rows = []
        for o in parsed:  # non-atomic: each order on its own
            cmd = "cmd-" + hashlib.sha256(f"{o.id}:{self._next()}".encode()).hexdigest()[:16]
            m = self.markets.get(o.market_hash)
            fail = None
            if m is None or m.status != "ACTIVE":
                fail = "INVALID_MARKET"
            elif o.expiry <= now + 2:
                fail = "EXPIRY_TOO_SOON"
            else:
                resting = sum(x.remaining for x in self._mine() if x.status in ("PENDING", "ACTIVE")
                              and x.market_hash == o.market_hash and x.outcome_one == o.outcome_one)
                if resting + o.total > self.available:
                    fail = "INSUFFICIENT_BALANCE"
            row = {"orderId": o.id, "status": "FAILED" if fail else "SUBMITTED", "commandId": cmd}
            if o.client_order_id:
                row["clientOrderId"] = o.client_order_id
            if fail:
                row["error"] = fail
                rows.append(row)
                continue
            o.visible_from = now + int(fault.get("visibility_delay", 0))  # delayed visibility (any fault kind)
            o.seq = self._next()
            self.orders[o.id] = o
            self.accepted.append(o.id)
            if now >= o.visible_from:
                self._activate(o)
            if body.get("waitForOutcome") is True:
                row["outcome"] = self._outcome(o)
            rows.append(row)
        return ok({"orders": rows})

    def _parse(self, it: dict, now: int) -> tuple[SimOrder | None, str | None]:
        try:
            salt = int(it["salt"], 16) if str(it["salt"]).startswith("0x") else int(it["salt"])
            intent = {"domain_name": self.meta["domain"]["name"], "domain_version": self.meta["domain"]["version"],
                      "chain_id": self.meta["chainId"], "verifying_contract": self.meta["domain"]["verifyingContract"],
                      "market_hash": it["marketHash"], "base_token": it["baseToken"],
                      "total_bet_size": it["totalBetSize"], "percentage_odds": it["percentageOdds"],
                      "salt": str(salt), "expiry": it["expiry"], "maker": it["maker"],
                      "is_maker_betting_outcome_one": it["isMakerBettingOutcomeOne"]}
            msg = eip712.signable(intent)
            oid = eip712.order_id(msg)
            signer = Account.recover_message(msg, signature=it["orderSignature"]).lower()
        except Exception:
            return None, "BAD_SIGNATURE"
        if signer != it["maker"].lower() or signer != self.account:
            return None, "BAD_SIGNATURE"
        if it["baseToken"].lower() != self.token:
            return None, "INVALID_BASE_TOKEN"
        odds, size = int(it["percentageOdds"]), int(it["totalBetSize"])
        if not 0 < odds < ODDS or odds % self.step:
            return None, "ODDS_NOT_ON_LADDER"
        if size < self.min_order:
            return None, "TOTAL_BET_SIZE_TOO_LOW"
        if it.get("postOnly") and it["timeInForce"] != "GTC":
            return None, "POST_ONLY_REQUIRES_GTC"
        return SimOrder(oid, signer, it["marketHash"].lower(), it["isMakerBettingOutcomeOne"], odds, size, size,
                        it["expiry"], it["timeInForce"], it.get("clientOrderId"), now,
                        post_only=bool(it.get("postOnly"))), None

    def _outcome(self, o: SimOrder) -> dict:
        filled = o.total - o.remaining
        if o.status == "PENDING":
            return {"state": "TIMEOUT"}
        if o.status == "ACTIVE":
            return {"state": "PARTIAL_FILL_RESTED" if filled else "RESTED", "fillAmount": str(filled)}
        if o.inactive_reason == "FILLED":
            return {"state": "FULLY_FILLED", "fillAmount": str(filled)}
        if filled:
            return {"state": "PARTIAL_FILL_DONE", "fillAmount": str(filled)}
        return {"state": "CANCELLED", "fillAmount": "0",
                "cancelReason": o.inactive_reason if o.inactive_reason in ("NO_LIQUIDITY", "POST_ONLY_WOULD_CROSS",
                                                                          "INSUFFICIENT_BALANCE") else "EXPIRED"}

    # ------------------------------------------------------------ cancels / heartbeat
    def _cancel_one(self, o: SimOrder, fault: dict) -> str:
        if fault.get("kind") == "fill_before_cancel":
            self.take(o.market_hash, outcome_one=not o.outcome_one, taker_stake=fault["taker_stake"],
                      max_taker_odds=ODDS - o.odds)
        if o.status not in ("PENDING", "ACTIVE"):
            return "NOT_ACTIVE"
        if fault.get("kind") == "unconfirmed":
            delay = fault.get("land_after")
            o.pending_cancel_at = None if delay is None else int(self.clock()) + delay
            return "UNCONFIRMED"
        self._inactivate(o, "USER_REQUESTED")
        return "CANCELLED"

    def _cancel_ids(self, body: Any, fault: dict) -> RestResponse:
        ids = [x.get("orderId", "").lower() for x in (body or {}).get("orders", [])]
        if not 1 <= len(ids) <= self.max_cancel:
            return err(400, "CANCEL_BATCH_SIZE_INVALID")
        out = {"cancelled": [], "notCancelled": [], "unconfirmed": []}
        for oid in ids:
            o = self.orders.get(oid)
            if o is None or o.maker != self.account or self._purged(o):
                out["notCancelled"].append({"orderId": oid, "reason": "NOT_FOUND"})
                continue
            if o.pending_cancel_at is not None:
                out["notCancelled"].append({"orderId": oid, "reason": "CANCEL_PENDING"})
                continue
            r = self._cancel_one(o, fault)
            cmd = "cancel-" + oid[2:10]
            if r == "CANCELLED":
                out["cancelled"].append({"orderId": oid, "commandId": cmd})
            elif r == "UNCONFIRMED":
                out["unconfirmed"].append({"orderId": oid, "commandId": cmd})
            else:
                out["notCancelled"].append({"orderId": oid, "reason": r})
        return ok(out)

    def _cancel_scope(self, scope: str, q: dict, fault: dict) -> RestResponse:
        delay = fault.get("land_after") if fault.get("kind") == "async_cancel" else 0
        out = []
        for o in self._mine():
            if o.status not in ("PENDING", "ACTIVE"):
                continue
            m = self.markets.get(o.market_hash)
            if scope == "market" and o.market_hash != q.get("marketHash", "").lower():
                continue
            if scope == "event" and (m is None or m.event_id != q.get("eventId")):
                continue
            out.append({"orderId": o.id, "commandId": "cancel-" + o.id[2:10]})
            if delay:
                o.pending_cancel_at = int(self.clock()) + delay
            else:
                self._inactivate(o, "USER_REQUESTED")
        return ok({"cancelledSubmitted": out})

    def _heartbeat(self, body: Any) -> RestResponse:
        t = (body or {}).get("timeoutSeconds")
        if type(t) is not int or not 0 <= t <= 3600:
            return err(400, "INVALID_TIMEOUT")
        self.heartbeat = None if t == 0 else (int(self.clock()), t)
        return ok({"timeoutSeconds": t})

    # ------------------------------------------------------------ reads
    def _lookup(self, o: SimOrder | None) -> RestResponse:
        if o is None or o.maker != self.account or not self._visible(o) or self._purged(o):
            return err(404, "Order not found")
        row = self._row(o)
        if row is None:
            return err(404, "Order not found")
        return ok({"order": row})

    def _fill_row(self, f: SimFill) -> dict:
        return {"id": f.id, "matchId": f.match_id, "orderId": f.order_id, "tradeId": f.trade_id,
                "marketHash": f.market_hash, "quarterlineMarketHash": None, "isParlay": False,
                "fillAmount": str(f.stake), "fillOdds": str(f.odds), "returnAmount": str(f.ret),
                "netReturnAmount": None, "isBettingOutcomeOne": f.outcome_one, "isMaker": f.is_maker,
                "status": f.status, "ceRefundAmount": "0", "ceRefundFeeAmount": "0",
                "createdAt": iso(f.created_at), "updatedAt": iso(f.updated_at),
                "settlement": None if f.settlement is None else dict(f.settlement)}

    def _fills(self, q: dict) -> RestResponse:
        now = int(self.clock())
        rows = [f for f in self.fills if now >= f.visible_from and self._ours(f.order_id)
                and (not q.get("orderId") or f.order_id == q["orderId"].lower())]
        per = int(q.get("perPage", "100"))
        start = int(q.get("nextKey", "0"))
        data: dict[str, Any] = {"fills": [self._fill_row(f) for f in rows[start:start + per]]}
        if start + per < len(rows):
            data["nextKey"] = str(start + per)  # absent -- not null -- on the last page
        return ok(data)

    def _markets_active(self, q: dict) -> RestResponse:
        """GET /markets/active: ACTIVE rows, eventId / sportIds filters, pageSize + paginationKey -> nextKey.
        The final nextKey points at an empty page, as the contract allows."""
        rows = [self._market_row(m) for m in self.markets.values() if m.status == "ACTIVE"]
        if q.get("eventId"):
            rows = [r for r in rows if r["sportXeventId"] == q["eventId"]]
        if q.get("sportIds"):
            ids = {int(x) for x in q["sportIds"].split(",") if x}
            rows = [r for r in rows if r["sportId"] in ids]
        rows.sort(key=lambda r: r["marketHash"])
        size = int(q.get("pageSize", 100))
        start = int(q.get("paginationKey", "0") or 0)
        page = rows[start:start + size]
        nxt = str(start + size) if start < len(rows) else None
        return ok({"markets": page, "nextKey": nxt})

    def _market_row(self, m: SimMarket) -> dict:
        # Moneyline-style row (type 52): the team names are the outcome names; sport / league ids are test values.
        return {"marketHash": m.market_hash, "status": m.status, "sportXeventId": m.event_id,
                "outcomeOneName": m.outcome_one_name, "outcomeTwoName": m.outcome_two_name,
                "teamOneName": m.outcome_one_name, "teamTwoName": m.outcome_two_name, "sportId": 5,
                "leagueId": getattr(m, "league_id", 1236), "gameTime": m.game_time, "type": 52}

    def _levels(self, market_hash: str, outcome_one: bool) -> list[tuple[int, int]]:
        acc: dict[int, int] = {}
        for o in self.orders.values():
            if o.market_hash == market_hash and o.outcome_one == outcome_one and o.status == "ACTIVE" and o.remaining:
                acc[o.odds] = acc.get(o.odds, 0) + o.remaining
        return sorted(acc.items(), key=lambda x: -x[0])

    def _book(self, market_hash: str, taker: bool) -> RestResponse:
        one, two = self._levels(market_hash, True), self._levels(market_hash, False)
        if not taker:
            return ok({"marketHash": market_hash, "outcomeOne": [{"percentageOdds": str(p), "size": str(s)}
                                                                 for p, s in one],
                       "outcomeTwo": [{"percentageOdds": str(p), "size": str(s)} for p, s in two]})

        def flip(levels):  # makers on the OTHER outcome, seen as taker price + taker stake capacity
            return [{"percentageOdds": str(ODDS - p), "size": str(s * (ODDS - p) // p)} for p, s in levels]
        return ok({"marketHash": market_hash, "outcomeOne": flip(two), "outcomeTwo": flip(one)})

    def _best(self, hashes: str) -> RestResponse:
        out = []
        for h in [x.lower() for x in hashes.split(",") if x]:
            row = {"marketHash": h}
            for name, side in (("outcomeOne", True), ("outcomeTwo", False)):
                lv = self._levels(h, side)
                row[name] = {"percentageOdds": str(lv[0][0]), "size": str(lv[0][1])} if lv else None
            out.append(row)
        return ok({"bestOdds": out})
