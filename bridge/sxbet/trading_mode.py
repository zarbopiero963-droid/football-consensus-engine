"""SX Bet MCP dual mode (issue #187, P5; governance §2.6).

READ_ONLY (default): read and simulation tools only. No order-request tool is registered; nothing is ever queued.
LIVE_TRADING: the owner order-request tools (place / cancel / status: bridge/sxbet/order_request_tools.py) are
registered. They still never sign or send: they queue ONE request for the isolated signer, which re-verifies the
owner command, the owner-signed limits, the kill switch and the owner-signed LIVE flag, and can refuse anything.

The mode is server configuration only (env SXBET_MCP_MODE, set by the owner); no tool argument can change it.
LIVE_TRADING needs a complete order-request configuration and is REFUSED on mainnet: the owner has not
authorized working trading capabilities on mainnet (decision of 16-11-2026). Lifting that is a reviewed code
change merged by the owner (MAINNET_LIVE_TRADING_AUTHORIZED), never a variable.

Before queueing, every trading request reads the signer's published status (signer/status.py) and is refused
when: the status is missing, ambiguous or stale (signer unreachable), the kill switch state is unknown, the LIVE flag does not verify, or the signer's last
cycle failed (PLACE only: a risk-reducing cancel is not blocked by a degraded cycle, e.g. after the automatic kill
switch). With the kill switch ENGAGED a CANCEL is allowed and a PLACE is QUEUED (never refused here): the signer
refuses it and voids its order for good (review 71e3625, owner decision 16-11-2026). The signer re-checks all.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

READ_ONLY = "READ_ONLY"
LIVE_TRADING = "LIVE_TRADING"
MODES = (READ_ONLY, LIVE_TRADING)
# Owner decision 16-11-2026: no working trading capability exposed on mainnet. Only a reviewed, owner-merged code
# change may set this to True (§2.6: merge reserved to the owner for the order authorization flow).
MAINNET_LIVE_TRADING_AUTHORIZED = False
DEFAULT_STATUS_MAX_AGE_SECONDS = 120
_AGE_BOUNDS = (30, 900)
# Owner decision 16-11-2026 (ratified): while the kill switch is ENGAGED, owner cancels of orders ALREADY OPEN are
# allowed (owner command verified by the signer, idempotency key, append-only audit, reconciliation by the monitor);
# every new placement is refused. The worker processes CANCEL requests only while ENGAGED (signer/worker.py).
CANCEL_ALLOWED_WHILE_KILL_SWITCH_ENGAGED = True


@dataclass(frozen=True)
class TradingMode:
    mode: str = READ_ONLY
    error: str | None = None
    status_max_age_seconds: int = DEFAULT_STATUS_MAX_AGE_SECONDS

    def __post_init__(self) -> None:
        # Also for a mode built in code (review 25d43a2 P3-6): no unknown mode, no out-of-bounds staleness.
        if self.mode not in MODES:
            raise ValueError("SXBET_MCP_MODE_INVALID")
        if type(self.status_max_age_seconds) is not int or \
                not _AGE_BOUNDS[0] <= self.status_max_age_seconds <= _AGE_BOUNDS[1]:
            raise ValueError("SXBET_SIGNER_STATUS_MAX_AGE_INVALID")

    @property
    def live(self) -> bool:
        return self.mode == LIVE_TRADING

    @classmethod
    def from_env(cls, network: str, order_requests_enabled: bool, order_requests_error: str | None = None,
                 env: Mapping[str, str] | None = None) -> "TradingMode":
        """Fail closed: anything but a complete, permitted LIVE_TRADING configuration is READ_ONLY + a fixed code."""
        e = os.environ if env is None else env
        raw = e.get("SXBET_MCP_MODE", "").strip().upper()
        if raw in ("", READ_ONLY):
            return cls()
        if raw != LIVE_TRADING:
            return cls(error="SXBET_MCP_MODE_INVALID")
        age_raw = e.get("SXBET_SIGNER_STATUS_MAX_AGE_SECONDS", "").strip()
        age = DEFAULT_STATUS_MAX_AGE_SECONDS
        if age_raw:
            if not (age_raw.isascii() and age_raw.isdigit()) or not _AGE_BOUNDS[0] <= int(age_raw) <= _AGE_BOUNDS[1]:
                return cls(error="SXBET_SIGNER_STATUS_MAX_AGE_INVALID")
            age = int(age_raw)
        return cls.live_for(network, order_requests_enabled, order_requests_error, status_max_age_seconds=age)

    @classmethod
    def live_for(cls, network: str, order_requests_enabled: bool, order_requests_error: str | None = None, *,
                 status_max_age_seconds: int = DEFAULT_STATUS_MAX_AGE_SECONDS) -> "TradingMode":
        if network not in ("mainnet", "testnet"):
            return cls(error="SXBET_NETWORK_INVALID")
        if network == "mainnet" and MAINNET_LIVE_TRADING_AUTHORIZED is not True:
            return cls(error="LIVE_TRADING_MAINNET_NOT_AUTHORIZED")
        if not order_requests_enabled:
            return cls(error=order_requests_error or "ORDER_REQUESTS_NOT_CONFIGURED")
        return cls(mode=LIVE_TRADING, status_max_age_seconds=status_max_age_seconds)

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "configuration_error": self.error,
                "trading_tools_registered": self.live,
                "mainnet_live_trading_authorized": MAINNET_LIVE_TRADING_AUTHORIZED,
                "set_by": "server configuration (owner); no tool argument can change it"}


def precheck(rows: Iterable[Mapping[str, Any]] | None, *, max_age_seconds: int, kind: str) -> str | None:
    """None = the signer looks ready to consider the request; otherwise a fixed refusal code (fail closed)."""
    if kind not in ("PLACE", "CANCEL"):
        return "REQUEST_KIND_INVALID"
    if rows is None:
        return "SIGNER_STATUS_UNAVAILABLE"
    rows = list(rows)
    if not rows:
        return "SIGNER_STATUS_MISSING"
    if len(rows) > 1:
        return "SIGNER_STATUS_AMBIGUOUS"
    r = rows[0]
    age = r.get("age_seconds")
    if type(age) not in (int, float) or not math.isfinite(age) or age < -5 or age > max_age_seconds:
        return "SIGNER_UNREACHABLE"
    kill = r.get("kill_switch")
    if kill != "DISENGAGED":
        if kill == "ENGAGED":
            if not (kind == "CANCEL" and CANCEL_ALLOWED_WHILE_KILL_SWITCH_ENGAGED):
                return "KILL_SWITCH_ENGAGED"
        else:
            return "KILL_SWITCH_STATE_UNKNOWN"
    if r.get("live_trading") != "AUTHORIZED":
        return "LIVE_TRADING_NOT_AUTHORIZED"
    if kind != "CANCEL" and r.get("last_cycle_ok") is not True:
        return "SIGNER_LAST_CYCLE_FAILED"
    return None


__all__ = ["CANCEL_ALLOWED_WHILE_KILL_SWITCH_ENGAGED", "DEFAULT_STATUS_MAX_AGE_SECONDS", "LIVE_TRADING",
           "MAINNET_LIVE_TRADING_AUTHORIZED", "MODES", "READ_ONLY", "TradingMode", "precheck"]
