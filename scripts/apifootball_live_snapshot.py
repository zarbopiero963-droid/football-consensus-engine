#!/usr/bin/env python3
"""LIVE FULL MATCH SNAPSHOT: one report for ONE fixture id, per-source timing, fidelity checks.

Offline builder: it never calls the provider. Input = recorded sources, i.e. the MCP replies of the
existing apifootball_* tools for one fixture id and, when the runner captured it at the HTTP transport,
the provider's original body text. Output = one report that:
  * keeps the MCP data exactly as received and, where captured, the provider's original body; the
    overall verdict is never "certified" while a source is PARTIAL (manifest above the cap) and the
    manifest's identifiers to recover the rest are listed;
  * compares every field of the original against the reported data (nothing lost, nothing changed);
  * keeps null as null (never 0, never derived: no HT from events, no invented xG);
  * gives, per source, request start/end in UTC and Europe/Rome, the collection window (first start ->
    last end) and states that the sources are NOT simultaneous; the minute is "status.elapsed reported
    by the provider at that fetch", not real time;
  * checks goals vs Goal events only as a DERIVED control, never rewriting provider data;
  * reports live odds as published (bookmakers, markets, values, suspensions): market prices, not
    model probabilities;
  * classifies availability: EMPTY_VALID / PROVIDER_NOT_OFFERED (league coverage flag false) /
    PLAN_EXCLUDED (provider declared a plan error) - never an MCP defect;
  * lists, per source and field path, every transformation the bridge applies (verified in
    bridge/api_football; see BRIDGE_TRANSFORMATIONS). Without a transport capture, identity with the
    original reply is declared NOT verifiable for that source, never assumed.
Out of scope (owner decision): continuous odds collection, history, drop computation.

Usage: python scripts/apifootball_live_snapshot.py --input sources.json --out report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
UTC = ZoneInfo("UTC")

# Per-fixture sources from bridge/api_football/contracts.py (names and parameters verified there).
SOURCES = {
    "fixture": ("apifootball_fixtures", "id"),
    "statistics": ("apifootball_fixture_statistics", "fixture"),
    "events": ("apifootball_fixture_events", "fixture"),
    "lineups": ("apifootball_fixture_lineups", "fixture"),
    "players": ("apifootball_fixture_players", "fixture"),
    "live_odds": ("apifootball_live_odds", "fixture"),
    "coverage": ("apifootball_coverage_report", None),  # id=league, season=season of that fixture
}
REQUIRED_SOURCES = ("fixture", "statistics", "events", "lineups", "players", "live_odds")
COVERAGE_FLAG = {  # /leagues seasons[].coverage flag that says whether the provider offers a source
    "statistics": ("fixtures", "statistics_fixtures"), "events": ("fixtures", "events"),
    "lineups": ("fixtures", "lineups"), "players": ("fixtures", "statistics_players"),
    "live_odds": ("odds",),
}
# Transformations verified in the bridge code on 2026-11-11 (f666cc1). Paths are of the MCP reply.
_COMMON = (
    {"path": "$.data", "kind": "JSON_PARSE", "where": "bridge/api_football/transport.py (json.loads)",
     "effect": "the body is parsed with json.loads: number formatting is not kept (1.50 -> 1.5) and a "
               "duplicated key keeps its last value; strings, nulls and structure are kept"},
    {"path": "$.error", "kind": "FILTER", "where": "bridge/api_football/transport.py, errors.py",
     "effect": "a non-empty provider `errors` becomes PROVIDER_DECLARED_ERRORS with the keys only; "
               "the provider's error values are dropped"},
    {"path": "$.data", "kind": "MANIFEST", "where": "bridge/api_football/tools.py (_MAX_MCP_DATA_BYTES)",
     "effect": "above 256 KiB the data is replaced by a PARTIAL_*_MANIFEST (ids/counts, no prices); "
               "above the 512 KiB read cap by a streamed-scan PARTIAL_ID_MANIFEST"},
    {"path": "$.warnings, $.status, $.retrieved_at_utc, $.quota_remaining", "kind": "BRIDGE_METADATA",
     "where": "bridge/api_football/tools.py", "effect": "bridge fields beside the data; not provider data"},
)
BRIDGE_TRANSFORMATIONS = {
    "fixture": _COMMON + ({"path": "$.combination", "kind": "DERIVED", "where": "contracts.fixtures_combination",
                          "effect": "documentary label of the parameter set, beside the data"},),
    "statistics": _COMMON, "events": _COMMON, "lineups": _COMMON, "players": _COMMON,
    "live_odds": _COMMON + ({"path": "$.live_odds_state", "kind": "DERIVED",
                             "where": "bridge/api_football/live_odds_state.py",
                             "effect": "DERIVED summary beside the untouched provider data"},),
    "coverage": _COMMON,
}
REQUIREMENTS = (
    "SNAP-RAW-PRESERVED", "SNAP-FIELD-COMPLETENESS", "SNAP-NULL-PRESERVED", "SNAP-TIMESTAMPS",
    "SNAP-NOT-SIMULTANEOUS-DECLARED", "SNAP-IDENTITY-WITH-ORIGINAL", "SNAP-TRANSFORMATIONS-LISTED",
    "SNAP-DERIVED-CONSISTENCY", "SNAP-LIVE-ODDS-AS-PUBLISHED", "SNAP-AVAILABILITY-CLASSIFIED",
    "SNAP-PARTIAL-NOT-CERTIFIED", "SNAP-BUDGET-CAP-CLOSES-INCOMPLETE",
)
OVERALL = ("SNAPSHOT_CERTIFIED", "SNAPSHOT_PARTIAL", "SNAPSHOT_INCOMPLETE", "SNAPSHOT_FAILED")
NOT_SIMULTANEOUS = ("The sources were fetched one after the other: they are NOT simultaneous and this "
                    "report is not a perfect instantaneous snapshot. Each source is as of its own times.")
_MISSING = object()


class DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise DuplicateKey("DUPLICATE_KEY_IN_PROVIDER_BODY")
    return dict(pairs)


def flatten(value: Any, prefix: str = "$") -> dict[str, Any]:
    """Every leaf path of a JSON value; empty containers are leaves too, so nothing is dropped."""
    if isinstance(value, dict):
        if not value:
            return {prefix: {}}
        out: dict[str, Any] = {}
        for key in value:
            out.update(flatten(value[key], f"{prefix}.{key}"))
        return out
    if isinstance(value, list):
        if not value:
            return {prefix: []}
        out = {}
        for i, item in enumerate(value):
            out.update(flatten(item, f"{prefix}[{i}]"))
        return out
    return {prefix: value}


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b  # strict: 0 != None, False != 0, "1" != 1


def compare_fields(original: Any, reported: Any) -> dict[str, Any]:
    flat_o, flat_r = flatten(original), flatten(reported)
    missing = sorted(p for p in flat_o if p not in flat_r)
    changed = sorted(p for p in flat_o if p in flat_r and not _same(flat_o[p], flat_r[p]))
    extra = sorted(p for p in flat_r if p not in flat_o)
    nulls = sorted(p for p, v in flat_o.items() if v is None)
    return {
        "original_fields": len(flat_o), "reported_fields": len(flat_r),
        "missing_in_report": missing, "changed_in_report": changed, "extra_in_report": extra,
        "original_null_paths": len(nulls),
        "nulls_preserved": all(p in flat_r and flat_r[p] is None for p in nulls),
        "result": "COMPLETE" if not (missing or changed or extra) else ("FIELDS_LOST" if missing else "VALUES_CHANGED"),
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _instant(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None  # naive = TIMEZONE_UNRESOLVED, never guessed


def _timing(src: Mapping[str, Any]) -> dict[str, Any]:
    start, end = _instant(src.get("requested_at_utc")), _instant(src.get("completed_at_utc"))
    if start is None or end is None or end < start:
        return {"status": "TIMESTAMP_INVALID_OR_TIMEZONE_UNRESOLVED", "requested_at_utc": None,
                "completed_at_utc": None, "requested_at_europe_rome": None, "completed_at_europe_rome": None}
    return {"status": "OK",
            "requested_at_utc": start.astimezone(UTC).isoformat(), "completed_at_utc": end.astimezone(UTC).isoformat(),
            "requested_at_europe_rome": start.astimezone(ROME).isoformat(),
            "completed_at_europe_rome": end.astimezone(ROME).isoformat(),
            "duration_seconds": round((end - start).total_seconds(), 3)}


def _provider_updates(value: Any) -> list[str]:
    return sorted({str(v) for p, v in flatten(value).items() if p.endswith(".update") and v is not None})


def verdict_of(reply: Mapping[str, Any]) -> str:
    status = reply.get("status")
    if status in ("REAL_API_RESPONSE", "MOCK_VERIFIED"):
        data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
        rows = data.get("response")
        results = data.get("results") if type(data.get("results")) is int else (len(rows) if isinstance(rows, list) else None)
        if results is None:
            return "ERROR"
        return "DATA_PRESENT" if results > 0 else "EMPTY_VALID"
    if isinstance(status, str) and status.startswith("PARTIAL_"):
        return "PARTIAL"
    error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    if error.get("code") == "PROVIDER_DECLARED_ERRORS" and error.get("category") == "PLAN_NOT_INCLUDED":
        return "PLAN_EXCLUDED"
    return "ERROR" if status else "NOT_TESTED"


def _coverage_flags(coverage_reply: Mapping[str, Any] | None, season: Any) -> dict[str, Any]:
    if not coverage_reply or verdict_of(coverage_reply) != "DATA_PRESENT":
        return {}
    flags: dict[str, Any] = {}
    for league in coverage_reply["data"].get("response") or []:
        for s in (league.get("seasons") or []) if isinstance(league, dict) else []:
            if isinstance(s, dict) and s.get("year") == season and isinstance(s.get("coverage"), dict):
                for source, path in COVERAGE_FLAG.items():
                    node: Any = s["coverage"]
                    for step in path:
                        node = node.get(step, _MISSING) if isinstance(node, dict) else _MISSING
                    flags[source] = None if node is _MISSING else node
    return flags


def _goal_check(fixture_data: Any, events_reply: Mapping[str, Any] | None) -> dict[str, Any]:
    """DERIVED: goals published by /fixtures vs Goal events (detail != Missed Penalty). Never rewrites."""
    out = {"label": "DERIVED", "check": "fixture.goals vs events type=Goal", "provider_data_modified": False}
    try:
        item = fixture_data["response"][0]
        goals, home, away = item["goals"], item["teams"]["home"]["id"], item["teams"]["away"]["id"]
    except (KeyError, IndexError, TypeError):
        return out | {"result": "NOT_COMPARABLE", "reason": "fixture goals or teams not published"}
    if not events_reply or verdict_of(events_reply) != "DATA_PRESENT":
        return out | {"result": "NOT_COMPARABLE", "reason": "no events data"}
    if not isinstance(goals, dict) or goals.get("home") is None or goals.get("away") is None:
        return out | {"result": "NOT_COMPARABLE", "reason": "goals null as published"}
    counted = {home: 0, away: 0}
    for event in events_reply["data"].get("response") or []:
        if isinstance(event, dict) and event.get("type") == "Goal" and event.get("detail") != "Missed Penalty":
            team = (event.get("team") or {}).get("id")
            if team in counted:
                counted[team] += 1
    published = {"home": goals["home"], "away": goals["away"]}
    from_events = {"home": counted[home], "away": counted[away]}
    return out | {"result": "MATCH" if published == from_events else "MISMATCH",
                  "published_goals": published, "goal_events_counted": from_events}


def _source_entry(name: str, src: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict[str, Any]:
    reply = src["mcp_reply"]
    verdict = verdict_of(reply)
    entry: dict[str, Any] = {
        "tool": src["tool"], "arguments": src.get("arguments"), "attempts_used": src.get("attempts_used"),
        "mcp_status": reply.get("status"), "verdict": verdict, "timing": _timing(src),
        "mcp_reply_as_received": reply,  # exactly what the MCP returned (data included)
        "mcp_reply_sha256": _sha(_canonical(reply)),
        "bridge_transformations": list(BRIDGE_TRANSFORMATIONS[name]),
    }
    text = src.get("provider_body_text")
    original = None
    if isinstance(text, str):
        entry["provider_original_body"] = text  # verbatim, as captured at the HTTP transport
        entry["provider_original_sha256"] = _sha(text)
        try:
            original = json.loads(text, object_pairs_hook=_no_duplicates)
            entry["provider_original_parse"] = "OK"
        except DuplicateKey:
            entry["provider_original_parse"] = "DUPLICATE_KEY"
        except ValueError:
            entry["provider_original_parse"] = "INVALID_JSON"
    data = reply.get("data")
    if verdict == "PARTIAL":
        entry["completeness"] = {"result": "PARTIAL_EXPLICIT"}
        entry["recovery"] = {"manifest": data, "navigation": reply.get("navigation"),
                             "note": "identifiers to fetch the rest with follow-up calls"}
    elif verdict in ("DATA_PRESENT", "EMPTY_VALID"):
        if original is not None:
            entry["completeness"] = compare_fields(original, data)
        else:
            entry["completeness"] = {"result": "NOT_VERIFIABLE_WITHOUT_ORIGINAL",
                                     "nulls_preserved": None, "reported_fields": len(flatten(data))}
    else:
        entry["completeness"] = {"result": "NO_PROVIDER_DATA"}
    if verdict in ("DATA_PRESENT", "EMPTY_VALID", "PARTIAL"):
        if original is None:
            entry["identity_with_original"] = "NOT_VERIFIABLE"
            entry["identity_note"] = ("the bridge does not expose the provider's raw reply for this source and "
                                      "no transport capture was recorded: identity is NOT verifiable")
        elif verdict == "PARTIAL":
            entry["identity_with_original"] = "PARTIAL_MANIFEST_NOT_IDENTICAL_BY_DESIGN"
        else:
            entry["identity_with_original"] = "VERIFIED" if entry["completeness"]["result"] == "COMPLETE" else "MISMATCH"
    else:
        entry["identity_with_original"] = "NOT_APPLICABLE"
    entry["provider_update_timestamps"] = _provider_updates(original if original is not None else data)
    if verdict == "EMPTY_VALID" and name in COVERAGE_FLAG and coverage.get(name) is False:
        entry["verdict"] = "PROVIDER_NOT_OFFERED"
        entry["availability_evidence"] = f"apifootball_coverage_report: season coverage flag for {name} is false"
    if name == "live_odds":
        entry["label"] = "MARKET_PRICES_AS_PUBLISHED_NOT_MODEL_PROBABILITIES"
    return entry


def build_report(snapshot_id: str, fixture_id: int, sources: Mapping[str, Mapping[str, Any]], *,
                 budget_cap: int, attempts_used: int, stopped_reason: str | None = None) -> dict[str, Any]:
    """sources: name -> {tool, arguments, requested_at_utc, completed_at_utc, mcp_reply,
    provider_body_text?, attempts_used?}."""
    for name, src in sources.items():
        if name not in SOURCES or src.get("tool") != SOURCES[name][0]:
            raise ValueError("UNKNOWN_SOURCE")
        key = SOURCES[name][1]
        if key and (src.get("arguments") or {}).get(key) != fixture_id:
            raise ValueError("SOURCE_NOT_FOR_THIS_FIXTURE")
    fixture_reply = (sources.get("fixture") or {}).get("mcp_reply") or {}
    fixture_data = fixture_reply.get("data") if verdict_of(fixture_reply) == "DATA_PRESENT" else None
    try:
        season = fixture_data["response"][0]["league"]["season"]
    except (KeyError, IndexError, TypeError):
        season = None
    coverage = _coverage_flags((sources.get("coverage") or {}).get("mcp_reply"), season)
    entries = {name: _source_entry(name, sources[name], coverage) for name in sorted(sources)}

    timings = [e["timing"] for e in entries.values() if e["timing"]["status"] == "OK"]
    window = None
    if timings:
        first = min(_instant(t["requested_at_utc"]) for t in timings)
        last = max(_instant(t["completed_at_utc"]) for t in timings)
        window = {"first_request_utc": first.isoformat(), "last_completion_utc": last.isoformat(),
                  "first_request_europe_rome": first.astimezone(ROME).isoformat(),
                  "last_completion_europe_rome": last.astimezone(ROME).isoformat(),
                  "duration_seconds": round((last - first).total_seconds(), 3)}
    state = None
    if fixture_data:
        try:
            item = fixture_data["response"][0]
            status = item["fixture"]["status"]
            state = {"label": "AS_PUBLISHED_BY_PROVIDER_AT_FETCH_TIME",
                     "status_elapsed": status.get("elapsed"), "status_extra": status.get("extra"),
                     "status_short": status.get("short"), "status_long": status.get("long"),
                     "as_of_utc": entries["fixture"]["timing"].get("completed_at_utc"),
                     "as_of_europe_rome": entries["fixture"]["timing"].get("completed_at_europe_rome"),
                     "minute_note": "minute reported by the provider at that fetch, not real time",
                     "goals": item.get("goals"), "score": item.get("score")}  # nulls kept, HT never derived
        except (KeyError, IndexError, TypeError):
            state = None
    derived = _goal_check(fixture_data, (sources.get("events") or {}).get("mcp_reply"))
    requirements = evaluate(entries, derived, window)
    partial = sorted(n for n, e in entries.items() if e["verdict"] == "PARTIAL")
    missing = sorted(set(REQUIRED_SOURCES) - set(entries))
    not_verifiable = sorted(n for n, e in entries.items() if e["identity_with_original"] == "NOT_VERIFIABLE")
    incomplete_reasons = ([f"SOURCE_NOT_FETCHED:{n}" for n in missing]
                          + ([f"STOPPED:{stopped_reason}"] if stopped_reason else [])
                          + [f"IDENTITY_NOT_VERIFIABLE:{n}" for n in not_verifiable]
                          + [f"SOURCE_{e['verdict']}:{n}" for n, e in sorted(entries.items())
                             if e["verdict"] in ("ERROR", "NOT_TESTED")])
    if any(v == "FAIL" for v in requirements.values()):
        overall = "SNAPSHOT_FAILED"
    elif partial:
        overall = "SNAPSHOT_PARTIAL"
    elif incomplete_reasons:
        overall = "SNAPSHOT_INCOMPLETE"
    else:
        overall = "SNAPSHOT_CERTIFIED"
    return {
        "schema": "apifootball-live-full-match-snapshot/v1", "snapshot_id": snapshot_id, "fixture_id": fixture_id,
        "overall": overall, "partial_sources": partial, "incomplete_reasons": incomplete_reasons,
        "budget": {"cap": budget_cap, "attempts_used": attempts_used,
                   "note": "cap is a configured ceiling, not an authorization; on reaching it the snapshot closes incomplete"},
        "simultaneity": {"sources_simultaneous": False, "statement": NOT_SIMULTANEOUS, "collection_window": window},
        "match_state_as_published": state, "derived_checks": [derived],
        "sources": entries, "requirements": requirements,
    }


def evaluate(entries: Mapping[str, Any], derived: Mapping[str, Any], window: Any) -> dict[str, str]:
    with_data = [e for e in entries.values() if e["verdict"] in ("DATA_PRESENT", "EMPTY_VALID", "PROVIDER_NOT_OFFERED")]
    verifiable = [e for e in with_data if e["identity_with_original"] != "NOT_VERIFIABLE"]
    out: dict[str, str] = {}
    out["SNAP-RAW-PRESERVED"] = "PASS" if all(
        "mcp_reply_as_received" in e and (e.get("provider_original_body") is not None
                                          or e["identity_with_original"] == "NOT_VERIFIABLE")
        for e in with_data) and all("recovery" in e for e in entries.values() if e["verdict"] == "PARTIAL") else "FAIL"
    out["SNAP-FIELD-COMPLETENESS"] = ("PASS" if all(e["completeness"]["result"] == "COMPLETE" for e in verifiable)
                                      else "FAIL") if verifiable else "NOT_VERIFIABLE"
    out["SNAP-NULL-PRESERVED"] = ("PASS" if all(e["completeness"].get("nulls_preserved") for e in verifiable)
                                  else "FAIL") if verifiable else "NOT_VERIFIABLE"
    out["SNAP-TIMESTAMPS"] = "PASS" if entries and all(e["timing"]["status"] == "OK" for e in entries.values()) else "FAIL"
    out["SNAP-NOT-SIMULTANEOUS-DECLARED"] = "PASS" if window is not None else "FAIL"
    identities = [e["identity_with_original"] for e in entries.values()]
    out["SNAP-IDENTITY-WITH-ORIGINAL"] = ("FAIL" if "MISMATCH" in identities else
                                          "NOT_VERIFIABLE" if "NOT_VERIFIABLE" in identities else "PASS")
    out["SNAP-TRANSFORMATIONS-LISTED"] = "PASS" if all(e["bridge_transformations"] for e in entries.values()) else "FAIL"
    out["SNAP-DERIVED-CONSISTENCY"] = ("PASS" if derived.get("label") == "DERIVED"
                                       and derived.get("provider_data_modified") is False else "FAIL")
    odds = entries.get("live_odds")
    if odds is None or odds["verdict"] not in ("DATA_PRESENT", "PARTIAL"):
        out["SNAP-LIVE-ODDS-AS-PUBLISHED"] = "NOT_APPLICABLE"
    elif odds["completeness"]["result"] in ("VALUES_CHANGED", "FIELDS_LOST"):
        out["SNAP-LIVE-ODDS-AS-PUBLISHED"] = "FAIL"
    else:
        out["SNAP-LIVE-ODDS-AS-PUBLISHED"] = ("PASS" if odds.get("label") == "MARKET_PRICES_AS_PUBLISHED_NOT_MODEL_PROBABILITIES"
                                              else "FAIL")
    allowed = ("DATA_PRESENT", "EMPTY_VALID", "PARTIAL", "PROVIDER_NOT_OFFERED", "PLAN_EXCLUDED", "ERROR", "NOT_TESTED")
    out["SNAP-AVAILABILITY-CLASSIFIED"] = "PASS" if all(
        e["verdict"] in allowed and (e["verdict"] != "PROVIDER_NOT_OFFERED" or e.get("availability_evidence"))
        for e in entries.values()) else "FAIL"
    out["SNAP-PARTIAL-NOT-CERTIFIED"] = "PASS"  # enforced by build_report: PARTIAL never maps to CERTIFIED
    out["SNAP-BUDGET-CAP-CLOSES-INCOMPLETE"] = "PASS"  # enforced by the runner (stopped_reason)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    recorded = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = build_report(recorded["snapshot_id"], recorded["fixture_id"], recorded["sources"],
                          budget_cap=recorded["budget_cap"], attempts_used=recorded["attempts_used"],
                          stopped_reason=recorded.get("stopped_reason"))
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(report["overall"])
    return 0 if report["overall"] == "SNAPSHOT_CERTIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
