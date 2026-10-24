"""Change detector tests. The OpenAPI documents below are SYNTHETIC test inputs,
not API-FOOTBALL contracts; they never feed the registry."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from bridge.api_football import contracts, registry

ROOT = Path(__file__).resolve().parents[2]


def synthetic_spec():
    return {
        "openapi": "3.0.0",
        "info": {"title": "SYNTHETIC", "version": "0.0-test"},
        "components": {"parameters": {"Fixture": {"name": "fixture", "in": "query", "required": True,
                                                  "schema": {"type": "integer", "minimum": 1}}}},
        "paths": {
            "/fixtures": {"get": {"summary": "s", "parameters": [
                {"name": "id", "in": "query", "schema": {"type": "integer"}},
                {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["NS", "FT"], "maxLength": 20}},
                {"name": "x-header", "in": "header", "schema": {"type": "string"}},
            ], "responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object", "description": "envelope", "properties": {"response": {"type": "array"}}}}}},
                "499": {"description": "synthetic error"}}}},
            "/predictions": {"get": {"parameters": [{"$ref": "#/components/parameters/Fixture"}],
                                     "responses": {"200": {"description": "ok"}}}},
        },
    }


def normalized(spec=None):
    return contracts.normalize_openapi(spec or synthetic_spec())


def kinds(changes):
    return {(c.classification, c.kind, c.parameter) for c in changes}


def test_normalization_is_deterministic_and_resolves_refs():
    first, second = normalized(), normalized(json.loads(json.dumps(synthetic_spec())))
    assert contracts.sha256(first) == contracts.sha256(second)
    assert set(first["endpoints"]["/fixtures"]["params"]) == {"id", "status"}  # header params excluded
    assert first["endpoints"]["/predictions"]["params"]["fixture"]["required"] is True
    assert first["endpoints"]["/predictions"]["params"]["fixture"]["minimum"] == 1


def test_identical_sources_produce_no_changes():
    assert contracts.diff(normalized(), normalized()) == []


@pytest.mark.parametrize("text,reason", [
    ("", "EMPTY_SOURCE"),
    ("<html><title>Just a moment...</title></html>", "CLOUDFLARE_CHALLENGE"),
    ("<html><body>docs</body></html>", "NO_MACHINE_READABLE_CONTRACT"),
    ('{"openapi": "3.0.0", "paths": {}}', "NO_PATHS_SECTION"),
])
def test_unavailable_or_incomplete_sources_are_refused(text, reason):
    with pytest.raises(contracts.DocumentationUnavailable, match=reason):
        contracts.extract_spec(text)


def test_redoc_html_state_is_extracted():
    html = ("<html><script>const __redoc_state = " + json.dumps({"spec": {"data": synthetic_spec()}})
            + ";</script></html>")
    assert contracts.extract_spec(html)["info"]["title"] == "SYNTHETIC"


def test_registry_view_has_names_only_and_filling_it_is_additive():
    view = contracts.registry_view(registry.load_raw())
    assert view["endpoints"]["/fixtures"]["params"]["from"] == {"known": False}
    assert view["endpoints"]["/timezone"]["params"] is None
    changes = contracts.diff(view, normalized())
    by_path = {(c.path, c.kind) for c in changes}
    assert ("/fixtures", "PARAMETER_REMOVED") in by_path   # owner baseline names absent from the synthetic source
    assert ("/predictions", "CONTRACT_FILLED") in by_path
    # 37 registry endpoints missing from the 2-endpoint synthetic source must block, not be silently dropped.
    removed = [c for c in changes if c.kind == "ENDPOINT_REMOVED"]
    assert len(removed) == 37 and all(c.classification == "BREAKING" for c in removed)
    assert contracts.summarize(changes)["blocking"] is True


def mutated(mutation):
    spec = synthetic_spec()
    mutation(spec)
    return contracts.diff(normalized(), normalized(spec))


def params(spec, path="/fixtures"):
    return spec["paths"][path]["get"]["parameters"]


@pytest.mark.parametrize("mutation,expected", [
    (lambda s: s["paths"].update({"/odds": {"get": {"responses": {}}}}), ("ADDITIVE", "ENDPOINT_ADDED", None)),
    (lambda s: s["paths"].pop("/predictions"), ("BREAKING", "ENDPOINT_REMOVED", None)),
    (lambda s: s["paths"]["/fixtures"]["get"].update(deprecated=True), ("BREAKING", "ENDPOINT_DEPRECATED", None)),
    (lambda s: params(s).append({"name": "team", "in": "query", "schema": {"type": "integer"}}),
     ("ADDITIVE", "PARAMETER_ADDED", "team")),
    (lambda s: params(s).append({"name": "team", "in": "query", "required": True, "schema": {"type": "integer"}}),
     ("BREAKING", "PARAMETER_ADDED", "team")),
    (lambda s: params(s).pop(0), ("BREAKING", "PARAMETER_REMOVED", "id")),
    (lambda s: params(s)[0].update(required=True), ("BREAKING", "PARAMETER_BECAME_REQUIRED", "id")),
    (lambda s: s["components"]["parameters"]["Fixture"].update(required=False),
     ("ADDITIVE", "PARAMETER_BECAME_OPTIONAL", "fixture")),
    (lambda s: params(s)[0]["schema"].update(type="string"), ("BREAKING", "TYPE_CHANGED", "id")),
    (lambda s: params(s)[1]["schema"].update(enum=["NS"]), ("BREAKING", "ENUM_VALUES_REMOVED", "status")),
    (lambda s: params(s)[1]["schema"].update(enum=["NS", "FT", "HT"]), ("ADDITIVE", "ENUM_VALUES_ADDED", "status")),
    (lambda s: params(s)[1]["schema"].update(maxLength=10), ("BREAKING", "CONSTRAINT_CHANGED", "status")),
    (lambda s: params(s)[1]["schema"].update(maxLength=40), ("ADDITIVE", "CONSTRAINT_CHANGED", "status")),
    (lambda s: params(s)[0]["schema"].update(maximum=99), ("BREAKING", "CONSTRAINT_ADDED", "id")),
    (lambda s: params(s).append({"name": "page", "in": "query", "schema": {"type": "integer"}}),
     ("BREAKING", "PAGINATION_CHANGED", None)),
    (lambda s: s["paths"]["/fixtures"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
     ["properties"].update(paging={"type": "object"}), ("UNKNOWN", "RESPONSE_SCHEMA_CHANGED", None)),
    (lambda s: s["paths"]["/fixtures"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
     .update(description="other words"), ("DOCUMENTATION_ONLY", "RESPONSE_DOCS_CHANGED", None)),
    (lambda s: s["paths"]["/fixtures"]["get"]["responses"].update({"498": {"description": "new"}}),
     ("UNKNOWN", "ERRORS_CHANGED", None)),
    (lambda s: s["paths"]["/fixtures"]["get"].update(summary="reworded"),
     ("DOCUMENTATION_ONLY", "DESCRIPTION_CHANGED", None)),
])
def test_changes_are_classified(mutation, expected):
    assert expected in kinds(mutated(mutation))


def test_documentation_only_changes_do_not_block():
    changes = mutated(lambda s: s["paths"]["/fixtures"]["get"].update(summary="reworded"))
    assert contracts.summarize(changes) == {
        "counts": {"ADDITIVE": 0, "BREAKING": 0, "DOCUMENTATION_ONLY": 1, "UNKNOWN": 0}, "blocking": False,
        "changes": [{"classification": "DOCUMENTATION_ONLY", "kind": "DESCRIPTION_CHANGED", "path": "/fixtures",
                     "parameter": None, "detail": "operation text changed"}]}


def test_unknown_detail_is_never_treated_as_removal():
    old = normalized()
    new = copy.deepcopy(old)
    new["endpoints"]["/fixtures"]["params"] = None
    assert kinds(contracts.diff(old, new)) == {("UNKNOWN", "PARAMETERS_NOT_IN_SOURCE", None)}


@pytest.fixture
def script(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("af_contract_script_ingest",
                                                  ROOT / "scripts" / "check_api_football_contracts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SNAPSHOTS", tmp_path / "snapshots")
    monkeypatch.setattr(module, "REPORTS", tmp_path / "reports")
    return module


def registry_bytes():
    return {path.name: path.read_bytes() for path in registry.REGISTRY_DIR.glob("*.json")}


def test_ingest_of_challenge_page_writes_nothing(script, tmp_path, capsys):
    source = tmp_path / "page.html"
    source.write_text("<html><title>Just a moment...</title></html>")
    before = registry_bytes()
    code = script.main(["ingest", "--source", str(source), "--source-url", "https://example.invalid/doc",
                        "--method", "test"])
    assert code == 3 and "DOCUMENTATION_UNAVAILABLE" in capsys.readouterr().out
    assert not (tmp_path / "snapshots").exists() and registry_bytes() == before


def test_ingest_writes_hashed_snapshot_and_report_but_never_edits_registry(script, tmp_path, capsys):
    source = tmp_path / "spec.json"
    source.write_text(json.dumps(synthetic_spec()))
    before = registry_bytes()
    code = script.main(["ingest", "--source", str(source), "--source-url", "https://example.invalid/doc",
                        "--method", "test"])
    assert code == 4  # synthetic source misses registry endpoints: blocking
    assert registry_bytes() == before
    snapshot_file = next((tmp_path / "snapshots").glob("*.json"))
    data = json.loads(snapshot_file.read_text())
    assert data["normalized_sha256"] == contracts.sha256(data["contract"])
    assert "<html" not in snapshot_file.read_text()
    report = json.loads(next((tmp_path / "reports").glob("change_report_*.json")).read_text())
    assert report["registry_modified"] is False and report["against_registry"]["blocking"] is True
    # A second identical ingest compares cleanly against the previous snapshot.
    capsys.readouterr()
    script.main(["ingest", "--source", str(source), "--source-url", "https://example.invalid/doc", "--method", "test"])
    reports = sorted((tmp_path / "reports").glob("change_report_*.json"))
    latest = json.loads(reports[-1].read_text())
    assert latest["against_previous_snapshot"]["counts"] == {"ADDITIVE": 0, "BREAKING": 0,
                                                             "DOCUMENTATION_ONLY": 0, "UNKNOWN": 0}
