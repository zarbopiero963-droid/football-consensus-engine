#!/usr/bin/env python3
"""Explicit opt-in, bounded live API-FOOTBALL smoke test. No import-time I/O."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import httpx

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.catalog import ALLOWED_PATHS

SAMPLE = ("/status", "/countries", "/leagues/seasons", "/odds/bookmakers")
MAX_CALLS = 4


def preflight(env):
    settings = Settings.from_env(env)
    if not settings.real_requests_authorized:
        raise ValueError("LIVE_TEST_NOT_AUTHORIZED")
    if settings.base_url != OFFICIAL_BASE_URL:
        raise ValueError("UNTRUSTED_API_ORIGIN")
    if settings.max_daily_requests < MAX_CALLS:
        raise ValueError("DAILY_LIMIT_BELOW_TEST_BUDGET")
    if any(path not in ALLOWED_PATHS for path in SAMPLE):
        raise ValueError("UNALLOWLISTED_TEST_PATH")
    return settings


def quota(headers, name):
    value = headers.get(name)
    return int(value) if isinstance(value, str) and value.isdecimal() and len(value) <= 12 else None


async def run_live(env, *, transport=None):
    settings = preflight(env)
    results = []
    async with httpx.AsyncClient(
        base_url=OFFICIAL_BASE_URL,
        timeout=settings.timeout_seconds,
        follow_redirects=False,
        transport=transport,
        trust_env=False,
    ) as client:
        for path in SAMPLE:
            # No retries: strict request cap and no uncontrolled fanout.
            try:
                response = await client.get(
                    path,
                    headers={"x-apisports-key": settings.api_key, "Accept": "application/json"},
                )
                day = quota(response.headers, "x-ratelimit-requests-remaining")
                minute = quota(response.headers, "x-ratelimit-remaining")
                entry = {"path": path, "http_status": response.status_code,
                         "remaining_day": day, "remaining_minute": minute,
                         "outcome": "NOT_VERIFIED"}
                if len(response.content) > 524288:
                    entry["outcome"] = "RESPONSE_TOO_LARGE"
                elif response.status_code != 200:
                    entry["outcome"] = "HTTP_ERROR"
                else:
                    try:
                        payload = response.json()
                        if isinstance(payload, dict) and "response" in payload and payload.get("errors") in (None, {}, []):
                            entry["outcome"] = "PASS"
                            entry["results"] = payload.get("results") if type(payload.get("results")) is int else None
                            entry["response_type"] = type(payload["response"]).__name__
                        else:
                            entry["outcome"] = "PROVIDER_ERROR_OR_INVALID_CONTRACT"
                    except (ValueError, TypeError, UnicodeError):
                        entry["outcome"] = "INVALID_JSON"
                results.append(entry)
                # Unknown quota is not assumed exhausted; known zero is a hard stop.
                if entry["outcome"] != "PASS" or day == 0 or minute == 0:
                    break
            except (httpx.RequestError, httpx.TimeoutException):
                results.append({"path": path, "outcome": "TRANSPORT_ERROR"})
                break
    return {"schema": "api-football-live-smoke/v1",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "origin": OFFICIAL_BASE_URL,
            "requests_attempted": len(results),
            "request_cap": MAX_CALLS,
            "complete": len(results) == len(SAMPLE) and all(x["outcome"] == "PASS" for x in results),
            "results": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorize-live-test", action="store_true", required=True)
    args = parser.parse_args()
    if not args.authorize_live_test:
        raise SystemExit("LIVE_TEST_OPT_IN_REQUIRED")
    try:
        result = asyncio.run(run_live(os.environ))
    except ValueError as exc:
        # Public fixed error codes only.
        raise SystemExit(str(exc)) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"requests_attempted": result["requests_attempted"], "complete": result["complete"]}))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
