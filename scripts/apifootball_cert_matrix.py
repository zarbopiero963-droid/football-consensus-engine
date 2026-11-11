#!/usr/bin/env python3
"""Offline certification matrix for API-FOOTBALL v3 (CLAUDE.md §2.3, §2.4, §4).

Built ONLY from the repository's own sources:
  * contracts:  bridge/api_football/contracts.py (ENDPOINTS, validate(), fixtures_combination());
  * MCP schema: bridge/api_football/tools.register_tools() on the real MCP SDK server class, the same
                function bridge/server.py calls (no env, no client, no network);
  * offline tests: static AST/regex scan of tests/test_*.py and tests/integration/test_*.py;
  * live evidence: artefacts already committed under evidence/api_football and
                tests/fixtures/apifootball_cert_20261111 (cited per row);
  * documentation: the snapshot docs/api_football/official_parameter_matrix_v3.9.3.json.

No provider call, no quota, no MCP/Render/database call. Output is deterministic (no clock, no
host data): docs/certification/apifootball/matrix.json and matrix.csv.

Usage:
  python scripts/apifootball_cert_matrix.py            # (re)write the two files
  python scripts/apifootball_cert_matrix.py --check    # exit 1 if the committed files differ
  python scripts/apifootball_cert_matrix.py --gate-offline  # PR gate: structure, schema, no invented params, --check
  python scripts/apifootball_cert_matrix.py --gate-live     # informative: full live certification (FAIL expected today)
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "certification" / "apifootball"
OUT_JSON = OUT_DIR / "matrix.json"
OUT_CSV = OUT_DIR / "matrix.csv"
DOCS_SNAPSHOT = ROOT / "docs" / "api_football" / "official_parameter_matrix_v3.9.3.json"
ROUTE_INVENTORY = ROOT / "evidence" / "api_football" / "route_inventory_40211464376.b64"
CAPABILITY_VERDICT = ROOT / "evidence" / "api_football" / "capability_run_41243344579" / "five_point_verdict.json"
LIVE_DELTA = ROOT / "evidence" / "api_football" / "capability_run_41240887865" / "live_delta.json"
CERT_20261111 = ROOT / "tests" / "fixtures" / "apifootball_cert_20261111"
SELF_TEST = "tests/test_apifootball_cert_matrix.py"

SCHEMA = "apifootball-cert-matrix/v1"
# CLAUDE.md §4 vocabulary plus two provider-availability verdicts (owner condition E, 2026-11-11): the
# provider does not offer the data, or the account's plan excludes it. Not an MCP defect; documented
# with evidence they do not block the live gate.
VERDICTS = ("HTTP_200", "DATA_PRESENT", "EMPTY_VALID", "PARTIAL", "PROVIDER_NOT_OFFERED", "PLAN_EXCLUDED",
            "ERROR", "NOT_TESTED")
PROVIDER_LIMITATION_VERDICTS = frozenset({"PROVIDER_NOT_OFFERED", "PLAN_EXCLUDED"})
LIVE_REQUIREMENTS = ("MANDATORY", "DATA_DEPENDENT", "OFFLINE_ONLY")
GATE_STATUSES = ("PASS", "REUSED_EQUIVALENT", "PROVIDER_LIMITATION", "FAIL", "BLOCKED", "NOT_TESTED", "STALE",
                 "NOT_APPLICABLE")
GATE_FAILING = frozenset({"FAIL", "BLOCKED", "NOT_TESTED", "STALE"})

# The official page was requested once on 2026-11-11 (read-only): HTTP 403, Cloudflare challenge page.
# Nothing was bypassed, so no row can be MATCH against today's official docs.
DOCS_LIVE_FETCH = {
    "url": "https://www.api-football.com/documentation-v3",
    "attempted_on_utc": "2026-11-11",
    "outcome": "HTTP_403_CLOUDFLARE_CHALLENGE_NOT_BYPASSED",
    "consequence": "docs_status=DOCS_UNVERIFIED on every row; docs_snapshot_status compares the "
                   "repository snapshot (retrieved 2026-11-01, no raw capture or hash stored)",
}

COLUMNS = (
    "row_id", "kind", "tool", "route", "item", "mcp_name", "mcp_exposed", "mcp_schema_type",
    "mcp_required", "contract_required", "contract_type", "constraints", "contract_status",
    "docs_snapshot_status", "docs_snapshot_type", "docs_snapshot_required", "docs_snapshot_constraints",
    "docs_status",
    "offline_status", "offline_tests", "live_evidence", "live_evidence_source",
    "live_evidence_scope", "source_sha", "evidence_date", "evidence_ref", "changes_since", "reuse_status",
    "reuse_reason", "reuse_proof", "live_requirement", "verdict", "gate_status", "live_calls_estimate", "notes",
)

_VERDICT_RANK = {"DATA_PRESENT": 0, "EMPTY_VALID": 1, "HTTP_200": 2, "PARTIAL": 3, "PLAN_EXCLUDED": 4,
                 "PROVIDER_NOT_OFFERED": 5, "ERROR": 6, "NOT_TESTED": 7}
_MCP_ALIASES = {"from": "from_date", "to": "to_date"}
# Routes whose data exists only in a time window (pre-match odds, in-play odds).
_DATA_DEPENDENT_ROUTES = frozenset({"/odds", "/odds/live"})
_DATA_DEPENDENT_PARAMS = frozenset({("apifootball_fixtures", "live")})
LOCAL_TOOLS = ("apifootball_health", "apifootball_quota_report")


# ---------------------------------------------------------------------------- live evidence ledger
# Scopes. None of them certifies the CURRENT bridge code (CLAUDE.md §7: no false success):
#   RAW_HTTPX_SCRIPT  - direct httpx script outside the MCP tools and the PostgreSQL coordinator
#                       (gap G1 in docs/certification/areas/apifootball.md), before 218f9b9;
#   MCP_RENDER_848c007 - real MCP client -> Render on 848c007; bridge/api_football changed after it
#                       (e155193, contracts.py/tools.py), so the evidence is STALE for current main.
_STALE_SCOPES = frozenset({"RAW_HTTPX_SCRIPT_2026-10-25", "RAW_HTTPX_SCRIPT_2026-10-31", "MCP_RENDER_848c007_2026-11-11"})
_RAW_SCOPES = frozenset({"RAW_HTTPX_SCRIPT_2026-10-25", "RAW_HTTPX_SCRIPT_2026-10-31"})

# ---- AF-01 (owner decision 2026-11-11): selective reuse of the 848c007 MCP evidence ----------
# Reuse is allowed only for a row whose route was not modified by e155193 (#193) and whose shared
# dependencies did not change in a way that can alter the recorded behaviour. Equivalence is shown
# deterministically by tests/test_apifootball_cert_reuse_equivalence.py:
#   (1) `git diff --name-only 848c007 HEAD` over the API-FOOTBALL code path equals CODE_PATH_CHANGES
#       (skipped only when 848c007 is not in the local history, e.g. a shallow clone);
#   (2) the recorded provider envelope replayed through the CURRENT bridge (real resilience client and
#       transport over httpx.MockTransport) yields the recorded MCP reply on every recorded field.
# The 2026-10 evidence (direct httpx outside MCP and the coordinator) is never reusable.
REUSE_SOURCE_SHA = "848c007"
REUSE_EVIDENCE_DATE = "2026-11-11"
CODE_PATH = ("bridge/api_football", "bridge/server.py", "bridge/db_identity.py", "requirements.txt")
CODE_PATH_CHANGES = ("bridge/api_football/contracts.py", "bridge/api_football/stream_scan.py",
                     "bridge/api_football/tools.py", "bridge/server.py")
CHANGES_SINCE = ("e155193 (#193): bridge/api_football/contracts.py, stream_scan.py, tools.py; "
                 "#198 (squash a46ba72 on main): bridge/server.py healthz fields and OAuth-throttle startup "
                 "logs only (no API-FOOTBALL tool path); 6615acb, 6c69521, d09571f, f666cc1 touch no file "
                 "of the API-FOOTBALL code path")
_PROOF = "tests/test_apifootball_cert_reuse_equivalence.py"
_SAME_REPLY = (" Proof: the recorded envelope replayed through current main gives the recorded MCP reply "
               "(status, parameters, pagination, pagination_state, warnings, network_calls_made).")
_REUSE = {
    ("apifootball_player_profiles", "PARAMETER", "page"): ("REUSED_EQUIVALENT",
        "Route /players/profiles not modified by e155193 (contract entry unchanged; contracts.py diff only "
        "touches /fixtures). tools.py: pagination_state changes only for rows == 0 and page > 1 (A09 has "
        "rows); success warning text unchanged. stream_scan.py: only replies above the read cap (A09 was a "
        "plain REAL_API_RESPONSE)." + _SAME_REPLY, _PROOF + "::test_a09_replay_equals_recorded_reply"),
    ("apifootball_player_profiles", "BEHAVIOUR", "ROUTE_CALL"): ("REUSED_EQUIVALENT",
        "Same case and reasoning as player_profiles.page (A09)." + _SAME_REPLY,
        _PROOF + "::test_a09_replay_equals_recorded_reply"),
    ("apifootball_player_profiles", "BEHAVIOUR", "PAGINATION"): ("STALE",
        "e155193 changed pagination_state (EMPTY_PAGE) on this very route: pagination behaviour as a whole "
        "must be re-tested (page 2 and a page beyond the total).",
        _PROOF + "::test_a10_replay_differs_from_recorded_reply"),
    ("apifootball_player_profiles", "BEHAVIOUR", "EMPTY_VALID"): ("STALE",
        "A10 recorded LAST_PAGE; current code returns EMPTY_PAGE for the same input (behaviour changed).",
        _PROOF + "::test_a10_replay_differs_from_recorded_reply"),
    ("apifootball_odds", "PARAMETER", "fixture"): ("REUSED_EQUIVALENT",
        "Route /odds not modified by e155193 (contract entry unchanged). tools.py changes on this path: "
        "PARTIAL manifest warning only for oversized replies (B06f was a plain REAL_API_RESPONSE under the "
        "256 KiB cap) and pagination_state only for empty pages > 1 (B06f is page 1 with rows). "
        "stream_scan.py: only replies above the read cap." + _SAME_REPLY,
        _PROOF + "::test_b06f_replay_equals_recorded_reply"),
    ("apifootball_odds", "BEHAVIOUR", "ROUTE_CALL"): ("REUSED_EQUIVALENT",
        "Same case and reasoning as odds.fixture (B06f)." + _SAME_REPLY,
        _PROOF + "::test_b06f_replay_equals_recorded_reply"),
    ("apifootball_fixtures", "PARAMETER", "team"): ("STALE",
        "Route /fixtures modified by e155193 (fixtures_combination: PROVIDER_REJECTED_OBSERVED).", ""),
    ("apifootball_fixtures", "PARAMETER", "season"): ("STALE",
        "Route /fixtures modified by e155193 (fixtures_combination: PROVIDER_REJECTED_OBSERVED).", ""),
    ("apifootball_fixtures", "BEHAVIOUR", "ROUTE_CALL"): ("STALE",
        "Route /fixtures modified by e155193 (fixtures_combination: PROVIDER_REJECTED_OBSERVED).", ""),
    ("apifootball_fixtures", "RULE", "F-REJECTED-season+venue"): ("STALE",
        "The observation is now a local refusal (0 provider calls) added by e155193; no live re-test needed.", ""),
}

# 2026-11-11 real certification: cases kept as fixtures in the repo (verbatim/trimmed MCP replies).
_CERT_CASES = (
    # (case, file, tool, item kind, items, verdict)
    ("A03", "a03_fixtures_team33_season2026.json", "apifootball_fixtures", "PARAMETER", ("team", "season"), "DATA_PRESENT"),
    ("A03", "a03_fixtures_team33_season2026.json", "apifootball_fixtures", "BEHAVIOUR", ("ROUTE_CALL",), "DATA_PRESENT"),
    ("A04", "a04_fixtures_venue556_season2026_declared_errors.json", "apifootball_fixtures", "RULE",
     ("F-REJECTED-season+venue",), "ERROR"),
    ("A09", "a09_player_profiles_page101.json", "apifootball_player_profiles", "PARAMETER", ("page",), "DATA_PRESENT"),
    ("A09", "a09_player_profiles_page101.json", "apifootball_player_profiles", "BEHAVIOUR", ("PAGINATION", "ROUTE_CALL"), "DATA_PRESENT"),
    ("A10", "a10_player_profiles_page2771_empty.json", "apifootball_player_profiles", "BEHAVIOUR", ("PAGINATION", "EMPTY_VALID"), "EMPTY_VALID"),
    ("B06f", "b06f_odds_fixture1557464_two_bookmakers.json", "apifootball_odds", "PARAMETER", ("fixture",), "DATA_PRESENT"),
    ("B06f", "b06f_odds_fixture1557464_two_bookmakers.json", "apifootball_odds", "BEHAVIOUR", ("ROUTE_CALL",), "DATA_PRESENT"),
)
_INVENTORY_OUTCOME = {
    "PASS_DATA": "DATA_PRESENT",
    "PASS_EMPTY_NOT_AVAILABILITY": "EMPTY_VALID",
    "BLOCKED_MISSING_REAL_PREREQUISITES": "BLOCKED",
}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _route_inventory() -> dict[str, dict[str, Any]]:
    raw = base64.b64decode(ROUTE_INVENTORY.read_bytes())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        records = json.loads(archive.read("route_inventory.json"))["records"]
    return {r["path"]: r for r in records}


def live_ledger(endpoints: Mapping[str, Any]) -> dict[tuple[str, str, str], list[dict[str, str]]]:
    """(tool, kind, item) -> evidence entries; read from committed artefacts only."""
    ledger: dict[tuple[str, str, str], list[dict[str, str]]] = {}

    def add(tool: str, kind: str, item: str, verdict: str, source: str, scope: str) -> None:
        entry = {"verdict": verdict, "source": source, "scope": scope}
        if scope in _RAW_SCOPES:
            entry["reuse_status"] = "NOT_REUSABLE"
            entry["reuse_reason"] = "direct httpx script outside MCP and the PostgreSQL coordinator (G1)"
        elif scope == "MCP_RENDER_848c007_2026-11-11":
            status, reason, proof = _REUSE[(tool, kind, item)]  # KeyError: an undecided row
            entry.update(reuse_status=status, reuse_reason=reason, reuse_proof=proof,
                         source_sha=REUSE_SOURCE_SHA, evidence_date=REUSE_EVIDENCE_DATE,
                         evidence_ref=source.split(" (case")[0] + "#observed_mcp_reply",
                         changes_since=CHANGES_SINCE)
        ledger.setdefault((tool, kind, item), []).append(entry)

    inventory = _route_inventory()
    src = _rel(ROUTE_INVENTORY) + " (run 40211464376)"
    for tool, endpoint in sorted(endpoints.items()):
        if tool == "apifootball_coverage_report":
            continue  # same path as /leagues but the inventory did not record the parameters used
        record = inventory.get(endpoint.path)
        if record is None:
            continue
        verdict = _INVENTORY_OUTCOME.get(record["outcome"], "ERROR")
        add(tool, "BEHAVIOUR", "ROUTE_CALL", verdict,
            f"{src} path={endpoint.path} outcome={record['outcome']} results={record.get('results')}",
            "RAW_HTTPX_SCRIPT_2026-10-25")

    verdict = json.loads(CAPABILITY_VERDICT.read_text(encoding="utf-8"))
    markets = verdict.get("markets", {})
    if isinstance(markets.get("prematch_values_observed"), int) and markets["prematch_values_observed"] > 0:
        add("apifootball_odds", "BEHAVIOUR", "ROUTE_CALL", "DATA_PRESENT",
            f"{_rel(CAPABILITY_VERDICT)} markets.prematch_values_observed={markets['prematch_values_observed']}",
            "RAW_HTTPX_SCRIPT_2026-10-31")
    if isinstance(markets.get("live_values_observed"), int) and markets["live_values_observed"] > 0:
        add("apifootball_live_odds", "BEHAVIOUR", "ROUTE_CALL", "DATA_PRESENT",
            f"{_rel(CAPABILITY_VERDICT)} markets.live_values_observed={markets['live_values_observed']}",
            "RAW_HTTPX_SCRIPT_2026-10-31")
    for entry in json.loads(LIVE_DELTA.read_text(encoding="utf-8")):
        if entry.get("path") == "/odds/live" and entry.get("fixture"):
            add("apifootball_live_odds", "PARAMETER", "fixture", "HTTP_200",
                f"{_rel(LIVE_DELTA)} fixture={entry['fixture']} status={entry.get('status')}",
                "RAW_HTTPX_SCRIPT_2026-10-31")

    for case, name, tool, kind, items, verdict in _CERT_CASES:
        path = CERT_20261111 / name
        if not path.is_file():
            continue
        for item in items:
            add(tool, kind, item, verdict, f"{_rel(path)} (case {case})", "MCP_RENDER_848c007_2026-11-11")
    return ledger


# ---------------------------------------------------------------------------- contracts
def _contract_type(tool: str, field: str) -> tuple[str, str]:
    from bridge.api_football import contracts as c

    if tool == "apifootball_live_bets" and field == "id":
        return "decimal_string", "string of 1..10 decimal digits"
    if field in c._POSITIVE:
        bits = ["integer 1..2000000000"]
        if field == "season":
            bits.append("1990..2100")
        if field == "page":
            bits.append(f"<= MAX_PAGE ({c.MAX_PAGE}, transport paging bound)")
        if field in ("last", "next"):
            limit = c.FIXTURE_LAST_NEXT_MAX if tool == "apifootball_fixtures" else 100
            if tool == "apifootball_leagues":
                limit = 99
            bits.append(f"<= {limit}")
        return "integer", "; ".join(bits)
    if field in c._BOOL_FIELDS:
        return "boolean", "bool sent as 'true'/'false'"
    if field == "ids" or field in c._ID_LISTS:
        return "id_list", "'id-id-...' 1..20 positive ids"
    if field == "h2h":
        return "h2h", "'id-id' exactly two positive ids"
    if field in c._DATE_FIELDS:
        return "date", "YYYY-MM-DD (ISO date)"
    if field == "round":
        return "text", "1..128 chars, no control characters"
    if field in c._TEXT:
        bits = ["1..128 chars, no control characters"]
        if field == "timezone":
            return "timezone", "IANA zone (pinned tzdata); provider /timezone catalog is final authority"
        if field == "live":
            return "live_filter", f"'all' or 'id-id' without duplicates; max ids {c.LIVE_IDS_MAXIMUM}"
        if field == "search":
            bits.append(f"min length {c._MIN_SEARCH.get(tool, 1)}")
        if tool == "apifootball_leagues" and field == "type":
            bits.append("enum league|cup")
        if tool == "apifootball_leagues" and field == "code":
            bits.append("2..6 chars")
        return "text", "; ".join(bits)
    return "UNVERIFIED", "PARAMETER_TYPE_UNVERIFIED"


def _name_targets(node: ast.AST, tools: frozenset[str]) -> set[str]:
    """Tool names a validate() condition compares `name` against."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name) and sub.left.id == "name":
            for comp in sub.comparators:
                for const in ast.walk(comp):
                    if isinstance(const, ast.Constant) and const.value in tools:
                        found.add(const.value)
    return found


