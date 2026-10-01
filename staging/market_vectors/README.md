# Native market staging (M26 / issue43-C)

Run `python -m staging.market_vectors.run --neon --known-at <explicit-offset-time>`
with the configured database connection, or use `--input dataset.json` for a
saved `{runs: [...], rows: [...]}` export. Output goes only to stdout. The SQL
adapter sets `TRANSACTION READ ONLY`, reads the latest captured StatsUltra run
at the cutoff and its existing pilot rows, and never migrates, rewrites slots,
creates tickets or calls production ingestion.

The initial source is **StatsUltra only**, using its actually observed DOM
`match-card` / `match-card-long` / `match-card-stat` structure. Raw gzip is
bounded and its original SHA-256 is verified before parsing. Native values
come from `Over 2.5` and `Both score` labels within the same card; scripts and
adjacent cards cannot fill a missing field. Exact ordered source team names
(Unicode/case/whitespace normalization only) associate a card to a row in the
same run; duplicate cards/rows and unresolved associations are excluded.
This verifies source-row association, not cross-source fixture identity.

Source percentages retain their published text, raw values and explicit scale
100. Output market IDs are `FT_OU_2.5` and `FT_BTTS`. Under and NG are not
published by this observed source format, so their components remain null.
These vectors are PARTIAL or ABSENT, never silently completed. Invalid
percentages or ambiguous fields are BROKEN. A real published 0% remains zero.
No 1X2 probability, bookmaker price, correct score or expected goals enters the
native OU/BTTS normalization. Additional sources/labels require real evidence
and a separate source-specific extension.

Existing stored values are comparison diagnostics. An old stored/inferred
opposite is flagged as `unpublished_opposite_in_storage`, not imported into the
native vector and not deleted. Hash/row/parser provenance stays with each vector.
This report is a new read-only diagnostic interpretation of existing raw, not
a replacement historical pilot run or an official new prospective snapshot.

The source capture timestamp uses run completion conservatively; this is not
a server seal or proof of kickoff timezone. The SQL knowledge cutoff selects
finished acquisitions, not a certified database import-time history. The
existing timezone/matching status is retained and every record explicitly has
`prematch_certified=false`, no additional source vote and
`production_eligible=false`. Migration 0005 gives new run/row inserts a server
seal; legacy evidence remains `SERVER_SEAL_UNAVAILABLE`. A new seal is reported
only when both server columns exist and are known at the cutoff; it does not
resolve StatsUltra kickoff time or prove prematch eligibility. Native completeness alone never supplies
promotion, consensus eligibility, settlement or scoring readiness.
