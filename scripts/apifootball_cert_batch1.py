#!/usr/bin/env python3
"""AF-05: first real API-FOOTBALL certification batch. PREPARED ONLY: dispatch is NOT authorized.

Plan: docs/certification/apifootball/batch1_plan.json (generated here, deterministic), at most
HARD_CAP = 78 provider HTTP attempts, every attempt counted (first send, retry, timeout,
ambiguous reply).

Budget model (owner conditions A and C, 2026-11-11):
  * the counter is PERSISTENT and SHARED: a campaign ledger in PostgreSQL, keyed by campaign_id;
  * write-ahead: an attempt is reserved (committed) BEFORE the HTTP request leaves, by an httpx
    transport wrapper under the real resilience client. Retries, timeouts and ambiguous replies
    are therefore always counted;
  * a restart or re-run with the same campaign_id continues the SAME budget, never a new one;
  * RetryPolicy(max_attempts=1): one attempt per planned call, no automatic retry;
  * every request also takes a lease of the shared quota coordinator (PostgresQuota, CLAUDE.md §2.4).
  * every request needs the owner authorization variable APIFOOTBALL_CERT_CAMPAIGN_AUTHORIZED=true,
    checked by the transport itself before the reservation.
The campaign ledger is bridge migration 0010 (dedicated tables). PATH A (owner mandate
FCB-OWNER-APIFOOTBALL-PATH-A-20261112): it lives in the DEDICATED database fcb_cert (same Aiven
service), owned by the NOLOGIN role fcb_cert_owner, installed only by scripts/apifootball_cert_path_a.py
from an owner-gated workflow. The runner logs in as fcb_cert_runner with ONE DSN
(APIFOOTBALL_COORDINATOR_DATABASE_URL -> DATABASE_URL): database defaultdb for the shared quota
coordinator, the same credentials with dbname fcb_cert for the ledger. Before any request it refuses
an identity that is not a plain dedicated role (RUNNER_IDENTITY_NOT_DEDICATED, e.g. the administrator)
and a ledger whose owner, guards or counter evidence are not intact (CAMPAIGN_LEDGER_INTEGRITY_FAILED).
Where 0010 is not installed, PostgresCampaignLedger refuses with CAMPAIGN_LEDGER_MIGRATION_REQUIRED.

Usage:
  python scripts/apifootball_cert_batch1.py --write-plan
  python scripts/apifootball_cert_batch1.py --check-plan
  python scripts/apifootball_cert_batch1.py --execute --authorize-live-campaign --campaign-id ID --out DIR
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLAN_PATH = ROOT / "docs" / "certification" / "apifootball" / "batch1_plan.json"
SNAPSHOT_PLAN_PATH = ROOT / "docs" / "certification" / "apifootball" / "live_snapshot_plan.json"
# AF-15: the budget is the owner-APPROVED PLAN's (api_football_cert_plans, bridge 0010), shared by
# every campaign_id of the plan. The plan is identified by the SHA-256 of the file committed on main;
# a plan that is not approved stops the runner before any request (PLAN_NOT_APPROVED).
# The plan budget is enforced by PostgreSQL itself (bridge 0010): a per-plan counter incremented in
# the same transaction as each reservation by a SECURITY DEFINER trigger, whatever the isolation.
SERIALIZATION_RETRIES = 3  # a 40001/40P01 conflict is retried in a NEW transaction, same budget
HARD_CAP = 78  # owner decision 2026-11-11: initial requests + retries + timeouts + ambiguous replies
ATTEMPTS_PER_CALL = 1  # RetryPolicy(max_attempts=1)
CAMPAIGN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
LEDGER_TABLES = ("api_football_cert_plans", "api_football_cert_plan_usage", "api_football_cert_campaigns",
                 "api_football_cert_campaign_attempts", "api_football_cert_campaign_outcomes")  # bridge migration 0010
# A RESERVED attempt is not a SENT request. Each reserved attempt ends in exactly one state:
#   RESPONSE_RECEIVED  the provider answered (HTTP status recorded after the exchange);
#   TRANSPORT_ERROR    the send failed (timeout, reset...): it MAY have reached the provider;
#   RESERVED_UNKNOWN   no outcome recorded: the process stopped after the reservation, before,
#                      during or after the send. It consumes budget, is never retried and never
#                      recovered automatically; the evidence reports it as is.
OUTCOMES = {"HTTP_RESPONSE": "RESPONSE_RECEIVED", "TRANSPORT_ERROR": "TRANSPORT_ERROR"}
RESERVED_UNKNOWN = "RESERVED_UNKNOWN"
# Owner authorization (repository variable passed by the workflow). Checked by the transport before
# EVERY request, whatever the entry point: without it nothing is reserved and nothing is sent.
AUTH_VAR = "APIFOOTBALL_CERT_CAMPAIGN_AUTHORIZED"
# PATH A: the ledger database and the NOLOGIN role that owns it and every ledger object.
LEDGER_DATABASE = "fcb_cert"
LEDGER_OWNER_ROLE = "fcb_cert_owner"
# The connected identity must be a plain role: not the administrator (CREATEROLE/CREATEDB/BYPASSRLS),
# not a superuser, no SET ROLE in effect and member of no role at all (so it can act as nobody else).
RUNNER_IDENTITY_SQL = (
    "SELECT (CURRENT_USER OPERATOR(pg_catalog.=) SESSION_USER AND NOT r.rolsuper AND NOT r.rolcreaterole "
    "AND NOT r.rolcreatedb AND NOT r.rolbypassrls AND NOT r.rolreplication AND NOT EXISTS ("
    "SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member OPERATOR(pg_catalog.=) r.oid)) AS ok "
    "FROM pg_catalog.pg_roles r WHERE r.rolname OPERATOR(pg_catalog.=) CURRENT_USER")
# The ledger as installed: 5 tables owned by the NOLOGIN owner (not the caller), nobody holding SET or
# INHERIT on the owner, the 12 0010 triggers present and ENABLED, the budget trigger SECURITY DEFINER
# owned by the owner with its pinned search_path. Catalog only (readable by every role).
LEDGER_INTEGRITY_SQL = (
    "SELECT (NOT o.rolcanlogin AND o.rolname OPERATOR(pg_catalog.<>) CURRENT_USER "
    "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.roleid OPERATOR(pg_catalog.=) o.oid "
    "AND (m.inherit_option OR m.set_option)) "
    "AND (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace "
    "WHERE n.nspname OPERATOR(pg_catalog.=) %(schema)s AND c.relname OPERATOR(pg_catalog.=) ANY (%(tables)s) "
    "AND c.relkind OPERATOR(pg_catalog.=) 'r' AND c.relowner OPERATOR(pg_catalog.=) o.oid) OPERATOR(pg_catalog.=) 5 "
    "AND (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) t.tgrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace WHERE n.nspname OPERATOR(pg_catalog.=) %(schema)s "
    "AND c.relname OPERATOR(pg_catalog.=) ANY (%(tables)s) AND NOT t.tgisinternal AND t.tgenabled OPERATOR(pg_catalog.=) 'O' "
    "AND t.tgname OPERATOR(pg_catalog.~) '^api_football_cert_') OPERATOR(pg_catalog.=) 12 "
    "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) t.tgrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace WHERE n.nspname OPERATOR(pg_catalog.=) %(schema)s "
    "AND c.relname OPERATOR(pg_catalog.=) ANY (%(tables)s) AND t.tgenabled OPERATOR(pg_catalog.<>) 'O') "
    "AND EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace "
    "WHERE n.nspname OPERATOR(pg_catalog.=) %(schema)s AND p.proname OPERATOR(pg_catalog.=) 'api_football_cert_attempt_guard' "
    "AND p.prosecdef AND p.proowner OPERATOR(pg_catalog.=) o.oid "
    "AND p.proconfig OPERATOR(pg_catalog.=) ARRAY['search_path=pg_catalog, pg_temp']::pg_catalog.text[])) AS ok "
    "FROM pg_catalog.pg_roles o WHERE o.rolname OPERATOR(pg_catalog.=) %(owner)s")


def runner_identity_problem(conn) -> str | None:
    """None when the connection's identity is a dedicated plain role, else a fixed code."""
    row = conn.execute(RUNNER_IDENTITY_SQL).fetchone()
    ok = row is not None and (row["ok"] if isinstance(row, dict) else row[0])
    return None if ok else "RUNNER_IDENTITY_NOT_DEDICATED"

