"""Classify a non-empty fcb_pg18_test from read-only evidence (review of #178, P2-1/P2-2/P3-1).

Input 1: the output of sql/aiven/verify_schema.sql (psql -X -A -t -F '='): seven `key=count`
lines, then one `version=name=sha256` line per schema_migrations row.
Input 2: the expected `version=name=sha256` lines computed from sql/migrations/bridge/0001-0007.

The database is accepted (read-only) only when the object counts equal the ones the apply step
verifies AND the recorded rows equal the repo files line by line: exactly 7 rows, each with a
well-formed name and a 64-hex checksum. A single row carrying the whole expected string in one
column, an extra row, a changed checksum or extra objects are all refused.

Prints BRIDGE_0001_0007 and exits 0, or prints a fixed refusal code and exits 1. Stdlib only.
"""
from __future__ import annotations

import re
import sys

EXPECTED_COUNTS = ["tables=29", "functions=4", "triggers=9", "indexes=54",
                   "constraints_information_schema=307", "constraints_pg_constraint=61",
                   "schema_migrations_rows=7"]
ROW_RE = re.compile(r"^([1-7])=(000[1-7]_[a-z0-9_]+\.sql)=([0-9a-f]{64})$")


def classify(evidence: str, files: str) -> str:
    lines = [l for l in evidence.splitlines() if l.strip()]
    expected_rows = [l for l in files.splitlines() if l.strip()]
    if len(expected_rows) != 7 or not all(ROW_RE.fullmatch(r) for r in expected_rows):
        return "REPO_FILES_UNEXPECTED"
    if lines[:7] != EXPECTED_COUNTS:
        return "OBJECT_COUNTS_DIFFER"
    rows = lines[7:]
    if len(rows) != 7 or not all(ROW_RE.fullmatch(r) for r in rows):
        return "MIGRATION_ROWS_MALFORMED"
    if rows != expected_rows:
        return "MIGRATION_ROWS_DIFFER"
    return "BRIDGE_0001_0007"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("USAGE")
        return 2
    with open(argv[1], encoding="utf-8") as e, open(argv[2], encoding="utf-8") as f:
        state = classify(e.read(), f.read())
    print(state)
    return 0 if state == "BRIDGE_0001_0007" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
