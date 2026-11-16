"""Signer status published for the MCP bridge (issue #187, P5; governance §2.6). Runs ONLY in the signer process.

After every worker cycle the signer publishes ONE row per signer_id: its network, the kill-switch state, whether
the owner-signed LIVE flag verifies now, and whether the last cycle was clean. The MCP reads it before queueing a
trading request and refuses early (kill switch engaged or unknown, LIVE not authorized, signer silent). This is a
courtesy check only: the signer re-verifies everything on its own when it processes a request.

The row is STATUS, not audit: it is overwritten in place (upsert); the audit stays in the append-only ledger and
request events. `updated_at` comes from the database clock, so the MCP measures staleness on the same clock.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

KILL_STATES = ("ENGAGED", "DISENGAGED", "UNKNOWN")
LIVE_STATES = ("AUTHORIZED", "NOT_AUTHORIZED")


class StatusError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def status_from_cycle(report: Mapping[str, Any], live_code: str | None) -> dict[str, Any]:
    """Map a worker cycle report and the gateway's live_status() to the published row (fail closed)."""
    engaged = report.get("kill_switch_engaged")
    kill = "ENGAGED" if engaged is True else "DISENGAGED" if engaged is False else "UNKNOWN"
    # The kill switch may have been engaged (or an engage attempted) at the END of this cycle (review 25d43a2 P3-2).
    if "kill_switch_engaged_now" in report:
        kill = "ENGAGED"
    elif "kill_switch_engage_error" in report:
        kill = "UNKNOWN"
    live_ok = live_code is None                       # only an explicit None means "verified now"
    return {"kill_switch": kill,
            "live_trading": "AUTHORIZED" if live_ok else "NOT_AUTHORIZED",
            "live_reason": None if live_ok else str(live_code)[:64],
            "last_cycle_ok": report.get("consecutive_failures") == 0}


class StatusPublisher:
    """Worker `status_sink`: builds the row from the cycle report and the gateway, then writes it."""

    def __init__(self, store: Any, *, signer_id: str, network: str, gateway: Any = None):
        if network not in ("mainnet", "testnet") or not isinstance(signer_id, str) or not signer_id:
            raise StatusError("SIGNER_STATUS_CONFIG_INVALID")
        self._store, self._signer_id, self._network, self._gateway = store, signer_id, network, gateway

    def __call__(self, report: Mapping[str, Any]) -> None:
        try:
            live_code = "LIVE_TRADING_DISABLED" if self._gateway is None else self._gateway.live_status()
        except Exception:
            live_code = "LIVE_STATUS_UNREADABLE"
        self._store.publish(self._signer_id, self._network, status_from_cycle(report, live_code))


class PostgresSignerStatus:
    """Signer role: INSERT/UPDATE on signer_status (its own row). The MCP role only SELECTs it (WP-009 grants)."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

    def _run(self, sql: str, params: Sequence[Any]) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
        finally:
            conn.close()

    def publish(self, signer_id: str, network: str, row: Mapping[str, Any]) -> None:
        if row["kill_switch"] not in KILL_STATES or row["live_trading"] not in LIVE_STATES:
            raise StatusError("SIGNER_STATUS_ROW_INVALID")
        self._run(
            "INSERT INTO signer_status (signer_id, network, kill_switch, live_trading, live_reason, last_cycle_ok, "
            "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (signer_id) DO UPDATE SET network = EXCLUDED.network, kill_switch = EXCLUDED.kill_switch, "
            "live_trading = EXCLUDED.live_trading, live_reason = EXCLUDED.live_reason, "
            "last_cycle_ok = EXCLUDED.last_cycle_ok, updated_at = EXCLUDED.updated_at",
            (signer_id, network, row["kill_switch"], row["live_trading"], row["live_reason"],
             bool(row["last_cycle_ok"])))


__all__ = ["KILL_STATES", "LIVE_STATES", "PostgresSignerStatus", "StatusError", "StatusPublisher",
           "status_from_cycle"]
