"""FIX-01: complete, safe retrieval of replies above the 256 KiB MCP cap.

Design choice: continuation is PLAIN, RE-VALIDATED PROVIDER PARAMETERS, never a token.
Every follow-up is an ordinary tool call (validated, quota-charged, authorized like any
other), needs no secret, no server state and no new env var, and survives restarts and
redeploys. No opaque token is needed: every split below uses documented parameters.

Two inputs, one manifest:
  * IN_MEMORY: a reply up to the transport read cap (512 KiB) that was parsed;
  * STREAMED_SCAN: a larger reply scanned by stream_scan.IdScanner (bounded, never buffered).
Neither returns provider data or prices: identifiers in provider order, counts, paging, a
hash of the provider reply and, per follow-up call, the ids and row count it must return.

Completeness is claimed only when it is provable:
  * every row has a valid id (an int in the range the MCP itself accepts);
  * every row is in exactly one follow-up call;
  * every follow-up returns a SUBSET of the rows already seen, with a size provably under the
    read cap: in IN_MEMORY mode the subset of a reply that fitted under the cap; in
    STREAMED_SCAN mode the measured raw bytes of its rows plus an envelope margin (assumption
    stated in the manifest: the same row comes back with the same content);
  * the provider's `results` equals the rows seen.
A follow-up whose rows would have a different shape (fixtures?ids returns detailed fixtures,
size not evidenced) is offered for navigation but never counted as complete.

Order is NOT asserted (the provider's order under another filter is not documented): the
client compares id SETS, counts and the SHA-256 of the sorted ids; any difference means the
data changed between reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

READ_CAP_BYTES = 524_288          # transport._MAX_BODY, unchanged
ENVELOPE_MARGIN_BYTES = 16_384    # get/parameters/errors/results/paging around the rows
ID_MIN, ID_MAX = 1, 2_000_000_000  # what contracts._positive_int accepts
IDS_SLICE_MAX = 20                 # documented: "Maximum of 20 fixtures ids"
# fixtures?ids returns detailed fixtures (events, lineups, statistics, players): ASSUMED up to
# about 100 KB each, size not evidenced in the repo. Two per call keeps an ASSUMED-size reply
# under the 256 KiB MCP cap; it is never counted as provably complete.
IDS_DETAIL_SLICE = 2
_MAX_ROW_IDS = 20_000

# Row fields read per provider path; shared with the streaming scanner.
ROW_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "/fixtures": {"id": ("fixture", "id"), "league": ("league", "id"), "season": ("league", "season"),
                  "round": ("league", "round")},
    "/leagues": {"id": ("league", "id")},
    "/teams": {"id": ("team", "id")},
    "/venues": {"id": ("id",)},
    "/coachs": {"id": ("id",)},
    "/players": {"id": ("player", "id")},
    "/players/profiles": {"id": ("player", "id")},
    "/injuries": {"id": ("fixture", "id")},
    "/transfers": {"id": ("player", "id")},
    "/odds": {"id": ("fixture", "id")},
    "/odds/live": {"id": ("fixture", "id")},
}


@dataclass(frozen=True)
class Strategy:
    path: str
    kind: str                      # "FIXTURES" (group / ids) or "ADD_ID"
    param: str                     # the documented parameter that selects one id
    drop: tuple[str, ...] = ()     # original parameters a narrower call no longer needs


STRATEGIES: dict[str, Strategy] = {
    "apifootball_fixtures": Strategy("/fixtures", "FIXTURES", "ids"),
    "apifootball_leagues": Strategy("/leagues", "ADD_ID", "id"),
    "apifootball_teams": Strategy("/teams", "ADD_ID", "id"),
    "apifootball_venues": Strategy("/venues", "ADD_ID", "id"),
    "apifootball_coaches": Strategy("/coachs", "ADD_ID", "id"),
    "apifootball_players": Strategy("/players", "ADD_ID", "id", drop=("page", "search")),
    "apifootball_player_profiles": Strategy("/players/profiles", "ADD_ID", "player", drop=("page", "search")),
    "apifootball_injuries": Strategy("/injuries", "ADD_ID", "fixture", drop=("date", "ids", "league")),
    "apifootball_transfers": Strategy("/transfers", "ADD_ID", "player"),
    "apifootball_odds": Strategy("/odds", "ADD_ID", "fixture", drop=("league", "season", "date", "page")),
    "apifootball_live_odds": Strategy("/odds/live", "ADD_ID", "fixture", drop=("league",)),
}
TOOL_PATHS = {tool: s.path for tool, s in STRATEGIES.items()} | {"apifootball_coverage_report": "/leagues"}


@dataclass
class Row:
    id: Any
    fields: dict[str, Any] = field(default_factory=dict)
    size: int | None = None
    invalid: frozenset = frozenset()  # fields that are unusable (duplicate, not a scalar, unsafe)


def _safe_text(value: Any) -> bool:
    from .stream_scan import SAFE_TEXT
    return isinstance(value, str) and SAFE_TEXT.fullmatch(value) is not None


def valid_id(value: Any) -> bool:
    return type(value) is int and ID_MIN <= value <= ID_MAX


def mcp_arguments(args: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Provider names -> MCP tool argument names (`from`/`to` are `from_date`/`to_date`)."""
    if args is None:
        return None
    return {("from_date" if k == "from" else "to_date" if k == "to" else k): v for k, v in args.items()}


