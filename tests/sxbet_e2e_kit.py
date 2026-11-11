"""SIMULATED end-to-end stack: intent -> owner command -> risk -> signer -> gateway -> SimExchange -> ledger.

TESTS ONLY. Every key is a fresh THROWAWAY key (Ed25519 owner key, never-funded EOA), every limit is a small
test limit in an owner-signed (test key) risk config v2, and the LIVE_TRADING flag is a TEST flag signed by the
test owner key, exactly as tests/test_sxbet_gateway.py does. The exchange is `SimExchange` (in memory): no
network, no API key, no production code path. Results are SIMULATED, not evidence of SX Bet behaviour.

Intentional change (§2.6 rewrite, #187): `command()` / `cancel_command()` build the owner's literal chat
command (`OwnerCommandAuthorization`) that replaced the per-order / per-cancel Ed25519 approvals.
"""
from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account

from bridge.sxbet import hedging as hg
from bridge.sxbet import positions as pm
from bridge.sxbet import simulation as bsim
from signer import cancellation as cx
from signer import gateway as gw
from signer import rest_adapter as ra
from signer import command as cmd
from signer.audit import AuditLog
from signer.core import KEY_ENV, Signer, SignerConfig
from signer.orders import OrderLedger
from sxbet_signer_kit import (ACCOUNT_ID, ESCROW, EVENT_ID, EXCHANGE_LIMITS, FEES, GENEROUS, H, META_RAW, NOW,
                              SIGNER_ID, TOKEN, Clock, command_text, load_limits, make_signer, pub)
from sxbet_sim_exchange import ODDS, SimExchange

P = 10 ** 18          # one implied-probability point
USDC = 1_000_000      # one USDC in base units (6 decimals)
LIMITS = ra.ExchangeLimits(max_create_orders=10, max_cancel_orders=100)
# Small TEST limits (owner-signed with the throwaway owner key).
# v2: the daily loss limit also counts the worst case of open orders, so it is above the total exposure here.
SMALL_LIMITS = dict(GENEROUS, max_stake_per_order_base_units=10 * USDC, max_exposure_per_market_base_units=20 * USDC,
                    max_exposure_per_event_base_units=25 * USDC, max_total_exposure_base_units=30 * USDC,
                    max_daily_loss_base_units=45 * USDC, max_open_orders=5, min_decimal_odds="1.11",
                    max_decimal_odds="10", max_quote_age_seconds=30, pregame_only=False)


def get(sender, path, **query):
    resp = sender.send(ra.RestRequest("GET", path, tuple((k, str(v)) for k, v in query.items()), None))
    assert resp.http_status == 200, (path, resp)
    return resp.body["data"]


