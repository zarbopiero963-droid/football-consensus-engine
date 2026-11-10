# API-FOOTBALL — full capability (FIX-01..FIX-09)

- Mandate: FCB-OWNER-AF-FULL-CAPABILITY-ONE-PR-20261111. One branch, one deliverable, one commit per fix.
- Base: `origin/main` `475dae8`. Gap ids (G01..G13, D06) refer to the reconciliation matrix of 2026-11-10.
- Evidence in this document is **offline only**: unit tests, `httpx.MockTransport` behind the real transport,
  resilience client and MCP tools, and a throwaway local PostgreSQL 18 for the quota coordinator. **No call
  was made to API-FOOTBALL, Render or the production MCP.** No row below is a real-provider PASS.
- Status legend: `VERIFIED_OFFLINE` (code + RED/GREEN tests), `PARTIAL` (part of the requirement is
  blocked, the block is stated), `NOT_PROVEN_REAL` (needs the real campaign in
  `apifootball_full_capability_budget.md`).

## 1. Coverage matrix

| Fix | Code | Tests (RED on 475dae8 → GREEN) | Evidence | Status |
|---|---|---|---|---|
| FIX-01 responses > 256 KiB | `bridge/api_football/continuation.py` (new), `tools.py` (`PARTIAL_ID_MANIFEST`, `navigation` on `PROVIDER_RESPONSE_TOO_LARGE`), `scripts/mcp_e2e_render.py` (`check_id_manifest`) | `tests/test_apifootball_continuation.py` (19): 600 fixtures over the cap retrieved in 30 `ids` slices, no loss/duplicate, same order, 31 real requests; follow-ups run in a **fresh process** (restart); two requests never mix; a row removed upstream between reads is detected; `ids` halved, single `id` not splittable; every strategy yields validated calls covering each row once; rows without id / duplicate ids / endpoints without a documented split never claimed complete; read cap still refuses before parsing | Offline, real transport + resilience + tools | **PARTIAL** — complete for replies between 256 KiB and the read cap; above the read cap (512 KiB, 2 MiB for odds, unchanged by owner rule) nothing is parsed, so `fixtures?date=` without league and `leagues` without filters (estimated above 512 KiB, ASSUMED) get only a `NOT_COMPLETE` narrowing hint. See §5 R1 |
| FIX-02 fixtures `live` | `contracts.py` (`_live_filter`, `LIVE_IDS_MAXIMUM`) | `tests/test_apifootball_fixtures_live_filter.py` (24) | Capture: `live` = `Enum: "all" \| "id-id"`; no documented maximum | **VERIFIED_OFFLINE**; id-count limit `DOCS_UNVERIFIED` (only the 128-character bound applies) |
| FIX-03 /fixtures combinations | `contracts.py` (`fixtures_combination`), `tools.py` (`combination` label) | `tests/test_apifootball_fixtures_combinations.py` (54) | Capture only (§2) | **VERIFIED_OFFLINE**; real provider semantics of `DOCS_UNVERIFIED` combinations NOT_PROVEN_REAL |
| FIX-04 page > 100 | `contracts.py` (`MAX_PAGE`), `transport.py` (empty page past the end), `tools.py` (`pagination_state`) | `tests/test_apifootball_paging_beyond_100.py` (58) | Capture: `page` "Default: 1", no maximum; players/profiles 250 per page | **VERIFIED_OFFLINE**; provider behaviour on a page past the end `DOCS_UNVERIFIED` (D06) |
| FIX-05 odds manifest | `odds_manifest.py` (rewritten, compatible), `tools.py` (detail levels, `response_sha256`) | `tests/test_apifootball_odds_manifest_navigation.py` (12) + existing `tests/test_odds_manifest_and_parameter_matrix.py` | Repo fixtures for live selection fields | **VERIFIED_OFFLINE** |
| FIX-06 timezones | `bridge/api_football/timezones.py` (new), `contracts.py`, `tools.py` (`timezone_context`, `timezone_check`), E2E N00 | `tests/test_apifootball_timezones.py` (52) | §3 | **VERIFIED_OFFLINE**; provider acceptance of each zone NOT_PROVEN_REAL |
| FIX-07 timestamps and cache | `transport.py` (`provider_http_date`), `cache.py` (`get_with_age`), `resilience.py` (`_cache_hit`), `tools.py` (`_reply_timing`, `_finish_timing`) | `tests/test_apifootball_timestamps_cache.py` (13) | The provider `Date` header is not captured in the repo: it is used when present, null otherwise | **VERIFIED_OFFLINE** |
| FIX-08 local quota codes | `quota.py` (typed `QuotaExceeded`, retry/reset SQL), `tools.py` (`_local_quota_error`), E2E runner stops | `tests/test_apifootball_local_quota_codes.py` (11), `tests/integration/test_apifootball_quota_codes_postgres.py` (8, real PostgreSQL 18) | Local PG cluster on port 55521, removed afterwards | **VERIFIED_OFFLINE** (real coordinator code on real PostgreSQL; not on Neon/Aiven) |
| FIX-09 suspended live odds | `bridge/api_football/live_odds_state.py` (new), `tools.py` (`live_odds_state`), E2E runner (`LIVE_PARTIAL_STATES`) | `tests/test_apifootball_live_odds_states.py` (28), sanitized shapes `tests/fixtures/apifootball_live_odds/shapes.json` | Shapes are repo-derived or `ASSUMED`; **no real `/odds/live` payload is captured** | **VERIFIED_OFFLINE**; **C08 stays NOT proven**: a real PASS needs new real evidence |
| P3 errors.py reserved keys | `errors.py` | `tests/test_apifootball_review_p3.py` (11) | — | VERIFIED_OFFLINE |
| P3 circuit hold vs success() | `circuit.py` (`_hold_until`) | `tests/test_apifootball_review_p3.py` (11) | — | VERIFIED_OFFLINE |
| P3 single-flight over-count | `resilience.py` (`shared_flight`, follower copy), `tools.py` (`_real_calls`) | `tests/test_apifootball_review_p3.py` (11) | — | VERIFIED_OFFLINE |

