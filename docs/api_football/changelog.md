# Contract registry changelog

## 2026-10-31 (PR #125 hardening)
- `/odds` oversized-page manifest: missing `bookmakers`/`bets`/`values` and non-string `get` now fail closed with an explicit `manifest_error` code instead of an unhandled exception (`PROVIDER_REQUEST_FAILED`); missing is never counted as zero.
- Manifest result now keeps `quota_remaining`, `http_status` and `source_status` (mock vs real provenance) and is itself size-checked against the 256 KiB MCP cap.
- Tool-level and transport-level regressions (2 MiB cap only on `/odds`; `/odds/live` and other routes stay at 512 KiB).
- No provider calls, secrets, cron, Render or live-gate changes.

## 2026-10-25
- Added canonical machine-readable endpoint and parameter registry.
- Added deterministic SHA-256 snapshot support and offline semantic detector.
- Preserved pending status for unreconciled official documentation.
- No provider calls, secrets, cron, or live-gate changes.
