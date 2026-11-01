"""SX Bet live READ-ONLY certification (roadmap #130).

Runs ONLY when explicitly authorized (``--authorize-live-read``), normally from
the ``SX Bet Live Read-Only Certification`` workflow_dispatch job.

What it exercises, all against the REAL SX Bet mainnet API:
  * the 23 ``sxbet_*`` tools through the real MCP stack (Starlette app ->
    OAuth middleware -> MCP JSON-RPC -> tool -> host-pinned GET client). Only
    the OAuth token validator is stubbed, because a CI job has no user login;
  * one deliberately invalid key against an authenticated GET route (error
    handling, fixed codes);
  * the realtime WebSocket (Centrifugo) with a short-lived token fetched with
    ``GET /user/realtime-token-v3/api-key``: public channels and the account's
    own order channel, read-only subscriptions for a bounded time.

Never: POST/DELETE, order placement or cancellation, heartbeats, approvals,
transfers, signatures, private keys. The API key is read from the environment
only (never argv/URL), is never printed, and the report is leak-checked for the
key, the realtime token and the account address before it is written.
Account responses are reported as counts and field names only (no balances,
amounts or addresses).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

SOCCER_HINT = "soccer"
REST_ENDPOINT = "https://api.sx.bet"
WS_ENDPOINT = "wss://realtime.sx.bet/connection/websocket"
TOKEN_PATH = "/user/realtime-token-v3/api-key"
MAX_MCP_CALLS = 60
EXPECTED_SXBET_TOOLS = 36
PUBLIC_SAMPLE_KEYS = ("marketHash", "sportXeventId", "leagueLabel", "teamOneName", "teamTwoName",
                      "outcomeOneName", "outcomeTwoName", "type", "line", "gameTime", "status", "liveEnabled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def shape(value: Any) -> dict[str, Any]:
    """Non-sensitive description of an account payload: type, size and field names only."""
    if isinstance(value, list):
        first = value[0] if value and isinstance(value[0], dict) else None
        return {"type": "list", "items": len(value), "item_fields": sorted(first)[:40] if first else []}
    if isinstance(value, dict):
        out: dict[str, Any] = {"type": "object", "fields": sorted(value)[:40]}
        for k, v in value.items():
            if isinstance(v, list):
                out[f"{k}_items"] = len(v)
        return out
    return {"type": type(value).__name__}


def leak_free(text: str, secrets: list[str]) -> bool:
    """Case-insensitive, so a checksummed or lower-cased address spelling is caught too."""
    low = text.lower()
    return not any(s and len(s) >= 6 and (s in text or s.lower() in low) for s in secrets)


def is_empty_payload(data: Any) -> bool:
    """[] / {} or a page whose item lists are all empty (e.g. {"markets": [], "nextKey": None})."""
    if isinstance(data, (list, dict)) and not data:
        return True
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        return bool(lists) and all(not v for v in lists)
    return False


def jwt_sub(token: str) -> str | None:
    """Read the ``sub`` claim locally (no verification needed: only used to name our own channel)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        return sub if isinstance(sub, str) and sub.startswith("0x") and len(sub) == 42 else None
    except Exception:
        return None