Regression: `tests/test_apifootball_full_capability_regression.py` drives all 40 provider tools end to end
(path, exact parameters, lossless data, time fields, exact `network_calls_made`, cache HIT keeping the
acquisition time). Contract tests against the official matrix (`tests/test_apifootball_official_parameter_matrix.py`)
are unchanged and green.

Spot mutations (each applied alone, then reverted): 17/17 killed — last/next 99→100, live duplicate check,
timezone catalog check, ids slice 20→21, missing `suspended` read as open, reserved-key truncation,
success() erasing the hold, day pre-check, rolling retry from the newest slot, `odd` copied into a manifest,
rows accepted on a page past the end, shared flight counted twice, runner ignoring live PARTIAL, bridge
clock used as provider timestamp, page bound removed, unscoped fixtures allowed, documented label for
supersets.

### Continuation design (FIX-01)

Plain, re-validated provider parameters, never a token. Every follow-up (`ids` ≤ 20, an `ids` request
halved, one documented id filter added, or `page = current + 1`) is an ordinary tool call: validated,
authorized, quota-charged, no secret, no server state, no new env var, valid across restarts and redeploys.
An opaque token was not needed because every split has a documented parameter, so no HMAC key exists.
Change detection is client-side and stateless: the manifest carries `response_sha256`, `results_declared`,
the row ids in provider order and, per call, `expect_ids` / `expect_rows`.

### Declared behaviour changes (pre-existing tests changed on purpose)

