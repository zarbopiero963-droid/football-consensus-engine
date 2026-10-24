"""Contract normalization, snapshots and semantic change detection.

Pure, offline functions. A detected change is reported, never applied: the
registry is only edited through a reviewed PR. `None` always means "unknown"
and is never treated as a removal.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

ADDITIVE, BREAKING, DOCUMENTATION_ONLY, UNKNOWN = "ADDITIVE", "BREAKING", "DOCUMENTATION_ONLY", "UNKNOWN"
BLOCKING = frozenset({BREAKING, UNKNOWN})
_DOC_KEYS = frozenset({"description", "summary", "title", "example", "examples", "externalDocs"})
_CONSTRAINTS = ("minimum", "maximum", "min_length", "max_length")
# Larger value is stricter for minimum/min_length; smaller is stricter for maximum/max_length.
_STRICTER_WHEN_LARGER = {"minimum": True, "min_length": True, "maximum": False, "max_length": False}


class DocumentationUnavailable(ValueError):
    """The source is not a readable contract (challenge page, empty, missing sections)."""


@dataclass(frozen=True)
class Change:
    classification: str
    kind: str
    path: str
    parameter: str | None
    detail: str


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ extraction

def extract_spec(text: str) -> dict:
    """Return an OpenAPI document from raw JSON or a Redoc HTML page, else raise."""
    stripped = text.lstrip()
    if not stripped:
        raise DocumentationUnavailable("EMPTY_SOURCE")
    if "Just a moment..." in text[:20000] or "cf-challenge" in text[:20000] or "challenge-platform" in text[:20000]:
        raise DocumentationUnavailable("CLOUDFLARE_CHALLENGE")
    if stripped.startswith("{"):
        spec = json.loads(stripped)
    else:
        match = re.search(r"__redoc_state\s*=\s*(\{.*?\})\s*;?\s*</script>", text, re.S)
        if not match:
            raise DocumentationUnavailable("NO_MACHINE_READABLE_CONTRACT")
        state = json.loads(match.group(1))
        spec = (state.get("spec") or {}).get("data")
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict) or not spec["paths"]:
        raise DocumentationUnavailable("NO_PATHS_SECTION")
    return spec


# --------------------------------------------------------------- normalization

def _resolve(spec: dict, node, depth: int = 0):
    if depth > 40:
        return {"$ref": "MAX_DEPTH"}
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = spec
            for part in ref[2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(spec, target, depth + 1)
        return {key: _resolve(spec, value, depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(spec, value, depth + 1) for value in node]
    return node


def _strip_docs(node):
    if isinstance(node, dict):
        return {key: _strip_docs(value) for key, value in node.items() if key not in _DOC_KEYS}
    if isinstance(node, list):
        return [_strip_docs(value) for value in node]
    return node


def _param(raw: dict) -> dict:
    schema = raw.get("schema") or {}
    enum = schema.get("enum")
    return {
        "known": True,
        "required": raw.get("required") if isinstance(raw.get("required"), bool) else False,
        "type": schema.get("type"),
        "format": schema.get("format"),
        "enum": sorted(map(str, enum)) if isinstance(enum, list) else None,
        "minimum": schema.get("minimum"),
        "maximum": schema.get("maximum"),
        "min_length": schema.get("minLength"),
        "max_length": schema.get("maxLength"),
        "pattern": schema.get("pattern"),
        "description_sha256": sha256(raw.get("description")) if raw.get("description") else None,
    }


def normalize_openapi(spec: dict) -> dict:
    endpoints = {}
    for path, item in sorted(spec.get("paths", {}).items()):
        item = _resolve(spec, item)
        operation = item.get("get")
        if not isinstance(operation, dict):
            continue
        params = {}
        for raw in list(item.get("parameters", [])) + list(operation.get("parameters", [])):
            if isinstance(raw, dict) and raw.get("in") == "query" and raw.get("name"):
                params[raw["name"]] = _param(raw)
        responses = operation.get("responses") or {}
        ok = ((responses.get("200") or {}).get("content") or {}).get("application/json", {}).get("schema")
        errors = {code: _strip_docs(value) for code, value in responses.items() if str(code) != "200"}
        endpoints[path] = {
            "method": "GET",
            "deprecated": bool(operation.get("deprecated", False)),
            "description_sha256": sha256([operation.get("summary"), operation.get("description")]),
            "params": dict(sorted(params.items())),
            "pagination": {"page_param": "page" in params},
            "response_schema_sha256": sha256(_strip_docs(ok)) if ok is not None else None,
            "response_schema_doc_sha256": sha256(ok) if ok is not None else None,
            "errors_sha256": sha256(errors) if errors else None,
        }
    return {"endpoints": endpoints}


def registry_view(raw: dict) -> dict:
    """The registry expressed in normalized form; every unverified field is None."""
    params = raw["parameters"]["endpoints"]
    endpoints = {}
    for endpoint in raw["endpoints"]["endpoints"]:
        entry = params[endpoint["id"]]
        listed = entry.get("params")
        endpoints[endpoint["path"]] = {
            "method": endpoint["method"],
            "deprecated": endpoint["deprecated"] if isinstance(endpoint.get("deprecated"), bool) else None,
            "description_sha256": None,
            "params": None if listed is None else {
                # Names only: every other attribute is unknown, not absent.
                item["wire"]: {"known": False} for item in listed},
            "pagination": None,
            "response_schema_sha256": None,
            "response_schema_doc_sha256": None,
            "errors_sha256": None,
        }
    return {"endpoints": endpoints}


def snapshot(normalized: dict, source_url: str, acquired_at_utc: str, method: str) -> dict:
    return {"source_url": source_url, "acquired_at_utc": acquired_at_utc, "acquisition_method": method,
            "normalized_sha256": sha256(normalized), "endpoint_count": len(normalized["endpoints"]),
            "contract": normalized}


# ---------------------------------------------------------------------- diffing

def _param_changes(path: str, name: str, old: dict, new: dict) -> list[Change]:
    out = []

    def add(classification, kind, detail):
        out.append(Change(classification, kind, path, name, detail))

    if not old.get("known"):
        if new.get("known"):
            add(ADDITIVE, "CONTRACT_FILLED", "parameter attributes documented")
        return out
    if not new.get("known"):
        add(UNKNOWN, "PARAMETER_DETAIL_NOT_IN_SOURCE", "source carries no attribute detail")
        return out
    if old["required"] is False and new["required"] is True:
        add(BREAKING, "PARAMETER_BECAME_REQUIRED", "optional -> required")
    elif old["required"] is True and new["required"] is False:
        add(ADDITIVE, "PARAMETER_BECAME_OPTIONAL", "required -> optional")
    for key in ("type", "format"):
        if old[key] is None and new[key] is not None:
            add(BREAKING, f"{key.upper()}_ADDED", f"{key}={new[key]}")
        elif old[key] is not None and new[key] != old[key]:
            add(BREAKING if new[key] is not None else UNKNOWN, f"{key.upper()}_CHANGED", f"{old[key]} -> {new[key]}")
    if old["enum"] is None and new["enum"] is not None:
        add(BREAKING, "ENUM_ADDED", "values now restricted")
    elif old["enum"] is not None and new["enum"] is not None:
        removed, added = set(old["enum"]) - set(new["enum"]), set(new["enum"]) - set(old["enum"])
        if removed:
            add(BREAKING, "ENUM_VALUES_REMOVED", ", ".join(sorted(removed)))
        if added:
            add(ADDITIVE, "ENUM_VALUES_ADDED", ", ".join(sorted(added)))
    elif old["enum"] is not None and new["enum"] is None:
        add(UNKNOWN, "ENUM_NO_LONGER_DOCUMENTED", "enum absent in new source")
    for key in _CONSTRAINTS:
        before, after = old[key], new[key]
        if before == after:
            continue
        if before is None:
            add(BREAKING, "CONSTRAINT_ADDED", f"{key}={after}")
        elif after is None:
            add(ADDITIVE, "CONSTRAINT_REMOVED", f"{key} removed")
        else:
            stricter = (after > before) == _STRICTER_WHEN_LARGER[key]
            add(BREAKING if stricter else ADDITIVE, "CONSTRAINT_CHANGED", f"{key}: {before} -> {after}")
    if old["pattern"] != new["pattern"] and new["pattern"] is not None:
        add(UNKNOWN if old["pattern"] is not None else BREAKING, "PATTERN_CHANGED", "pattern differs")
    if old["description_sha256"] and new["description_sha256"] and old["description_sha256"] != new["description_sha256"]:
        add(DOCUMENTATION_ONLY, "PARAMETER_DESCRIPTION_CHANGED", "description text changed")
    return out


def diff(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    old_endpoints, new_endpoints = old["endpoints"], new["endpoints"]
    for path in sorted(set(new_endpoints) - set(old_endpoints)):
        changes.append(Change(ADDITIVE, "ENDPOINT_ADDED", path, None, "new endpoint in source"))
    for path in sorted(set(old_endpoints) - set(new_endpoints)):
        changes.append(Change(BREAKING, "ENDPOINT_REMOVED", path, None,
                              "endpoint absent from source; removal requires documented owner review"))
    for path in sorted(set(old_endpoints) & set(new_endpoints)):
        before, after = old_endpoints[path], new_endpoints[path]
        if after.get("deprecated") and not before.get("deprecated"):
            changes.append(Change(BREAKING, "ENDPOINT_DEPRECATED", path, None, "marked deprecated"))
        old_params, new_params = before.get("params"), after.get("params")
        if old_params is None and new_params is not None:
            changes.append(Change(ADDITIVE, "CONTRACT_FILLED", path, None, f"{len(new_params)} parameters documented"))
        elif old_params is not None and new_params is not None:
            for name in sorted(set(new_params) - set(old_params)):
                kind = BREAKING if new_params[name].get("required") else ADDITIVE
                changes.append(Change(kind, "PARAMETER_ADDED", path, name,
                                      "required" if new_params[name].get("required") else "optional"))
            for name in sorted(set(old_params) - set(new_params)):
                changes.append(Change(BREAKING, "PARAMETER_REMOVED", path, name, "parameter absent from source"))
            for name in sorted(set(old_params) & set(new_params)):
                changes.extend(_param_changes(path, name, old_params[name], new_params[name]))
        elif old_params is not None and new_params is None:
            changes.append(Change(UNKNOWN, "PARAMETERS_NOT_IN_SOURCE", path, None, "source carries no parameter detail"))
        if before.get("pagination") is not None and after.get("pagination") != before.get("pagination"):
            changes.append(Change(BREAKING, "PAGINATION_CHANGED", path, None, "pagination contract differs"))
        for key, kind in (("response_schema_sha256", "RESPONSE_SCHEMA_CHANGED"), ("errors_sha256", "ERRORS_CHANGED")):
            if before.get(key) is None and after.get(key) is not None:
                changes.append(Change(ADDITIVE, "CONTRACT_FILLED", path, None, f"{key} documented"))
            elif before.get(key) is not None and after.get(key) != before.get(key):
                changes.append(Change(UNKNOWN, kind, path, None, "needs field-level review"))
        if (before.get("response_schema_sha256") == after.get("response_schema_sha256")
                and before.get("response_schema_doc_sha256") and after.get("response_schema_doc_sha256")
                and before["response_schema_doc_sha256"] != after["response_schema_doc_sha256"]):
            changes.append(Change(DOCUMENTATION_ONLY, "RESPONSE_DOCS_CHANGED", path, None, "schema descriptions only"))
        if before.get("description_sha256") and after.get("description_sha256") \
                and before["description_sha256"] != after["description_sha256"]:
            changes.append(Change(DOCUMENTATION_ONLY, "DESCRIPTION_CHANGED", path, None, "operation text changed"))
    return changes


def summarize(changes: list[Change]) -> dict:
    counts = {name: 0 for name in (ADDITIVE, BREAKING, DOCUMENTATION_ONLY, UNKNOWN)}
    for change in changes:
        counts[change.classification] += 1
    return {"counts": counts, "blocking": any(c.classification in BLOCKING for c in changes),
            "changes": [asdict(change) for change in changes]}