def provider_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
    return {("from" if k == "from_date" else "to" if k == "to_date" else k): v for k, v in args.items()}


def _dig(row: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def response_sha256(response: Any) -> str:
    canonical = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sorted_ids_sha256(ids: list[Any]) -> str:
    return hashlib.sha256(json.dumps(sorted(i for i in ids if valid_id(i))).encode()).hexdigest()


def _next_page(request: Mapping[str, Any], paging: Any) -> dict[str, Any] | None:
    if isinstance(paging, dict) and type(paging.get("current")) is int and type(paging.get("total")) is int \
            and paging["current"] < paging["total"]:
        return mcp_arguments({**request, "page": paging["current"] + 1})
    return None


def _combination(tool: str, args: Mapping[str, Any]) -> str:
    """Evidence of a follow-up's parameter set: fixtures use the FIX-03 table; elsewhere only a
    single documented parameter (timezone aside) is DOCUMENTED_VALID."""
    if tool == "apifootball_fixtures":
        from .contracts import ParameterError, fixtures_combination
        try:
            return fixtures_combination(args)["status"]
        except ParameterError:
            return "MCP_REJECTED"
    return "DOCUMENTED_VALID" if len([k for k in args if k != "timezone"]) == 1 else "DOCS_UNVERIFIED"


class _Plan:
    def __init__(self, tool: str, mode: str):
        self.tool, self.mode = tool, mode
        self.calls: list[dict[str, Any]] = []
        self.reason: str | None = None
        self.size_provable = True
        self.split_by: str | None = None

    def add(self, args: dict[str, Any], rows: list[Row]) -> None:
        ids = [r.id for r in rows]
        call = {"arguments": mcp_arguments(args), "expect_ids": list(dict.fromkeys(ids)),
                "expect_rows": len(rows), "combination_status": _combination(self.tool, args)}
        if len(call["expect_ids"]) > 20:  # small id sets are compared directly; the hash keeps big ones cheap
            call["expect_ids_sorted_sha256"] = sorted_ids_sha256(ids)
        if self.mode == "STREAMED_SCAN":
            sizes = [r.size for r in rows]
            if any(type(s) is not int for s in sizes) or sum(sizes) + ENVELOPE_MARGIN_BYTES > READ_CAP_BYTES:
                self.size_provable = False
                call["size_provable"] = False
            else:
                call["expect_bytes_max"] = sum(sizes) + ENVELOPE_MARGIN_BYTES
        self.calls.append(call)


def _groups(rows: list[Row], key) -> dict[Any, list[Row]]:
    out: dict[Any, list[Row]] = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def _plan_fixtures(plan: _Plan, request: Mapping[str, Any], rows: list[Row]) -> None:
    carried = {k: request[k] for k in ("timezone",) if k in request}
    if "id" in request:
        plan.reason = "NOT_SPLITTABLE_SINGLE_FIXTURE_ID"
        return
    if len({r.id for r in rows}) != len(rows):
        plan.reason = "DUPLICATE_FIXTURE_IDS_IN_REPLY"
        return
    if "ids" in request:
        if len(rows) <= 1:
            plan.reason = "NOT_SPLITTABLE_SINGLE_FIXTURE_ID"
            return
        half = (len(rows) + 1) // 2  # the same detailed rows, halved: a subset, size provable
        plan.split_by = "ids_halves"
        for chunk in (rows[:half], rows[half:]):
            ids = [r.id for r in chunk]
            plan.add({**({"id": ids[0]} if len(ids) == 1 else {"ids": "-".join(map(str, ids))}), **carried}, chunk)
        return
    group = None
    if "date" in request and "league" not in request and "live" not in request:
        group = ("league_season", lambda r: (r.fields.get("league"), r.fields.get("season")),
                 lambda k: {"league": k[0], "season": k[1]})
    elif "league" in request and "season" in request and "round" not in request:
        group = ("round", lambda r: r.fields.get("round"), lambda k: {"round": k})
    if group is not None:
        name, key, extra = group
        groups = _groups(rows, key)
        used = ("league", "season") if name == "league_season" else ("round",)
        keys_ok = all((valid_id(k[0]) and valid_id(k[1])) if name == "league_season" else _safe_text(k)
                      for k in groups) and not any(f in r.invalid for r in rows for f in used)
        if keys_ok and len(groups) > 1:
            plan.split_by = name
            for k, members in groups.items():
                plan.add({**request, **extra(k)}, members)
            return
    # Detailed fixtures by ids: navigable, size not evidenced, never claimed complete.
    plan.split_by = "ids_detail_slices"
    plan.size_provable = False
    plan.reason = "IDS_DETAIL_REPLY_SIZE_UNPROVEN"
    for start in range(0, len(rows), IDS_DETAIL_SLICE):
        chunk = rows[start:start + IDS_DETAIL_SLICE]
        ids = [r.id for r in chunk]
        plan.add({**({"id": ids[0]} if len(ids) == 1 else {"ids": "-".join(map(str, ids))}), **carried}, chunk)
    for c in plan.calls:
        c["size_provable"] = False


def _plan_add_id(plan: _Plan, request: Mapping[str, Any], rows: list[Row], s: Strategy) -> None:
    if s.param in request:
        plan.reason = "NOT_SPLITTABLE_ALREADY_FILTERED_BY_" + s.param.upper()
        return
    base = {k: v for k, v in request.items() if k not in s.drop}
    groups = _groups(rows, lambda r: r.id)
    if len(groups) <= 1:
        plan.reason = "NOT_SPLITTABLE_SINGLE_IDENTIFIER"
        return
    plan.split_by = s.param
    for rid, members in groups.items():
        plan.add({**base, s.param: rid}, members)


def build_manifest(tool: str, request: Mapping[str, Any], rows: list[Row], *, mode: str, results: Any,
                   paging: Any, body_hash: str, hash_basis: str, with_row_ids: bool = True,
                   bytes_scanned: int | None = None) -> dict[str, Any]:
    strategy = STRATEGIES.get(tool)
    plan = _Plan(tool, mode)
    without_id = sum(1 for r in rows if not valid_id(r.id) or "id" in r.invalid)
    results_match = (results == len(rows)) if type(results) is int else None
    if strategy is None:
        plan.reason = "NO_DOCUMENTED_SPLIT_FOR_THIS_ENDPOINT"
    elif without_id:
        plan.reason = "ROWS_WITHOUT_VALID_IDENTIFIER"
    elif strategy.kind == "FIXTURES":
        _plan_fixtures(plan, request, rows)
    else:
        _plan_add_id(plan, request, rows, strategy)
    covered = sum(c["expect_rows"] for c in plan.calls)
    complete = bool(plan.calls) and plan.size_provable and covered == len(rows) and results_match is not False
    if plan.calls and not complete and plan.reason is None:
        plan.reason = ("RESULTS_COUNT_MISMATCH" if results_match is False else
                       "FOLLOW_UP_MAY_EXCEED_READ_CAP" if not plan.size_provable else "ROWS_NOT_COVERED")
    unverified = sorted({c["combination_status"] for c in plan.calls} - {"DOCUMENTED_VALID"})
    ids = [r.id if valid_id(r.id) and "id" not in r.invalid else None for r in rows]  # never a raw string
    manifest = {
        "manifest": "PARTIAL_ID_MANIFEST", "mode": mode, "partial": True, "data_included": False,
        "prices_included": False, "reason": "RESPONSE_EXCEEDS_MCP_SIZE_LIMIT" if mode == "IN_MEMORY"
        else "RESPONSE_EXCEEDS_READ_CAP_SCANNED_FOR_IDS",
        "results_declared": results if type(results) is int else None, "results_match": results_match,
        "rows_in_reply": len(rows), "rows_without_id": without_id,
        "hash_basis": hash_basis, "reply_sha256": body_hash, "row_ids_sorted_sha256": sorted_ids_sha256(ids),
        "paging": paging if isinstance(paging, dict) else None,
        "next_page_arguments": _next_page(request, paging),
        "follow_up": {"tool": tool, "split_by": plan.split_by, "splittable": bool(plan.calls),
                      "calls": plan.calls, "reason": plan.reason,
                      "combination_evidence": "DOCUMENTED_VALID" if not unverified else unverified[0]},
        "complete_retrieval_available": complete,
        "completeness_basis": None if not complete else (
            "SUBSET_OF_A_REPLY_UNDER_THE_READ_CAP" if mode == "IN_MEMORY" else "MEASURED_RAW_ROW_BYTES"),
        "completeness_conditions": ["the same row is returned with the same content under the narrower filter"]
        + (["the provider accepts the DOCS_UNVERIFIED parameter combinations"] if unverified else []),
        "order": "NOT_ASSERTED",
        "verification": "Each follow-up reply must hold exactly the id set expect_ids (sorted SHA-256 "
                        "expect_ids_sorted_sha256) with expect_rows rows; order is not asserted. Any difference "
                        "means the provider data changed between reads: re-read the original call and compare "
                        "reply_sha256 / row_ids_sorted_sha256 / results_declared. Nothing is fetched "
                        "automatically; every call is validated and quota-charged.",
    }
    if bytes_scanned is not None:
        manifest["bytes_scanned"] = bytes_scanned
    if with_row_ids:
        manifest["row_ids"] = ids[:_MAX_ROW_IDS]
        manifest["row_ids_complete"] = len(ids) <= _MAX_ROW_IDS
    return manifest


def id_manifest(tool: str, request: Mapping[str, Any], envelope: Any, *, with_row_ids: bool = True) -> dict[str, Any]:
    """IN_MEMORY manifest of an oversized parsed reply. Raises ValueError on a reply that is not
    a provider envelope with a `response` list (fail closed)."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("response"), list):
        raise ValueError("MANIFEST_RESPONSE_NOT_A_LIST")
    path = TOOL_PATHS.get(tool)
    fields = ROW_FIELDS.get(path, {}) if path else {}
    rows = []
    for r in envelope["response"]:
        values = {name: _dig(r, p) for name, p in fields.items()}
        invalid = frozenset(name for name, v in values.items()
                            if isinstance(v, (dict, list)) or (isinstance(v, str) and (name == "id" or not _safe_text(v))))
        values = {k: (None if k in invalid else v) for k, v in values.items()}
        rows.append(Row(values.pop("id", None), values,
                        len(json.dumps(r, separators=(",", ":"), ensure_ascii=False).encode("utf-8")), invalid))
    return build_manifest(tool, request, rows, mode="IN_MEMORY", results=envelope.get("results"),
                          paging=envelope.get("paging"), body_hash=response_sha256(envelope["response"]),
                          hash_basis="CANONICAL_JSON_OF_RESPONSE", with_row_ids=with_row_ids)


def scan_manifest(tool: str, request: Mapping[str, Any], scan: Mapping[str, Any], *,
                  with_row_ids: bool = True) -> dict[str, Any]:
    """STREAMED_SCAN manifest built from stream_scan.IdScanner output (ids and row sizes only)."""
    rows = [Row(r.get("id"), {k: v for k, v in r.items() if k not in ("id", "size", "invalid")}, r.get("size"),
                frozenset(r.get("invalid") or ()))
            for r in scan.get("rows", [])]
    return build_manifest(tool, request, rows, mode="STREAMED_SCAN", results=scan.get("results"),
                          paging=scan.get("paging"), body_hash=scan.get("body_sha256"),
                          hash_basis="RAW_BODY_BYTES", with_row_ids=with_row_ids,
                          bytes_scanned=scan.get("bytes"))


_NARROWING = {
    "apifootball_fixtures": "Add league and season (or team/venue with season) to a date or live query.",
    "apifootball_leagues": "Filter by country (names from apifootball_countries), type, season, current or id.",
    "apifootball_injuries": "Use fixture, ids (<= 20) or a team/player with season instead of league+season.",
    "apifootball_transfers": "Use player instead of team.",
    "apifootball_teams": "Add league and season, or use id/search.",
    "apifootball_venues": "Use id, name or city instead of country.",
}


def read_cap_navigation(tool: str, request: Mapping[str, Any] | None = None, *,
                        reason: str = "READ_CAP_EXCEEDED_BEFORE_PARSE") -> dict[str, Any]:
    """No identifiers could be listed (endpoint not scannable, or a scan bound was hit): a
    narrowing hint only, never a completeness claim. An oversized `ids` request is halved."""
    request = request or {}
    out: dict[str, Any] = {"complete_retrieval_available": False, "reason": reason, "status": "NOT_COMPLETE",
                           "row_ids": None, "calls": None,
                           "narrowing": _NARROWING.get(tool, "Add a documented filter that narrows the listing."),
                           "note": "Rows of this reply were not listed; no completeness is claimed."}
    ids = str(request.get("ids", "")).split("-") if tool == "apifootball_fixtures" and request.get("ids") else []
    if len(ids) > 1:
        half = (len(ids) + 1) // 2
        carried = {k: request[k] for k in ("timezone",) if k in request}
        out["narrowing"] = "Halve the ids list: call each half separately."
        out["calls"] = [{"arguments": mcp_arguments({**({"id": int(p[0])} if len(p) == 1 else {"ids": "-".join(p)}),
                                                    **carried})}
                        for p in (ids[:half], ids[half:])]
    elif tool == "apifootball_fixtures" and request.get("ids"):
        out["narrowing"] = "A single fixture above the bound cannot be split with documented parameters."
    return out