def _raised_codes(nodes: Iterable[ast.AST]) -> list[str]:
    codes: list[str] = []
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call) and sub.exc.args:
                arg = sub.exc.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value not in codes:
                    codes.append(arg.value)
    return codes


def combination_rules(endpoints: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """tool -> [(rule id, contract status)] extracted from contracts.py source (no hand list)."""
    from bridge.api_football import contracts as c

    tools = frozenset(endpoints)
    tree = ast.parse((ROOT / "bridge" / "api_football" / "contracts.py").read_text(encoding="utf-8"))
    validate_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "validate")
    rules: dict[str, list[tuple[str, str]]] = {t: [] for t in tools}
    for node in ast.walk(validate_fn):
        if isinstance(node, ast.If):
            targets = _name_targets(node.test, tools)
            if not targets:
                continue
            for code in _raised_codes(node.body):
                for tool in targets:
                    if (code, "MCP_REJECTED") not in rules[tool]:
                        rules[tool].append((code, "MCP_REJECTED"))
    for tool, endpoint in endpoints.items():
        if endpoint.required:
            rules[tool].append(("REQUIRED_PARAMETER_MISSING", "MCP_REJECTED"))
        if "search" in endpoint.fields and c._MIN_SEARCH.get(tool, 1) > 1:
            rules[tool].append(("SEARCH_TOO_SHORT", "MCP_REJECTED"))
    combo_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fixtures_combination")
    for code in _raised_codes(combo_fn.body):
        if code != "PAGE_OR_BATCH_TOO_LARGE" and (code, "MCP_REJECTED") not in rules["apifootball_fixtures"]:
            rules["apifootball_fixtures"].append((code, "MCP_REJECTED"))
    for rule, _keys in c._FIXTURE_DOCUMENTED:
        rules["apifootball_fixtures"].append((rule, c.DOCUMENTED_VALID))
    for _keys, evidence in c._FIXTURE_PROVIDER_REJECTED:
        rules["apifootball_fixtures"].append((evidence["rule"], c.PROVIDER_REJECTED_OBSERVED))
    rules["apifootball_fixtures"].append(("F-COMBINATION-*", c.DOCS_UNVERIFIED))
    return {t: sorted(set(v)) for t, v in rules.items()}


