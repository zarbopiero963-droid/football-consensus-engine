# C — API-FOOTBALL v3 component: independent audit (read-only)

Auditor: Claude (independent). Date: 2026-11-09. Target: `main` **a8fb2289** (#175), checked out as a detached worktree in `/tmp/claude-0/audit_af/wt`. The repo was not modified and nothing was committed. **No API-FOOTBALL provider call was made.** I ran one read-only aggregate `SELECT` on the Aiven `defaultdb` quota tables (counts only, no key hashes).
Scratch: `/tmp/claude-0/audit_af/` (repro scripts in `scripts/repro.py` and `scripts/repro2.py`; local throw-away PG16 in `/tmp/claude-0/audit_af_pg`).

## 0. Test runs (evidence)

| Run | Result |
|---|---|
| `pytest tests/ -k "football or apifootball or odds_manifest"` (venv py3.13, offline) | **402 passed, 28 skipped**. All 28 skips are "requires dedicated TEST_DATABASE_URL" |
| Same 28 integration tests with `TEST_DATABASE_URL` set to a local **PostgreSQL 16** (`tests/integration/test_apifootball_quota.py` ×9, `..._live_harness_postgres.py` ×5, `..._orphan_lease_postgres.py`, `..._schema_init_postgres.py`, `test_defaultdb_restore_postgres.py` ×12) | **28 passed**. Note: production is PG **18.6** and was not tested on 18 |
| `tests/test_mcp_offline_protocol_e2e.py` (real MCP JSON-RPC over stdio: schema exposes every contract field, `from/to`→`from_date/to_date`) | 2 passed |
| `scripts/repro.py` and `repro2.py` (offline, httpx.MockTransport plus a fake quota) | reproduce findings F-01…F-07 below |
| Official docs fetch `https://www.api-football.com/documentation-v3` (curl and WebFetch) | **HTTP 403, Cloudflare "Just a moment…" challenge**. Not bypassed (§2.1). For this audit the docs are **DOCS_UNVERIFIED**; the comparison below uses the repo's own capture `docs/api_football/official_parameter_matrix_v3.9.3.json` (retrieved 2026-11-01 through Browser Use, no stored hash) |
| Aiven `defaultdb` read-only aggregate: `api_football_quota_counters`, `_leases`, `_attempts` | **counters: 0 rows; leases 0; attempts 0** |

## 1. Capability table

Legend: PASS_REAL = real provider evidence that is valid for the current code path; PASS_OFFLINE = offline/mock/local-PG tests on a8fb228; IMPLEMENTED_UNVERIFIED = code exists with no test proving the claimed property; NOT_IMPLEMENTED; FAIL; DOCS_UNVERIFIED.

**No item is PASS_REAL.** All real calls predate the current code path and the DB initialization (see §1b).

| Item | Status | Evidence | Gap |
|---|---|---|---|
| 40 contracts → 38 official football paths + `/status`; 42 MCP tools (40 + `apifootball_health` + `apifootball_quota_report`) | PASS_OFFLINE | `contracts.py:51-96`, `tools.py:436-441` (asserts), `catalog.py:9-25` | real per-route evidence only from 2026-10-25/31 on older code |
| Parameter names vs official docs v3.9.3 | PASS_OFFLINE / DOCS_UNVERIFIED | My diff of `ENDPOINTS` against `official_parameter_matrix_v3.9.3.json`: **0 invented, 0 missing, required sets equal** on all 39 paths (`/status` has no matrix entry). Live doc site 403 today | the capture has no content hash (WP-014); `/status` is not in the matrix |
| MCP schema exposes every contract param | PASS_OFFLINE | `tests/test_mcp_offline_protocol_e2e.py:83-90` (passed) | — |
| PENDING/DOCS_UNVERIFIED labels | FAIL (inconsistent) | contracts `DOCUMENTED_OFFICIAL` (`contracts.py:19`), catalog `PENDING_OFFICIAL_RECONCILIATION` (`catalog.py:8`), registry `PENDING_OFFICIAL_VERIFICATION` (`registry.py:15,45`), health `PARTIALLY_RECONCILED_OFFICIAL` (`tools.py:205`), `sources.json` PENDING; SOURCE URL `documentation_v3` vs capture `documentation-v3` | WP-014 open |
| Parameter constraints (search lengths, id types) | FAIL (minor) | F-06: `countries` search 1 char, `venues`/`coaches` <3, `bookmakers`/`bets` ≠3 all reach the provider (`contracts.py:29-30` covers only 5 tools); `/odds/bets` and `/odds/bookmakers` `id` validated as int (docs: bets id string) | quota wasted on provider-rejected calls |
| Fixtures (`/fixtures`, rounds, h2h) | PASS_OFFLINE | validation `contracts.py:190-195,234-237`; tests `test_apifootball_typed_mcp.py`, `test_apifootball_full_catalog.py` | F-03: a whole-day `date=` response >256 KiB → `PAYLOAD_TOO_LARGE`, >512 KiB → `PROVIDER_RESPONSE_TOO_LARGE`, 1 quota unit spent, no data and no manifest |
| Fixture statistics (incl. `half`), events, lineups, players | PASS_OFFLINE | `tests/test_api_football_half_contract.py`, `test_api_football_fixture_statistic_values.py` | real `half` evidence 2026-10-31 only (pre-coordinator) |
| Teams, team statistics/seasons/countries, venues | PASS_OFFLINE | `test_api_football_team_statistics_shape.py` | — |
| Players, profiles, squads, teams, top* | PASS_OFFLINE | contracts and tests | `page` capped at 100 (`contracts.py:118`) but `/players/profiles` (250/page) can exceed 100 pages, so later pages are unreachable (F-08) |
| Injuries, sidelined, trophies, transfers, coaches | PASS_OFFLINE | `contracts.py:63,89-91,219-224` | — |
| Standings | PASS_OFFLINE | `contracts.py:76,188` (season required + league/team) | — |
| Predictions (provider-labelled, not a model) | PASS_OFFLINE | `tools.py:280-282` returns raw only | no `PROVIDER_PREDICTION` label distinguishing it from odds in the output (doc string only) |
| Odds prematch `/odds` + PARTIAL_ODDS_MANIFEST | PASS_OFFLINE | `tools.py:95-122`, `odds_manifest.py:9-50`, `tests/test_odds_manifest_and_parameter_matrix.py` | F-04: a **single-fixture** reply >256 KiB returns a manifest whose `retrieval` hint points back at `apifootball_odds(fixture=…)`, i.e. the same oversized call; no bookmaker or bet ids are listed, so the hint loops |
| Live odds `/odds/live` + manifest with bet ids | PASS_OFFLINE | `odds_manifest.py:53-118`; suspended counted only if `is True` (missing stays unknown) | real evidence 2026-10-31 only |
| Bookmakers / bets / live bets / odds mapping | PASS_OFFLINE | `contracts.py:67-68,93-94` | — |
| 256 KiB MCP cap | PASS_OFFLINE | `tools.py:20,94,123-126` | non-odds endpoints have no partial manifest (F-03) |
| Provider read bounds 512 KiB / 2 MiB odds, stream-closed | PASS_OFFLINE | `transport.py:23-25,97-100` | — |
| Missing stays missing / no derived probabilities in provider layer | PASS_OFFLINE | grep shows no `or 0`, complements or implied probabilities in `bridge/api_football/*`; `quota_remaining` is None when the header is absent (`transport.py:62-66`) | — |
| Timestamps: UTC canonical, raw kept | PARTIAL (IMPLEMENTED_UNVERIFIED) | provider body returned losslessly; `retrieved_at_utc` local UTC; `timezone` param accepts only `"UTC"` (`contracts.py:104-109`) | `provider_timestamp_utc` is always `None` (`tools.py:60`, `resilience.py:290`) |
| Europe/Rome derived output (WP-020/D09) | **NOT_IMPLEMENTED** | `grep -r "Europe/Rome\|zoneinfo\|TIMEZONE_UNRESOLVED" bridge/` → 0 hits; `timezone="Europe/Rome"` → `INVALID_INPUT TIMEZONE_NOT_CERTIFIED` (repro) | entire WP-020 |
| Pagination (`paging` validated, passed through) | PASS_OFFLINE | `transport.py:112-119`, `tools.py:151-153`, `tests/test_apifootball_real_reply_pagination.py` | page ≥2 never exercised for real (WP-015) |
| Shared PostgreSQL coordinator (counters / leases / rolling attempts, advisory lock) | PASS_OFFLINE (local PG16) | `quota.py:147-224`; 9 integration tests passed (`test_separate_process_equivalent_coordinators_do_not_over_admit`, `test_concurrency_shared_across_independent_coordinator_instances`, `test_provider_zero_header_marked_shared_and_persistent`, …) | not run on PG18; prod never exercised (0 counter rows) |
| One quota unit per attempt, retries counted | PASS_OFFLINE | `resilience.py:251-261`; `test_retry_503_twice_then_success_has_three_distinct_quota_debits`; repro: one 503 call = 3 units | — |
| Retry policy bounded (≤3, 5xx/timeouts only; 429/4xx no retry) | PASS_OFFLINE | `resilience.py:45-55`, `errors.py:289-293`; `test_non_retryable_status_never_storms` | — |
| Circuit breaker (per-process, threshold 2, 30 s, single half-open probe) | PASS_OFFLINE | `circuit.py`; `test_circuit_opens_recovers_with_exactly_one_half_open_probe` | counts one failure per *call*, so it opens only after 2 calls × 3 attempts = **6 units** (repro2) |
| Concurrency (shared leases + local semaphore + bounded queue) | PASS_OFFLINE | `quota.py:162-170`, `resilience.py:83-85,112-113,128-144` | — |
| Budget stop (day/minute, provider zero written back) | PASS_OFFLINE | `quota.py:189-211,301-332`; 429 → `mark_exhausted("minute")` | F-05: an HTTP-200 reply with `errors.requests` (provider quota error) and `x-ratelimit-*-remaining: 0` is **not** written back (headers ignored on that path) |
| DB unavailable → fail closed | PASS_OFFLINE | `resilience.py:196-231` → `QUOTA_COORDINATOR_UNAVAILABLE`, no HTTP; `test_shared_quota_backend_unavailable_fails_closed_without_exposing_exception`; harness: `live_harness.py:217-220` `COORDINATOR_DATABASE_REQUIRED` | — |
| Cache (in-process, TTL per endpoint, `/status` never cached, HIT = 0 units) | PASS_OFFLINE | `cache.py:143-154`; repro: second identical call HIT, units unchanged | per-process only, not shared across instances (acceptable, documented) |
| Fixed error codes to client | **FAIL** (WP-016) | repro: HTTP 429, 401, 503 and circuit-open **all → `PROVIDER_REQUEST_FAILED`** (`tools.py:170-180` allowlist omits `PROVIDER_HTTP_*`, `PROVIDER_CIRCUIT_OPEN`, `PROVIDER_REQUEST_QUEUE_TIMEOUT`, `PROVIDER_NO_REPLY`, `PROVIDER_REPLY_CONTRACT_INVALID`) | WP-016 |
| `network_calls_made` accounting | **FAIL** | repro: `PROVIDER_ERROR` after 3 real HTTP attempts → `network_calls_made: 0`; `PAYLOAD_TOO_LARGE` after 1 real call → 0. The offline warning "Offline PR-03 tool…" stays on real-error replies | F-02 |
| Secrets: key only in header, hashed in DB, `/status` PII stripped | PASS_OFFLINE | `transport.py:79,87`, `quota.py:83-87`, `tools.py:136-147`; `test_plaintext_key_is_never_written_to_budget_tables` | — |
| Live scripts through coordinator (WP-003) | PASS_OFFLINE | `live_harness.py` (`coordinated_client`); scripts call it (`api_football_live_smoke.py:45`, `_39_route_inventory.py:114`, `capability_probe.py:57`); workflows pass `secrets.APIFOOTBALL_COORDINATOR_DATABASE_URL` and fail on empty | secret existence and that it equals the **Render runtime DB** could not be verified (GitHub env API 403 via proxy). If it is a different DB, the "shared" budget is not shared |
| Out-of-scope modules (WP-021) | NOT_IMPLEMENTED | still present: `capabilities.py`, `indicators.py` (MatchPilot #34), `gap_matrix.py`, `registry_import.py`. Only tests import them (no runtime import) | removal pending, low risk |

### 1b. Real-call evidence inventory

| Evidence | Date | Path | Valid for current code? |
|---|---|---|---|
| route inventory runs 40202803422, 40205480029, **40211464376** (`evidence/api_football/route_inventory_*.b64`) | 2026-10-25 | direct httpx, **bypassing the coordinator** (pre-#167) | No |
| capability runs 40205461384…40220381426; 41240887865, 41242779991, **41243344579** (`evidence/api_football/capability_*`) | 2026-10-25 / 2026-10-31 | direct httpx (pre-#167) | No |
| live-smoke 40200574155 | 2026-10-25 | **failure** | No |
| `docs/api_football/live_five_point_sample_2026-10-25.md`, MCP-client narrative (CSV AF-005 "REAL_MCP_CLIENT_VERIFIED") | 2026-10-25 | narrative only, no raw artefact or hash, pre-218f9b9 params | No |
| `FCB_TEST_EVIDENCE_INDEX.json` (baseline 342fed6, 2026-11-02) | — | itself states "direct httpx script (bypasses coordinator)" | — |

`gh run list`: **no api-football live workflow run after 2026-10-31.** Everything predates #167 (WP-003), the 2026-11-01 parameter additions and the **2026-11-07 DB initialization**.

**Correction to PHASE0.** `api_football_quota_attempts = 0` is **not** evidence of zero attempts. That table is a 60-second rolling window: rows are DELETEd on every reservation (`quota.py:175-180`). The durable evidence is `api_football_quota_counters`, which I queried read-only: **0 rows.** So no reservation has ever been made against this DB. This confirms 0 coordinated provider attempts since initialization, provided the workflows' coordinator secret points at the same DB.

## 2. Findings

| ID | Sev | Finding | Reproduction |
|---|---|---|---|
| F-01 | **P1** | WP-016 not done. Every provider HTTP error (401, 403, 429, 5xx), `PROVIDER_CIRCUIT_OPEN`, `PROVIDER_REQUEST_QUEUE_TIMEOUT`, `PROVIDER_NO_REPLY` and `PROVIDER_REPLY_CONTRACT_INVALID` collapses to `PROVIDER_REQUEST_FAILED`. The client cannot tell "invalid key / plan" (401/403), "rate-limited" (429) or "breaker open, nothing sent" from a generic failure, so it cannot obey back-off. Codes are fixed and secret-free, so this is not a leak | `scripts/repro.py` → `http_429/401/503`, `circuit_open`, all `PROVIDER_REQUEST_FAILED`; `tools.py:170-180` |
| F-02 | **P2** | `network_calls_made` under-reports: 0 after 3 real attempts that ended in error, 0 after a real call whose body exceeded 256 KiB. The "Offline PR-03 tool" warning also remains on real errors. This is false accounting (§2.4 "contabilizzati", §7 no false claims) | repro: `PROVIDER_ERROR network_calls_made 0 quota units 3`; `tools.py:65,123-126,162-181` |
| F-03 | **P2** | Non-odds oversize: no partial or manifest path. `/fixtures?date=` for a full day, `/fixtures?live=all`, large `/teams`/`/players` replies come back as `PAYLOAD_TOO_LARGE` (256–512 KiB) or `PROVIDER_RESPONSE_TOO_LARGE` (>512 KiB), with no data, no identifiers and the quota unit spent. Fail-closed is correct (no silent truncation), but the capability is unusable and there are no ids to recover the rest (§2.3 asks for "identificativi per recuperare il resto") | repro `fixtures_300KiB`, `fixtures_600KiB` |
| F-04 | **P2** | `/odds` manifest dead end for a single oversized fixture. It lists counts only (no bookmaker ids or bet ids), and `retrieval` tells the client to call `apifootball_odds(fixture=<id>, page=1)`, the call that just overflowed. The live manifest does carry bet ids; the prematch one should too (bookmaker + bet) | repro `odds_single_fixture_oversize` (4 bookmakers × 60 markets → manifest, `bookmaker_ids_in_manifest False`) |
| F-05 | **P2** | Provider quota error in an HTTP-200 body is not reconciled. API-FOOTBALL signals limits with HTTP 200 + `errors:{requests|rateLimit:…}`. `transport.py:108-109` raises `PROVIDER_DECLARED_ERRORS` before reading `x-ratelimit-*` headers, so `mark_exhausted` never runs. Other workers and workflows keep spending against an exhausted key until the local budget runs out | repro `declared_error_quota` → `mark_exhausted []` with both headers 0 |
| F-06 | P3 | Search and ID constraints are under-validated against the captured docs. `countries` search must be exactly 3; `venues`/`coachs` ≥3; `bookmakers`/`bets` exactly 3; `/odds/bets` `id` is a string per the docs. Short searches reach the provider and cost 1 unit each | repro `short_search_*` → `REAL_API_RESPONSE`, 1 unit each |
| F-07 | P3 | The breaker counts one failure per call, not per attempt, so it opens only after 6 units on a persistent 5xx (threshold 2 × 3 attempts). This is bounded but wasteful on a 100/day plan | `scripts/repro2.py` |
| F-08 | P3 | `page ≤ 100` hard cap (`contracts.py:118`). `/players/profiles` (250/page) has more pages than that, so the tail of the catalogue is unreachable. The total is visible in `paging`, so the truncation is not silent | code reading |
| F-09 | P3 | `api_football_quota_counters` minute and day rows are never pruned (unbounded slow growth). The attempts table is not an audit log, yet PHASE0 used it as one (see 1b) | `quota.py:175-180,195-211` |
| F-10 | P2 (process) | WP-014 open. Five different documentary statuses across contracts, catalog, registry, health and `sources.json`; two different source URLs; the docs capture of 2026-11-01 has no hash. Today the docs sit behind a Cloudflare challenge (403) → DOCS_UNVERIFIED for any re-verification without a browser session | file:line in table |
| F-11 | P2 (verification) | Coordinator secret not verified. Whether `APIFOOTBALL_COORDINATOR_DATABASE_URL` exists in env `api-football-live` and equals Render's `DATABASE_URL` is unverifiable from here. If they differ, workflow and runtime budgets are **not** shared (§2.4) | GitHub environments API → 403 through the session proxy |
| F-12 | P3 | Integration tests ran on PG16 only. CI does not run them (they skip without `TEST_DATABASE_URL`), and production is PG18.6 (WP-019) | skip reasons in pytest `-rs` |

No P0 found in this component. Nothing bypasses the coordinator in the current code: the runtime goes through `ResilientFootballClient` → `PostgresQuota` (`wiring.py:169-179`) and the scripts go through `coordinated_client`. There is no secret leakage, no invented parameter and no derived probabilities.

## 3. Work-package status

| WP | Requirement | Real state at a8fb228 | Status | Closure criteria |
|---|---|---|---|---|
| WP-003 (A1) | Live scripts only via the PG coordinator + breaker, fail closed without DB | Done in code (`live_harness.py`, 3 scripts, 3 workflows with a DATABASE_URL guard); offline + PG16 integration tests pass | **DONE_CODE / NOT_REAL_VERIFIED** | owner confirms the secret exists **and equals the runtime DB**; one smoke run (≤4 calls) shows `api_football_quota_counters` day `used` +N in prod DB = calls sent |
| WP-004 (A2) | Versioned migration for quota tables | tables exist in prod (schema_migrations 1..8); 0 rows | DONE (by PHASE0) | — |
| WP-014 (A3) | One source of truth for doc status + stored capture & hash | 5 inconsistent labels; no hash | **OPEN** | single status enum derived from one file (`official_parameter_matrix` + sha256 of the capture); contracts, registry, catalog, health and `sources.json` read it; a test asserts equality |
| WP-015 (A4) | Real MCP-client certification on current SHA: page ≥2, the 36 new params, odds manifest | none since 2026-10-31; 0 counters in prod | **OPEN (blocked on OAuth re-auth + owner live run)** | plan §4 executed on the current SHA with raw artefacts + hashes, counters reconciled |
| WP-016 (A5) | Fixed codes 429/401/circuit/queue preserved in MCP output | collapsed to `PROVIDER_REQUEST_FAILED` (F-01); accounting wrong (F-02) | **OPEN** | allowlist covers `PROVIDER_HTTP_{400,401,403,404,429,5xx}`, `PROVIDER_CIRCUIT_OPEN`, `PROVIDER_REQUEST_QUEUE_TIMEOUT`, `PROVIDER_NO_REPLY`, `PROVIDER_REPLY_CONTRACT_INVALID`; RED/GREEN tests through the tool layer; `network_calls_made` = attempts actually sent |
| WP-020 (A6, D09) | UTC canonical, raw kept, Europe/Rome DERIVED with DST/ambiguous tests | absent (0 hits) | **NOT_IMPLEMENTED** | a derived block outside `data` (e.g. `derived.kickoff_europe_rome`, label `DERIVED`) computed with `zoneinfo` from the provider `fixture.timestamp`/`date`; `TIMEZONE_UNRESOLVED` when the source tz is not proven; DST-edge tests (last Sunday of Mar/Oct) |
| WP-021 (A7, D10) | Remove inactive out-of-scope modules | `capabilities.py`, `indicators.py`, `gap_matrix.py`, `registry_import.py` present; only tests import them | **OPEN** | import graph shows no runtime, server or script reference; delete modules + their tests; tool count stays 42; full suite green |
| (new) | F-03/F-04/F-05 | — | OPEN | manifest with ids for non-odds and single-fixture odds; HTTP-200 quota errors reconciled via headers/`mark_exhausted` |

## 4. Minimal real-test plan (within D04 budget, no plan upgrade)

Preconditions (owner):
- (a) a real MCP client re-authenticates via OAuth;
- (b) confirm the `APIFOOTBALL_COORDINATOR_DATABASE_URL` secret = the runtime DB;
- (c) read the runtime `APIFOOTBALL_MAX_DAILY_REQUESTS`; the code default is 80, the prod value is unverified;
- (d) a read-only snapshot of `api_football_quota_counters` before and after.

**Budget: 16 provider calls total**, within the 80/day runtime default and the provider's 100/day free plan. The run is on one UTC day, with 0 other jobs.

| # | Tool call (current SHA, real MCP client) | Units | Verifies |
|---|---|---|---|
| 0 | `apifootball_health` | 0 | `quota_coordinator: READY`, limits |
| 1 | `apifootball_status` | 1 | key/plan, PII stripped, `quota_remaining` headers |
| 2 | `apifootball_fixtures(league=135, season=<cur>, from_date=D, to_date=D+2)` | 1 | targeted filter, raw `fixture.date`/`timezone`/`timestamp` kept |
| 3 | same call again within 15 s | 0 | cache HIT, `network_calls_made 0`, counters unchanged |
| 4 | `apifootball_fixtures(id=X)` (finished fixture) | 1 | ids, raw timestamps |
| 5 | `apifootball_fixture_statistics(fixture=X, half=true)` | 1 | new `half` param; nulls stay null |
| 6 | `apifootball_fixture_events(fixture=X)` | 1 | events |
| 7 | `apifootball_injuries(fixture=X)` | 1 | injuries |
| 8 | `apifootball_standings(league=135, season=<cur>)` | 1 | standings |
| 9 | `apifootball_players(league=135, season=<cur>, page=2)` | 1 | **page ≥ 2** |
| 10 | `apifootball_odds(fixture=Y upcoming)` | 1 | prematch prices, or the manifest (F-04 check) |
| 11 | `apifootball_odds(date=D, page=2)` | 1 | odds page ≥2 / PARTIAL_ODDS_MANIFEST |
| 12 | `apifootball_live_odds(league=<live league>)` | 1 | live / manifest with bet ids |
| 13 | `apifootball_live_odds(fixture=Z, bet=<id from 12>)` | 1 | exact retrieval path |
| 14 | `apifootball_bookmakers(search="bet")`, `apifootball_bets(search="ove")` | 2 | catalogs (3-char search) |
| 15 | negative: `apifootball_fixtures(timezone="Europe/Rome")`, `apifootball_odds(page=2)` | 0 | INVALID_INPUT, no unit |
| 16 | `api-football-live-smoke` workflow (cap 4) on the same day | ≤ 2 | shared counters: the workflow sees runtime `used` (needs `MAX_DAILY_REQUESTS` ≥ used+…; otherwise the expected outcome is **refused, 0 sent**, which also counts as a PASS of sharing) |

**Acceptance:** the prod `api_football_quota_counters` day `used` increases by exactly the number of non-HIT calls; leases return to 0; there are no error lines in the Render logs. Each reply is stored as a raw artefact with sha256. Results are classified HTTP_200 / DATA_PRESENT / EMPTY_VALID / PARTIAL / ERROR / NOT_TESTED. The 39-route inventory re-run (39 units) is **not** in this minimal plan; run it alone on a separate UTC day if the owner wants per-route re-certification.
