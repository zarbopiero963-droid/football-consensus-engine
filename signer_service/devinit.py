"""`python -m signer_service dev-init <dir>`: throwaway DEV material only, nothing real.

Writes into <dir> (mode 0600 for secret files):
- mcp-secret.hex: a random DEV HMAC secret (the MCP side would hold the same value);
- market.json: SIMULATED testnet market data (fake addresses, fake labels);
- owner.pub: public key of a THROWAWAY owner Ed25519 key, used once to sign risk-config.json with tiny
  limits and then discarded (the private key is never written anywhere);
- risk-config.json: the owner-signed DEV risk config.
Prints only file paths and env variable names, never a secret value.
"""
from __future__ import annotations

import json
import os
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from signer import risk as rk

DEV_MARKET_HASH = "0x" + "5e" * 32
DEV_LIMITS = dict(max_stake_per_order_base_units=2_000_000, max_exposure_per_market_base_units=4_000_000,
                  max_exposure_per_event_base_units=4_000_000, max_total_exposure_base_units=4_000_000,
                  session_loss_limit_base_units=4_000_000, daily_loss_limit_base_units=4_000_000,
                  max_open_orders=3, slippage_tolerance_bps=50, min_percentage_odds=10 ** 18,
                  max_percentage_odds=90 * 10 ** 18, max_quote_age_seconds=30)


def simulated_market() -> dict:
    return {
        "metadata": {"network": "testnet", "chain_id": 79479957, "verifying_contract": "0x" + "0d" * 20,
                     "base_token": "0x" + "1b" * 20, "odds_ladder_step_size": 125},
        "markets": {DEV_MARKET_HASH: {
            "status": "ACTIVE", "sportXeventId": "L-SIMULATED-1", "eventLabel": "Squadra Simulata A v Squadra Simulata B",
            "marketLabel": "Moneyline (SIMULATED)", "outcomeOneName": "Squadra Simulata A",
            "outcomeTwoName": "Squadra Simulata B",
            "outcomeOne": {"best_percentage_odds": str(40 * 10 ** 18), "available_liquidity_base_units": "50000000"},
            "outcomeTwo": {"best_percentage_odds": str(55 * 10 ** 18), "available_liquidity_base_units": "50000000"}}},
        "balance_base_units": "10000000",
        "fees": {"makerPayoutFee": "0", "takerPayoutFee": "0.03", "refundFee": "0.01"},
    }


def _write(path: str, text: str, secret: bool = False) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if secret else 0o644)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)


def dev_init(directory: str) -> dict[str, str]:
    os.makedirs(directory, exist_ok=True)
    paths = {n: os.path.join(directory, n) for n in ("mcp-secret.hex", "market.json", "owner.pub",
                                                    "risk-config.json")}
    _write(paths["mcp-secret.hex"], secrets.token_hex(32), secret=True)
    _write(paths["market.json"], json.dumps(simulated_market(), indent=2))
    owner = Ed25519PrivateKey.generate()  # THROWAWAY: signs the dev risk config, then goes out of scope
    _write(paths["owner.pub"], owner.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
    sig = owner.sign(rk.risk_config_message(DEV_LIMITS)).hex()
    del owner
    _write(paths["risk-config.json"], json.dumps({"limits": DEV_LIMITS, "signature": sig}))
    return paths
