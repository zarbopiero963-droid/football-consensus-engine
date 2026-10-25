#!/usr/bin/env python3
"""Manual-only capped catalog probe. No raw response or secrets in logs."""
import asyncio
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

import httpx
from bridge.api_football.config import Settings, OFFICIAL_BASE_URL
from bridge.api_football.capability_inventory import inventory_payload
from bridge.api_football.coverage_certification import pagination_verdict
from bridge.api_football.five_point_evidence import extract_odds, field_types, live_delta

# Catalogs first; fixtures provide REAL prerequisites for statistics and odds.
PATHS = ("/odds/bookmakers", "/odds/bets", "/odds/live/bets",
         "/fixtures", "/fixtures", "/fixtures/statistics", "/odds", "/odds/live",
         "/fixtures/events", "/fixtures/lineups", "/fixtures/players", "/teams/statistics")
CAP = 60
# Completion requires all provider pages; request budget may still stop a scan.
MAX_FIXTURE_SAMPLES = 3

async def run(env, *, transport=None, sleep=asyncio.sleep):
    cfg = Settings.from_env(env)
    if not cfg.real_requests_authorized or cfg.base_url != OFFICIAL_BASE_URL or cfg.max_daily_requests < CAP:
        raise ValueError("LIVE_INVENTORY_NOT_AUTHORIZED")
    evidence, calls, stopped = [], 0, False
    observed = {}
    fixtures = []
    teams = []
    live_fixtures = []
    statistics_samples = []
    async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, trust_env=False,
                                 follow_redirects=False, timeout=cfg.timeout_seconds, transport=transport) as client:
        for idx, path in enumerate(PATHS):
            params = {}
            if path == "/fixtures":
                params = {"live": "all"} if idx > 0 and PATHS[idx - 1] == "/fixtures" else {"league": 39, "season": 2025, "last": 5}
            elif path in ("/fixtures/statistics", "/fixtures/events", "/fixtures/lineups", "/fixtures/players", "/odds"):
                if "fixture" not in observed:
                    evidence.append({"path": path, "status": "BLOCKED_REQUIRES_OBSERVED_FIXTURE"})
                    continue
                params = {"fixture": observed["fixture"]}
            elif path == "/teams/statistics":
                if "team" not in observed:
                    evidence.append({"path": path, "status": "BLOCKED_REQUIRES_OBSERVED_TEAM"})
                    continue
                params = {"league": 39, "season": 2025, "team": observed["team"]}
            elif path == "/odds/live":
                if "live_fixture" not in observed:
                    evidence.append({"path": path, "status": "BLOCKED_REQUIRES_OBSERVED_LIVE_FIXTURE"})
                    continue
                params = {"fixture": observed["live_fixture"]}
            samples = [params]
            if path in ("/fixtures/statistics", "/fixtures/events", "/fixtures/lineups", "/fixtures/players", "/odds"):
                samples = [{"fixture": fid} for fid in fixtures[:MAX_FIXTURE_SAMPLES]] or [params]
            elif path == "/teams/statistics":
                samples = [{"league": 39, "season": 2025, "team": tid} for tid in teams[:MAX_FIXTURE_SAMPLES]] or [params]
            for sample_params in samples:
              page = 1
              while True:
                  if stopped or calls >= CAP:
                      evidence.append({"path": path, "status": "BLOCKED_QUOTA", "page": page})
                      break
                  query = dict(sample_params)
                  if page > 1:
                      query["page"] = page
                  if calls:
                      await sleep(60 / min(cfg.max_rpm, 8))
                  calls += 1
                  try:
                      async with client.stream("GET", path, params=query,
                          headers={"x-apisports-key": cfg.api_key, "Accept": "application/json"}) as response:
                          row = {"path": path, "page": page, "mode": "LIVE" if params.get("live") == "all" else "STANDARD", "http_status": response.status_code}
                          if response.status_code != 200:
                              row["status"] = "HTTP_ERROR"
                              evidence.append(row)
                              stopped = True
                              break
                          payload = await response.aread()
                          if len(payload) > 524288:
                              evidence.append({**row, "status": "TOO_LARGE"})
                              stopped = True
                              break
                          parsed = json.loads(payload)
                          row.update(inventory_payload(path, parsed))
                          row["response_sha256"] = hashlib.sha256(payload).hexdigest()
                          row["response_bytes"] = len(payload)
                          paging = parsed.get("paging") if isinstance(parsed, dict) else None
                          row["pagination"] = {k: paging.get(k) for k in ("current", "total")} if isinstance(paging, dict) else None
                          body = parsed.get("response") if isinstance(parsed, dict) else None
                          if path == "/fixtures" and isinstance(body, list):
                              for match in body:
                                  if not isinstance(match, dict):
                                      continue
                                  fixture = match.get("fixture") or {}
                                  teams = match.get("teams") or {}
                                  if isinstance(fixture, dict) and type(fixture.get("id")) is int:
                                      observed.setdefault("fixture", fixture["id"])
                                      if (fixture.get("status") or {}).get("short") in ("1H", "HT", "2H", "ET", "BT", "P", "LIVE"):
                                          observed.setdefault("live_fixture", fixture["id"])
                                  home = teams.get("home") if isinstance(teams, dict) else None
                                  if isinstance(home, dict) and type(home.get("id")) is int:
                                      observed.setdefault("team", home["id"])
                            if path == "/fixtures" and isinstance(body, list):
                              for match in body:
                                  if not isinstance(match, dict):
                                      continue
                                  fixture_obj = match.get("fixture") or {}
                                  teams_obj = match.get("teams") or {}
                                  fid = fixture_obj.get("id") if isinstance(fixture_obj, dict) else None
                                  if type(fid) is int and fid not in fixtures:
                                      fixtures.append(fid)
                                  home = teams_obj.get("home") if isinstance(teams_obj, dict) else None
                                  tid = home.get("id") if isinstance(home, dict) else None
                                  if type(tid) is int and tid not in teams:
                                      teams.append(tid)
                                  st = fixture_obj.get("status") if isinstance(fixture_obj, dict) else None
                                  if type(fid) is int and isinstance(st, dict) and st.get("short") in ("1H","HT","2H","ET","BT","P","LIVE") and fid not in live_fixtures:
                                      live_fixtures.append(fid)
                          if path in ("/fixtures/statistics", "/fixtures/events", "/fixtures/lineups", "/fixtures/players", "/teams/statistics"):
                              statistics_samples.append({"path": path, "parameters": query, "field_types": field_types([parsed]), "response_sha256": row["response_sha256"]})
                          # Observe real bookmaker/market values from odds response, with no invented IDs.
                          if path in ("/odds", "/odds/live") and isinstance(body, list):
                              row["observed_market_schema"] = True
                              row["odds_observations"] = extract_odds(parsed, phase="LIVE" if path == "/odds/live" else "PREMATCH")
                          evidence.append(row)
                          if row["status"] not in ("OBSERVED", "NO_DATA_OBSERVED"):
                              stopped = True
                              break
                          if response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("x-ratelimit-requests-remaining") == "0":
                              stopped = True
                              break
                          total = paging.get("total", 1) if isinstance(paging, dict) else 1
                          if type(total) is not int or total < 1 or total <= page:
                              break
                          if path not in ("/odds/bookmakers", "/odds/bets", "/odds/live/bets", "/odds"):
                              evidence.append({"path": path, "status": "PAGINATION_UNSUPPORTED_OR_NOT_VERIFIED", "page": page + 1})
                              break
                          if page >= MAX_PAGES:
                              evidence.append({"path": path, "status": "PAGINATION_CAP_REACHED", "page": page + 1, "provider_total_pages": total})
                              break
                          page += 1
                  except (httpx.RequestError, ValueError, UnicodeDecodeError):
                      evidence.append({"path": path, "status": "NETWORK_OR_PAYLOAD_ERROR", "page": page})
                      stopped = True
                      break
    evidence.append({"path": "_statistics_samples", "status": "OBSERVATION_SUMMARY",
                     "samples": statistics_samples, "distinct_fixtures": len(fixtures),
                     "distinct_teams": len(teams), "requests_attempted": calls})
    return evidence

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp/apifootball_capability_evidence"))
    args = parser.parse_args()
    output = asyncio.run(run(os.environ))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "capabilities.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
    with (args.out_dir / "catalogs.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=["source_path", "phase", "id", "name"])
        writer.writeheader()
        for result in output:
            for item in result.get("catalog", []):
                writer.writerow({"source_path":result["path"],"phase":result.get("phase","UNKNOWN"),"id":item["id"],"name":item["name"]})
    with (args.out_dir / "field_registry.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=["source_path", "json_path", "types"])
        writer.writeheader()
        for result in output:
            for json_path, types in result.get("fields", {}).items():
                writer.writerow({"source_path":result["path"],"json_path":json_path,"types":",".join(types)})
    catalog_paths = ("/odds/bookmakers", "/odds/bets", "/odds/live/bets", "/odds")
    coverage = [pagination_verdict(output, p) for p in catalog_paths]
    (args.out_dir / "pagination_coverage.json").write_text(json.dumps(coverage, indent=2) + chr(10), encoding="utf-8")
    with (args.out_dir / "odds_observations.csv").open("w", newline="", encoding="utf-8") as fp:
        cols = ["phase", "fixture_id", "bookmaker_id", "bookmaker_name", "market_id", "market_name", "selection", "odd", "handicap", "main", "suspended", "updated"]
        writer = csv.DictWriter(fp, fieldnames=cols)
        writer.writeheader()
        for result in output:
            for item in result.get("odds_observations", []):
                writer.writerow({key: item.get(key) for key in cols})
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/tmp/api-football-capability-summary.md"))
    lines = ["### API-FOOTBALL catalog inventory", "", "Catalog does not prove bookie/market availability or full pagination.", "",
             "| Endpoint | State | Records | Pages |", "|---|---|---:|---|"]
    for row in output:
        lines.append("| {} | {} | {} | {} |".format(row["path"], row["status"], row.get("count", "-"), row.get("pagination", "-")))
    lines.extend(["", "### Catalog pagination", "", "| Catalog | Coverage | Pages | Provider total |", "|---|---|---:|---:|"])
    for item in coverage:
        lines.append("| {} | {} | {} | {} |".format(item["path"], item["status"], item["observed_pages"], item.get("provider_total_pages", "UNKNOWN")))
    summary.open("a").write(chr(10).join(lines) + chr(10))
    print("Catalog paths evaluated: {}".format(len(output)))
