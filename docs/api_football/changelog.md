# Contract registry changelog

## 2026-11-11 (findings of the real certification)
- `/fixtures`: new status `PROVIDER_REJECTED_OBSERVED` (empirical, not documentary) for the one parameter set the real provider was observed to refuse: exactly `venue` + `season` (A04: declared errors on both keys). Refused locally as `INVALID_INPUT` `FIXTURE_COMBINATION_PROVIDER_REJECTED` with the evidence (date, case, error keys, category), 0 provider calls. No other combination changes.
- Warnings: "Provider calls require separate owner authorization and key." now appears only where it is true (gated replies, test mocks). Authorized real replies carry the provider-data warning (success), an error warning (provider or local error), a scan-bound warning or a partial-manifest warning.
- `pagination_state`: a page after page 1 with no rows is `EMPTY_PAGE` with `possible_out_of_range: true` and `has_next: null` (A10: the provider echoed `total` = requested page for a page beyond the data). Provider `paging` values are unchanged.
- Streamed scan (F-B05): runs of short scalar members in subtrees the scan never reads are consumed by one regular expression (same grammar, same bounds and codes); about 2.5x faster on a synthetic `/leagues` payload. Effect on Render Free not yet verified live.

## 2026-11-10 (full capability, FIX-01..FIX-09)
- `timezone`: any IANA zone (Python zoneinfo; no captured provider catalog), provider is the final authority. `TIMEZONE_NOT_CERTIFIED` is removed (owner-approved); unknown zones are `TIMEZONE_NOT_IN_CATALOG`.
- `/fixtures`: `live` accepts `all` or `id-id`; combinations labelled DOCUMENTED_VALID / DOCS_UNVERIFIED / MCP_REJECTED; `last`/`next` <= 99.
- `page`: no local cap of 100 (bound 100000, the transport paging bound); `pagination_state` on paginated tools.
- Oversized non-odds replies: `PARTIAL_ID_MANIFEST` with plain re-validated follow-up calls; odds manifests navigable by bookmaker/market, pre-match and live namespaces separate.
- Times: provider `Date` header as `provider_timestamp_utc`, acquisition time kept on cache HIT, labelled bridge-clock times.
- Local quota: `LOCAL_RATE_LIMITED` (retry_after_seconds) and `LOCAL_DAILY_QUOTA_EXHAUSTED` (reset_at_utc) replace `LOCAL_QUOTA_EXHAUSTED`.
- Review follow-up (PR #190): replies above the 512 KiB read cap are scanned in a bounded stream for ids (`STREAMED_SCAN`, 16 MiB / 20 s / 20 000 rows / depth 32 / 64 KiB token, else `PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN`); completeness only when follow-ups are provably under the read cap; timezone catalog from the pinned `tzdata` package (provider acceptance unverified); a bad live price is MALFORMED.
- Details: `docs/certification/areas/apifootball_full_capability.md`.

## 2026-11-01 (official parameter reconciliation, v3.9.3)
- Source: the official documentation page https://www.api-football.com/documentation-v3 (v3.9.3), read in the bridge's Browser Use session. The browser passed the site's standard challenge by itself; nothing was bypassed. Direct HTTP and the OpenAPI file still return 403. Extracted matrix: `official_parameter_matrix_v3.9.3.json` (38 endpoints, 140 query parameters, required flags, documented constraints, pagination).
- Before: 104/140 documented parameters exposed. After: 140/140 exposed, none invented (test-enforced in both directions).
- Added parameters:
  - `/leagues`: `name`, `code`, `type`, `current`, `last`;
  - `/teams`: `name`, `country`;
  - `/fixtures/headtohead`: `date`, `league`, `season`, `from`, `to`, `timezone`;
  - `/fixtures/statistics`: `type`;
  - `/fixtures/events` and `/fixtures/lineups`: `team`, `player`, `type`;
  - `/injuries`: `player`, `date`, `timezone`;
  - `/players`: `league`, `search`;
  - `/players/seasons`: `player`;
  - `/players/profiles`: `player`, `search`;
  - `/trophies` and `/sidelined`: `players`, `coachs` (up to 20 ids).
- Documented rules now enforced:
  - minimum `search` length: 3 for leagues/teams/live bets, 4 for players/profiles;
  - `/leagues`: `type` is `league` or `cup`, `last` ≤ 99, `code` 2–6 characters;
  - `/players`: `season` needs `id`, `league` or `team`, and `search` needs `league` or `team`;
  - `/injuries`: `season` is required with `league`/`team`/`player`. Behaviour change: `team` alone is now refused (`INJURY_FILTER_REQUIRED`);
  - `/standings`: only `season` is documented as required. `league` is no longer mandatory, but `league` or `team` must bound the query (`STANDINGS_FILTER_REQUIRED`).
- The 6 contracts that were `PENDING` (`/players`, `/players/profiles`, `/injuries`, `/odds/mapping`, `/odds/bookmakers`, `/odds/bets`) are now `DOCUMENTED_OFFICIAL`. `timezone` stays restricted to `UTC` (`TIMEZONE_NOT_CERTIFIED`) by policy.

## 2026-10-31 (live odds manifest)
- `/odds/live` replies over the 256 KiB MCP cap now return `PARTIAL_ODDS_MANIFEST` with `phase: LIVE`: per fixture `update`, `elapsed`, counts, and every market's `bet_id`/name/selections/suspended (no prices). Prices: `apifootball_live_odds(fixture=<id>, bet=<bet_id>)`. Malformed lists fail closed; missing `suspended` is counted as unknown, not false.
- `/odds/live` transport read cap aligned with `/odds` (2 MiB, bounded); all other routes stay at 512 KiB.

## 2026-10-31 (real MCP client run findings)
- Real client run (Claude MCP connector → Render, 14 provider calls) found: top-level `pagination` always null on real replies (read from the wrapper, not the provider envelope); `/odds/mapping` declared `paging.total=45` but the tool exposed no `page` (44/45 pages unreachable); real replies carried the offline placeholder warning.
- Fixes: `pagination` taken from the provider envelope; `apifootball_odds_mapping(page)` (bounded 1..100, no other filter, still PENDING until `page=2` is verified live); real-reply warning reflects provider data. No change to quota, auth or limits.

## 2026-10-31 (PR #125 hardening)
- `/odds` oversized-page manifest: missing `bookmakers`/`bets`/`values` and non-string `get` now fail closed with an explicit `manifest_error` code instead of an unhandled exception (`PROVIDER_REQUEST_FAILED`); missing is never counted as zero.
- Manifest result now keeps `quota_remaining`, `http_status` and `source_status` (mock vs real provenance) and is itself size-checked against the 256 KiB MCP cap.
- Tool-level and transport-level regressions (2 MiB cap only on `/odds`; `/odds/live` and other routes stay at 512 KiB).
- No provider calls, secrets, cron, Render or live-gate changes.

## 2026-10-31 (first call after process start)
- Owner real test after PR #126: call 1 `QUOTA_COORDINATOR_UNAVAILABLE`, call 2 HTTP 200. Render logs: first API-FOOTBALL call of process [59] (started 21:50:08, no restart); ~3.85 s total; one fresh DB connection measured at ~0.95 s.
- Root cause: the first PostgresQuota operation per process runs schema init (connect + DDL) and the reservation (connect + statements) inside the same 3 s lease timeout. Reproduced on real PostgreSQL with the measured latency.
- Fix: one-time `prepare()` runs before the timed reservation with its own 10 s shielded bound; reservation timeout stays 3 s. Fixed secret-free diagnostics: `QUOTA_COORDINATOR_UNAVAILABLE stage=<prepare|reserve> reason=<TIMEOUT|ERROR> error_type=<class> elapsed_ms=<n>`.

## 2026-10-31 (quota coordinator orphan lease)
- Root cause of `QUOTA_COORDINATOR_UNAVAILABLE` followed by repeated `LOCAL_CONCURRENCY_BUSY`: the 3 s coordinator timeout abandoned a worker thread that still committed its lease; with concurrency 1 the slot stayed held for the 180 s lease expiry. Reproduced on real PostgreSQL.
- Fix: the reservation is shielded; on timeout or cancellation a compensating release returns the orphan lease. Budget counters stay consumed (conservative); limits, timeout value and error codes unchanged.

## 2026-10-25
- Added canonical machine-readable endpoint and parameter registry.
- Added deterministic SHA-256 snapshot support and offline semantic detector.
- Preserved pending status for unreconciled official documentation.
- No provider calls, secrets, cron, or live-gate changes.