class Stack:
    """One signer process + its gateway + the simulated exchange. `restart()` models a process restart."""

    def __init__(self, tmp_path, *, balance=50 * USDC, limits=None, options=None, purge_after_seconds=900,
                 seed_book=True, anchor=None):
        self.tmp = tmp_path
        self.clock = Clock()
        self.owner = Ed25519PrivateKey.generate()          # THROWAWAY owner approval key
        self.acct = Account.create()                       # THROWAWAY EOA, never funded
        self.key = self.acct.key.hex()
        self.maker = self.acct.address.lower()
        self.risk = load_limits(tmp_path, self.owner, **{**SMALL_LIMITS, **(limits or {})})
        self.sim = SimExchange(clock=self.clock, account=self.acct.address, meta=META_RAW, fees=FEES,
                               balance_base_units=balance, purge_after_seconds=purge_after_seconds)
        self.sim.add_market(H, EVENT_ID, "Home FC", "Away FC")
        if seed_book:
            # Third-party makers that do NOT cross a 40% GTC on outcome one: a taker backing outcome one pays
            # 1e20 - 59% = 41%, a taker backing outcome two pays 1e20 - 35% = 65%.
            self.sim.add_liquidity(H, outcome_one=False, maker_odds=59 * P, size=59 * USDC)
            self.sim.add_liquidity(H, outcome_one=True, maker_odds=35 * P, size=35 * USDC)
        self.options = options
        self.anchor = anchor          # optional AnchorStore (e.g. PostgresAnchorStore on a THROWAWAY test DB)
        self.owner_ack = None         # optional owner-signed LedgerHeadAck for the next restart
        self.metadata = bsim.parse_metadata(META_RAW, "mainnet")
        self._n = 0
        self._build()

    def _build(self):
        if self.anchor is None:
            self.signer = make_signer(self.tmp, (self.acct, self.key), self.owner, clock=self.clock,
                                      risk_config=self.risk, ledger_path=self.tmp / "ledger.jsonl",
                                      audit_path=self.tmp / "audit.jsonl")
        else:
            path = str(self.tmp / "ledger.jsonl")
            kw = dict(signer_id=SIGNER_ID, signer_address=self.acct.address, clock=self.clock, anchor=self.anchor)
            if os.path.exists(path):
                led = OrderLedger.open(path, owner_ack=self.owner_ack, owner_public_key=self.owner.public_key(), **kw)
            else:
                led = OrderLedger.create(path, **kw)
            self.owner_ack = None
            cfg = SignerConfig(signer_id=SIGNER_ID, network="mainnet", chain_id=4162, verifying_contract=ESCROW,
                               base_token=TOKEN, odds_ladder_step_size=125, account_id=ACCOUNT_ID)
            self.signer = Signer(cfg, owner_public_key=pub(self.owner), ledger=led,
                                 audit=AuditLog(str(self.tmp / "audit.jsonl")), risk_config=self.risk,
                                 env={KEY_ENV: self.key}, clock=self.clock)
        self.ledger = self.signer._ledger
        self.adapter = ra.OfficialRestAdapter(self.sim, limits=LIMITS, options=self.options)
        self.gw = gw.SubmissionGateway(self.flag(), owner_public_key=self.owner.public_key(), transport=self.adapter,
                                       ledger=self.ledger, audit=AuditLog(str(self.tmp / "gw-audit.jsonl")),
                                       odds_ladder_step_size=125, network="mainnet", clock=self.clock)
        self.cx = cx.CancellationService(self.gw, account_id=ACCOUNT_ID)

    # ------------------------------------------------------------ cross-process handoff (tests only)
    _PROCESS_LOCAL = ("signer", "ledger", "adapter", "gw", "cx")

    def handoff(self) -> dict:
        """Picklable copy of this stack for a "spawn" child process, WITHOUT the process-local objects
        (signer, ledger handle, gateway, adapter, cancellation service: the child rebuilds them with `_build()`,
        like a real separate signer process). The throwaway owner key travels as raw bytes. The ledger must be
        closed first; the Signer itself is deliberately not serialisable and is never sent."""
        state = {k: v for k, v in vars(self).items() if k not in self._PROCESS_LOCAL and k != "owner"}
        state["owner_raw"] = self.owner.private_bytes_raw()
        return state

    @classmethod
    def from_handoff(cls, state: dict) -> "Stack":
        """Rebuild a stack from `handoff()` in another process. Call `_build()` to start its signer."""
        s = cls.__new__(cls)
        state = dict(state)
        s.owner = Ed25519PrivateKey.from_private_bytes(state.pop("owner_raw"))
        vars(s).update(state)
        return s

    def flag(self):
        """TEST-ONLY owner-signed LIVE_TRADING flag (throwaway owner key, 1 h). Never a production path."""
        body = {"live_trading": "ENABLED", "signer_id": SIGNER_ID, "network": "mainnet",
                "issued_at": int(self.clock()) - 1, "expires_at": int(self.clock()) + 3600}
        body["signature"] = self.owner.sign(gw.live_flag_message(body)).hex()
        p = self.tmp / "flag.json"
        p.write_text(json.dumps(body))
        return gw.load_live_trading_flag(str(p), self.owner.public_key(), signer_id=SIGNER_ID, network="mainnet",
                                         now=int(self.clock()))

    def restart(self, owner_ack=None):
        """The signer process dies and starts again: ledger replayed from disk, in-memory caches lost."""
        self.ledger.close()
        self.owner_ack = owner_ack
        self._build()

    # ------------------------------------------------------------ reads (through the RestSender, GET only)
    def quote(self, outcome_one, limit_odds=ODDS - 1, market=H):
        book = get(self.sim, "/orderbook-v3/snapshot/market", marketHash=market, showTakerPerspective="true")
        levels = book["outcomeOne" if outcome_one else "outcomeTwo"]
        prices = [int(lv["percentageOdds"]) for lv in levels]
        liq = sum(int(lv["size"]) for lv in levels if int(lv["percentageOdds"]) <= limit_odds)
        return (min(prices) if prices else None), liq

    def context(self, intent):
        one = intent["is_maker_betting_outcome_one"]
        best, liq = self.quote(one, int(intent["percentage_odds"]), intent["market_hash"])
        now = int(self.clock())
        (market,) = get(self.sim, "/markets/find", marketHashes=intent["market_hash"])
        bal = get(self.sim, "/user/balance-v3")["balances"][0]
        rows = get(self.sim, "/markets/find", marketHashes=",".join(sorted(self.sim.markets)))
        return {"market": market, "market_observed_at": now,
                "event_markets": [r for r in rows if r["sportXeventId"] == market["sportXeventId"]],
                "quote": {"outcome": 1 if one else 2, "best_percentage_odds": str(best if best else 0),
                          "available_liquidity_base_units": str(liq), "observed_at": now},
                "balance": {"available_base_units": bal["availableAmount"], "observed_at": now},
                "fees": get(self.sim, "/user/fees-v3"),
                "exchange_limits": {**EXCHANGE_LIMITS, "observed_at": now}}

    # ------------------------------------------------------------ the owner-commanded order path
    def intent(self, key, *, outcome_one=True, stake=5 * USDC, odds=40 * P, tif="GTC", expiry=None, market=H):
        salt = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        return bsim.build_intent(network="mainnet", metadata=self.metadata, market_hash=market,
                                 betting_outcome_one=outcome_one, stake_base_units=stake,
                                 requested_percentage_odds=odds, time_in_force=tif,
                                 expiry=expiry or int(self.clock()) + 3600, idempotency_key=key,
                                 now=int(self.clock()), maker=self.acct.address, salt=salt)

    def _auth(self, text, prefix, command_id=None, received_at=None):
        self._n += 1
        return cmd.OwnerCommandAuthorization(
            command_id=command_id or f"{prefix}-{self._n:012d}", command_text=text,
            command_text_sha256=cmd.text_sha256(text),
            received_at=int(self.clock()) if received_at is None else received_at,
            client_ref_sha256=cmd.text_sha256("e2e-conversation"))

    def command(self, intent, ctx, *, command_id=None, received_at=None, text=None):
        """The OWNER's literal command in the trading chat for exactly this intent, as the MCP forwards it."""
        m = ctx["market"]
        label = m["outcomeOneName" if intent["is_maker_betting_outcome_one"] else "outcomeTwoName"]
        text = text or command_text(intent, label=label, teams=(m["teamOneName"], m["teamTwoName"]))
        return self._auth(text, "e2e-cmd", command_id, received_at)

    def sign(self, intent, command=None, ctx=None):
        ctx = ctx or self.context(intent)
        return self.signer.sign_order(intent, command or self.command(intent, ctx), ctx)

    def place(self, key, **kw):
        i = self.intent(key, **kw)
        signed = self.sign(i)
        return i, signed, self.gw.submit(signed, i)

    def state(self, key):
        return self.ledger.get(key).state

    def exposure(self):
        return self.ledger.exposure()

    # ------------------------------------------------------------ cancel commands (owner, per operation)
    def cancel_command(self, scope, *, order_ids=(), market_hash=None, event_id=None, command_id=None,
                       received_at=None):
        ref = f" (rif. c{self._n + 1})"
        if scope == cx.SCOPE_ORDER_IDS:
            text = ("Cancella l'ordine " + order_ids[0]) if len(order_ids) == 1 else \
                ("Cancella gli ordini " + ", ".join(order_ids))
        elif scope == cx.SCOPE_MARKET:
            text = f"Cancella gli ordini sul mercato {market_hash}"
        elif scope == cx.SCOPE_EVENT:
            text = f"Cancella gli ordini dell'evento {event_id}"
        else:
            text = "Cancella tutti gli ordini"
        return self._auth(text + ref, "e2e-cancel", command_id, received_at)

    # ------------------------------------------------------------ positions from the exchange reads
    def all_fills(self):
        rows, key = [], None
        while True:
            q = {"perPage": 100, **({"nextKey": key} if key else {})}
            data = get(self.sim, "/fills-v3", **q)
            rows += data["fills"]
            if "nextKey" not in data:
                return rows
            key = data["nextKey"]

    def rollup(self):
        return pm.build_positions(fills={"fills": self.all_fills()}, open_orders=get(self.sim, "/orders-v3"),
                                  balance=get(self.sim, "/user/balance-v3"), fees=get(self.sim, "/user/fees-v3"),
                                  best_odds=get(self.sim, "/orders-v3/odds/best", marketHashes=H),
                                  markets=get(self.sim, "/markets/find", marketHashes=H),
                                  base_token=self.sim.token, escrow=self.sim.escrow)

    def position(self, market=H):
        return next(m for m in self.rollup()["markets"] if m["market_hash"] == market)

    def book(self):
        return get(self.sim, "/orderbook-v3/snapshot/market", marketHash=H)

    def market_row(self):
        return get(self.sim, "/markets/find", marketHashes=H)[0]

    def set_book(self, levels):
        """Replace the third-party book: [(outcome_one, maker_odds, size), ...]."""
        self.sim.clear_liquidity(H)
        for one, p, size in levels:
            self.sim.add_liquidity(H, outcome_one=one, maker_odds=p, size=size)

    def snapshot(self, market=H, *, max_slippage=2 * P, mode="FULL"):
        """What a market_snapshot read function returns: position (with mark-to-market), market, taker prices,
        and a hedge SIMULATION (bridge/sxbet/hedging.py) on the live simulated book."""
        pos = self.position(market)
        best = get(self.sim, "/orders-v3/odds/best", marketHashes=market)["bestOdds"][0]
        prices = {"outcome_one": None if best["outcomeTwo"] is None else ODDS - int(best["outcomeTwo"]["percentageOdds"]),
                  "outcome_two": None if best["outcomeOne"] is None else ODDS - int(best["outcomeOne"]["percentageOdds"])}
        row = get(self.sim, "/markets/find", marketHashes=market)[0]
        hedge = hg.simulate_hedge(market_position=pos, book=get(self.sim, "/orderbook-v3/snapshot/market",
                                                                 marketHash=market),
                                  market=row, fees=get(self.sim, "/user/fees-v3"), mode=mode,
                                  max_slippage_percentage_odds=max_slippage,
                                  own_resting_orders=get(self.sim, "/orders-v3")["orders"], data_age_seconds=0,
                                  max_data_age_seconds=10, odds_ladder_step_size=125,
                                  min_order_base_units=self.metadata["min_order_base_units"])
        return {"market_position": pos, "market": row, "data_age_seconds": 0, "taker_percentage_odds": prices,
                "hedge": hedge}

    def execute_proposal(self, params, key, *, tif="IOC"):
        """OWNER-COMMANDED execution of a proposal (SIMULATED): the proposal's exact parameters become an intent,
        the owner commands exactly those parameters, the signer runs every check. Ours, not a native SX Bet feature."""
        i = self.intent(key, outcome_one=params["betting_outcome_one"], stake=int(params["stake_base_units"]),
                        odds=int(params["percentage_odds_limit"]), tif=tif, market=params["market_hash"])
        sg = self.sign(i)
        r = self.gw.submit(sg, i)
        return i, sg, r


__all__ = ["ACCOUNT_ID", "EVENT_ID", "FEES", "H", "LIMITS", "NOW", "ODDS", "P", "SIGNER_ID", "SMALL_LIMITS", "Stack",
           "USDC", "get"]