def centrifugo_frames(raw: str | bytes) -> list[dict[str, Any]]:
    """Centrifugo JSON protocol may batch several newline-delimited replies in one frame."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, kind: str, outcome: str, **info: Any) -> None:
        self.rows.append({"test": name, "kind": kind, "outcome": outcome, **info})


def _mcp_harness(api_key: str):
    os.environ.update({"SXBET_ENABLED": "true", "SXBET_NETWORK": "mainnet", "SXBET_MAX_RPM": "30",
                       "SXBET_API_KEY": api_key})
    os.environ.pop("DATABASE_URL", None)
    import bridge.server as srv
    from starlette.testclient import TestClient

    async def noop(*a, **k):
        return None

    async def idle():
        await asyncio.Event().wait()

    async def stop_all():
        return {"failed": []}

    srv.bridge.prepare_cloak_runtime = noop
    srv.bridge.reaper_loop = idle
    srv.bridge.stop_all = stop_all
    srv.oauth_store.valid_access_token = lambda token, resource, scope: token == "CI_LOCAL_TEST_TOKEN"
    host = srv.public_host
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Host": host, "Authorization": "Bearer CI_LOCAL_TEST_TOKEN"}
    return srv, TestClient(srv.app, base_url="https://" + host), headers


secrets_extra: list[str] = []  # addresses seen at run time: never allowed in the report


def run_mcp(rec: Recorder, api_key: str) -> dict[str, Any]:
    srv, client, headers = _mcp_harness(api_key)
    calls = {"n": 0}
    ctx: dict[str, Any] = {}

    def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        r = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": calls["n"], "method": method, "params": params})
        body = r.json()
        return {"http": r.status_code, "body": body}

    def tool(name: str, **args: Any) -> dict[str, Any]:
        payload = _tool_once(name, **args)
        err = payload.get("error") or {}
        if err.get("code") == "SXBET_LOCAL_RATE_LIMITED":
            # Respect the bridge's own limiter: wait the window it reports, then retry once (no limit raised).
            wait = min(int(err.get("retry_after_seconds") or 61), 61)
            calls["paced_seconds"] = calls.get("paced_seconds", 0) + wait
            time.sleep(wait)
            payload = _tool_once(name, **args)
        return payload

    def _tool_once(name: str, **args: Any) -> dict[str, Any]:
        if calls["n"] >= MAX_MCP_CALLS:
            raise RuntimeError("MCP call budget exhausted")
        reply = rpc("tools/call", {"name": name, "arguments": args})
        result = reply["body"].get("result", {})
        payload = result.get("structuredContent")
        if payload is None:
            try:
                payload = json.loads(result["content"][0]["text"])
            except Exception:
                payload = {"status": "UNPARSEABLE"}
        payload["_mcp_http"] = reply["http"]
        payload["_mcp_is_error"] = bool(result.get("isError"))
        return payload

    exercised: set[str] = set()

    def row(name: str, out: dict[str, Any], *, expect: str = "REAL_API_RESPONSE",
            extra: dict[str, Any] | None = None, expect_not_found: bool = False,
            scope_sensitive: bool = False) -> dict[str, Any]:
        if isinstance(out.get("tool"), str):
            exercised.add(out["tool"])
        status = out.get("status")
        info = {"mcp_http": out.get("_mcp_http"), "status": status, "http_status": out.get("http_status"),
                "error_code": (out.get("error") or {}).get("code"), "network_calls_made": out.get("network_calls_made")}
        data = out.get("data")
        if status == "REAL_API_RESPONSE":
            info["data_shape"] = shape(data)
        code = (out.get("error") or {}).get("code")
        outcome = "PASS" if status == expect else ("FAIL" if status in ("PROVIDER_ERROR", "UNPARSEABLE", "INVALID_INPUT") else "PARTIAL")
        if expect_not_found and status == "PROVIDER_ERROR" and out.get("http_status") == 404:
            outcome = "PASS"  # documented: a missing or foreign id returns 404
        elif status == "PROVIDER_ERROR" and code == "SXBET_UNAUTHORIZED" and scope_sensitive:
            outcome = "BLOCKED_EXTERNAL"  # the key lacks the documented scope; owner decision, not a code failure
        if status == "REAL_API_RESPONSE" and is_empty_payload(data):
            outcome = "EMPTY_VALID"
        rec.add(name, "REAL_API_VIA_MCP" if out.get("network_calls_made") else "MCP_NO_NETWORK", outcome,
                **info, **(extra or {}))
        return out

    with client:
        listed = rpc("tools/list", {})
        tools = listed["body"]["result"]["tools"]
        sx = [t for t in tools if t["name"].startswith("sxbet_")]
        bad_schema = [t["name"] for t in sx if t.get("inputSchema", {}).get("type") != "object"]
        rec.add("mcp_tools_list", "MCP_PROTOCOL", "PASS" if len(sx) == EXPECTED_SXBET_TOOLS and not bad_schema else "FAIL",
                total_tools=len(tools), sxbet_tools=len(sx), schema_errors=bad_schema,
                has_next_cursor="nextCursor" in listed["body"]["result"])
        ctx["sx_tool_names"] = sorted(t["name"] for t in sx)

        health = tool("sxbet_health")
        exercised.add("sxbet_health")
        rec.add("sxbet_health", "MCP_NO_NETWORK",
                "PASS" if health.get("mode") == "READ_ONLY" and health.get("write_tools") == []
                and health.get("api_key_present") is bool(api_key) else "FAIL",
                mode=health.get("mode"), write_tools=health.get("write_tools"),
                api_key_present=health.get("api_key_present"), live_trading=str(health.get("live_trading", ""))[:11])
        sports = tool("sxbet_sports")
        row("sxbet_sports", sports)
        soccer = next((s for s in (sports.get("data") or []) if SOCCER_HINT in str(s.get("label", "")).lower()), None)
        sport_id = soccer.get("sportId") if soccer else 5
        ctx["soccer_sport_id"] = sport_id
        row("sxbet_leagues", tool("sxbet_leagues", sport_id=sport_id))

        p1 = row("sxbet_markets_active_prematch_p1", tool("sxbet_markets_active", sport_ids=[sport_id], only_main_line=True, page_size=20))
        m1 = (p1.get("data") or {}).get("markets", []) if isinstance(p1.get("data"), dict) else []
        if p1.get("next_key"):
            p2 = tool("sxbet_markets_active", sport_ids=[sport_id], only_main_line=True, page_size=20, pagination_key=p1["next_key"])
            m2 = (p2.get("data") or {}).get("markets", []) if isinstance(p2.get("data"), dict) else []
            overlap = len({m.get("marketHash") for m in m1} & {m.get("marketHash") for m in m2})
            row("sxbet_markets_active_prematch_p2", p2, extra={"overlap_with_p1": overlap})
        else:
            rec.add("sxbet_markets_active_prematch_p2", "REAL_API_VIA_MCP", "NOT_TESTED", reason="NO_NEXT_KEY")
        live = row("sxbet_markets_active_live", tool("sxbet_markets_active", sport_ids=[sport_id], live_only=True, page_size=20))
        ml = (live.get("data") or {}).get("markets", []) if isinstance(live.get("data"), dict) else []
        ctx["prematch_markets_sample"] = [{k: m.get(k) for k in PUBLIC_SAMPLE_KEYS} for m in m1[:5]]
        ctx["live_markets_sample"] = [{k: m.get(k) for k in PUBLIC_SAMPLE_KEYS} for m in ml[:5]]
        ctx["prematch_markets_page1"] = len(m1)
        ctx["live_markets_page1"] = len(ml)

        hashes = [m["marketHash"] for m in m1[:3] if isinstance(m.get("marketHash"), str)]
        live_hashes = [m["marketHash"] for m in ml[:2] if isinstance(m.get("marketHash"), str)]
        events = [m["sportXeventId"] for m in (m1[:2] + ml[:1]) if isinstance(m.get("sportXeventId"), str)]
        league_id = next((m.get("leagueId") for m in m1 if isinstance(m.get("leagueId"), int)), None)
        ctx["probe_market_hash"] = hashes[0] if hashes else None
        ctx["probe_event_id"] = events[0] if events else None

        def skipped(name: str, reason: str) -> None:
            rec.add(name, "REAL_API_VIA_MCP", "NOT_TESTED", reason=reason)

        if hashes:
            row("sxbet_markets_find", tool("sxbet_markets_find", market_hashes=hashes))
        else:
            skipped("sxbet_markets_find", "NO_ACTIVE_MARKET_AT_RUN_TIME")
        if league_id is not None:
            row("sxbet_fixtures_active", tool("sxbet_fixtures_active", league_id=league_id))
        else:
            skipped("sxbet_fixtures_active", "NO_LEAGUE_ID_AT_RUN_TIME")
        if events:
            row("sxbet_fixture_status", tool("sxbet_fixture_status", sportx_event_ids=events))
            row("sxbet_live_scores", tool("sxbet_live_scores", sportx_event_ids=events))
        else:
            skipped("sxbet_fixture_status", "NO_EVENT_AT_RUN_TIME")
            skipped("sxbet_live_scores", "NO_EVENT_AT_RUN_TIME")
        row("sxbet_metadata", tool("sxbet_metadata"))
        row("sxbet_markets_popular", tool("sxbet_markets_popular"))
        row("sxbet_search", tool("sxbet_search", query="Inter"))
        row("sxbet_teams", tool("sxbet_teams", sport_id=sport_id, per_page=5))
        row("sxbet_public_trades", tool("sxbet_public_trades", per_page=5))

        # negative input: must be rejected before any network call
        row("invalid_input_rejected_offline", tool("sxbet_markets_find", market_hashes=["0xabc"]), expect="INVALID_INPUT")

        # authenticated READ routes
        if not api_key:
            for name in ("sxbet_orderbook_market", "sxbet_orderbook_event", "sxbet_best_odds", "sxbet_balance",
                         "sxbet_fees", "sxbet_positions", "sxbet_open_orders", "sxbet_trades", "sxbet_fills", "sxbet_ledger",
                         "sxbet_order", "sxbet_order_by_client_id", "sxbet_trade", "sxbet_privacy",
                         "sxbet_public_market_orders", "sxbet_public_user_orders", "sxbet_public_positions",
                         "sxbet_proxy_wallet", "sxbet_proxy_deploy_pending", "sxbet_proxy_funding_pending",
                         "sxbet_proxy_funding_status", "sxbet_proxy_address_check", "sxbet_safe_info"):
                rec.add(name, "REAL_API_VIA_MCP", "NOT_TESTED", reason="SXBET_API_KEY_SECRET_ABSENT")
        else:
            if hashes:
                row("sxbet_orderbook_market_prematch", tool("sxbet_orderbook_market", market_hash=hashes[0]))
                row("sxbet_best_odds_prematch", tool("sxbet_best_odds", market_hashes=hashes))
            else:
                skipped("sxbet_orderbook_market_prematch", "NO_ACTIVE_MARKET_AT_RUN_TIME")
                skipped("sxbet_best_odds_prematch", "NO_ACTIVE_MARKET_AT_RUN_TIME")
            if live_hashes:
                row("sxbet_orderbook_market_live", tool("sxbet_orderbook_market", market_hash=live_hashes[0]))
                row("sxbet_best_odds_live", tool("sxbet_best_odds", market_hashes=live_hashes))
            else:
                skipped("sxbet_orderbook_market_live", "NO_LIVE_SOCCER_MARKET_AT_RUN_TIME")
                skipped("sxbet_best_odds_live", "NO_LIVE_SOCCER_MARKET_AT_RUN_TIME")
            if events:
                row("sxbet_orderbook_event", tool("sxbet_orderbook_event", event_id=events[0]))
            else:
                skipped("sxbet_orderbook_event", "NO_EVENT_AT_RUN_TIME")
            row("sxbet_balance", tool("sxbet_balance"))
            row("sxbet_fees", tool("sxbet_fees"))
            row("sxbet_positions_open", tool("sxbet_positions", status=["MATCHED", "LOCKED"], per_page=5))
            row("sxbet_open_orders", tool("sxbet_open_orders", per_page=5))
            start = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
            row("sxbet_trades", tool("sxbet_trades", start_date=start, per_page=5))
            row("sxbet_fills", tool("sxbet_fills", start_date=start, per_page=5))
            row("sxbet_ledger", tool("sxbet_ledger", per_page=5))
            # Remaining official GET routes. Ids are format-valid probes that cannot exist: 404 is the documented answer.
            probe_hash = "0x" + "0" * 63 + "1"
            row("sxbet_order_not_found", tool("sxbet_order", order_id=probe_hash), expect_not_found=True)
            row("sxbet_order_by_client_id_not_found", tool("sxbet_order_by_client_id", client_order_id="fce-cert-nonexistent"),
                expect_not_found=True)
            row("sxbet_trade_not_found", tool("sxbet_trade", trade_id="fce-cert-nonexistent"), expect_not_found=True)
            row("sxbet_privacy", tool("sxbet_privacy"), scope_sensitive=True)
            pmo = tool("sxbet_public_market_orders", market_hash=hashes[0], per_page=5) if hashes else None
            if pmo is not None:
                row("sxbet_public_market_orders", pmo)
            else:
                skipped("sxbet_public_market_orders", "NO_ACTIVE_MARKET_AT_RUN_TIME")
            orders = ((pmo or {}).get("data") or {}).get("orders") if isinstance((pmo or {}).get("data"), dict) else None
            maker = next((o.get("userAddress") for o in (orders or []) if isinstance(o, dict)
                          and isinstance(o.get("userAddress"), str)), None)
            if maker:
                secrets_extra.append(maker)
                row("sxbet_public_user_orders", tool("sxbet_public_user_orders", user_address=maker, per_page=5))
                row("sxbet_public_positions", tool("sxbet_public_positions", user_address=maker,
                                                   status=["MATCHED", "LOCKED"], per_page=5))
            else:
                skipped("sxbet_public_user_orders", "NO_DEANONYMIZED_MAKER_ON_PROBE_MARKET")
                skipped("sxbet_public_positions", "NO_DEANONYMIZED_MAKER_ON_PROBE_MARKET")
            proxy = row("sxbet_proxy_wallet", tool("sxbet_proxy_wallet"), scope_sensitive=True)
            row("sxbet_proxy_deploy_pending", tool("sxbet_proxy_deploy_pending"), scope_sensitive=True)
            row("sxbet_proxy_funding_pending", tool("sxbet_proxy_funding_pending"), scope_sensitive=True)
            skipped("sxbet_proxy_funding_status", "NO_DEPOSIT_SESSION_ID (no deposit was ever started by this suite)")
            proxy_data = proxy.get("data") if isinstance(proxy.get("data"), dict) else {}
            def _addr(key):
                v = proxy_data.get(key)
                return v if isinstance(v, str) and len(v) == 42 else None
            # Documented fields of GET /user/proxy: obv3ProxyWalletAddress (the proxy) and
            # multisigSafeAddress (the Safe attached to it, input of GET /user/multisig/{safeAddress}/info).
            proxy_address, safe_address = _addr("obv3ProxyWalletAddress"), _addr("multisigSafeAddress")
            for a in (proxy_address, safe_address):
                if a:
                    secrets_extra.append(a)
            if proxy_address:
                row("sxbet_proxy_address_check", tool("sxbet_proxy_address_check", proxy_address=proxy_address),
                    scope_sensitive=True)
            else:
                skipped("sxbet_proxy_address_check", "NO_obv3ProxyWalletAddress_IN_REPLY")
            if safe_address:
                row("sxbet_safe_info", tool("sxbet_safe_info", safe_address=safe_address), scope_sensitive=True,
                    extra={"proxy_deployed": proxy_data.get("deployed")})
            else:
                skipped("sxbet_safe_info", "NO_multisigSafeAddress_IN_REPLY")
        missing = sorted(set(ctx["sx_tool_names"]) - exercised)
        rec.add("tool_coverage", "MCP_PROTOCOL",
                "PASS" if not missing else ("PARTIAL" if api_key or exercised else "FAIL"),
                exercised=len(exercised), published=len(ctx["sx_tool_names"]), not_exercised=missing)
    ctx["mcp_calls"] = calls["n"]
    ctx["paced_seconds_for_local_rate_limit"] = calls.get("paced_seconds", 0)
    return ctx


async def invalid_key_probe(rec: Recorder) -> None:
    from bridge.sxbet.client import SXBetError, SXBetReadClient, Settings
    client = SXBetReadClient(Settings(enabled=True, api_key="00000000-0000-0000-0000-000000000000", max_rpm=2))
    try:
        await client.get("/user/balance-v3", {}, auth=True)
        rec.add("invalid_key_rejected", "REAL_API_DIRECT", "FAIL", reason="INVALID_KEY_ACCEPTED")
    except SXBetError as exc:
        ok = exc.code == "SXBET_UNAUTHORIZED"
        rec.add("invalid_key_rejected", "REAL_API_DIRECT", "PASS" if ok else "PARTIAL",
                error_code=exc.code, http_status=exc.http_status)


async def fetch_realtime_token(api_key: str) -> tuple[int | None, str | None]:
    import httpx
    async with httpx.AsyncClient(base_url=REST_ENDPOINT, timeout=15, follow_redirects=False) as client:
        r = await client.get(TOKEN_PATH, headers={"x-sx-api-key": api_key, "Accept": "application/json"})
        if r.status_code != 200:
            return r.status_code, None
        try:
            body = r.json()
        except ValueError:
            return r.status_code, None
        token = body.get("token") if isinstance(body, dict) else None
        if token is None and isinstance(body, dict) and isinstance(body.get("data"), dict):
            token = body["data"].get("token")
        return r.status_code, token if isinstance(token, str) else None


async def realtime_probe(rec: Recorder, api_key: str, market_hash: str | None, event_id: str | None,
                         seconds: float, secrets: list[str]) -> None:
    if not api_key:
        rec.add("realtime_websocket", "REAL_WEBSOCKET", "NOT_TESTED", reason="SXBET_API_KEY_SECRET_ABSENT")
        return
    async with asyncio.timeout(20):
        status, token = await fetch_realtime_token(api_key)
    rec.add("realtime_token", "REAL_API_DIRECT", "PASS" if token else "FAIL", http_status=status)
    if not token:
        return
    secrets.append(token)
    sub = jwt_sub(token)
    if sub:
        secrets.append(sub)
        secrets.append(sub.lower())
    import websockets
    channels = ["best_odds_v3:global", "markets:global", "fixtures:live_scores", "recent_trades_v3:global"]
    if market_hash:
        channels.append(f"orderbook_v3:{market_hash}")
    if event_id:
        channels.append(f"orderbook_v3_event:{event_id}")
    account_channel = f"account:orders_v3_#{sub}" if sub else None
    if account_channel:
        channels.append(account_channel)
    else:
        rec.add("realtime_subscribe account:orders_v3_#<own address>", "REAL_WEBSOCKET", "NOT_TESTED",
                reason="TOKEN_SUB_NOT_AN_ADDRESS")
    ids = {}
    results: dict[str, dict[str, Any]] = {}
    pushes: dict[str, int] = {}
    connect_ok = False
    pings = 0
    try:
        async with asyncio.timeout(seconds + 45):  # 15 s open + 15 s connect reply + listen + margin
            async with websockets.connect(WS_ENDPOINT, open_timeout=15, max_size=2 ** 20) as ws:
                await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "fce-readonly-cert"}}))
                deadline = None
                next_id = 2
                while True:
                    timeout = 15 if deadline is None else max(0.1, deadline - time.monotonic())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except TimeoutError:
                        if deadline is None:
                            break
                        if time.monotonic() >= deadline:
                            break
                        continue
                    for frame in centrifugo_frames(raw):
                        if not frame:
                            pings += 1
                            await ws.send("{}")
                            continue
                        if frame.get("id") == 1:
                            connect_ok = "connect" in frame
                            if not connect_ok:
                                results["connect"] = {"error_code": (frame.get("error") or {}).get("code")}
                                raise StopAsyncIteration
                            for ch in channels:
                                ids[next_id] = ch
                                await ws.send(json.dumps({"id": next_id, "subscribe": {"channel": ch}}))
                                next_id += 1
                            deadline = time.monotonic() + seconds
                        elif frame.get("id") in ids:
                            ch = ids[frame["id"]]
                            results[ch] = ({"subscribed": True} if "subscribe" in frame else
                                           {"subscribed": False, "error_code": (frame.get("error") or {}).get("code")})
                        elif "push" in frame:
                            ch = frame["push"].get("channel", "?")
                            pushes[ch] = pushes.get(ch, 0) + 1
                    if deadline is not None and time.monotonic() >= deadline:
                        break
    except StopAsyncIteration:
        pass
    except Exception as exc:  # report the type only
        rec.add("realtime_websocket_connect", "REAL_WEBSOCKET", "FAIL", error_type=type(exc).__name__)
        return
    rec.add("realtime_websocket_connect", "REAL_WEBSOCKET", "PASS" if connect_ok else "FAIL",
            server_pings_answered=pings, listen_seconds=seconds, **({} if connect_ok else results.get("connect", {})))
    for ch in channels:
        label = "account:orders_v3_#<own address>" if ch == account_channel else (
            "orderbook_v3:<probe market>" if ch.startswith("orderbook_v3:") else (
                "orderbook_v3_event:<probe event>" if ch.startswith("orderbook_v3_event:") else ch))
        res = results.get(ch, {"subscribed": None})
        n = pushes.get(ch, 0)
        outcome = "PASS" if res.get("subscribed") and n else ("EMPTY_VALID" if res.get("subscribed") else
                                                              ("NOT_TESTED" if res.get("subscribed") is None else "FAIL"))
        rec.add(f"realtime_subscribe {label}", "REAL_WEBSOCKET", outcome,
                subscribed=res.get("subscribed"), error_code=res.get("error_code"), publications=n)


def main() -> int:
    import logging
    for noisy in ("httpx", "httpcore", "websockets", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # no request URLs/headers in CI logs
    ap = argparse.ArgumentParser()
    ap.add_argument("--authorize-live-read", action="store_true")
    ap.add_argument("--output", required=True)
    ap.add_argument("--ws-seconds", type=float, default=20.0)
    args = ap.parse_args()
    if not args.authorize_live_read:
        print("Refusing: pass --authorize-live-read (read-only live test)")
        return 2
    seconds = min(max(args.ws_seconds, 5.0), 60.0)
    api_key = os.environ.get("SXBET_API_KEY", "").strip()
    secrets = [api_key]
    secrets_extra.clear()
    rec = Recorder()
    started = _now()
    ctx: dict[str, Any] = {}
    try:
        ctx = run_mcp(rec, api_key)
    except Exception as exc:
        rec.add("mcp_suite", "REAL_API_VIA_MCP", "FAIL", error_type=type(exc).__name__)
    try:
        asyncio.run(invalid_key_probe(rec))
    except Exception as exc:
        rec.add("invalid_key_rejected", "REAL_API_DIRECT", "FAIL", error_type=type(exc).__name__)
    try:
        asyncio.run(realtime_probe(rec, api_key, ctx.get("probe_market_hash"), ctx.get("probe_event_id"),
                                   seconds, secrets))
    except Exception as exc:
        rec.add("realtime_websocket", "REAL_WEBSOCKET", "FAIL", error_type=type(exc).__name__)
    counts: dict[str, int] = {}
    for r in rec.rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    report = {"suite": "sxbet_live_readonly_certification", "network": "mainnet", "mode": "READ_ONLY",
              "started_at_utc": started, "finished_at_utc": _now(), "api_key_present": bool(api_key),
              "write_operations_attempted": 0, "outcome_counts": counts, "context": ctx, "results": rec.rows}
    secrets.extend(secrets_extra)
    text = json.dumps(report, indent=1, ensure_ascii=False, default=str)
    if not leak_free(text, secrets):
        text = json.dumps({"suite": "sxbet_live_readonly_certification", "outcome_counts": {"FAIL": 1},
                           "results": [{"test": "leak_guard", "outcome": "FAIL", "reason": "SENSITIVE_VALUE_IN_REPORT_SUPPRESSED"}]})
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(text)
    final_counts = json.loads(text).get("outcome_counts") or {}
    print(json.dumps({"outcome_counts": final_counts}))
    return 1 if final_counts.get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