L, S, T = 39, 2025, 33  # Premier League, season 2025 (2025/26, completed), Manchester United


def _b(call: str, *path: Any, take: int | None = None, join: str | None = None, add: int | None = None) -> dict:
    out: dict[str, Any] = {"$bind": call, "path": list(path)}
    if take is not None:
        out["take"] = take
    if join is not None:
        out["join"] = join
    if add is not None:
        out["add"] = add
    return out


FIX = _b("C33", "response", 0, "fixture", "id")
HOME = _b("C33", "response", 0, "teams", "home", "id")
PLAYER = _b("C54", "response", 0, "player", "id")

# (call id, tool, MCP arguments, extra matrix items covered, tags). First call of a tool = ROUTE_CALL.
# The values are public catalogue ids; ids that change with time are taken from earlier replies.
PLAN: tuple[tuple[str, str, dict[str, Any], tuple[str, ...], tuple[str, ...]], ...] = (
    ("C01", "apifootball_status", {}, (), ()),
    ("C02", "apifootball_timezones", {}, (), ()),
    ("C03", "apifootball_seasons", {}, (), ()),
    ("C04", "apifootball_team_countries", {}, (), ()),
    ("C05", "apifootball_countries", {"name": "England"}, (), ()),
    ("C06", "apifootball_countries", {"code": "GB-ENG"}, (), ()),
    ("C07", "apifootball_countries", {"search": "Eng"}, (), ()),
    ("C08", "apifootball_countries", {"search": "En"}, (), ("AF-02_SEARCH_MIN_LENGTH_PROBE",)),
    ("C09", "apifootball_leagues", {"id": L}, (), ()),
    ("C10", "apifootball_leagues", {"country": "England"}, (), ()),
    ("C11", "apifootball_leagues", {"code": "GB-ENG", "type": "league"}, (), ("JOINT_ATTRIBUTION",)),
    ("C12", "apifootball_leagues", {"id": L, "season": S}, (), ()),
    ("C13", "apifootball_leagues", {"team": T, "current": True}, (), ("JOINT_ATTRIBUTION",)),
    ("C14", "apifootball_leagues", {"search": "Premier"}, (), ()),
    ("C15", "apifootball_coverage_report", {"id": L, "season": S}, (), ()),
    ("C16", "apifootball_teams", {"id": T}, (), ()),
    ("C17", "apifootball_teams", {"league": L, "season": S}, (), ()),
    ("C18", "apifootball_teams", {"search": "Manchester"}, (), ()),
    ("C19", "apifootball_teams", {"venue": 556}, (), ()),
    ("C20", "apifootball_team_seasons", {"team": T}, (), ()),
    ("C21", "apifootball_team_statistics", {"league": L, "season": S, "team": T}, (), ()),
    ("C22", "apifootball_team_statistics", {"league": L, "season": S, "team": T, "date": "2026-01-31"}, (), ()),
    ("C23", "apifootball_venues", {"id": 556}, (), ()),
    ("C24", "apifootball_venues", {"city": "Manchester"}, (), ()),
    ("C25", "apifootball_venues", {"search": "Trafford"}, (), ()),
    ("C26", "apifootball_standings", {"league": L, "season": S}, (), ()),
    ("C27", "apifootball_standings", {"team": T, "season": S}, (), ()),
    ("C28", "apifootball_fixture_rounds", {"league": L, "season": S}, (), ()),
    ("C29", "apifootball_fixture_rounds", {"league": L, "season": S, "current": True}, (), ()),
    ("C30", "apifootball_fixture_rounds", {"league": L, "season": S, "dates": True}, (), ()),
    ("C31", "apifootball_fixture_headtohead", {"h2h": "33-34"}, (), ()),
    ("C32", "apifootball_fixture_headtohead", {"h2h": "33-34", "last": 5}, (), ()),
    ("C33", "apifootball_fixtures", {"league": L, "season": S, "from_date": "2026-05-01", "to_date": "2026-05-31"},
     ("F-LEAGUE-SEASON-RANGE",), ()),
    ("C34", "apifootball_fixtures", {"id": FIX}, ("F-ID",), ()),
    ("C35", "apifootball_fixtures", {"ids": _b("C33", "response", "*", "fixture", "id", take=2, join="-")}, ("F-IDS",), ()),
    ("C36", "apifootball_fixtures", {"date": "2026-05-24", "timezone": "Europe/Rome"}, ("F-DATE",), ("JOINT_ATTRIBUTION",)),
    ("C37", "apifootball_fixtures", {"league": L, "season": S}, ("F-LEAGUE-SEASON",), ()),
    ("C38", "apifootball_fixtures", {"team": T, "last": 5}, (), ()),
    ("C39", "apifootball_fixtures", {"team": T, "next": 5}, (), ()),
    ("C40", "apifootball_fixtures", {"league": L, "season": S, "round": "Regular Season - 38"}, (), ()),
    ("C41", "apifootball_fixtures", {"team": T, "season": S, "status": "FT"}, (), ()),
    ("C42", "apifootball_fixtures", {"venue": 556, "from_date": "2026-01-01", "to_date": "2026-05-31"}, (), ()),
    ("C43", "apifootball_fixture_headtohead", {"h2h": "33-34", "league": L, "season": S}, (), ("JOINT_ATTRIBUTION",)),
    ("C44", "apifootball_fixture_statistics", {"fixture": FIX}, (), ()),
    ("C45", "apifootball_fixture_statistics", {"fixture": FIX, "half": True}, (), ()),
    ("C46", "apifootball_fixture_statistics", {"fixture": FIX, "team": HOME}, (), ()),
    ("C47", "apifootball_fixture_events", {"fixture": FIX}, (), ()),
    ("C48", "apifootball_fixture_lineups", {"fixture": FIX}, (), ()),
    ("C49", "apifootball_fixture_lineups", {"fixture": FIX, "team": HOME}, (), ()),
    ("C50", "apifootball_fixture_players", {"fixture": FIX}, (), ()),
    ("C51", "apifootball_predictions", {"fixture": FIX}, (), ()),
    ("C52", "apifootball_injuries", {"fixture": FIX}, (), ()),
    ("C53", "apifootball_injuries", {"team": T, "season": S}, (), ()),
    ("C54", "apifootball_players", {"team": T, "season": S}, (), ()),
    ("C55", "apifootball_players", {"team": T, "season": S, "page": 2}, ("PAGINATION",), ()),
    ("C56", "apifootball_players", {"team": T, "season": S, "page": _b("C54", "paging", "total", add=1)}, ("PAGINATION",), ()),
    ("C57", "apifootball_players", {"id": PLAYER, "season": S}, (), ()),
    ("C58", "apifootball_players", {"team": T, "search": "Fernandes"}, (), ()),
    ("C59", "apifootball_player_profiles", {"player": PLAYER}, (), ()),
    ("C60", "apifootball_player_profiles", {"page": 2}, ("PAGINATION",), ()),
    ("C61", "apifootball_player_profiles", {"page": _b("C60", "paging", "total", add=1)}, ("PAGINATION",), ()),
    ("C62", "apifootball_player_seasons", {"player": PLAYER}, (), ()),
    ("C63", "apifootball_player_squads", {"team": T}, (), ()),
    ("C64", "apifootball_player_teams", {"player": PLAYER}, (), ()),
    ("C65", "apifootball_top_scorers", {"league": L, "season": S}, (), ()),
    ("C66", "apifootball_top_assists", {"league": L, "season": S}, (), ()),
    ("C67", "apifootball_top_yellow_cards", {"league": L, "season": S}, (), ()),
    ("C68", "apifootball_top_red_cards", {"league": L, "season": S}, (), ()),
    ("C69", "apifootball_transfers", {"team": T}, (), ()),
    ("C70", "apifootball_trophies", {"player": PLAYER}, (), ()),
    ("C71", "apifootball_sidelined", {"player": PLAYER}, (), ()),
    ("C72", "apifootball_coaches", {"team": T}, (), ()),
    ("C73", "apifootball_bets", {"id": 1}, (), ()),
    ("C74", "apifootball_bookmakers", {"id": 7}, (), ()),
    ("C75", "apifootball_live_bets", {"search": "Win"}, (), ()),
    ("C76", "apifootball_odds_mapping", {"page": 2}, ("PAGINATION",), ()),
    ("C77", "apifootball_odds_mapping", {"page": _b("C76", "paging", "total", add=1)}, ("PAGINATION",), ()),
    ("C78", "apifootball_fixture_events", {"fixture": FIX, "type": "Goal"}, (), ()),
)
_MCP_TO_CONTRACT = {"from_date": "from", "to_date": "to"}


