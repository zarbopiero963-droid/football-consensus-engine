"""WP3: resumable, quota-bounded scan of a paginated API-FOOTBALL endpoint (library only).

No MCP tool uses this module yet (exposure is an owner decision); it performs no I/O of its own.

* Only endpoints whose contract (contracts.ENDPOINTS) declares the documented `page` parameter
  can be scanned; the pager owns `page`, every other parameter is validated by the contract.
* One provider request per page, each through ResilientFootballClient.get(): the shared
  PostgreSQL quota coordinator (one lease per real attempt), rate limits, concurrency, circuit
  breaker and retry policy all apply unchanged. Any other client is refused.
* No page cap other than the caller's explicit, required `max_pages` budget, the quota
  coordinator and the transport's own paging bound (contracts.MAX_PAGE).
* A scan stops when the last page is fetched (COMPLETE), when the budget is spent or a call is
  refused or fails (PARTIAL, with the exact page to resume from and the pages already fetched),
  or on any inconsistency (REFUSED, fixed code, no rows presented as a dataset, no resume).
  Provider and quota failures keep the existing fixed codes and classification (tools._error_for).
* Missing stays missing: no `results`, no `paging` or a row without its natural id is refused,
  never defaulted. Rows are de-duplicated by the endpoint's natural id (continuation.ROW_FIELDS):
  a repeated id is an inconsistency (the docs document no overlap between pages). Without a
  natural id nothing is dropped; repeated rows are counted by full-row hash.
* to_payload() never exceeds the 256 KiB MCP cap: above it the rows are withheld and an explicit
  manifest (ids per page, then counts and hashes only) is returned; no prices, no truncated list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from .continuation import ROW_FIELDS, response_sha256, valid_id
from .contracts import ENDPOINTS, MAX_PAGE, ParameterError, validate

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
REFUSED = "REFUSED"
MCP_PAYLOAD_CAP_BYTES = 262_144  # == tools._MAX_MCP_DATA_BYTES
LABEL = "PROVIDER_ROWS_WITH_DERIVED_SCAN_STATE"

# Client/transport codes meaning the reply cannot be trusted as data: the scan is refused.
# Every other failure (quota, rate limit, concurrency, circuit, transport, HTTP status) only
# stops the scan, which stays resumable from the failed page.
_UNTRUSTED_REPLY_CODES = frozenset({
    "PROVIDER_DECLARED_ERRORS", "PROVIDER_INVALID_PAGINATION", "PROVIDER_INVALID_JSON",
    "PROVIDER_JSON_NOT_OBJECT", "PROVIDER_RESPONSE_FIELD_MISSING", "PROVIDER_REPLY_CONTRACT_INVALID",
})


class _Inconsistent(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def pageable_tools() -> tuple[str, ...]:
    return tuple(n for n, e in ENDPOINTS.items() if e.pagination and "page" in e.fields)


def _fingerprint(tool: str, params: Mapping[str, Any]) -> str:
    canonical = json.dumps([tool, sorted((k, type(v).__name__, v) for k, v in params.items())],
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_hash(row: Any) -> str:
    return response_sha256(row)


@dataclass(frozen=True)
class ResumeToken:
    """Where a scan continues: plain page numbers and the ids already seen (never a secret).
    `covered_from_page` is the first page of the chain of scans; only a chain that starts at
    page 1 and reaches the last page can be COMPLETE."""
    tool: str
    request_fingerprint: str
    next_page: int
    total: int | None
    covered_from_page: int
    seen_ids: frozenset = frozenset()
    seen_row_hashes: frozenset = frozenset()
    rows_retained: bool = True

    @classmethod
    def from_page(cls, tool: str, params: Mapping[str, Any], page: int) -> "ResumeToken":
        """Start at `page` with nothing known about earlier pages (never COMPLETE)."""
        return cls(tool, _fingerprint(tool, dict(params)), page, None, page)


@dataclass
class ScanResult:
    status: str
    tool: str
    endpoint: str | None
    parameters: dict[str, Any]
    start_page: int | None
    covered_from_page: int | None
    reason: str | None = None
    error: dict[str, Any] | None = None
    paging_total: int | None = None
    next_page: int | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)
    rows: list[Any] | None = None
    row_ids_by_page: dict[int, list[int]] = field(default_factory=dict)
    rows_not_retained_pages: list[int] = field(default_factory=list)
    natural_id: str | None = None
    duplicate_rows_by_hash: int = 0
    network_calls_made: int = 0
    _token: ResumeToken | None = field(default=None, repr=False)

    @property
    def pages_fetched(self) -> int:
        return len(self.pages)

    def resume_token(self) -> ResumeToken:
        if self._token is None:
            raise ValueError("NO_RESUME_POINT")
        return self._token

    def to_payload(self, max_bytes: int = MCP_PAYLOAD_CAP_BYTES) -> dict[str, Any]:
        """A JSON-safe dict no larger than `max_bytes`: the rows when they fit, else an explicit
        manifest (ids per page, then counts and hashes only). Nothing is cut silently."""
        out: dict[str, Any] = {
            "label": LABEL, "status": self.status, "reason": self.reason, "error": self.error,
            "tool": self.tool, "endpoint": self.endpoint, "parameters": self.parameters,
            "start_page": self.start_page, "covered_from_page": self.covered_from_page,
            "pages_fetched": self.pages_fetched, "paging_total": self.paging_total,
            "next_page": self.next_page, "natural_id": self.natural_id,
            "rows_count": None if self.rows is None else len(self.rows),
            "rows_not_retained_pages": list(self.rows_not_retained_pages),
            "duplicate_rows_by_hash": self.duplicate_rows_by_hash,
            "network_calls_made": self.network_calls_made, "auto_fetch": False,
            "pages": self.pages, "pages_status": "INCLUDED",
            "rows": self.rows,
            "rows_status": "INCLUDED" if self.rows is not None else "NOT_PRESENTED_REFUSED_SCAN",
            "row_ids_by_page": None,
        }
        if self.status != REFUSED and self.rows_not_retained_pages:
            out["rows_status"] = "INCLUDED_EXCEPT_PAGES_NOT_RETAINED"
        if _size(out) <= max_bytes:
            return out
        if self.rows is not None:
            out["rows"] = None
            out["rows_status"] = "WITHHELD_ABOVE_MCP_PAYLOAD_CAP"
            out["row_ids_by_page"] = {str(p): ids for p, ids in self.row_ids_by_page.items()}
            if _size(out) <= max_bytes:
                return out
            out["row_ids_by_page"] = None
            out["row_ids_status"] = "WITHHELD_ABOVE_MCP_PAYLOAD_CAP"
            out["row_ids_sha256"] = hashlib.sha256(json.dumps(
                sorted(i for ids in self.row_ids_by_page.values() for i in ids)).encode()).hexdigest()
            if _size(out) <= max_bytes:
                return out
        out["pages_sha256"] = response_sha256(self.pages)
        out["pages"] = None
        out["pages_status"] = "WITHHELD_ABOVE_MCP_PAYLOAD_CAP"
        return out


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _refused(tool: str, code: str, **kw: Any) -> ScanResult:
    return ScanResult(REFUSED, tool, None, {}, None, None, error={"code": code}, **kw)


def _dig(row: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def _paging_int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_PAGE


def _real_calls(outcome: Mapping[str, Any]) -> int:
    if outcome.get("cache_status") == "HIT" or outcome.get("shared_flight") is True:
        return 0
    n = outcome.get("provider_attempts")
    return n if type(n) is int and 0 <= n <= 3 else 1


async def scan_pages(client: Any, tool: str, params: Mapping[str, Any], *, max_pages: int,
                     resume: ResumeToken | None = None) -> ScanResult:
    """Scan pages of `tool` from page 1 (or `resume.next_page`) until the last page, the
    `max_pages` budget (required, no default), a refusal or an inconsistency."""
    from .resilience import ResilientFootballClient
    from .tools import _error_for

    if not isinstance(client, ResilientFootballClient):
        return _refused(tool, "PAGER_CLIENT_NOT_COORDINATED")
    contract = ENDPOINTS.get(tool)
    if contract is None:
        return _refused(tool, "UNKNOWN_TOOL")
    if tool not in pageable_tools():
        return _refused(tool, "PAGINATION_NOT_DOCUMENTED")
    if not isinstance(params, Mapping):
        return _refused(tool, "INVALID_PARAMETER_MAP")
    if "page" in params:
        return _refused(tool, "PAGE_PARAMETER_RESERVED")
    try:
        _, checked = validate(tool, params)
    except ParameterError as exc:
        return _refused(tool, str(exc))
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGE:
        return _refused(tool, "INVALID_PAGE_BUDGET")
    fingerprint = _fingerprint(tool, checked)
    if resume is not None:
        if (not isinstance(resume, ResumeToken) or resume.tool != tool
                or resume.request_fingerprint != fingerprint):
            return _refused(tool, "RESUME_REQUEST_MISMATCH")
        if (type(resume.next_page) is not int or not 1 <= resume.next_page <= MAX_PAGE
                or type(resume.covered_from_page) is not int
                or not 1 <= resume.covered_from_page <= resume.next_page
                or (resume.total is not None and not _paging_int(resume.total))):
            return _refused(tool, "INVALID_RESUME")
    settings = client.settings
    if not settings.api_key:
        return _refused(tool, "KEY_NOT_CONFIGURED")
    if not settings.real_requests_authorized:
        return _refused(tool, "LIVE_NOT_AUTHORIZED")

    start = resume.next_page if resume else 1
    fields = ROW_FIELDS.get(contract.path, {})
    id_path = fields.get("id")
    result = ScanResult(PARTIAL, tool, contract.path, dict(checked), start,
                        resume.covered_from_page if resume else 1,
                        natural_id=".".join(id_path) if id_path else None, rows=[])
    total = resume.total if resume else None
    seen_ids = set(resume.seen_ids) if resume else set()
    seen_hashes = set(resume.seen_row_hashes) if resume else set()
    rows_retained = resume.rows_retained if resume else True

    def refuse(code: str, page: int, error: dict[str, Any] | None = None) -> ScanResult:
        result.status, result.reason, result.rows = REFUSED, code, None
        result.error = {**(error or {"code": code}), "page": page}
        result.next_page, result._token = None, None
        return result

    page = start
    while True:
        try:
            outcome = await client.get(contract.path, {**checked, "page": page})
        except Exception as exc:
            error = _error_for(exc, contract.fields)
            attempts = getattr(exc, "provider_attempts", 0)
            result.network_calls_made += attempts if type(attempts) is int and 0 <= attempts <= 3 else 0
            if error["code"] in _UNTRUSTED_REPLY_CODES:
                return refuse(error["code"], page, error)
            result.status, result.reason, result.error = PARTIAL, error["code"], {**error, "page": page}
            result.next_page = page
            break
        result.network_calls_made += _real_calls(outcome) if isinstance(outcome, dict) else 0
        try:
            evidence, rows, ids, page_total = _check_page(outcome, page, total, id_path)
        except _Inconsistent as exc:
            return refuse(exc.code, page)
        for i in ids:
            if i in seen_ids:
                return refuse("DUPLICATE_NATURAL_ID", page)
            seen_ids.add(i)
        if id_path is None and rows is not None:
            for row in rows:
                h = _row_hash(row)
                if h in seen_hashes:
                    result.duplicate_rows_by_hash += 1
                seen_hashes.add(h)
        total = page_total
        result.paging_total = total
        result.pages.append(evidence)
        if id_path is not None:
            result.row_ids_by_page[page] = ids
        if rows is None:
            rows_retained = False
            result.rows_not_retained_pages.append(page)
        else:
            result.rows.extend(rows)
        if page >= total:  # the last page (or an empty listing on page 1)
            result.next_page = None
            if not rows_retained:
                result.reason = "PAGE_ROWS_NOT_RETAINED_ABOVE_READ_CAP"
            elif result.covered_from_page != 1:
                result.reason = "PAGES_BEFORE_START_NOT_COVERED"
            else:
                result.status = COMPLETE
            return result
        if len(result.pages) >= max_pages:
            result.reason, result.next_page = "PAGE_BUDGET_EXHAUSTED", page + 1
            break
        page += 1
    result._token = ResumeToken(tool, fingerprint, result.next_page, total, result.covered_from_page,
                                frozenset(seen_ids), frozenset(seen_hashes), rows_retained)
    return result


def _check_page(outcome: Any, page: int, known_total: int | None,
                id_path: tuple[str, ...] | None) -> tuple[dict[str, Any], list | None, list[int], int]:
    """(evidence, rows or None when not retained, natural ids, total) or _Inconsistent."""
    if not isinstance(outcome, dict):
        raise _Inconsistent("PROVIDER_REPLY_CONTRACT_INVALID")
    scan = outcome.get("scan")
    if isinstance(scan, dict):
        if scan.get("errors_nonempty"):
            raise _Inconsistent("PROVIDER_DECLARED_ERRORS")
        paging, results, raw_rows = scan.get("paging"), scan.get("results"), scan.get("rows")
        body_hash, basis, mode = scan.get("body_sha256"), "RAW_BODY_BYTES", "STREAMED_SCAN_IDS_ONLY"
        if not isinstance(raw_rows, list):
            raise _Inconsistent("RESPONSE_NOT_LIST")
        row_count = len(raw_rows)
        ids_raw = [None if "id" in (r.get("invalid") or ()) else r.get("id")
                   for r in raw_rows if isinstance(r, dict)]
        if len(ids_raw) != row_count:
            raise _Inconsistent("PROVIDER_REPLY_CONTRACT_INVALID")
        rows = None
    else:
        data = outcome.get("data")
        if not isinstance(data, dict):
            raise _Inconsistent("PROVIDER_REPLY_CONTRACT_INVALID")
        if data.get("errors") not in (None, [], {}):
            raise _Inconsistent("PROVIDER_DECLARED_ERRORS")
        paging, results, rows = data.get("paging"), data.get("results"), data.get("response")
        if not isinstance(rows, list):
            raise _Inconsistent("RESPONSE_NOT_LIST")
        body_hash, basis, mode = response_sha256(rows), "CANONICAL_JSON_OF_RESPONSE", "IN_MEMORY"
        row_count = len(rows)
        ids_raw = [_dig(r, id_path) for r in rows] if id_path else []
    if paging is None:
        raise _Inconsistent("PAGING_NOT_PUBLISHED")
    if not isinstance(paging, dict) or not _paging_int(paging.get("current")) \
            or not _paging_int(paging.get("total")):
        raise _Inconsistent("PAGING_VALUE_INVALID")
    current, total = paging["current"], paging["total"]
    if current != page:
        raise _Inconsistent("PAGE_ECHO_MISMATCH")
    if current > total and not (page == 1 and total == 0):
        raise _Inconsistent("PAGE_BEYOND_TOTAL")
    if known_total is not None and total != known_total:
        raise _Inconsistent("PAGING_TOTAL_CHANGED")
    if results is None:
        raise _Inconsistent("RESULTS_NOT_PUBLISHED")
    if type(results) is not int or results != row_count:
        raise _Inconsistent("RESULTS_ROWS_MISMATCH")
    if row_count == 0 and not (page == 1 and total <= 1):
        # F-A10: an empty page is never a normal page of a listing (the provider echoes
        # total = requested page beyond the data); only an empty listing on page 1 is.
        raise _Inconsistent("EMPTY_PAGE")
    ids: list[int] = []
    if id_path:
        for value in ids_raw:
            if not valid_id(value):
                raise _Inconsistent("NATURAL_ID_MISSING")
            ids.append(value)
        if len(set(ids)) != len(ids):
            raise _Inconsistent("DUPLICATE_NATURAL_ID")
    evidence = {
        "page": page, "results": results, "rows": row_count,
        "paging": {"current": current, "total": total},
        "response_sha256": body_hash if isinstance(body_hash, str) else None, "hash_basis": basis,
        "rows_mode": mode, "cache_status": outcome.get("cache_status"),
        "provider_attempts": outcome.get("provider_attempts"),
        "http_status": outcome.get("http_status"), "retrieved_at_utc": outcome.get("retrieved_at_utc"),
    }
    return evidence, rows, ids, total


__all__ = ["COMPLETE", "PARTIAL", "REFUSED", "MCP_PAYLOAD_CAP_BYTES", "MAX_PAGE", "ResumeToken",
           "ScanResult", "pageable_tools", "response_sha256", "scan_pages"]