# ---------------------------------------------------------------------------- MCP schema
def mcp_schemas() -> dict[str, dict[str, Any]]:
    """inputSchema of every apifootball_* tool, from the real MCP SDK server class (offline)."""
    import asyncio

    from mcp.server import MCPServer

    from bridge.api_football.config import Settings
    from bridge.api_football.tools import register_tools

    server_src = (ROOT / "bridge" / "server.py").read_text(encoding="utf-8")
    if "return register_tools(registry, settings, client=client)" not in server_src:
        raise SystemExit("bridge/server.py no longer registers API-FOOTBALL through register_tools()")

    async def collect() -> dict[str, dict[str, Any]]:
        instance = MCPServer("apifootball-cert-matrix")
        register_tools(instance, Settings(enabled=True, api_key=""))
        listing = await instance.list_tools()
        return {tool.name: tool.input_schema for tool in listing}

    return asyncio.run(collect())


def _schema_type(prop: Mapping[str, Any]) -> str:
    if "type" in prop:
        return str(prop["type"])
    kinds = sorted({str(x.get("type")) for x in prop.get("anyOf", []) if isinstance(x, Mapping)})
    return "|".join(kinds) if kinds else "UNSPECIFIED"


# ---------------------------------------------------------------------------- offline test scan
class TestIndex:
    """Each test function (decorators included) with its source text; static, never executed."""

    def __init__(self, files: Iterable[Path]):
        self.units: list[tuple[str, str]] = []
        for path in sorted(files):
            rel = _rel(path)
            if rel == SELF_TEST:
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            tree = ast.parse(text)
            # Module-level tables (case lists, parametrize sources) referenced by a test are part of it.
            tables: dict[str, str] = {}
            for node in tree.body:
                targets = node.targets if isinstance(node, ast.Assign) else (
                    [node.target] if isinstance(node, ast.AnnAssign) else [])
                for target in targets:
                    if isinstance(target, ast.Name):
                        tables[target.id] = "\n".join(lines[node.lineno - 1:node.end_lineno])

            def visit(body: list[ast.stmt], prefix: str) -> None:
                for node in body:
                    if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                        visit(node.body, prefix + node.name + "::")
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                        src = "\n".join(lines[start - 1:node.end_lineno])
                        used = sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in tables}
                                      | {n.id for d in node.decorator_list for n in ast.walk(d)
                                         if isinstance(n, ast.Name) and n.id in tables})
                        src = "\n".join([src] + [tables[name] for name in used])
                        self.units.append((f"{rel}::{prefix}{node.name}", src))

            visit(tree.body, "")
        self._by_tool: dict[str, list[tuple[str, str]]] = {}

    def mentioning(self, tool: str) -> list[tuple[str, str]]:
        if tool not in self._by_tool:
            pattern = re.compile(r"\b" + re.escape(tool) + r"\b")
            self._by_tool[tool] = [u for u in self.units if pattern.search(u[1])]
        return self._by_tool[tool]

    def find(self, tool: str | None, patterns: Iterable[str], files: str | None = None) -> list[str]:
        compiled = [re.compile(p) for p in patterns]
        units = self.mentioning(tool) if tool else self.units
        if files:
            units = [u for u in units if re.search(files, u[0].split("::")[0])]
        return sorted(uid for uid, src in units if any(c.search(src) for c in compiled))