| Test | Change | Fix |
|---|---|---|
| `test_mcp_e2e_apifootball_suite.py` | `STOP_LOCAL_QUOTA_EXHAUSTED` → `STOP_LOCAL_RATE_LIMITED` / `STOP_LOCAL_DAILY_QUOTA_EXHAUSTED`; N00 negative zone | FIX-08, FIX-06 |
| `test_apifootball_pr07_certification.py` | the joining caller of a shared flight reports 0 attempts | P3 |
| `test_apifootball_typed_mcp.py` | `TIMEZONE_NOT_CERTIFIED` → `TIMEZONE_NOT_IN_CATALOG`; oversized mock → `PARTIAL_ID_MANIFEST` | FIX-06, FIX-01 |
| `test_apifootball_full_catalog.py`, `test_apifootball_real_reply_pagination.py` | out-of-bound page is 100001 (101 is valid) | FIX-04 |
| `test_apifootball_resilience.py` | an empty page past the end is valid; with a row it stays invalid | FIX-04 / D06 |
| `test_apifootball_error_codes.py` | oversized players page → `PARTIAL_ID_MANIFEST` | FIX-01 |
| `test_odds_manifest_and_parameter_matrix.py` | no-price scan also skips the new bridge-clock time fields | FIX-07 |
| `test_mcp_e2e_apifootball_error_codes.py` | live facts gain `open_priced`, `live_state` | FIX-09 |

Other visible changes: `INVALID_INPUT` and gate replies now carry `retrieved_at_utc: null` (nothing was
acquired) plus `responded_at_utc`; `LOCAL_QUOTA_EXHAUSTED` no longer exists.

## 2. /fixtures combination table (FIX-03)

Source: `docs/api_football/official_parameter_matrix_v3.9.3.json` only. It lists the 15 parameters as
independent optional filters and records **no combination rule**; nothing becomes DOCUMENTED_VALID because
the docs do not forbid it. `timezone` may join any accepted set. `DOCS_UNVERIFIED` sets are forwarded with
`combination: {status, rule}` in the reply; the provider is the final authority (its `errors` keys come back
classified).

| Combination | Status | Basis / code |
|---|---|---|
| `id` | DOCUMENTED_VALID | single documented parameter; real PASS id=1557464 (runs on 37e74fd) |
| `ids` (≤ 20) | DOCUMENTED_VALID | single documented parameter, "Maximum of 20" |
| `live` = `all` or `id-id` (≥ 2 ids) | DOCUMENTED_VALID | documented enum; real `live=all` (13 fixtures) |
| `live` = one league id | DOCS_UNVERIFIED | documented form is a list ("several leagues ids") |
| `date` | DOCUMENTED_VALID | single documented parameter |
| `league` + `season` | DOCUMENTED_VALID | `season`: "The season of the league" |
| `league` + `season` + `from` + `to` | DOCUMENTED_VALID | same, plus real PASS on 37e74fd |
| `team` + `season` | DOCS_UNVERIFIED | no combination rule captured |
| `venue` + `season` | DOCS_UNVERIFIED | idem |
| `league`/`team`/`venue` + `last` or `next` | DOCS_UNVERIFIED | idem |
| `last` or `next` alone | DOCS_UNVERIFIED | idem |
| `league`+`season` + `round`/`status`/`team`/`venue` | DOCS_UNVERIFIED | idem |
| `team`/`venue` (+`season`) + `from` + `to` | DOCS_UNVERIFIED | idem |
| `date` + `league`/`season`/`team`/`status`/... | DOCS_UNVERIFIED | idem |
| `live` + `status`/`league`/... | DOCS_UNVERIFIED | idem |
| `id` + `ids` | DOCS_UNVERIFIED | idem (accepted before this PR as well) |
| no scoping filter (`{}`, `league`, `team`, `venue`, `season`, `status`, `round`, `timezone`, `from`+`to`, `team`+`from`, `league`+`team`) | MCP_REJECTED | `FIXTURE_FILTER_REQUIRED` (unbounded listing, B03) |
| `last` + `next` | MCP_REJECTED | `FIXTURE_LAST_NEXT_EXCLUSIVE` |
| `from` after `to` | MCP_REJECTED | `INVALID_DATE_RANGE` |
| `last` or `next` > 99 | MCP_REJECTED | `PAGE_OR_BATCH_TOO_LARGE` (documented "<= 2 characters") |

