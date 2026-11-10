"""FIX-01: complete, safe retrieval of non-odds replies above the 256 KiB MCP cap.

Design choice: continuation is PLAIN, RE-VALIDATED PROVIDER PARAMETERS, never a token.
Every follow-up is an ordinary tool call (validated, quota-charged, authorized like any
other), needs no secret, no server state and no new env var, and survives restarts and
redeploys. An opaque token was not needed: every split below is expressible with documented
parameters (`page`, `ids` <= 20, or one documented id filter added to the original request).

The manifest is price-free and data-free: identifiers in provider order, counts, the
provider's paging, a SHA-256 of the provider's `response` list and, per follow-up call, the
ids and row count it must return. The client checks each follow-up against it: a missing,
extra or reordered id, or a different count, means the data changed between reads (re-read
and compare `response_sha256` / `results_declared`). No row is skipped or duplicated: every
row id appears in exactly one follow-up call; rows without an identifier are counted and make
`complete_retrieval_available` false. Nothing is fetched automatically.

Replies above the transport read cap (512 KiB, unchanged) are refused before parsing, so no
id can be listed for them: only a narrowing suggestion is given, labelled NOT_COMPLETE.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


@dataclass(frozen=True)
class Strategy:
    kind: str                      # "IDS_SLICES" or "ADD_ID"
    id_path: tuple[str, ...]       # where the row identifier lives
    param: str                     # the documented parameter that selects it
    drop: tuple[str, ...] = ()     # original parameters a narrower call no longer needs
    note: str | None = None


# Only documented parameters (official_parameter_matrix_v3.9.3.json). Row id paths follow the
# provider shapes evidenced in the repo (fixture.id, league.id, team.id, player.id, id).
STRATEGIES: dict[str, Strategy] = {
    "apifootball_fixtures": Strategy("IDS_SLICES", ("fixture", "id"), "ids",
                                     note="fixtures?ids returns the same fixtures; its row detail may be richer "
                                          "than a listing row (DOCS_UNVERIFIED)"),
    "apifootball_leagues": Strategy("ADD_ID", ("league", "id"), "id"),
    "apifootball_teams": Strategy("ADD_ID", ("team", "id"), "id"),
    "apifootball_venues": Strategy("ADD_ID", ("id",), "id"),
    "apifootball_coaches": Strategy("ADD_ID", ("id",), "id"),
    "apifootball_players": Strategy("ADD_ID", ("player", "id"), "id", drop=("page", "search")),
    "apifootball_player_profiles": Strategy("ADD_ID", ("player", "id"), "player", drop=("page", "search")),
    "apifootball_injuries": Strategy("ADD_ID", ("fixture", "id"), "fixture", drop=("date", "ids", "league")),
    "apifootball_transfers": Strategy("ADD_ID", ("player", "id"), "player"),
}
IDS_SLICE_MAX = 20  # documented: "Maximum of 20 fixtures ids"
_MAX_ROW_IDS = 10_000


def _dig(row: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def response_sha256(response: Any) -> str:
    canonical = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _next_page(request: Mapping[str, Any], paging: Any) -> dict[str, Any] | None:
    if isinstance(paging, dict) and type(paging.get("current")) is int and type(paging.get("total")) is int \
            and paging["current"] < paging["total"]:
        return {**request, "page": paging["current"] + 1}
    return None


def _ids_slices(ids: list[int], request: Mapping[str, Any]) -> tuple[list[dict], str | None]:
    if "id" in request:
        return [], "NOT_SPLITTABLE_SINGLE_FIXTURE_ID"
    if "ids" in request:
        size = (len(ids) + 1) // 2           # halve an ids request that was itself too large
        if len(ids) <= 1:
            return [], "NOT_SPLITTABLE_SINGLE_FIXTURE_ID"
    else:
        size = IDS_SLICE_MAX
    carried = {k: request[k] for k in ("timezone",) if k in request}
    calls = []
    for start in range(0, len(ids), size):
        chunk = ids[start:start + size]
        args = {"id": chunk[0]} if len(chunk) == 1 else {"ids": "-".join(str(i) for i in chunk)}
        calls.append({"arguments": {**args, **carried}, "expect_ids": chunk, "expect_rows": len(chunk),
                      "order": "AS_LISTED"})
    return calls, None


def _add_id(rows_ids: list[Any], request: Mapping[str, Any], s: Strategy) -> tuple[list[dict], str | None]:
    if s.param in request:
        return [], "NOT_SPLITTABLE_ALREADY_FILTERED_BY_" + s.param.upper()
    base = {k: v for k, v in request.items() if k not in s.drop}
    groups: dict[int, int] = {}
    for rid in rows_ids:
        if type(rid) is int:
            groups[rid] = groups.get(rid, 0) + 1
    if len(groups) <= 1:
        return [], "NOT_SPLITTABLE_SINGLE_IDENTIFIER"
    return [{"arguments": {**base, s.param: rid}, "expect_ids": [rid], "expect_rows": n, "order": "ANY"}
            for rid, n in groups.items()], None


def id_manifest(tool: str, request: Mapping[str, Any], envelope: Any, *, with_row_ids: bool = True) -> dict[str, Any]:
    """Price-free, data-free manifest of an oversized reply. Raises ValueError on a reply that
    is not a provider envelope with a `response` list (fail closed)."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("response"), list):
        raise ValueError("MANIFEST_RESPONSE_NOT_A_LIST")
    response = envelope["response"]
    strategy = STRATEGIES.get(tool)
    row_ids = [_dig(r, strategy.id_path) for r in response] if strategy else []
    without_id = sum(1 for r in row_ids if type(r) is not int) if strategy else len(response)
    if strategy is None:
        calls, reason = [], "NO_DOCUMENTED_SPLIT_FOR_THIS_ENDPOINT"
    elif without_id:
        calls, reason = [], "ROWS_WITHOUT_IDENTIFIER"
    elif strategy.kind == "IDS_SLICES":
        if len(set(row_ids)) != len(row_ids):
            calls, reason = [], "DUPLICATE_FIXTURE_IDS_IN_REPLY"
        else:
            calls, reason = _ids_slices(row_ids, request)
    else:
        calls, reason = _add_id(row_ids, request, strategy)
    paging = envelope.get("paging")
    results = envelope.get("results")
    manifest = {
        "manifest": "PARTIAL_ID_MANIFEST", "partial": True, "data_included": False,
        "reason": "RESPONSE_EXCEEDS_MCP_SIZE_LIMIT",
        "results_declared": results if type(results) is int else None,
        "rows_in_reply": len(response), "rows_without_id": without_id,
        "response_sha256": response_sha256(response),
        "paging": paging if isinstance(paging, dict) else None,
        "next_page_arguments": _next_page(request, paging),
        "id_path": ".".join(strategy.id_path) if strategy else None,
        "follow_up": {"tool": tool, "strategy": strategy.kind if strategy else None,
                      "parameter": strategy.param if strategy else None,
                      "splittable": bool(calls), "calls": calls, "reason": reason,
                      "note": strategy.note if strategy else None},
        "complete_retrieval_available": bool(calls),
        "verification": "Each follow-up reply must hold exactly expect_ids with expect_rows rows (order AS_LISTED: "
                        "same order). Any difference means the provider data changed between reads: re-read the "
                        "original call and compare response_sha256 and results_declared. Nothing is fetched "
                        "automatically; every call is validated and quota-charged.",
    }
    if with_row_ids:
        manifest["row_ids"] = row_ids[:_MAX_ROW_IDS]
        manifest["row_ids_complete"] = len(row_ids) <= _MAX_ROW_IDS
    return manifest


# Above the read cap nothing was parsed: no ids, only a narrowing suggestion (never complete).
_NARROWING = {
    "apifootball_fixtures": "Add league and season (or team/venue with season) to a date or live query; "
                            "page through ids from a smaller listing.",
    "apifootball_leagues": "Filter by country (names from apifootball_countries), type, season, current or id.",
    "apifootball_injuries": "Use fixture, ids (<= 20) or a team/player with season instead of league+season.",
    "apifootball_transfers": "Use player instead of team.",
    "apifootball_teams": "Add league and season, or use id/search.",
    "apifootball_venues": "Use id, name or city instead of country.",
}


def read_cap_navigation(tool: str) -> dict[str, Any]:
    return {"complete_retrieval_available": False, "reason": "READ_CAP_EXCEEDED_BEFORE_PARSE",
            "status": "NOT_COMPLETE", "row_ids": None,
            "narrowing": _NARROWING.get(tool, "Add a documented filter that narrows the listing."),
            "note": "The transport read cap (512 KiB; 2 MiB for odds) is unchanged by owner rule: rows of a "
                    "larger reply are never read, so no identifier can be listed for them."}
