"""Shared offline helpers for the signer / risk / ledger / gateway tests.

Every key here is a freshly generated THROWAWAY key (eth_account.Account.create(),
Ed25519PrivateKey.generate()): never funded, never persisted, discarded after the
test. No network. Import only after pytest.importorskip("eth_account").
"""
from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account

from bridge.sxbet import simulation as sim  # test-only cross-check; signer never imports bridge
from signer import risk
from signer.approval import Approval, approval_message, build_binding
from signer.audit import AuditLog
from signer.core import KEY_ENV, Signer, SignerConfig
from signer.intent import intent_hash
from signer.orders import OrderLedger

ESCROW = "0x" + "0d" * 20
TOKEN = "0x" + "1b" * 20
H = "0x" + "ab" * 32
NOW = 1_800_000_000
SIGNER_ID = "signer-test-1"
ACCOUNT_ID = "sx-api-identity-test"
EVENT_ID = "L12003787"
LABELS = {"outcomeOneName": "Home FC", "outcomeTwoName": "Away FC"}
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
GENEROUS = dict(max_stake_per_order_base_units=10_000_000, max_exposure_per_market_base_units=20_000_000,
                max_exposure_per_event_base_units=20_000_000, max_total_exposure_base_units=20_000_000,
                session_loss_limit_base_units=50_000_000, daily_loss_limit_base_units=50_000_000,
                max_open_orders=10, slippage_tolerance_bps=100, min_percentage_odds=10 ** 17,
                max_percentage_odds=99 * 10 ** 18, max_quote_age_seconds=30)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def pub(k: Ed25519PrivateKey) -> bytes:
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def write_risk_config(path, owner, *, signed=True, signer_key=None, **overrides) -> str:
    limits = {**GENEROUS, **overrides}
    doc = {"limits": limits}
    if signed:
        doc["signature"] = (signer_key or owner).sign(risk.risk_config_message(limits)).hex()
    with open(path, "w", encoding="ascii") as fh:
        json.dump(doc, fh)
    return str(path)


def load_limits(tmp_path, owner, **overrides) -> risk.LoadedRiskConfig:
    p = write_risk_config(tmp_path / f"risk-{len(os.listdir(tmp_path))}.json", owner, **overrides)
    return risk.load_risk_config(p, owner.public_key())


def open_or_create_ledger(path, address, clock):
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


def approve(owner_key, intent, *, nonce=None, issued=NOW, ttl=60, signer_id=SIGNER_ID, ihash=None,
            binding=None, **binding_overrides):
    _n[0] += 1
    nonce = nonce or f"nonce-{_n[0]:016d}"
    if binding is None:
        label = LABELS["outcomeOneName" if intent["is_maker_betting_outcome_one"] else "outcomeTwoName"]
        binding = build_binding(intent, account_id=ACCOUNT_ID, event_id=EVENT_ID, selection_label=label,
                                expected_fees=FEES, max_exposure_base_units=int(intent["total_bet_size"]),
                                quoted_percentage_odds=40 * 10 ** 18, intent_hash=ihash or intent_hash(intent))
        binding.update(binding_overrides)
    msg = approval_message(binding=binding, approval_nonce=nonce, issued_at=issued, expires_at=issued + ttl,
                           signer_id=signer_id)
    return Approval(binding, nonce, issued, issued + ttl, signer_id, owner_key.sign(msg))


def context(intent=None, *, now=NOW, **overrides):
    side = 1 if intent is None or intent["is_maker_betting_outcome_one"] else 2
    ctx = {
        "market": {"marketHash": (intent or {}).get("market_hash", H), "status": "ACTIVE",
                   "sportXeventId": EVENT_ID, **LABELS},
        "market_observed_at": now,
        "quote": {"outcome": side, "best_percentage_odds": str(40 * 10 ** 18),
                  "available_liquidity_base_units": "100000000", "observed_at": now},
        "balance": {"available_base_units": "100000000", "observed_at": now},
        "fees": dict(FEES),
    }
    ctx.update(overrides)
    return ctx


def throwaway_account():
    acct = Account.create()  # fresh, never funded
    return acct, acct.key.hex()
