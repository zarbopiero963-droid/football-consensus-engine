#!/usr/bin/env python3
"""Bounded, manually invoked API-FOOTBALL 39-route evidence probe.

No import-time traffic. This does not certify every query-parameter combination.
Unknown prerequisites are recorded BLOCKED, never replaced with fabricated IDs.
"""
from __future__ import annotations
import argparse
import csv
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import httpx
from bridge.api_football.config import Settings, OFFICIAL_BASE_URL
from bridge.api_football.catalog import ALLOWED_PATHS, FOOTBALL_PATHS, ACCOUNT_PATHS

ROUTES = ACCOUNT_PATHS + FOOTBALL_PATHS
assert len(ROUTES) == 39 and len(set(ROUTES)) == 39
CAP = 39
MAX_BYTES = 524288
FIXED = {
    "/status": {},
    "/timezone": {}, "/countries": {}, "/leagues": {"id": 39, "season": 2025},
    "/leagues/seasons": {}, "/teams": {"league": 39, "season": 2025},
    "/teams/countries": {}, "/venues": {"search": "London"},
    "/standings": {"league": 39, "season": 2025},
    "/fixtures/rounds": {"league": 39, "season": 2025},
    "/fixtures": {"league": 39, "season": 2025, "last": 2},
    "/coachs": {"search": "Guardiola"},
    "/players/seasons": {}, "/players/profiles": {"page": 1},
    "/odds/bookmakers": {}, "/odds/bets": {},
    "/odds/mapping": {}, "/odds/live/bets": {},
}
NEED = {
    "/teams/statistics": ("team",), "/teams/seasons": ("team",),
    "/fixtures/headtohead": ("h2h",), "/fixtures/statistics": ("fixture",),
    "/fixtures/events": ("fixture",), "/fixtures/lineups": ("fixture",),
    "/fixtures/players": ("fixture",), "/injuries": ("fixture",),
    "/players": ("team",), "/players/squads": ("team",),
    "/players/teams": ("player",), "/players/topscorers": (),
    "/players/topassists": (), "/players/topyellowcards": (),
    "/players/topredcards": (), "/transfers": ("team",),
    "/trophies": ("player",), "/sidelined": ("player",),
    "/predictions": ("fixture",), "/odds": ("fixture",),
    "/odds/live": ("live_fixture",),
}
LEAGUE = {"league": 39, "season": 2025}
for p in ("/players/topscorers", "/players/topassists", "/players/topyellowcards", "/players/topredcards"):
    FIXED[p] = dict(LEAGUE)

def plan(path, ids):
    if path in FIXED:
        return dict(FIXED[path])
    requirements = NEED.get(path)
    if requirements is None:
        return None
    if any(not ids.get(key) for key in requirements):
        return None
    params = {key: ids[key] for key in requirements}
    if path in ("/teams/statistics", "/players"):
        params.update(LEAGUE if path == "/teams/statistics" else {"season": 2025})
    if path == "/odds/live":
        params = {"fixture": ids["live_fixture"]}
    if path == "/odds":
        params = {"fixture": ids["fixture"]}
    return params

def discover(path, payload, ids):
    """Extract only provider-observed numeric identifiers, without assumptions."""
    data = payload.get("response")
    if not isinstance(data, list):
        return
    for row in data[:3]:
        if not isinstance(row, dict):
            continue
        if path == "/teams":
            team = row.get("team")
            if isinstance(team, dict) and type(team.get("id")) is int:
                ids.setdefault("team", team["id"])
        if path == "/fixtures":
            fixture = row.get("fixture")
            teams = row.get("teams")
            if isinstance(fixture, dict) and type(fixture.get("id")) is int:
                ids.setdefault("fixture", fixture["id"])
                status = fixture.get("status")
                if isinstance(status, dict) and status.get("short") in ("1H", "HT", "2H", "ET", "BT", "P", "LIVE"):
                    ids.setdefault("live_fixture", fixture["id"])
            if isinstance(teams, dict):
                home, away = teams.get("home"), teams.get("away")
                if isinstance(home, dict) and isinstance(away, dict) and type(home.get("id")) is int and type(away.get("id")) is int:
                    ids.setdefault("h2h", str(home["id"]) + "-" + str(away["id"]))
        if path == "/players":
            player = row.get("player")
            if isinstance(player, dict) and type(player.get("id")) is int:
                ids.setdefault("player", player["id"])