def _param_patterns(field: str) -> list[str]:
    names = [field] + ([_MCP_ALIASES[field]] if field in _MCP_ALIASES else [])
    out: list[str] = []
    for name in names:
        out.append(r"""["']""" + re.escape(name) + r"""["']""")
        out.append(r"(?<![\w.])" + re.escape(name) + r"\s*=(?!=)")
    return out


_AF_FILES = r"(apifootball|api_football)"
_BEHAVIOUR_PATTERNS = {
    "ROUTE_CALL": [r"MockTransport|test_mock|MockFootballClient|FakeClient|fake_provider|_call\(|asyncio\.run|ResilientFootballClient"],
    "EMPTY_VALID": [r"""["']results["']\s*:\s*0\b""", r"""["']response["']\s*:\s*\[\s*\]""", r"EMPTY_VALID|EMPTY_PAGE|empty"],
    "NULL_HANDLING": [r"\bnull\b|\bnulls\b|NULL|missing|MISSING"],
    "PAYLOAD_CAP": [r"PARTIAL_\w*MANIFEST|STREAMED_SCAN|TOO_LARGE|262144|256\s*\*\s*1024|_CAP\b|oversized"],
    "PAGINATION": [r"pagination_state|paging|LAST_PAGE|EMPTY_PAGE|has_next"],
}
_GLOBAL_BEHAVIOURS = {
    "ERROR_HTTP_401": [r"\b401\b"],
    "ERROR_HTTP_403": [r"\b403\b"],
    "ERROR_HTTP_429": [r"\b429\b"],
    "ERROR_HTTP_5XX": [r"\b50[0-4]\b|5XX|5xx"],
    "ERROR_TIMEOUT": [r"TimeoutException|ReadTimeout|ConnectTimeout|PROVIDER_NO_REPLY|QUEUE_TIMEOUT"],
    "PROVIDER_DECLARED_ERRORS": [r"PROVIDER_DECLARED_ERRORS"],
    "NULL_PRESERVED": [r"\bnull\b|\bnulls\b|is None|MISSING"],
    "EMPTY_VALID": [r"""["']results["']\s*:\s*0\b|EMPTY_VALID|EMPTY_PAGE"""],
    "PAYLOAD_CAP_256KIB": [r"PARTIAL_\w*MANIFEST|262144|256\s*\*\s*1024|256 KiB"],
}


