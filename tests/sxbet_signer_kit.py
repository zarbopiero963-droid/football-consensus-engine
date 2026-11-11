"""Shared offline helpers for the signer / risk / ledger / gateway tests.

Every key here is a freshly generated THROWAWAY key (eth_account.Account.create(),
Ed25519PrivateKey.generate()): never funded, never persisted, discarded after the
test. No network. Import only after pytest.importorskip("eth_account").

Intentional change (owner-command authorization, §2.6 rewrite): orders and cancels are authorised by an
`OwnerCommandAuthorization` (the owner's literal chat command + a single-use id), built here by
`owner_command(intent)`, instead of a per-order Ed25519 `Approval`. Risk limits are the v2 schema.
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from fractions import Fraction

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account

from bridge.sxbet import simulation as sim  # test-only cross-check; signer never imports bridge
from signer import command as cmd
from signer import risk
from signer.audit import AuditLog
from signer.core import KEY_ENV, Signer, SignerConfig
from signer.orders import OrderLedger

ESCROW = "0x" + "0d" * 20
TOKEN = "0x" + "1b" * 20
H = "0x" + "ab" * 32
NOW = 1_800_000_000
SIGNER_ID = "signer-test-1"
ACCOUNT_ID = "sx-api-identity-test"
EVENT_ID = "L12003787"
LABELS = {"outcomeOneName": "Home FC", "outcomeTwoName": "Away FC"}
TEAMS = {"teamOneName": "Home FC", "teamTwoName": "Away FC"}
# Market-row fields the risk engine v2 / the command verification read (GET /markets/find). Moneyline-style
# row (type 52, no line); other sports / market types / lines are parametrised in the owner-command tests.
MARKET_EXTRA = {**TEAMS, "type": 52, "sportId": 5, "leagueId": 1236, "gameTime": NOW + 86_400}
FEES = {"refundFee": "0.01", "makerPayoutFee": "0", "takerPayoutFee": "0.03",
        "makerParlayPayoutFee": None, "takerParlayPayoutFee": "0.05"}
META_RAW = {
    "chainId": 4162,
    "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 4162, "verifyingContract": ESCROW},
    "activeAsset": {"symbol": "USDC", "baseToken": TOKEN, "escrowAddress": ESCROW, "decimals": 6},
    "oddsLadderStepSize": 125,
    "limits": {"orderSizeMinimumBaseUnits": "1000000", "minRestingOrderSizeBaseUnits": "100000",
               "maxCreateOrders": 10, "maxCancelOrders": 100},
    "bettingDelay": {"liveMsDefault": 8000, "pregameMsDefault": 0},
}
# Risk config v2 (one network block). Intentional change: v1 fields (session loss, slippage, percentage band)
# are gone; the v2 fields are owner decisions D1 (#187).
GENEROUS = dict(max_stake_per_order_base_units=10_000_000, max_stake_per_utc_day_base_units=1_000_000_000,
                max_exposure_per_market_base_units=20_000_000, max_exposure_per_event_base_units=20_000_000,
                max_total_exposure_base_units=20_000_000, max_daily_loss_base_units=50_000_000,
                max_open_orders=10, max_orders_per_hour=100, max_orders_per_utc_day=1000,
                min_seconds_between_orders=0, max_quote_age_seconds=30, command_max_age_seconds=120,
                min_decimal_odds="1.01", max_decimal_odds="1000", allowed_sport_ids="ANY",
                allowed_league_ids="ANY", allowed_market_types="ANY", pregame_only=True,
                market_time_in_force="FOK", limit_time_in_force="GTC")
# GET /metadata/obv3 `limits` + `activeAsset` as the risk context carries them (runtime metadata).
EXCHANGE_LIMITS = {"order_size_minimum_base_units": "1000000", "min_resting_order_size_base_units": "100000",
                   "base_token_symbol": "USDC", "base_token_decimals": 6}


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def pub(k: Ed25519PrivateKey) -> bytes:
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def write_risk_config(path, owner, *, signed=True, signer_key=None, networks=None, config_version=1,
                      **overrides) -> str:
    """Owner-signed (throwaway key) risk config v2. Default: the same limits on mainnet and testnet."""
    limits = {**GENEROUS, **overrides}
    nets = networks if networks is not None else {"mainnet": limits, "testnet": dict(limits)}
    body = {"schema": risk.RISK_CONFIG_SCHEMA, "config_version": config_version, "networks": nets}
    doc = dict(body)
    if signed:
        doc["signature"] = (signer_key or owner).sign(risk.risk_config_message(body)).hex()
    with open(path, "w", encoding="ascii") as fh:
        json.dump(doc, fh)
    return str(path)


def load_limits(tmp_path, owner, network="mainnet", **overrides) -> risk.LoadedRiskConfig:
    p = write_risk_config(tmp_path / f"risk-{len(os.listdir(tmp_path))}.json", owner, **overrides)
    return risk.load_risk_config(p, owner.public_key(), network=network)


_OPEN_LEDGERS: dict = {}


def open_or_create_ledger(path, address, clock):
    """Each call models a (re)started signer process. The ledger holds an exclusive file lock for its
    lifetime, so the previous instance on the same path (the "dead" process) is closed first."""
    prev = _OPEN_LEDGERS.pop(str(path), None)
    if prev is not None:
        prev.close()
    led = _open_or_create(path, address, clock)
    _OPEN_LEDGERS[str(path)] = led
    return led


def _open_or_create(path, address, clock):
    if os.path.exists(path):
        return OrderLedger.open(str(path), signer_id=SIGNER_ID, signer_address=address, clock=clock)
    return OrderLedger.create(str(path), signer_id=SIGNER_ID, signer_address=address, clock=clock)


def make_signer(tmp_path, throwaway, owner, *, limits=None, clock=None, ledger_path=None, audit_path=None,
                account_id=ACCOUNT_ID, risk_config=None, env_extra=None):
    acct, key = throwaway
    clock = clock or Clock()
    cfg = SignerConfig(signer_id=SIGNER_ID, network="mainnet", chain_id=4162, verifying_contract=ESCROW,
                       base_token=TOKEN, odds_ladder_step_size=125, account_id=account_id)
    if risk_config is None:
        risk_config = load_limits(tmp_path, owner, **(limits or {}))
    ledger = open_or_create_ledger(ledger_path or tmp_path / "ledger.jsonl", acct.address, clock)
    return Signer(cfg, owner_public_key=pub(owner), ledger=ledger,
                  audit=AuditLog(str(audit_path or tmp_path / "audit.jsonl")),
                  risk_config=risk_config, env={KEY_ENV: key, **(env_extra or {})}, clock=clock)


def make_intent(maker, **kw):
    args = dict(network="mainnet", metadata=sim.parse_metadata(META_RAW, "mainnet"), market_hash=H,
                betting_outcome_one=True, stake_base_units=5_000_000, requested_percentage_odds=40 * 10 ** 18,
                time_in_force="GTC", expiry=NOW + 3600, idempotency_key="order-key-0001", now=NOW,
                maker=maker, salt=0x8FA0CD4DBB399048CAF74B8455F0FCF502FC989FC3E2720003AA10756FAA70EF)
    args.update(kw)
    if "salt" not in kw and args["maker"] is not None:
        # Distinct salt per idempotency key: same params + same salt = same order id (refused by the ledger).
        args["salt"] = int(hashlib.sha256(args["idempotency_key"].encode()).hexdigest(), 16)
    return sim.build_intent(**args)


_n = [0]


def decimal_odds_for(p: int) -> str:
    """The shortest decimal odds text d with floor(1e20 / d) == p (what an owner command must say)."""
    for k in range(0, 41):
        d = Fraction(10 ** 20 * 10 ** k // p, 10 ** k)
        if d > 1 and (10 ** 20 * d.denominator) // d.numerator == p:
            return format(Decimal(d.numerator) / Decimal(d.denominator), "f").replace(".", ",")
    raise AssertionError("no finite decimal odds")


def stake_text(base_units: int, decimals: int = 6) -> str:
    t = format((Decimal(base_units) / Decimal(10 ** decimals)).normalize(), "f")
    if "." in t and len(t.split(".")[1]) == 3:
        t += "0"  # "1.125" would be ambiguous (thousands separator): "1.1250" is not
    return t


def command_text(intent, *, label=None, teams=("Home FC", "Away FC"), odds_text=None, tif=None, ref=True):
    """The owner's literal command for exactly this intent (IT grammar). A unique "(rif. N)" suffix keeps
    two otherwise identical commands distinct (the same text never authorises two operations)."""
    one = intent["is_maker_betting_outcome_one"]
    label = label or LABELS["outcomeOneName" if one else "outcomeTwoName"]
    odds = odds_text or decimal_odds_for(int(intent["requested_percentage_odds"]))
    stake = stake_text(int(intent["total_bet_size"]))
    event = f"{teams[0]}–{teams[1]}"
    _n[0] += 1
    suffix = f" (rif. {_n[0]})" if ref else ""
    if intent["time_in_force"] == "GTC":
        return f"Metti un Limit a quota {odds} per {stake} USDC su {label} in {event}{suffix}"
    return (f"Piazza {stake} USDC su {label} in {event} a quota minima {odds} "
            f"{tif or intent['time_in_force']}{suffix}")


def owner_command(intent=None, *, text=None, command_id=None, received_at=None, text_hash=None,
                  client_ref="conversation-test", **text_kw):
    """An OwnerCommandAuthorization as the MCP would forward it: a NEW single-use id, the literal text."""
    _n[0] += 1
    text = text if text is not None else command_text(intent, **text_kw)
    return cmd.OwnerCommandAuthorization(
        command_id=command_id or f"cmd-{_n[0]:016d}", command_text=text,
        command_text_sha256=text_hash or cmd.text_sha256(text),
        received_at=NOW if received_at is None else received_at,
        client_ref_sha256=cmd.text_sha256(client_ref))


def context(intent=None, *, now=NOW, **overrides):
    side = 1 if intent is None or intent["is_maker_betting_outcome_one"] else 2
    ctx = {
        "market": {"marketHash": (intent or {}).get("market_hash", H), "status": "ACTIVE",
                   "sportXeventId": EVENT_ID, **LABELS, **MARKET_EXTRA, "gameTime": now + 86_400},
        "market_observed_at": now,
        "quote": {"outcome": side, "best_percentage_odds": str(40 * 10 ** 18),
                  "available_liquidity_base_units": "100000000", "observed_at": now},
        "balance": {"available_base_units": "100000000", "observed_at": now},
        "fees": dict(FEES),
        "exchange_limits": {**EXCHANGE_LIMITS, "observed_at": now},
    }
    ctx.update(overrides)
    if "event_markets" not in overrides:
        # GET /markets/active?eventId=: here the event lists only this market (uniqueness, §2.3bis).
        ctx["event_markets"] = [ctx["market"]] if isinstance(ctx.get("market"), dict) else []
    return ctx


def throwaway_account():
    acct = Account.create()  # fresh, never funded
    return acct, acct.key.hex()
