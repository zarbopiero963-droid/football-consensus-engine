# API-FOOTBALL registry changelog

## registry_version 1 — 2026-10-24

- Initial registry: 39 endpoints from MatchPilot #248 (38 GET + `/status`), ids `AF-EP-001`..`AF-EP-039`.
- Parameter names for `/fixtures`, `/fixtures/statistics`, `/predictions`, `/odds`, `/teams/statistics` and
  `/odds/live` come from the owner baseline (FCB-AF-V3-OFFICIAL-CONTRACTS-01 §3). Types, required-ness, allowed
  values, combinations and response schemas are `PENDING_OFFICIAL_VERIFICATION`.
- The other 33 endpoints are `PENDING_OFFICIAL_VERIFICATION`, with no parameters and no tool.
- Official documentation: `DOCUMENTATION_UNAVAILABLE` (Cloudflare HTTP 403, no bypass). No snapshot.