def quota(headers, key):
    raw = headers.get(key)
    return int(raw) if raw and raw.isdecimal() and len(raw) <= 12 else None

async def inventory(env, *, transport=None, sleep=asyncio.sleep):
    config = Settings.from_env(env)
    if not config.real_requests_authorized:
        raise ValueError("LIVE_NOT_AUTHORIZED")
    if config.base_url != OFFICIAL_BASE_URL or config.max_daily_requests < CAP:
        raise ValueError("UNSAFE_OR_INSUFFICIENT_BUDGET")
    ids, rows = {}, []
    stopped = False
    async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, timeout=config.timeout_seconds,
                                 follow_redirects=False, trust_env=False, transport=transport) as client:
        count = 0
        for path in ROUTES:
            if path not in ALLOWED_PATHS:
                raise ValueError("UNALLOWLISTED_PATH")
            params = plan(path, ids)
            if params is None:
                rows.append({"path": path, "outcome": "BLOCKED_MISSING_REAL_PREREQUISITES"})
                continue
            if stopped or count >= CAP:
                rows.append({"path": path, "outcome": "BLOCKED_QUOTA_OR_PROVIDER"})
                continue
            if count:
                await sleep(60 / min(config.max_rpm, 8))
            count += 1
            row = {"path": path, "outcome": "NOT_VERIFIED", "http_status": None,
                   "results": None, "quota_day": None, "quota_minute": None}
            try:
                async with client.stream("GET", path, params=params, headers={
                    "x-apisports-key": config.api_key, "Accept": "application/json",
                }) as response:
                    row["http_status"] = response.status_code
                    row["quota_day"] = quota(response.headers, "x-ratelimit-requests-remaining")
                    row["quota_minute"] = quota(response.headers, "x-ratelimit-remaining")
                    if response.status_code != 200:
                        row["outcome"] = "HTTP_ERROR"
                        stopped = True
                    else:
                        content = await response.aread()
                        if len(content) > MAX_BYTES:
                            row["outcome"] = "PAYLOAD_TOO_LARGE"
                            stopped = True
                        else:
                            try:
                                payload = json.loads(content)
                                if not isinstance(payload, dict) or "response" not in payload or payload.get("errors") not in ({}, [], None):
                                    row["outcome"] = "PROVIDER_ERROR_OR_INVALID_RESPONSE"
                                    stopped = True
                                else:
                                    value = payload["response"]
                                    row["outcome"] = "PASS_DATA" if value else "PASS_EMPTY_NOT_AVAILABILITY"
                                    row["results"] = payload.get("results") if type(payload.get("results")) is int else None
                                    discover(path, payload, ids)
                            except (ValueError, UnicodeDecodeError):
                                row["outcome"] = "INVALID_JSON"
                                stopped = True
                    if row["quota_day"] == 0 or row["quota_minute"] == 0:
                        stopped = True
            except httpx.RequestError:
                row["outcome"] = "NETWORK_ERROR"
                stopped = True
            rows.append(row)
    return {"schema": "api-football-route-evidence/v1",
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "route_count": len(ROUTES), "requests_attempted": count,
            "cap": CAP, "records": rows,
            "note": "One read per route at most; missing data or prerequisite is not certified availability."}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-live-inventory", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(inventory(os.environ))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with args.output.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fp:
        keys = ["path", "outcome", "http_status", "results", "quota_day", "quota_minute"]
        writer = csv.DictWriter(fp, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["records"])
    print(json.dumps({"route_count": report["route_count"], "requests_attempted": report["requests_attempted"],
                      "outcomes": {key: sum(r["outcome"] == key for r in report["records"]) for key in sorted(set(r["outcome"] for r in report["records"]))}}))

if __name__ == "__main__":
    main()