# ---------------------------------------------------------------------------- rows
def _evidence_fields(entries: list[dict[str, str]] | None, requirement: str, expected_error: bool = False) -> dict[str, str]:
    if requirement == "OFFLINE_ONLY" and not entries:
        return {"live_evidence": "NOT_TESTED", "live_evidence_source": "", "live_evidence_scope": "",
                "verdict": "NOT_TESTED", "gate_status": "NOT_APPLICABLE"}
    if not entries:
        return {"live_evidence": "NOT_TESTED", "live_evidence_source": "", "live_evidence_scope": "",
                "verdict": "NOT_TESTED", "gate_status": "NOT_TESTED"}
    entries = sorted(entries, key=lambda e: (_VERDICT_RANK.get(e["verdict"], 9), e["source"]))
    usable = [e for e in entries if e["verdict"] in _VERDICT_RANK]
    best = usable[0]["verdict"] if usable else "NOT_TESTED"
    reused = [e for e in usable if e.get("reuse_status") == "REUSED_EQUIVALENT"]
    if not usable:
        gate = "BLOCKED"
    elif best == "ERROR" and not expected_error:
        gate = "FAIL"
    elif best in PROVIDER_LIMITATION_VERDICTS and usable[0].get("source"):
        gate = "PROVIDER_LIMITATION"  # documented provider/plan limitation, not an MCP defect
    elif any(e["scope"] not in _STALE_SCOPES for e in usable):
        gate = "PASS"
    elif reused and reused[0]["verdict"] != "ERROR":
        gate = "REUSED_EQUIVALENT"
    elif all(e["scope"] in _STALE_SCOPES for e in usable):
        gate = "STALE"
    else:
        gate = "PASS"
    if requirement == "OFFLINE_ONLY":
        gate = "NOT_APPLICABLE"
    mcp = [e for e in entries if e.get("source_sha")]
    pick = (reused or mcp or [{}])[0]
    reuse = {k: pick.get(k, "") for k in ("source_sha", "evidence_date", "evidence_ref", "changes_since",
                                          "reuse_status", "reuse_reason", "reuse_proof")}
    if not pick:
        reuse["reuse_status"] = "NOT_REUSABLE" if any(e.get("reuse_status") == "NOT_REUSABLE" for e in entries) else ""
        reuse["reuse_reason"] = entries[0].get("reuse_reason", "") if reuse["reuse_status"] else ""
    return {
        **reuse,
        "live_evidence": ";".join(e["verdict"] for e in entries),
        "live_evidence_source": " | ".join(e["source"] for e in entries),
        "live_evidence_scope": ";".join(sorted({e["scope"] for e in entries})),
        "verdict": best,
        "gate_status": gate,
    }


def _row(**values: Any) -> dict[str, str]:
    row = {c: "" for c in COLUMNS}
    for key, value in values.items():
        if key not in row:
            raise KeyError(key)
        if isinstance(value, bool):
            value = "true" if value else "false"
        row[key] = "" if value is None else str(value)
    return row


