"""Classify fcb_pg18_test from read-only evidence (reviews of #178).

`occupancy FILE`: FILE is the output of sql/aiven/pg18_occupancy.sql (psql -X -A -t -F '=').
Prints EMPTY (exit 0) only when every category is exactly 0; NOT_EMPTY (exit 0) when user
objects exist and no schema besides public does; otherwise a fixed refusal code (exit 1).
EMPTY is the ONLY state under which the workflow may apply migrations.

`migrated EVIDENCE FILES`: input 1: the output of sql/aiven/verify_schema.sql (psql -X -A -t -F '='): seven `key=count`
lines, then one `version=name=sha256` line per schema_migrations row.
Input 2: the expected `version=name=sha256` lines computed from sql/migrations/bridge/0001-0007.

The database is accepted (read-only) only when the object counts equal the ones the apply step
verifies AND the recorded rows equal the repo files line by line: exactly 7 rows, each with a
well-formed name and a 64-hex checksum. A single row carrying the whole expected string in one
column, an extra row, a changed checksum or a different number of tables, functions, triggers,
indexes or constraints are refused. This is consistency evidence for a diagnose report, not
proof of exact schema identity: object kinds the counts do not cover (views, sequences, types)
are not compared, and schema_migrations rows can be written by anyone with write access.

Prints BRIDGE_0001_0007 and exits 0, or prints a fixed refusal code and exits 1. Stdlib only.

Threat-model limit of the EMPTY gate (reviews of #178): the precheck, an in-transaction re-check
taken under the migration's advisory lock, and sql/aiven/pg18_post_apply_guard.sql (every user
object must carry the apply transaction's xmin) narrow a concurrent same-role writer but cannot
exclude it: code such a writer plants after the re-check can run inside the apply transaction
with the same privileges. The test database has a single administrative role, so such a writer
already controls the database.
"""
from __future__ import annotations

import re
import sys

EXPECTED_COUNTS = ["tables=29", "functions=4", "triggers=9", "indexes=54",
                   "constraints_information_schema=307", "constraints_pg_constraint=61",
                   "schema_migrations_rows=7"]
OCCUPANCY_KEYS = ["relations", "functions", "types", "other_objects", "default_privileges",
                  "database_settings", "unexpected_schemas"]
ROW_RE = re.compile(r"^([1-7])=(000[1-7]_[a-z0-9_]+\.sql)=([0-9a-f]{64})$")


def occupancy(evidence: str) -> str:
    lines = [l for l in evidence.splitlines() if l.strip()]
    parsed = [l.split("=", 1) for l in lines]
    if [p[0] for p in parsed] != OCCUPANCY_KEYS or any(len(p) != 2 or not re.fullmatch(r"[0-9]+", p[1]) for p in parsed):
        return "OCCUPANCY_EVIDENCE_MALFORMED"
    counts = {k: int(v) for k, v in parsed}
    if counts["unexpected_schemas"] != 0:
        return "UNEXPECTED_SCHEMAS"
    return "EMPTY" if not any(counts.values()) else "NOT_EMPTY"


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
    if len(argv) == 3 and argv[1] == "occupancy":
        with open(argv[2], encoding="utf-8") as e:
            state = occupancy(e.read())
        print(state)
        return 0 if state in ("EMPTY", "NOT_EMPTY") else 1
    if len(argv) == 4 and argv[1] == "migrated":
        with open(argv[2], encoding="utf-8") as e, open(argv[3], encoding="utf-8") as f:
            state = classify(e.read(), f.read())
        print(state)
        return 0 if state == "BRIDGE_0001_0007" else 1
    print("USAGE")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
