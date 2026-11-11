"""Offline certification matrix for API-FOOTBALL (scripts/apifootball_cert_matrix.py).

No provider, MCP, Render or database call: the matrix is built from the repository's own sources and
every socket connection is refused while it is built.
"""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("apifootball_cert_matrix", ROOT / "scripts" / "apifootball_cert_matrix.py")
matrix = importlib.util.module_from_spec(_SPEC)
sys.modules["apifootball_cert_matrix"] = matrix
_SPEC.loader.exec_module(matrix)


@pytest.fixture(scope="module")
def built():
    original = socket.socket.connect

    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted while building the matrix")

    socket.socket.connect = refuse
    try:
        rows = matrix.build_rows()
        again = matrix.build_rows()
        schemas = matrix.mcp_schemas()
    finally:
        socket.socket.connect = original
    return rows, again, schemas


def test_matrix_is_deterministic(built):
    rows, again, _ = built
    assert rows == again
    assert matrix.render(rows) == matrix.render(again)
    ids = [r["row_id"] for r in rows]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert all(list(r) == list(matrix.COLUMNS) for r in rows)


def test_every_apifootball_mcp_tool_appears(built):
    rows, _, schemas = built
    tools = {name for name in schemas if name.startswith("apifootball_")}
    assert len(tools) == 42
    assert tools == {r["tool"] for r in rows if r["tool"].startswith("apifootball_")}
    from bridge.api_football.contracts import ENDPOINTS
    for tool in ENDPOINTS:
        assert f"{tool}|BEHAVIOUR|ROUTE_CALL" in {r["row_id"] for r in rows}


def test_every_schema_and_contract_parameter_appears(built):
    rows, _, schemas = built
    from bridge.api_football.contracts import ENDPOINTS
    params = {(r["tool"], r["mcp_name"]): r for r in rows if r["kind"] == "PARAMETER" and r["tool"]}
    for tool, schema in schemas.items():
        for name in schema.get("properties", {}):
            row = params[(tool, name)]
            assert row["mcp_exposed"] == "true"
            assert row["mcp_required"] == ("true" if name in schema.get("required", []) else "false")
    for tool, endpoint in ENDPOINTS.items():
        for field in endpoint.fields:
            row = next(r for r in rows if r["row_id"] == f"{tool}|PARAMETER|{field}")
            assert row["mcp_exposed"] == "true", row["row_id"]
            assert row["contract_required"] == ("true" if field in endpoint.required else "false")
    assert sum(1 for r in rows if r["kind"] == "PARAMETER" and r["tool"]) == sum(len(e.fields) for e in ENDPOINTS.values())
    # No invented parameter in either direction (CLAUDE.md §4).
    assert not [r["row_id"] for r in rows if r["contract_status"] == "NOT_IN_CONTRACT"]


def test_vocabularies_and_honesty(built):
    rows, _, _ = built
    for r in rows:
        assert r["verdict"] in matrix.VERDICTS, r["row_id"]
        assert r["live_requirement"] in matrix.LIVE_REQUIREMENTS, r["row_id"]
        assert r["gate_status"] in matrix.GATE_STATUSES, r["row_id"]
        assert r["offline_status"] in ("COVERED", "NOT_COVERED"), r["row_id"]
        assert (r["offline_status"] == "COVERED") == bool(r["offline_tests"]), r["row_id"]
        # The official page was not reachable (Cloudflare 403, not bypassed): nothing is MATCH today.
        assert r["docs_status"] == "DOCS_UNVERIFIED"
        if r["gate_status"] == "PASS":
            assert set(r["live_evidence_scope"].split(";")) - matrix._STALE_SCOPES, r["row_id"]
        if r["verdict"] == "NOT_TESTED":
            assert r["gate_status"] in ("NOT_TESTED", "BLOCKED", "NOT_APPLICABLE"), r["row_id"]
        if r["verdict"] != "NOT_TESTED":
            assert r["live_evidence_source"], r["row_id"]


def test_cited_test_ids_exist(built):
    rows, _, _ = built
    cited = {t for r in rows for t in r["offline_tests"].split(";") if t}
    for test_id in sorted(cited):
        path, *names = test_id.split("::")
        text = (ROOT / path).read_text(encoding="utf-8")
        assert f"def {names[-1]}(" in text, test_id
        assert path != matrix.SELF_TEST


