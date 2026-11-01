"""Environment configuration of the dev signer service. Secrets come from files / env only, never logged.

Env (all SXBET_SIGNER_SERVICE_* unless noted):
  STATE_DIR (required)            SQLite stores, ledger, audit, enrolment code file
  HOST (default 127.0.0.1), PORT (default 8787)
  ALLOW_NON_LOOPBACK_BIND         must equal NON_LOOPBACK_ACK to bind anything but loopback
  MCP_KEY_ID, MCP_SECRET_FILE     HMAC key id + file with >= 32 bytes hex secret (dev: written by dev-init)
  OWNER_PUBLIC_KEY_HEX            owner Ed25519 public key (verifies the risk config; flag is never loaded)
  SXBET_SIGNER_RISK_CONFIG        owner-signed risk config path (missing => all-zero limits => all refused)
  MARKET_FILE                     simulated market data JSON (the only provider in this repository)
  SIGNER_ID, ACCOUNT_ID, REQUEST_TTL (<= 120), RP_ID (default localhost), ORIGINS (comma separated)
Refused at start: SXBET_SIGNER_PRIVATE_KEY or SXBET_LIVE_TRADING_FLAG present (see service.py).
"""
from __future__ import annotations

import ipaddress
import os
from typing import Mapping

from .auth import HmacKey
from .service import ServiceConfig, ServiceConfigError

P = "SXBET_SIGNER_SERVICE_"
NON_LOOPBACK_ACK = "I_UNDERSTAND_PRIVATE_NETWORK_ONLY"


def check_bind(host: str, env: Mapping[str, str]) -> None:
    """Loopback only, unless the explicit acknowledgement is set (private network such as a VPN only)."""
    if host == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and env.get(P + "ALLOW_NON_LOOPBACK_BIND") != NON_LOOPBACK_ACK:
        raise ServiceConfigError("SIGNER_SERVICE_NON_LOOPBACK_BIND_REFUSED")


def _read_secret(path: str | None) -> bytes:
    if not path:
        raise ServiceConfigError("AUTH_CONFIG_MISSING")
    try:
        with open(path, encoding="ascii") as fh:
            return bytes.fromhex(fh.read().strip())
    except (OSError, ValueError):
        raise ServiceConfigError("AUTH_CONFIG_INVALID") from None


def load(env: Mapping[str, str] | None = None) -> tuple[ServiceConfig, str, str, int]:
    env = os.environ if env is None else env
    state_dir = env.get(P + "STATE_DIR")
    if not state_dir:
        raise ServiceConfigError("SIGNER_SERVICE_STATE_DIR_MISSING")
    try:
        key = HmacKey(env.get(P + "MCP_KEY_ID", ""), _read_secret(env.get(P + "MCP_SECRET_FILE")))
        owner = bytes.fromhex(env.get(P + "OWNER_PUBLIC_KEY_HEX", ""))
        ttl = int(env.get(P + "REQUEST_TTL", "90"))
        port = int(env.get(P + "PORT", "8787"))
    except ValueError:
        raise ServiceConfigError("SIGNER_SERVICE_CONFIG_INVALID") from None
    market = env.get(P + "MARKET_FILE")
    if not market:
        raise ServiceConfigError("SIGNER_SERVICE_MARKET_FILE_MISSING")
    origins = tuple(o.strip() for o in env.get(P + "ORIGINS", f"http://localhost:{port}").split(",") if o.strip())
    cfg = ServiceConfig(state_dir=state_dir, owner_public_key=owner, hmac_keys=(key,),
                        signer_id=env.get(P + "SIGNER_ID", "signer-service-dev"),
                        account_id=env.get(P + "ACCOUNT_ID", ""), request_ttl_seconds=ttl,
                        rp_id=env.get(P + "RP_ID", "localhost"), origins=origins,
                        risk_config_path=env.get("SXBET_SIGNER_RISK_CONFIG"))
    host = env.get(P + "HOST", "127.0.0.1")
    check_bind(host, env)
    return cfg, market, host, port