def _matrix_module():
    spec = importlib.util.spec_from_file_location("apifootball_cert_matrix_b1", ROOT / "scripts" / "apifootball_cert_matrix.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["apifootball_cert_matrix_b1"] = module
    spec.loader.exec_module(module)
    return module


def _placeholder(binding: Mapping[str, Any]) -> Any:
    if binding.get("join"):
        return "-".join(str(i + 1) for i in range(binding.get("take", 2)))
    return 1 + int(binding.get("add", 0))


def _contract_args(args: Mapping[str, Any]) -> dict[str, Any]:
    return {_MCP_TO_CONTRACT.get(k, k): (_placeholder(v) if isinstance(v, dict) else v) for k, v in args.items()}


def build_plan(rows: list[dict[str, str]] | None = None) -> dict[str, Any]:
    from bridge.api_football.contracts import validate

    if rows is None:
        rows = _matrix_module().build_rows()
    by_id = {r["row_id"]: r for r in rows}
    pending = {r["row_id"] for r in rows if r["live_requirement"] == "MANDATORY"
               and r["gate_status"] in ("NOT_TESTED", "STALE", "BLOCKED", "FAIL")}
    seen_tools: set[str] = set()
    pagination_parts: dict[str, int] = {}
    calls = []
    ids = [c[0] for c in PLAN]
    assert len(ids) == len(set(ids)), "duplicate call ids"
    for call_id, tool, args, extra, tags in PLAN:
        validate(tool, _contract_args(args))  # a plan call the contract would refuse is a plan bug
        for value in args.values():
            if isinstance(value, dict):
                assert ids.index(value["$bind"]) < ids.index(call_id), (call_id, "binding to a later call")
        covers = [f"{tool}|PARAMETER|{_MCP_TO_CONTRACT.get(k, k)}" for k in sorted(args)]
        if tool not in seen_tools:
            covers.append(f"{tool}|BEHAVIOUR|ROUTE_CALL")
            seen_tools.add(tool)
        for item in extra:
            if item == "PAGINATION":
                pagination_parts[tool] = pagination_parts.get(tool, 0) + 1
                continue
            covers.append(f"{tool}|RULE|{item}")
        missing = [c for c in covers if c not in by_id]
        assert not missing, (call_id, missing)
        calls.append({"call_id": call_id, "tool": tool, "arguments": args, "max_attempts": ATTEMPTS_PER_CALL,
                      "covers_rows": sorted(covers), "tags": list(tags)})
    for tool, parts in pagination_parts.items():  # page 2 and a page beyond the total: both needed
        if parts >= 2:
            last = [c for c in calls if c["tool"] == tool][-1]
            last["covers_rows"] = sorted(set(last["covers_rows"]) | {f"{tool}|BEHAVIOUR|PAGINATION"})
    total = sum(c["max_attempts"] for c in calls)
    assert total <= HARD_CAP, total
    covered = sorted({r for c in calls for r in c["covers_rows"]})
    covered_pending = sorted(set(covered) & pending)
    plan = {
        "schema": "apifootball-cert-batch1/v1",
        "status": "PREPARED_NOT_AUTHORIZED",
        "hard_cap_attempts": HARD_CAP,
        "max_attempts_total": total,
        "attempt_model": "every HTTP attempt counts (first send, retry, timeout, ambiguous reply); "
                         "RetryPolicy(max_attempts=1); write-ahead reservation in the campaign ledger",
        "binding_rules": "a {'$bind': call, 'path': [...]} argument is read from that call's provider data; "
                         "'take'+'join' joins the first N values; 'add' adds to an integer; an unresolved "
                         "binding SKIPS the call with 0 attempts (rows stay NOT_TESTED)",
        "verdict_rules": {
            "REAL_API_RESPONSE with results > 0": "DATA_PRESENT",
            "REAL_API_RESPONSE with results == 0": "EMPTY_VALID",
            "PARTIAL_*": "PARTIAL",
            "PROVIDER_DECLARED_ERRORS category PLAN_NOT_INCLUDED": "PLAN_EXCLUDED",
            "any other status or error": "ERROR",
            "not sent (budget, binding, ledger)": "NOT_TESTED",
        },
        "af02": {
            "search_min_length": "C08 countries(search='En'): the provider answer decides whether the "
                                 "contract needs the documented minimum (separate PR)",
            "odds_bets_id_type": "no dedicated call: on the wire an integer and a string id are the same "
                                 "query text, so a real call cannot separate them; stays DOCS_UNVERIFIED "
                                 "(bets.id acceptance is covered by C73)",
        },
        "not_in_batch": "DATA_DEPENDENT rows (pre-match/in-play odds, fixtures.live, payload cap, null "
                        "handling) and the MANDATORY rows listed in mandatory_pending_not_covered",
        "summary": {
            "calls": len(calls),
            "rows_covered": len(covered),
            "mandatory_pending_before": len(pending),
            "mandatory_pending_covered": len(covered_pending),
            "mandatory_pending_not_covered": len(pending - set(covered)),
        },
        "mandatory_pending_not_covered": sorted(pending - set(covered)),
        "calls": calls,
    }
    return plan


def render_plan(plan: Mapping[str, Any]) -> str:
    return json.dumps(plan, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def plan_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- campaign ledger
class BudgetExhausted(RuntimeError):
    """Fixed code only."""


class NotAuthorized(RuntimeError):
    """Fixed code only."""


class SerializationRefused(RuntimeError):
    """PostgreSQL cancelled the reservation for a concurrency conflict (SQLSTATE 40001 / 40P01):
    nothing was reserved, nothing is sent. Distinct from CAMPAIGN_BUDGET_EXHAUSTED."""


class LedgerUnavailable(RuntimeError):
    """Fixed code only."""


class CampaignLedger(Protocol):
    async def open(self, campaign_id: str, budget: int, plan_sha: str) -> None: ...
    async def reserve(self, campaign_id: str, call_id: str) -> int: ...  # write-ahead; returns seq
    async def used(self, campaign_id: str) -> int: ...
    async def attempted_calls(self, campaign_id: str) -> set[str]: ...
    async def record_outcome(self, campaign_id: str, seq: int, outcome: str, http_status: int | None) -> None: ...
    async def attempt_states(self, campaign_id: str) -> list[dict[str, Any]]: ...
    async def plan_usage(self, campaign_id: str) -> dict[str, Any]: ...  # {plan_sha256, budget, used}


def state_counts(states: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"RESPONSE_RECEIVED": 0, "TRANSPORT_ERROR": 0, RESERVED_UNKNOWN: 0}
    for item in states:
        counts[item["state"]] += 1
    return counts


class PostgresCampaignLedger:
    """Campaign ledger on bridge migration 0010 (api_football_cert_campaigns / _attempts).

    Fails closed: if the tables are missing it raises CAMPAIGN_LEDGER_MIGRATION_REQUIRED before any
    provider request; it never creates or alters a table. A reservation is one short transaction,
    committed BEFORE the HTTP request leaves, explicitly READ COMMITTED: INSERT of the attempt; the
    0010 SECURITY DEFINER trigger increments the plan counter in the same transaction (used < budget,
    whatever the isolation level) and assigns seq from it. A serialization conflict is
    SERIALIZATION_FAILURE, never CAMPAIGN_BUDGET_EXHAUSTED. Database errors become fixed codes; no
    message, DSN or value is surfaced."""


    def __init__(self, connect=None, schema: str = "public", owner_role: str | None = None):
        if connect is None:  # PATH A: the runner's DSN, database fcb_cert; DSN never logged
            def connect():
                import psycopg
                from psycopg.rows import dict_row

                from app.db import database_url
                return psycopg.connect(database_url(), dbname=LEDGER_DATABASE, row_factory=dict_row)
            owner_role = owner_role or LEDGER_OWNER_ROLE
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("INVALID_SCHEMA")
        if owner_role is not None and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", owner_role):
            raise ValueError("INVALID_OWNER_ROLE")
        self._owner_role = owner_role  # PATH A integrity checks (None: offline/legacy tests only)
        self._connect = connect
        self._schema = schema  # "public" in production; a throwaway schema in integration tests
        self._plans = f"{schema}.api_football_cert_plans"
        self._campaigns = f"{schema}.api_football_cert_campaigns"
        self._attempts = f"{schema}.api_football_cert_campaign_attempts"
        self._outcomes = f"{schema}.api_football_cert_campaign_outcomes"

    @staticmethod
    def _one(cur, key: str, index: int = 0):
        row = cur.fetchone()
        if row is None:
            return None
        return row[key] if isinstance(row, dict) else row[index]

    @staticmethod
    def _read_committed(conn):
        """Review #199 P1: every ledger transaction is BEGIN ISOLATION LEVEL READ COMMITTED, explicitly,
        whatever the role or session default (the 0010 budget trigger refuses any other level)."""
        if getattr(conn, "info", None) is not None:  # a psycopg connection (offline fakes have none)
            import psycopg
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        return conn

    def _require_schema(self, cur) -> None:
        for table in LEDGER_TABLES:
            cur.execute("SELECT pg_catalog.to_regclass(%s) AS t", (f"{self._schema}.{table}",))
            if self._one(cur, "t") is None:
                raise LedgerUnavailable("CAMPAIGN_LEDGER_MIGRATION_REQUIRED")
        if self._owner_role is not None:  # every ledger transaction, before any write
            cur.execute(RUNNER_IDENTITY_SQL)
            if not self._one(cur, "ok"):
                raise LedgerUnavailable("RUNNER_IDENTITY_NOT_DEDICATED")
            cur.execute(LEDGER_INTEGRITY_SQL, {"schema": self._schema, "tables": list(LEDGER_TABLES),
                                               "owner": self._owner_role})
            if not self._one(cur, "ok"):
                raise LedgerUnavailable("CAMPAIGN_LEDGER_INTEGRITY_FAILED")

    def _plan_evidence_consistent(self, cur, plan_sha: str) -> None:
        """PATH A: the committed attempts of the plan carry seqs exactly 1..n, n <= budget. A counter
        reset (only possible as a deliberate owner/administrator act) leaves duplicate seqs: refused."""
        cur.execute(f"SELECT pg_catalog.count(a.seq) AS n, pg_catalog.count(DISTINCT a.seq) AS d, "
                    f"COALESCE(pg_catalog.max(a.seq), 0) AS m, pg_catalog.min(p.budget) AS b "
                    f"FROM {self._campaigns} c JOIN {self._plans} p ON p.plan_sha256 = c.plan_sha256 "
                    f"LEFT JOIN {self._attempts} a ON a.campaign_id = c.campaign_id WHERE c.plan_sha256 = %s",
                    (plan_sha,))
        row = cur.fetchone()
        n, d, m, b = ((row["n"], row["d"], row["m"], row["b"]) if isinstance(row, dict) else tuple(row))
        if not (n == d == m and (b is None or m <= b)):
            raise LedgerUnavailable("CAMPAIGN_LEDGER_INTEGRITY_FAILED")

    def _guarded(self, fn, *args):
        try:
            return fn(*args)
        except (LedgerUnavailable, BudgetExhausted, SerializationRefused, NotAuthorized):
            raise
        except Exception as exc:  # psycopg errors: fixed code only, never the message
            text = type(exc).__name__ + ":" + (getattr(getattr(exc, "diag", None), "message_primary", "") or "")
            if getattr(exc, "sqlstate", None) in ("40001", "40P01"):  # a conflict, NOT an exhausted budget
                raise SerializationRefused("SERIALIZATION_FAILURE") from None
            if "API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED" in text:  # only the counter at the budget
                raise BudgetExhausted("CAMPAIGN_BUDGET_EXHAUSTED") from None
            if "API_FOOTBALL_CERT_PLAN_NOT_APPROVED" in text:
                raise LedgerUnavailable("PLAN_NOT_APPROVED") from None
            raise LedgerUnavailable("CAMPAIGN_LEDGER_UNAVAILABLE") from None

    def _open(self, campaign_id: str, budget: int, plan_sha: str) -> None:
        """Bind campaign_id to an APPROVED plan (AF-15). `budget` is only the runner's own ceiling:
        the budget that counts is the plan's, in the database, shared by all its campaigns."""
        if not 1 <= budget <= HARD_CAP or not re.fullmatch(r"[0-9a-f]{64}", plan_sha or ""):
            raise LedgerUnavailable("CAMPAIGN_BUDGET_OUT_OF_RANGE")
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            cur.execute(f"SELECT budget FROM {self._plans} WHERE plan_sha256=%s", (plan_sha,))
            if self._one(cur, "budget") is None:
                raise LedgerUnavailable("PLAN_NOT_APPROVED")  # before any request
            cur.execute(f"INSERT INTO {self._campaigns} (campaign_id, plan_sha256) "
                        "VALUES (%s, %s) ON CONFLICT (campaign_id) DO NOTHING", (campaign_id, plan_sha))
            cur.execute(f"SELECT plan_sha256 FROM {self._campaigns} WHERE campaign_id=%s", (campaign_id,))
            if self._one(cur, "plan_sha256") != plan_sha:
                raise LedgerUnavailable("CAMPAIGN_PLAN_MISMATCH")
            if self._owner_role is not None:
                self._plan_evidence_consistent(cur, plan_sha)

    def _reserve(self, campaign_id: str, call_id: str) -> int:
        """One short READ COMMITTED transaction, committed BEFORE the request is sent. The 0010
        trigger increments the plan counter in this same transaction and assigns seq from it; a
        rollback (or a killed process) undoes both."""
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            cur.execute(f"SELECT p.budget FROM {self._campaigns} c JOIN {self._plans} p "
                        "ON p.plan_sha256 = c.plan_sha256 WHERE c.campaign_id=%s", (campaign_id,))
            if self._one(cur, "budget") is None:
                raise LedgerUnavailable("PLAN_NOT_APPROVED")
            cur.execute(f"INSERT INTO {self._attempts} (campaign_id, call_id) VALUES (%s, %s) RETURNING seq",
                        (campaign_id, call_id))
            return int(self._one(cur, "seq"))  # committed on leaving the block

    def _reserve_with_retry(self, campaign_id: str, call_id: str) -> int:
        """A serialization conflict (40001/40P01) is retried in a NEW transaction: the owner
        authorization and the approved plan are checked again and the same plan budget applies (the
        cancelled transaction reserved nothing). Still conflicting -> SERIALIZATION_FAILURE."""
        for attempt in range(SERIALIZATION_RETRIES):
            if attempt and os.environ.get(AUTH_VAR, "") != "true":
                raise NotAuthorized("CAMPAIGN_NOT_AUTHORIZED_BY_OWNER")
            try:
                return self._guarded(self._reserve, campaign_id, call_id)
            except SerializationRefused:
                continue
        raise SerializationRefused("SERIALIZATION_FAILURE")

    def _plan_usage(self, campaign_id: str) -> dict[str, Any]:
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            cur.execute(f"SELECT p.plan_sha256, p.budget, (SELECT count(*) FROM {self._attempts} a JOIN "
                        f"{self._campaigns} k ON k.campaign_id = a.campaign_id WHERE k.plan_sha256 = p.plan_sha256) AS used "
                        f"FROM {self._campaigns} c JOIN {self._plans} p ON p.plan_sha256 = c.plan_sha256 "
                        "WHERE c.campaign_id=%s", (campaign_id,))
            row = cur.fetchone()
            if row is None:
                raise LedgerUnavailable("CAMPAIGN_NOT_OPEN")
            sha, budget, used = (row["plan_sha256"], row["budget"], row["used"]) if isinstance(row, dict) else row
            return {"plan_sha256": sha, "budget": int(budget), "used": int(used)}

    def _used(self, campaign_id: str) -> int:
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            cur.execute(f"SELECT count(*) AS n FROM {self._attempts} WHERE campaign_id=%s",
                        (campaign_id,))
            return int(self._one(cur, "n"))

    def _attempted(self, campaign_id: str) -> set[str]:
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            cur.execute(f"SELECT DISTINCT call_id FROM {self._attempts} WHERE campaign_id=%s",
                        (campaign_id,))
            return {r["call_id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}

    def _record(self, campaign_id: str, seq: int, outcome: str, http_status: int | None) -> None:
        if outcome not in OUTCOMES or (outcome == "HTTP_RESPONSE") != (type(http_status) is int):
            raise LedgerUnavailable("CAMPAIGN_OUTCOME_INVALID")
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            # Append-only: the first outcome of an attempt stays; INSERT privilege only.
            cur.execute(f"INSERT INTO {self._outcomes} (campaign_id, seq, outcome, http_status) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (campaign_id, seq) DO NOTHING",
                        (campaign_id, seq, outcome, http_status))

    def _states(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._read_committed(self._connect()) as conn, conn.cursor() as cur:
            self._require_schema(cur)
            cur.execute(f"SELECT a.seq, a.call_id, o.outcome, o.http_status FROM {self._attempts} a "
                        f"LEFT JOIN {self._outcomes} o ON o.campaign_id = a.campaign_id AND o.seq = a.seq "
                        "WHERE a.campaign_id=%s ORDER BY a.seq", (campaign_id,))
            out = []
            for r in cur.fetchall():
                seq, call_id, outcome, status = ((r["seq"], r["call_id"], r["outcome"], r["http_status"])
                                                 if isinstance(r, dict) else tuple(r))
                out.append({"seq": seq, "call_id": call_id, "state": OUTCOMES.get(outcome, RESERVED_UNKNOWN),
                            "http_status": status})
            return out

    async def record_outcome(self, campaign_id, seq, outcome, http_status):
        await asyncio.to_thread(self._guarded, self._record, campaign_id, seq, outcome, http_status)

    async def attempt_states(self, campaign_id):
        return await asyncio.to_thread(self._guarded, self._states, campaign_id)

    async def plan_usage(self, campaign_id):
        return await asyncio.to_thread(self._guarded, self._plan_usage, campaign_id)

    async def open(self, campaign_id, budget, plan_sha):
        await asyncio.to_thread(self._guarded, self._open, campaign_id, budget, plan_sha)

    async def reserve(self, campaign_id, call_id):
        return await asyncio.to_thread(self._reserve_with_retry, campaign_id, call_id)

    async def used(self, campaign_id):
        return await asyncio.to_thread(self._guarded, self._used, campaign_id)

    async def attempted_calls(self, campaign_id):
        return await asyncio.to_thread(self._guarded, self._attempted, campaign_id)


_CURRENT_CALL: contextvars.ContextVar[str] = contextvars.ContextVar("af_cert_call", default="UNKNOWN")
# Per planned call: how many attempts the transport reserved and the fixed code of a refusal. A call
# refused BEFORE any reservation (owner authorization, ledger, budget) sent nothing: NOT_TESTED.
_CALL_LOG: contextvars.ContextVar[dict | None] = contextvars.ContextVar("af_cert_call_log", default=None)
PRE_SEND_REFUSALS = (NotAuthorized, BudgetExhausted, LedgerUnavailable, SerializationRefused)


def _new_call_log() -> dict[str, Any]:
    return {"reserved": 0, "refused": None}


def refused_before_send(log: Mapping[str, Any] | None, out: Mapping[str, Any] | None = None) -> str | None:
    """A fixed code when the call reserved (hence sent) NOTHING, None otherwise. Nothing reserved
    means nothing left: the refusal of the transport (owner authorization, ledger, budget) or, when
    the client stopped before the transport (e.g. CIRCUIT_OPEN after earlier refusals), its own
    error code. Such a call is NOT_TESTED, never a provider ERROR."""
    if log is None or log.get("reserved"):
        return None
    if log.get("refused"):
        return str(log["refused"])
    error = (out or {}).get("error") if isinstance((out or {}).get("error"), dict) else {}
    code = error.get("code")
    return str(code) if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else "NOT_SENT"


CAPTURE_MAX_BYTES = 16 * 1024 * 1024  # == the streamed-scan bound; above it nothing is captured


async def _record_outcome(ledger: CampaignLedger, campaign_id: str, seq: int, outcome: str, status: int | None) -> None:
    """Best effort AFTER the exchange: if the outcome cannot be written the attempt simply stays
    RESERVED_UNKNOWN (conservative: counted, never retried); the provider reply is not discarded."""
    try:
        await ledger.record_outcome(campaign_id, seq, outcome, status)
    except Exception:  # noqa: BLE001 - fixed state in the ledger, nothing surfaced
        return None


def budget_transport(inner, ledger: CampaignLedger, campaign_id: str, capture: dict[str, str] | None = None):
    """httpx transport that reserves one ledger attempt BEFORE every request it sends (write-ahead).
    With `capture`, the provider body text of each call is kept verbatim (snapshot mode)."""
    import httpx

    class _BudgetTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            log = _CALL_LOG.get()
            try:
                return await self._send(request, log)
            except PRE_SEND_REFUSALS as exc:  # fixed code only; the client sees the same exception
                if log is not None:
                    log["refused"] = str(exc)
                raise

        async def _send(self, request, log):
            call_id = _CURRENT_CALL.get()
            if os.environ.get(AUTH_VAR, "") != "true":
                raise NotAuthorized("CAMPAIGN_NOT_AUTHORIZED_BY_OWNER")  # before any reservation or send
            seq = await ledger.reserve(campaign_id, call_id)  # write-ahead: raises before any send
            if log is not None:
                log["reserved"] += 1
            # From here the attempt is RESERVED: it stays RESERVED_UNKNOWN in the ledger until an
            # outcome is recorded (a crash before, during or after the send records nothing).
            try:
                response = await inner.handle_async_request(request)
            except Exception:
                await _record_outcome(ledger, campaign_id, seq, "TRANSPORT_ERROR", None)
                raise
            await _record_outcome(ledger, campaign_id, seq, "HTTP_RESPONSE", response.status_code)
            if capture is None:
                return response
            body = await response.aread()
            if len(body) <= CAPTURE_MAX_BYTES:
                capture[call_id] = body.decode("utf-8", errors="replace")
            headers = [(k, v) for k, v in response.headers.multi_items()
                       if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(response.status_code, headers=headers, content=body, request=request)

        async def aclose(self):
            await inner.aclose()

    return _BudgetTransport()


class _Registry:
    def __init__(self):
        self.tools: dict[str, Any] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(settings, ledger: CampaignLedger, campaign_id: str, inner_transport, quota, capture=None):
    import httpx

    from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
    from bridge.api_football.tools import register_tools
    from bridge.api_football.transport import InjectedHTTPXTransport

    http = httpx.AsyncClient(base_url=settings.base_url, timeout=settings.timeout_seconds, follow_redirects=False,
                             transport=budget_transport(inner_transport, ledger, campaign_id, capture))
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=settings.api_key),
                                     quota=quota, retry=RetryPolicy(max_attempts=ATTEMPTS_PER_CALL))
    registry = _Registry()
    register_tools(registry, settings, client=client)
    return registry.tools, http


# ---------------------------------------------------------------------------- execution
def _resolve(value: Any, replies: Mapping[str, Any]) -> Any:
    if not isinstance(value, dict):
        return value
    node: Any = replies.get(value["$bind"])
    values = [node]
    for step in value["path"]:
        nxt = []
        for v in values:
            if step == "*" and isinstance(v, list):
                nxt.extend(v)
            elif isinstance(step, int) and isinstance(v, list) and 0 <= step < len(v):
                nxt.append(v[step])
            elif isinstance(step, str) and isinstance(v, dict) and step in v:
                nxt.append(v[step])
        values = nxt
    values = [v for v in values if type(v) is int and v > 0]
    if not values:
        raise LookupError("BINDING_UNRESOLVED")
    if value.get("join"):
        take = values[: value.get("take", len(values))]
        if len(take) < value.get("take", 1):
            raise LookupError("BINDING_UNRESOLVED")
        return value["join"].join(str(v) for v in take)
    return values[0] + int(value.get("add", 0))


def classify(out: Mapping[str, Any]) -> str:
    status = out.get("status")
    if status == "REAL_API_RESPONSE":
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        results = data.get("results")
        if type(results) is not int:
            rows = data.get("response")
            results = len(rows) if isinstance(rows, list) else None
        if results is None:
            return "ERROR"
        return "DATA_PRESENT" if results > 0 else "EMPTY_VALID"
    if isinstance(status, str) and status.startswith("PARTIAL_"):
        return "PARTIAL"
    error = out.get("error") if isinstance(out.get("error"), dict) else {}
    if error.get("code") == "PROVIDER_DECLARED_ERRORS" and error.get("category") == "PLAN_NOT_INCLUDED":
        return "PLAN_EXCLUDED"
    return "ERROR"


def _summary_of(out: Mapping[str, Any]) -> dict[str, Any]:
    """Shapes and counts only: no provider values, no prices, no raw payload."""
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    error = out.get("error") if isinstance(out.get("error"), dict) else {}
    return {
        "status": out.get("status"),
        "error_code": error.get("code"), "error_category": error.get("category"),
        "error_keys": error.get("keys") if isinstance(error.get("keys"), list) else None,
        "results": data.get("results") if type(data.get("results")) is int else None,
        "paging": out.get("pagination") if isinstance(out.get("pagination"), dict) else None,
        "pagination_state": (out.get("pagination_state") or {}).get("state") if isinstance(out.get("pagination_state"), dict) else None,
        "network_calls_made": out.get("network_calls_made") if type(out.get("network_calls_made")) is int else None,
    }


async def execute(plan: Mapping[str, Any], *, campaign_id: str, settings, ledger: CampaignLedger,
                  inner_transport, quota=None, plan_text: str, source_sha: str) -> dict[str, Any]:
    if not CAMPAIGN_ID.fullmatch(campaign_id):
        raise ValueError("INVALID_CAMPAIGN_ID")
    if plan.get("max_attempts_total", HARD_CAP + 1) > HARD_CAP:
        raise ValueError("PLAN_ABOVE_HARD_CAP")
    await ledger.open(campaign_id, HARD_CAP, plan_sha256(plan_text))
    already = await ledger.attempted_calls(campaign_id)
    prior_states = {s["call_id"]: s["state"] for s in await ledger.attempt_states(campaign_id)}
    tools, http = _tools(settings, ledger, campaign_id, inner_transport, quota)
    replies: dict[str, Any] = {}
    results = []
    try:
        for call in plan["calls"]:
            entry = {"call_id": call["call_id"], "tool": call["tool"], "covers_rows": call["covers_rows"]}
            if call["call_id"] in already:
                entry.update(verdict="NOT_TESTED", outcome="ALREADY_ATTEMPTED_IN_CAMPAIGN_NOT_RETRIED",
                             ledger_state=prior_states.get(call["call_id"], RESERVED_UNKNOWN))
                results.append(entry)
                continue
            if await ledger.used(campaign_id) >= HARD_CAP:
                entry.update(verdict="NOT_TESTED", outcome="CAMPAIGN_BUDGET_EXHAUSTED")
                results.append(entry)
                continue
            try:
                args = {k: _resolve(v, replies) for k, v in call["arguments"].items()}
            except LookupError:
                entry.update(verdict="NOT_TESTED", outcome="BINDING_UNRESOLVED")
                results.append(entry)
                continue
            token = _CURRENT_CALL.set(call["call_id"])
            log_token = _CALL_LOG.set(log := _new_call_log())
            try:
                out = await tools[call["tool"]](**args)
            finally:
                _CALL_LOG.reset(log_token)
                _CURRENT_CALL.reset(token)
            refusal = refused_before_send(log, out)
            if refusal is not None:  # nothing reserved, nothing sent: not a provider result
                entry.update(verdict="NOT_TESTED", outcome="REFUSED_BEFORE_SEND", refusal_code=refusal)
                results.append(entry)
                continue
            if isinstance(out.get("data"), dict):
                replies[call["call_id"]] = out["data"]
            entry.update(verdict=classify(out), outcome="ATTEMPTED", reply=_summary_of(out))  # >= 1 attempt reserved
            results.append(entry)
    finally:
        await http.aclose()
    return {
        "schema": "apifootball-cert-batch1-run/v1", "campaign_id": campaign_id, "source_sha": source_sha,
        "plan_sha256": plan_sha256(plan_text), "hard_cap_attempts": HARD_CAP,
        "ledger_attempts_used": await ledger.used(campaign_id),
        "plan_budget": await ledger.plan_usage(campaign_id),  # AF-15: shared by every campaign of the plan
        "ledger_attempt_states": state_counts(states := await ledger.attempt_states(campaign_id)),
        "ledger_attempts": states, "calls": results,
    }


def write_artifacts(run: Mapping[str, Any], out_dir: Path) -> list[Path]:
    """One JSON per matrix requirement row, plus the campaign summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    rows: dict[str, list[dict[str, Any]]] = {}
    for call in run["calls"]:
        for row_id in call["covers_rows"]:
            rows.setdefault(row_id, []).append({k: call.get(k) for k in ("call_id", "verdict", "outcome", "reply")})
    for row_id, calls in sorted(rows.items()):
        name = re.sub(r"[^A-Za-z0-9_.+-]", "_", row_id) + ".json"
        path = out_dir / "rows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"row_id": row_id, "campaign_id": run["campaign_id"],
                                    "source_sha": run["source_sha"], "calls": calls},
                                   indent=1, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    summary = out_dir / "campaign_summary.json"
    summary.write_text(json.dumps({k: v for k, v in run.items() if k != "calls"} |
                                  {"calls": [{k: c.get(k) for k in ("call_id", "tool", "verdict", "outcome")}
                                             for c in run["calls"]]}, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
    written.append(summary)
    return written


SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?://", re.I), re.compile(r"x-apisports-key", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), re.compile(r"password\s*[=:]", re.I),
    re.compile(r"sslrootcert|PGPASSWORD", re.I),
)


def scan_for_secrets(paths: list[Path], secrets: list[str]) -> list[str]:
    """Names of artefacts holding a secret value or a secret-shaped pattern (never the value)."""
    bad = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if any(s and s in text for s in secrets) or any(p.search(text) for p in SECRET_PATTERNS):
            bad.append(path.name)
    return bad


# ---------------------------------------------------------------------------- live full match snapshot
# Separate campaign `live_full_match_snapshot`: one fixture, its own snapshot_id in the same persistent
# campaign ledger, a configured CEILING of SNAPSHOT_CAP attempts (not an authorization to spend them).
# Run only while a match is live. On reaching the ceiling the snapshot closes INCOMPLETE; nothing is
# retried. Out of scope: continuous odds collection, history, drop computation.
SNAPSHOT_CAP = 8
SNAPSHOT_STEPS = (  # (source, tool, argument builder from the fixture reply)
    ("fixture", "apifootball_fixtures"),
    ("statistics", "apifootball_fixture_statistics"),
    ("events", "apifootball_fixture_events"),
    ("lineups", "apifootball_fixture_lineups"),
    ("players", "apifootball_fixture_players"),
    ("live_odds", "apifootball_live_odds"),
    ("coverage", "apifootball_coverage_report"),
)


def render_snapshot_plan() -> str:
    """AF-15: the snapshot's committed plan. Fixed (no fixture, no snapshot_id): its approved budget
    (at most SNAPSHOT_CAP) is shared by every snapshot_id; a new snapshot_id renews nothing."""
    return json.dumps({"schema": "apifootball-live-full-match-snapshot-plan/v1", "campaign": "live_full_match_snapshot",
                       "plan_kind": "LIVE_FULL_MATCH_SNAPSHOT", "cap": SNAPSHOT_CAP,
                       "steps": [s for s, _ in SNAPSHOT_STEPS], "status": "PREPARED_NOT_AUTHORIZED"},
                      indent=1, sort_keys=True) + "\n"


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def execute_snapshot(*, snapshot_id: str, fixture_id: int | None, settings, ledger: CampaignLedger,
                           inner_transport, quota=None, cap: int = SNAPSHOT_CAP, clock=_utc_now) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("apifootball_live_snapshot_b1", ROOT / "scripts" / "apifootball_live_snapshot.py")
    snap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snap)
    if not CAMPAIGN_ID.fullmatch(snapshot_id):
        raise ValueError("INVALID_SNAPSHOT_ID")
    if not 1 <= cap <= SNAPSHOT_CAP:
        raise ValueError("SNAPSHOT_CAP_OUT_OF_RANGE")
    plan_text = render_snapshot_plan()  # committed as SNAPSHOT_PLAN_PATH; approved in the database
    await ledger.open(snapshot_id, cap, plan_sha256(plan_text))
    already = await ledger.attempted_calls(snapshot_id)
    capture: dict[str, str] = {}
    tools, http = _tools(settings, ledger, snapshot_id, inner_transport, quota, capture)
    sources: dict[str, Any] = {}
    stopped: str | None = None

    async def call(call_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal stopped
        if call_id in already:
            stopped = stopped or "RESTART_CALLS_ALREADY_ATTEMPTED_NOT_RETRIED"
            return None
        if await ledger.used(snapshot_id) >= cap:
            stopped = stopped or "BUDGET_CAP_REACHED"
            return None
        before = await ledger.used(snapshot_id)
        token = _CURRENT_CALL.set(call_id)
        log_token = _CALL_LOG.set(log := _new_call_log())
        started = clock()
        try:
            out = await tools[tool](**args)
        finally:
            _CALL_LOG.reset(log_token)
            _CURRENT_CALL.reset(token)
        ended = clock()
        refusal = refused_before_send(log, out)
        if refusal is not None:  # nothing sent: the source stays NOT_TESTED, the snapshot stops
            stopped = stopped or f"REFUSED_BEFORE_SEND:{refusal}"
            return None
        used_now = await ledger.used(snapshot_id)
        if used_now == before and out.get("network_calls_made", 0):
            stopped = stopped or "LEDGER_ACCOUNTING_MISMATCH"
        return {"out": out, "requested_at_utc": started, "completed_at_utc": ended, "attempts_used": used_now - before}

    try:
        if fixture_id is None:  # choose the match: costs one attempt
            chosen = await call("S00", "apifootball_fixtures", {"live": "all"})
            rows = ((chosen or {}).get("out", {}).get("data") or {}).get("response") if chosen else None
            ids = [r.get("fixture", {}).get("id") for r in rows or [] if isinstance(r, dict)]
            fixture_id = next((i for i in ids if type(i) is int and i > 0), None)
            if fixture_id is None:
                stopped = stopped or "NO_LIVE_FIXTURE_SELECTED"
        league = season = None
        for index, (source, tool) in enumerate(SNAPSHOT_STEPS, start=1):
            if fixture_id is None:
                break
            if source == "coverage":
                if league is None or season is None:
                    continue
                args = {"id": league, "season": season}
            else:
                args = {"id" if source == "fixture" else "fixture": fixture_id}
            got = await call(f"S{index:02d}", tool, args)
            if got is None:
                continue
            body = capture.get(f"S{index:02d}")
            sources[source] = {"tool": tool, "arguments": args, "mcp_reply": got["out"],
                               "requested_at_utc": got["requested_at_utc"], "completed_at_utc": got["completed_at_utc"],
                               "attempts_used": got["attempts_used"],
                               **({"provider_body_text": body} if body is not None else {})}
            if source == "fixture":
                try:
                    league_info = got["out"]["data"]["response"][0]["league"]
                    league, season = league_info["id"], league_info["season"]
                except (KeyError, IndexError, TypeError):
                    league = season = None
    finally:
        await http.aclose()
    used = await ledger.used(snapshot_id)
    states = state_counts(await ledger.attempt_states(snapshot_id))  # RESERVED vs answered, explicit
    if fixture_id is None:
        return {"schema": "apifootball-live-full-match-snapshot/v1", "snapshot_id": snapshot_id,
                "overall": "SNAPSHOT_INCOMPLETE", "incomplete_reasons": [f"STOPPED:{stopped}"],
                "budget": {"cap": cap, "attempts_used": used, "attempt_states": states}, "sources": {}}
    report = snap.build_report(snapshot_id, fixture_id, sources, budget_cap=cap, attempts_used=used,
                               stopped_reason=stopped)
    report["budget"]["attempt_states"] = states
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-plan", action="store_true")
    mode.add_argument("--check-plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--campaign", choices=("batch1", "live_full_match_snapshot"), default="batch1")
    parser.add_argument("--authorize-live-campaign", action="store_true")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--fixture-id", type=int, default=None)
    parser.add_argument("--out", default="")
    mode.add_argument("--plan-approval-request", choices=("batch1", "live_full_match_snapshot"),
                      help="print '<plan_sha256> <budget> <plan_kind>' of the committed plan (approval workflow)")
    args = parser.parse_args(argv)
    text = render_plan(build_plan())
    snapshot_text = render_snapshot_plan()
    if args.write_plan:
        PLAN_PATH.write_text(text, encoding="utf-8")
        SNAPSHOT_PLAN_PATH.write_text(snapshot_text, encoding="utf-8")
        print(f"PLAN_WRITTEN calls={len(json.loads(text)['calls'])} attempts={json.loads(text)['max_attempts_total']}")
        return 0
    committed = {p: p.read_text(encoding="utf-8") if p.is_file() else None for p in (PLAN_PATH, SNAPSHOT_PLAN_PATH)}
    up_to_date = committed[PLAN_PATH] == text and committed[SNAPSHOT_PLAN_PATH] == snapshot_text
    if args.check_plan:
        print("PLAN_UP_TO_DATE" if up_to_date else "PLAN_OUT_OF_DATE")
        return 0 if up_to_date else 1
    if not up_to_date:  # the plan that runs (and is approved) is exactly the committed file
        print("PLAN_OUT_OF_DATE")
        return 2
    if args.plan_approval_request:
        if args.plan_approval_request == "batch1":
            print(f"{plan_sha256(text)} {json.loads(text)['max_attempts_total']} BATCH1")
        else:
            print(f"{plan_sha256(snapshot_text)} {SNAPSHOT_CAP} LIVE_FULL_MATCH_SNAPSHOT")
        return 0
    # --execute: owner-dispatched workflow only. Fails closed on every missing precondition.
    if not args.authorize_live_campaign or not args.out or os.environ.get(AUTH_VAR, "") != "true":
        print("LIVE_CAMPAIGN_NOT_AUTHORIZED")
        return 2
    import httpx

    from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
    settings = Settings.from_env(os.environ)
    if not settings.real_requests_authorized or settings.base_url != OFFICIAL_BASE_URL:
        print("LIVE_CAMPAIGN_NOT_AUTHORIZED")
        return 2
    if not os.environ.get("DATABASE_URL", "").strip():
        print("COORDINATOR_DATABASE_REQUIRED")
        return 2
    try:  # PATH A: the coordinator connection (defaultdb) must be the dedicated runner too
        from app.db import connect as _coordinator_connect
        with _coordinator_connect() as conn:
            identity = runner_identity_problem(conn)
    except Exception:  # noqa: BLE001 - fixed code only, never the message or the DSN
        identity = "COORDINATOR_DATABASE_UNAVAILABLE"
    if identity:
        print(f"CAMPAIGN_REFUSED {identity}")
        return 2
    if args.campaign == "live_full_match_snapshot":
        try:
            report = asyncio.run(execute_snapshot(snapshot_id=args.campaign_id, fixture_id=args.fixture_id,
                                                  settings=settings, ledger=PostgresCampaignLedger(),
                                                  inner_transport=httpx.AsyncHTTPTransport()))
        except (LedgerUnavailable, ValueError) as exc:
            print(f"CAMPAIGN_REFUSED {exc.args[0] if exc.args and isinstance(exc.args[0], str) else 'UNKNOWN'}")
            return 2
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "live_full_match_snapshot.json"
        path.write_text(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        if scan_for_secrets([path], [settings.api_key, os.environ.get("DATABASE_URL", "")]):
            path.unlink()
            print("ARTIFACT_LEAK_DETECTED files=1 (artefact deleted)")
            return 3
        print(f"SNAPSHOT_DONE {report['overall']} attempts_used={report['budget']['attempts_used']}")
        return 0
    try:
        run = asyncio.run(execute(json.loads(text), campaign_id=args.campaign_id, settings=settings,
                                  ledger=PostgresCampaignLedger(), inner_transport=httpx.AsyncHTTPTransport(),
                                  plan_text=text, source_sha=os.environ.get("GITHUB_SHA", "UNKNOWN")[:40]))
    except (LedgerUnavailable, ValueError) as exc:
        print(f"CAMPAIGN_REFUSED {exc.args[0] if exc.args and isinstance(exc.args[0], str) else 'UNKNOWN'}")
        return 2
    paths = write_artifacts(run, Path(args.out))
    leaks = scan_for_secrets(paths, [settings.api_key, os.environ.get("DATABASE_URL", "")])
    if leaks:
        for path in paths:
            path.unlink()
        print(f"ARTIFACT_LEAK_DETECTED files={len(leaks)} (artefacts deleted)")
        return 3
    print(f"CAMPAIGN_DONE attempts_used={run['ledger_attempts_used']} of {HARD_CAP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
