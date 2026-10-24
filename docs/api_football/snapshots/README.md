# Normalized contract snapshots

Written only by `scripts/check_api_football_contracts.py ingest`. Each file is named
`<YYYY-MM-DD>_<sha256[:12]>.json` and contains `source_url`, `acquired_at_utc`, `acquisition_method`,
`normalized_sha256` and the normalized `contract`. Raw documentation pages are never stored here.

`check` recomputes every `normalized_sha256`. Snapshots are append-only: earlier ones are kept for semantic diffs.

No official snapshot exists yet. Status: `DOCUMENTATION_UNAVAILABLE` (2026-10-24, see `../sources.json`).
