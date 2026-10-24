"""Read-only, hash-pinned MatchPilot FPT registry import.

This parses ONLY a separately exported manifest supplied by the auditor.
It does not connect to MatchPilot, PostgreSQL, Render, Neon or any
provider. An explicit checksum/source commit prevents a synthetic list
from accidentally being mistaken for the official 325-name registry.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .gap_matrix import (
    FptField, FIELD_COUNT_REQUIRED, GapMatrixError, build_gap_matrix
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_MAX_MANIFEST_BYTES = 262_144


@dataclass(frozen=True)
class RegistryEvidence:
    commit_sha: str
    file_sha256: str
    source_path: str
    fields: tuple[FptField, ...]
    source_kind: str = "MANIFEST_ONLY_NOT_EMPIRICAL_PROVIDER_DATA"


def import_registry_manifest(
    payload: bytes, *,
    expected_sha256: str,
    expected_source_commit: str,
) -> RegistryEvidence:
    """Accept a verified MatchPilot registry manifest without mutating it.

    Shape:
      {"source_repository":"zarbopiero963-droid/MatchPilot",
       "source_commit":"<40-hex>",
       "source_path":"<path to export>",
       "schema_version":"fpt-schema-4",
       "fields":[{"field_name":str,"family":str,"timing_class":str},...]}
    Use sha256 of exact input bytes; source_commit provenance must still
    be independently checked by a read-only reviewer. Hash matching
    proves bytes match a supplied checksum, not factual authenticity.
    """
    if not isinstance(payload, bytes) or not (2 <= len(payload) <= _MAX_MANIFEST_BYTES):
        raise GapMatrixError("MANIFEST_SIZE_INVALID")
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise GapMatrixError("EXPECTED_DIGEST_INVALID")
    if not isinstance(expected_source_commit, str) or not _SHA40.fullmatch(expected_source_commit):
        raise GapMatrixError("SOURCE_COMMIT_INVALID")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise GapMatrixError("REGISTRY_DIGEST_MISMATCH")
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GapMatrixError("MANIFEST_NOT_VALID_JSON") from None
    if not isinstance(doc, dict):
        raise GapMatrixError("MANIFEST_NOT_OBJECT")
    if doc.get("source_repository") != "zarbopiero963-droid/MatchPilot":
        raise GapMatrixError("INVALID_SOURCE_REPOSITORY")
    if doc.get("source_commit") != expected_source_commit:
        raise GapMatrixError("SOURCE_COMMIT_MISMATCH")
    if doc.get("schema_version") != "fpt-schema-4":
        raise GapMatrixError("UNKNOWN_FPT_SCHEMA_VERSION")
    path = doc.get("source_path")
    if not isinstance(path, str) or not (
        1 <= len(path) <= 256
    ) or path.startswith("/") or ".." in path or "\\" in path:
        raise GapMatrixError("INVALID_MANIFEST_SOURCE_PATH")
    rows = doc.get("fields")
    if not isinstance(rows, list) or len(rows) != FIELD_COUNT_REQUIRED:
        raise GapMatrixError("FPT_FIELD_REGISTRY_INCOMPLETE")
    fields: list[FptField] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "field_name", "family", "timing_class"
        }:
            raise GapMatrixError("INVALID_FPT_ROW_SHAPE")
        fields.append(FptField(row["field_name"], row["family"], row["timing_class"]))
    # Reuse canonical cardinality/identity checks; no shortcut to PASS.
    build_gap_matrix(fields)
    return RegistryEvidence(
        commit_sha=expected_source_commit,
        file_sha256=digest,
        source_path=path,
        fields=tuple(fields),
    )