def test_known_live_evidence_is_cited_and_stale(built):
    rows, _, _ = built
    by_id = {r["row_id"]: r for r in rows}
    a04 = by_id["apifootball_fixtures|RULE|F-REJECTED-season+venue"]
    assert a04["verdict"] == "ERROR" and a04["live_requirement"] == "OFFLINE_ONLY"
    assert "a04_fixtures_venue556_season2026_declared_errors.json" in a04["live_evidence_source"]
    odds_live = by_id["apifootball_live_odds|BEHAVIOUR|ROUTE_CALL"]
    assert odds_live["verdict"] == "DATA_PRESENT" and odds_live["gate_status"] == "STALE"
    assert odds_live["live_requirement"] == "DATA_DEPENDENT"
    assert by_id["apifootball_coverage_report|BEHAVIOUR|ROUTE_CALL"]["verdict"] == "NOT_TESTED"


def test_check_mode_passes_on_committed_matrix(capsys):
    assert matrix.main(["--check"]) == 0
    assert "MATRIX_UP_TO_DATE" in capsys.readouterr().out


def test_gate_mode_exit_code_matches_gate(built, capsys):
    rows, _, _ = built
    ok, _failing = matrix.gate(rows)
    assert matrix.main(["--gate"]) == (0 if ok else 1)
    out = capsys.readouterr().out
    assert ("GATE PASS" if ok else "GATE FAIL") in out


def _synthetic(requirement, gate_status, row_id="x"):
    row = {c: "" for c in matrix.COLUMNS}
    row.update(row_id=row_id, live_requirement=requirement, gate_status=gate_status)
    return row


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED", "NOT_TESTED", "STALE"])
def test_gate_fails_on_mandatory_rows_not_passing(status):
    ok, failing = matrix.gate([_synthetic("MANDATORY", "PASS", "a"), _synthetic("MANDATORY", status, "b")])
    assert not ok and [r["row_id"] for r in failing] == ["b"]


@pytest.mark.parametrize("requirement", ["DATA_DEPENDENT", "OFFLINE_ONLY"])
@pytest.mark.parametrize("status", ["FAIL", "BLOCKED", "NOT_TESTED", "STALE", "NOT_APPLICABLE"])
def test_gate_ignores_non_mandatory_rows(requirement, status):
    ok, failing = matrix.gate([_synthetic("MANDATORY", "PASS"), _synthetic(requirement, status)])
    assert ok and failing == []


def test_gate_passes_on_all_mandatory_pass_and_on_empty():
    assert matrix.gate([_synthetic("MANDATORY", "PASS")]) == (True, [])
    assert matrix.gate([]) == (True, [])


def test_evidence_classification():
    fresh = {"verdict": "DATA_PRESENT", "source": "s", "scope": "MCP_CURRENT_MAIN"}
    stale = {"verdict": "DATA_PRESENT", "source": "s", "scope": "RAW_HTTPX_SCRIPT_2026-10-25"}
    error = {"verdict": "ERROR", "source": "e", "scope": "MCP_CURRENT_MAIN"}
    blocked = {"verdict": "BLOCKED", "source": "b", "scope": "RAW_HTTPX_SCRIPT_2026-10-25"}
    f = matrix._evidence_fields
    assert f(None, "MANDATORY")["gate_status"] == "NOT_TESTED"
    assert f(None, "OFFLINE_ONLY")["gate_status"] == "NOT_APPLICABLE"
    assert f([fresh], "MANDATORY")["gate_status"] == "PASS"
    assert f([stale], "MANDATORY")["gate_status"] == "STALE"
    assert f([stale, fresh], "MANDATORY")["gate_status"] == "PASS"
    assert f([error], "MANDATORY")["gate_status"] == "FAIL"
    assert f([error], "MANDATORY", expected_error=True)["gate_status"] == "PASS"
    out = f([blocked], "MANDATORY")
    assert out["gate_status"] == "BLOCKED" and out["verdict"] == "NOT_TESTED"
    best = f([blocked, stale], "DATA_DEPENDENT")
    assert best["verdict"] == "DATA_PRESENT" and best["gate_status"] == "STALE"