def build_rows() -> list[dict[str, str]]:
    from bridge.api_football.contracts import (
        DOCS_UNVERIFIED, DOCUMENTED_VALID, ENDPOINTS, PROVIDER_REJECTED_OBSERVED,
    )

    schemas = mcp_schemas()
    docs = json.loads(DOCS_SNAPSHOT.read_text(encoding="utf-8"))
    doc_params = {e["path"]: {p["name"]: p for p in e["query_parameters"]} for e in docs["endpoints"]}
    route_fields: dict[str, set[str]] = {}
    for endpoint in ENDPOINTS.values():
        route_fields.setdefault(endpoint.path, set()).update(endpoint.fields)
    index = TestIndex(list((ROOT / "tests").glob("test_*.py")) + list((ROOT / "tests" / "integration").glob("test_*.py")))
    ledger = live_ledger(ENDPOINTS)
    rules = combination_rules(ENDPOINTS)
    rows: list[dict[str, str]] = []

    def offline(ids: list[str]) -> dict[str, str]:
        return {"offline_status": "COVERED" if ids else "NOT_COVERED", "offline_tests": ";".join(ids)}

    for tool in sorted(ENDPOINTS):
        endpoint = ENDPOINTS[tool]
        schema = schemas.get(tool, {})
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        route_docs = doc_params.get(endpoint.path)
        route_dd = endpoint.path in _DATA_DEPENDENT_ROUTES

        # route-level behaviours
        behaviours = ["ROUTE_CALL", "EMPTY_VALID", "NULL_HANDLING", "PAYLOAD_CAP"]
        if endpoint.pagination:
            behaviours.append("PAGINATION")
        for behaviour in behaviours:
            if behaviour == "ROUTE_CALL":
                requirement = "DATA_DEPENDENT" if route_dd else "MANDATORY"
                calls = 1
            elif behaviour == "PAGINATION":
                requirement, calls = ("DATA_DEPENDENT" if route_dd else "MANDATORY"), 2
            else:
                requirement, calls = "DATA_DEPENDENT", 1
            ids = index.find(tool, _BEHAVIOUR_PATTERNS[behaviour])
            ev = _evidence_fields(ledger.get((tool, "BEHAVIOUR", behaviour)), requirement)
            rows.append(_row(
                row_id=f"{tool}|BEHAVIOUR|{behaviour}", kind="BEHAVIOUR", tool=tool, route=endpoint.path,
                item=behaviour, contract_status=endpoint.confidence,
                docs_snapshot_status="MATCH" if route_docs is not None else "DOCS_UNVERIFIED",
                docs_status="DOCS_UNVERIFIED", live_requirement=requirement,
                live_calls_estimate=calls if ev["gate_status"] in GATE_FAILING else 0,
                notes=("path absent from the docs snapshot (38 football paths)" if route_docs is None else ""),
                **offline(ids), **ev))

        # parameters
        for field in endpoint.fields:
            mcp_name = _MCP_ALIASES.get(field, field)
            ctype, constraints = _contract_type(tool, field)
            doc = (route_docs or {}).get(field)
            if route_docs is None:
                snap, snap_type, snap_req = "DOCS_UNVERIFIED", "", ""
                snap_constraints = ""
            elif doc is None:
                snap, snap_type, snap_req = "MISSING_IN_DOCS", "", ""
                snap_constraints = ""
            else:
                snap, snap_type, snap_req = "MATCH", doc.get("type", ""), bool(doc.get("required"))
                snap_constraints = doc.get("constraints_and_description", "")
            notes = []
            if doc is not None:
                if (doc.get("type") == "integer") != (ctype == "integer"):
                    notes.append(f"docs type {doc.get('type')} vs contract {ctype} (encoding, not a new parameter)")
                if bool(doc.get("required")) != (field in endpoint.required) and tool != "apifootball_coverage_report":
                    notes.append("required flag differs from docs snapshot")
                if field == "search":
                    from bridge.api_football.contracts import _MIN_SEARCH
                    found = re.search(r"(>=|=)\s*(\d+)\s*characters", doc.get("constraints_and_description", ""))
                    local_min = _MIN_SEARCH.get(tool, 1)
                    if found and int(found.group(2)) != local_min:
                        notes.append(f"CONSTRAINT_DRIFT: docs snapshot '{found.group(1)} {found.group(2)} characters' "
                                     f"vs contract min length {local_min} (not changed here)")
            if tool == "apifootball_live_odds" or (tool, field) in _DATA_DEPENDENT_PARAMS or route_dd:
                requirement = "DATA_DEPENDENT"
            else:
                requirement = "MANDATORY"
            ids = index.find(tool, _param_patterns(field))
            ev = _evidence_fields(ledger.get((tool, "PARAMETER", field)), requirement)
            rows.append(_row(
                row_id=f"{tool}|PARAMETER|{field}", kind="PARAMETER", tool=tool, route=endpoint.path, item=field,
                mcp_name=mcp_name, mcp_exposed=mcp_name in props,
                mcp_schema_type=_schema_type(props.get(mcp_name, {})) if mcp_name in props else "",
                mcp_required=mcp_name in required, contract_required=field in endpoint.required,
                contract_type=ctype, constraints=constraints, contract_status=endpoint.confidence,
                docs_snapshot_status=snap, docs_snapshot_type=snap_type, docs_snapshot_required=snap_req,
                docs_snapshot_constraints=snap_constraints,
                docs_status="DOCS_UNVERIFIED", live_requirement=requirement,
                live_calls_estimate=1 if ev["gate_status"] in GATE_FAILING else 0,
                notes="; ".join(notes), **offline(ids), **ev))

        # MCP schema parameters with no contract field (must be none)
        contract_names = {_MCP_ALIASES.get(f, f) for f in endpoint.fields}
        for extra in sorted(set(props) - contract_names):
            rows.append(_row(
                row_id=f"{tool}|PARAMETER|MCP_ONLY:{extra}", kind="PARAMETER", tool=tool, route=endpoint.path,
                item=extra, mcp_name=extra, mcp_exposed=True, contract_status="NOT_IN_CONTRACT",
                docs_snapshot_status="DOCS_UNVERIFIED", docs_status="DOCS_UNVERIFIED",
                live_requirement="OFFLINE_ONLY", offline_status="NOT_COVERED", verdict="NOT_TESTED",
                gate_status="NOT_APPLICABLE", live_calls_estimate=0,
                notes="MCP schema parameter without contract field"))

        # combination / constraint rules
        for rule, status in rules[tool]:
            if status == DOCUMENTED_VALID:
                requirement = "DATA_DEPENDENT" if rule == "F-LIVE" else "MANDATORY"
            else:
                requirement = "OFFLINE_ONLY"
            if rule == "F-COMBINATION-*":
                ids = index.find(tool, [r"DOCS_UNVERIFIED"])
            elif rule.startswith("F-"):
                ids = index.find(None, [re.escape(rule) + r"(?![\w+-])"], files=_AF_FILES)
                if status == PROVIDER_REJECTED_OBSERVED:
                    ids = sorted(set(ids) | set(index.find(None, [r"FIXTURE_COMBINATION_PROVIDER_REJECTED|PROVIDER_REJECTED_OBSERVED"], files=_AF_FILES)))
            else:
                ids = index.find(tool, [re.escape(rule)])
            ev = _evidence_fields(ledger.get((tool, "RULE", rule)), requirement,
                                  expected_error=status == PROVIDER_REJECTED_OBSERVED)
            notes = {
                DOCS_UNVERIFIED: "any other scoped /fixtures combination: forwarded and labelled; provider is final authority",
                PROVIDER_REJECTED_OBSERVED: "refused locally (0 provider calls); never extended to similar sets",
            }.get(status, "local refusal, fixed code, 0 provider calls" if status == "MCP_REJECTED" else "")
            rows.append(_row(
                row_id=f"{tool}|RULE|{rule}", kind="RULE", tool=tool, route=endpoint.path, item=rule,
                contract_status=status,
                docs_snapshot_status="DOCS_UNVERIFIED" if status in (DOCS_UNVERIFIED, PROVIDER_REJECTED_OBSERVED, "MCP_REJECTED") else "MATCH",
                docs_status="DOCS_UNVERIFIED", live_requirement=requirement,
                live_calls_estimate=1 if ev["gate_status"] in GATE_FAILING else 0,
                notes=notes, **offline(ids), **ev))


    # parameters documented in the snapshot that no contract exposes (route-level union)
    for path in sorted(doc_params):
        for field in sorted(set(doc_params[path]) - route_fields.get(path, set())):
            rows.append(_row(
                row_id=f"-|PARAMETER|{path}:{field}", kind="PARAMETER", tool="", route=path, item=field,
                contract_status="NOT_IN_CONTRACT", docs_snapshot_status="MISSING_IN_CONTRACT",
                docs_status="DOCS_UNVERIFIED", live_requirement="OFFLINE_ONLY", offline_status="NOT_COVERED",
                verdict="NOT_TESTED", gate_status="NOT_APPLICABLE", live_calls_estimate=0))

    # local diagnostic tools (no provider route)
    for tool in LOCAL_TOOLS:
        props = schemas.get(tool, {}).get("properties", {})
        ids = index.find(tool, [r"."])
        rows.append(_row(
            row_id=f"{tool}|LOCAL_TOOL|-", kind="LOCAL_TOOL", tool=tool, route="LOCAL", item="-",
            mcp_exposed=tool in schemas, contract_status="LOCAL_DIAGNOSTIC_NO_PROVIDER_CALL",
            docs_snapshot_status="DOCS_UNVERIFIED", docs_status="DOCS_UNVERIFIED",
            live_requirement="OFFLINE_ONLY", verdict="NOT_TESTED", gate_status="NOT_APPLICABLE",
            live_calls_estimate=0, notes=f"{len(props)} schema parameters", **offline(ids)))

    # cross-cutting behaviours (shared transport / resilience / tools layer)
    for behaviour, patterns in _GLOBAL_BEHAVIOURS.items():
        ids = index.find(None, patterns, files=_AF_FILES)
        requirement = "DATA_DEPENDENT" if behaviour in ("NULL_PRESERVED", "EMPTY_VALID", "PAYLOAD_CAP_256KIB") else "OFFLINE_ONLY"
        ev = _evidence_fields(None, requirement)
        rows.append(_row(
            row_id=f"*|BEHAVIOUR|{behaviour}", kind="BEHAVIOUR", tool="*", route="*", item=behaviour,
            contract_status="SHARED_TRANSPORT", docs_snapshot_status="DOCS_UNVERIFIED", docs_status="DOCS_UNVERIFIED",
            live_requirement=requirement, live_calls_estimate=0,
            notes=("must not be provoked live (invalid key, quota burn, or not reproducible on demand); "
                   "covered through httpx.MockTransport" if requirement == "OFFLINE_ONLY"
                   else "observed per route in the route rows"),
            **offline(ids), **ev))

    # LIVE FULL MATCH SNAPSHOT (owner requirement 2026-11-11): one row per fidelity requirement.
    import importlib.util
    spec = importlib.util.spec_from_file_location("apifootball_live_snapshot_m", ROOT / "scripts" / "apifootball_live_snapshot.py")
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)
    for item in snapshot.REQUIREMENTS:
        ids = index.find(None, [re.escape(item) + r"(?![\w-])"], files=r"test_apifootball_live_snapshot")
        rows.append(_row(
            row_id=f"snapshot|SNAPSHOT|{item}", kind="SNAPSHOT", tool="snapshot", route="LIVE_FULL_MATCH_SNAPSHOT",
            item=item, contract_status="SNAPSHOT_REQUIREMENT", docs_snapshot_status="DOCS_UNVERIFIED",
            docs_status="DOCS_UNVERIFIED", live_requirement="DATA_DEPENDENT", verdict="NOT_TESTED",
            gate_status="NOT_TESTED", live_calls_estimate=0,
            notes=("needs one live match; campaign live_full_match_snapshot, ceiling "
                   "8 attempts per snapshot (scripts/apifootball_cert_batch1.py)"), **offline(ids)))

    # unknown MCP tools (an apifootball_* tool with neither contract nor local role)
    for tool in sorted(set(schemas) - set(ENDPOINTS) - set(LOCAL_TOOLS)):
        rows.append(_row(row_id=f"{tool}|LOCAL_TOOL|UNCLASSIFIED", kind="LOCAL_TOOL", tool=tool, route="UNKNOWN",
                         item="-", contract_status="UNCLASSIFIED", docs_snapshot_status="DOCS_UNVERIFIED",
                         docs_status="DOCS_UNVERIFIED", live_requirement="MANDATORY", offline_status="NOT_COVERED",
                         verdict="NOT_TESTED", gate_status="NOT_TESTED", live_calls_estimate=1))

    rows.sort(key=lambda r: r["row_id"])
    ids = [r["row_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate row ids"
    return rows


# ---------------------------------------------------------------------------- summary / gate
def summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    def count(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items()))

    budget: dict[str, dict[str, int]] = {}
    for r in rows:
        calls = int(r["live_calls_estimate"] or 0)
        if not calls:
            continue
        group = budget.setdefault(f"{r['live_requirement']}:{r['kind']}", {"rows": 0, "calls_upper_bound": 0})
        group["rows"] += 1
        group["calls_upper_bound"] += calls
    pending_routes = sorted({r["route"] for r in rows if int(r["live_calls_estimate"] or 0) and r["route"] not in ("*", "LOCAL")})
    return {
        "rows": len(rows),
        "by_kind": count("kind"),
        "offline_status": count("offline_status"),
        "verdict": count("verdict"),
        "gate_status": count("gate_status"),
        "live_requirement": count("live_requirement"),
        "docs_status": count("docs_status"),
        "docs_snapshot_status": count("docs_snapshot_status"),
        "mcp_tools": len({r["tool"] for r in rows if r["tool"].startswith("apifootball_")}),
        "live_budget": {
            "groups": dict(sorted(budget.items())),
            "calls_upper_bound": sum(g["calls_upper_bound"] for g in budget.values()),
            "routes_pending": len(pending_routes),
        },
    }


def gate_offline(rows: list[dict[str, str]], schemas: Mapping[str, Any], files_up_to_date: bool) -> list[str]:
    """Offline PR gate (owner condition D): failures as fixed strings; empty list = PASS."""
    from bridge.api_football.contracts import ENDPOINTS

    failures: list[str] = []
    if not files_up_to_date:
        failures.append("MATRIX_OUT_OF_DATE")
    ids = [r.get("row_id") for r in rows]
    if len(ids) != len(set(ids)):
        failures.append("DUPLICATE_ROW_IDS")
    vocab = {"verdict": VERDICTS, "live_requirement": LIVE_REQUIREMENTS, "gate_status": GATE_STATUSES,
             "offline_status": ("COVERED", "NOT_COVERED")}
    for r in rows:
        if list(r) != list(COLUMNS):
            failures.append(f"BAD_COLUMNS:{r.get('row_id')}")
        for key, allowed in vocab.items():
            if r.get(key) not in allowed:
                failures.append(f"BAD_{key.upper()}:{r.get('row_id')}")
        if (r.get("offline_status") == "COVERED") != bool(r.get("offline_tests")):
            failures.append(f"COVERAGE_WITHOUT_TESTS:{r.get('row_id')}")
        if r.get("contract_status") in ("NOT_IN_CONTRACT", "UNCLASSIFIED"):
            failures.append(f"INVENTED_OR_UNCLASSIFIED:{r.get('row_id')}")
        if r.get("gate_status") == "REUSED_EQUIVALENT" and not (r.get("reuse_proof") and r.get("source_sha")
                                                                 and r.get("changes_since")):
            failures.append(f"REUSE_WITHOUT_PROOF:{r.get('row_id')}")
    by_id = {r.get("row_id"): r for r in rows}
    reverse_alias = {v: k for k, v in _MCP_ALIASES.items()}
    for tool, schema in schemas.items():
        if not tool.startswith("apifootball_"):
            continue
        if not any(r.get("tool") == tool for r in rows):
            failures.append(f"TOOL_MISSING:{tool}")
        if tool in LOCAL_TOOLS:
            continue
        for name in schema.get("properties", {}):
            row = by_id.get(f"{tool}|PARAMETER|{reverse_alias.get(name, name)}")
            if row is None or row.get("mcp_exposed") != "true":
                failures.append(f"SCHEMA_PARAMETER_MISSING:{tool}.{name}")
    for tool, endpoint in ENDPOINTS.items():
        for field in endpoint.fields:
            if f"{tool}|PARAMETER|{field}" not in by_id:
                failures.append(f"CONTRACT_PARAMETER_MISSING:{tool}.{field}")
    return failures


def gate(rows: list[dict[str, str]]) -> tuple[bool, list[dict[str, str]]]:
    """Live gate: False when a MANDATORY row is FAIL / BLOCKED / NOT_TESTED / STALE (CLAUDE.md §4, §7).
    PASS, REUSED_EQUIVALENT and PROVIDER_LIMITATION (documented) do not block."""
    failing = [r for r in rows if r.get("live_requirement") == "MANDATORY" and r.get("gate_status") in GATE_FAILING]
    return (not failing, failing)


# ---------------------------------------------------------------------------- render
def render(rows: list[dict[str, str]]) -> tuple[str, str]:
    from bridge.api_football.contracts import SOURCE, VERSION

    document = {
        "schema": SCHEMA,
        "contract_version": VERSION,
        "contract_source": SOURCE,
        "inputs": {
            "contracts": "bridge/api_football/contracts.py",
            "mcp_schema": "bridge/api_football/tools.py:register_tools (called by bridge/server.py)",
            "tests": "tests/test_*.py, tests/integration/test_*.py (static scan, " + SELF_TEST + " excluded)",
            "docs_snapshot": _rel(DOCS_SNAPSHOT),
            "live_evidence": sorted({_rel(ROUTE_INVENTORY), _rel(CAPABILITY_VERDICT), _rel(LIVE_DELTA), _rel(CERT_20261111)}),
        },
        "docs_live_fetch": DOCS_LIVE_FETCH,
        "vocabulary": {
            "verdict": list(VERDICTS), "live_requirement": list(LIVE_REQUIREMENTS),
            "gate_status": list(GATE_STATUSES), "gate_failing": sorted(GATE_FAILING),
            "offline_status": ["COVERED", "NOT_COVERED"],
            "docs_snapshot_status": ["MATCH", "MISSING_IN_CONTRACT", "MISSING_IN_DOCS", "DOCS_UNVERIFIED"],
        },
        "stale_scopes": sorted(_STALE_SCOPES),
        "summary": summary(rows),
        "columns": list(COLUMNS),
        "rows": rows,
    }
    text_json = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return text_json, buffer.getvalue()


def _print_summary(rows: list[dict[str, str]]) -> None:
    s = summary(rows)
    print(f"rows={s['rows']} mcp_tools={s['mcp_tools']}")
    for key in ("by_kind", "offline_status", "verdict", "gate_status", "live_requirement", "docs_snapshot_status"):
        print(f"{key}: " + ", ".join(f"{k}={v}" for k, v in s[key].items()))
    print(f"live budget (upper bound): {s['live_budget']['calls_upper_bound']} calls over "
          f"{s['live_budget']['routes_pending']} routes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if the committed matrix differs")
    mode.add_argument("--gate-offline", action="store_true", help="PR gate: structure, schema coverage, "
                      "no invented parameter, committed files up to date")
    mode.add_argument("--gate-live", action="store_true", help="informative: fail if a MANDATORY row is not "
                      "certified live (FAIL expected until a live campaign runs)")
    args = parser.parse_args(argv)

    rows = build_rows()
    text_json, text_csv = render(rows)
    stale = [p for p, text in ((OUT_JSON, text_json), (OUT_CSV, text_csv))
             if not p.is_file() or p.read_text(encoding="utf-8") != text]
    if args.gate_offline:
        failures = gate_offline(rows, mcp_schemas(), not stale)
        print(f"GATE_OFFLINE {'PASS' if not failures else 'FAIL'}: rows={len(rows)} failures={len(failures)}")
        for failure in failures:
            print(f"  {failure}")
        return 0 if not failures else 1
    if args.check:
        if stale:
            print("MATRIX_OUT_OF_DATE: " + ", ".join(_rel(p) for p in stale)
                  + " (run python scripts/apifootball_cert_matrix.py)")
            return 1
        print("MATRIX_UP_TO_DATE")
        return 0
    if args.gate_live:
        _print_summary(rows)
        ok, failing = gate(rows)
        by_status: dict[str, int] = {}
        for r in failing:
            by_status[r["gate_status"]] = by_status.get(r["gate_status"], 0) + 1
        print("GATE_LIVE " + ("PASS" if ok else "FAIL") + f": {len(failing)} MANDATORY rows not PASS "
              + json.dumps(dict(sorted(by_status.items()))))
        for r in failing[:20]:
            print(f"  {r['gate_status']:<10} {r['row_id']}")
        if len(failing) > 20:
            print(f"  ... {len(failing) - 20} more (full list: matrix.csv, live_requirement=MANDATORY)")
        return 0 if ok else 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(text_json, encoding="utf-8")
    OUT_CSV.write_text(text_csv, encoding="utf-8")
    _print_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