## 3. Timezone source (FIX-06)

- No captured copy of the provider's `/timezone` catalog exists in the repository. The validation source is
  **Python `zoneinfo` (IANA tz database of the host)**, minus `Factory`, `localtime`, `posixrules`.
- The provider stays the final authority: a zone it rejects returns a declared `timezone` error
  (`INVALID_PARAMETER`), never retried.
- An unreadable tz database fails closed: only `UTC` is accepted (`TIMEZONE_CATALOG_UNAVAILABLE`).
- The bridge never converts times. `fixture.timestamp` (provider Unix seconds, UTC) and `fixture.date`
  (provider offset) pass through untouched. `timezone_check` (DERIVED, outside the data) verifies each
  published offset against the IANA offset at that instant: from a UTC instant there is exactly one local
  time, so DST gaps and repeated hours are exact (tested on the 2026 Rome spring and autumn changes).
- Owner-approved removal of `TIMEZONE_NOT_CERTIFIED`; the pinning tests are changed and declared (§1).

## 4. Other notes

- FIX-07: `provider_timestamp_utc` is the provider's own HTTP `Date` header (strict IMF-fixdate), source
  `PROVIDER_HTTP_DATE_HEADER`; otherwise null. `retrieved_at_utc` = bridge clock at acquisition (kept on a cache
  HIT), `responded_at_utc` = bridge clock at response, `age_seconds` = their difference, `cache_age_seconds`
  = cache clock on a HIT; `time_sources` labels each. Item-level provider times (`update`,
  `fixture.timestamp`) stay inside the data.
- FIX-08: limits and defaults untouched (`APIFOOTBALL_MAX_RPM=60`, `APIFOOTBALL_MAX_DAILY_REQUESTS=7000`,
  `max_concurrency` 1 as configured; code defaults 8/80/2 unchanged). `LOCAL_RATE_LIMITED` carries
  `retry_after_seconds` from the (n − limit + 1)-th oldest attempt in the rolling window or from the
  provider-zero marker; `LOCAL_DAILY_QUOTA_EXHAUSTED` carries `reset_at_utc` = next UTC midnight (the day bucket
  is `epoch // 86400` of the database clock). A spent day is reported before the minute checks.

## 5. Residual risks

| Id | Risk | Mitigation / owner decision |
|---|---|---|
| R1 | Non-odds replies above the 512 KiB read cap (likely `fixtures?date=` without league, `leagues` without filters) are not retrievable completely: nothing is parsed, so no ids | Owner decision: either keep (current) or approve a bounded streaming id scan / higher read cap in a separate owner-merged PR |
| R2 | `fixtures?ids=` may return richer rows than a listing (ASSUMED from upstream behaviour, not captured): a 20-id slice could itself exceed 256 KiB | The manifest of an `ids` request halves it; a single `id` over the cap is reported not splittable |
| R3 | `ADD_ID` splits cost one call per distinct id (e.g. hundreds of leagues) | The call list is explicit; nothing is fetched automatically |
| R4 | `DOCS_UNVERIFIED` combinations may be refused or reinterpreted upstream | Labelled in every reply; provider errors surface with keys; real check in the budget proposal |
| R5 | Host tz database may differ from the provider catalog (or be missing on the runtime image) | Provider is final authority; missing database fails closed to UTC |
| R6 | Provider `Date` header not evidenced in a captured reply | Optional: null when absent or malformed |
| R7 | No real `/odds/live` payload captured; selection-level fields come from repo fixtures | C08 stays NOT proven; states are DERIVED and conservative (missing `suspended` never open) |
| R8 | Behaviour of a page past the end is DOCS_UNVERIFIED (D06) | Accepted only without rows; with rows `PROVIDER_INVALID_PAGINATION` |
| R9 | The official site returned 403 on 2026-11-10 (D01): the capture of 2026-11-01 is the only documentary source | Re-capture when the site is reachable |

Real certification: proposal (not executed) in `apifootball_full_capability_budget.md`.
