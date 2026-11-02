# API-FOOTBALL — Phase A reconciliation, code first (read-only)

- Repo: `/home/user/football-consensus-engine`. Local HEAD is `11729cb`, but `git diff --stat origin/main -- bridge tests` is empty, so the checked-out `bridge/` and `tests/` match `origin/main` = `342fed6`.
- Date: 2026-11-02. No network calls, no repo changes.
- Latest API-FOOTBALL code change on main: `218f9b9` (2026-11-01, raised the exposed parameters to 140). **None of the real evidence in the repo was recorded after that commit.**

## 1. Real counts (computed from the code, not taken from old reports)

| Item | Count | Source |
|---|---|---|
| `apifootball_*` MCP tools | **42** | `tools.py` `@add` = 42, asserted by `assert len(names)==42` and the offline E2E `tools/list` |
| of which are provider contracts | 40 | `contracts.py:ENDPOINTS` |
| of which are local diagnostics | 2 | `apifootball_health`, `apifootball_quota_report` |
| distinct provider paths | 39 = 38 football + `/status` | `catalog.py`. `/leagues` serves 2 tools (`leagues`, `coverage_report`) |
| declared query parameters | **142** over 40 contracts; **140** without the duplicated `/leagues` view (`coverage_report`: id, season) | `sum(len(fields))` |
| MCP schema parameters | 142. Every contract's fields equal its MCP signature (`from`/`to` are exposed as `from_date`/`to_date`), and the required sets match | introspection + `test_mcp_offline_protocol_e2e.py` |
| contracts with `confidence` other than `DOCUMENTED_OFFICIAL` | **0 / 40** | `contracts.py` |
| contracts with `pagination=True` | 4 (players, player_profiles, odds, odds_mapping) | `contracts.py` |

Old claims checked: "42 tools / 38 football endpoints / 140 params" is **correct**, with two caveats. There are 39 paths if `/status` is counted, and there are 142 declared parameters if `coverage_report` is counted.

## 2. Verification levels (numerator / denominator)

| Level | Tools (n/42) | Notes |
|---|---|---|
| Implemented | 42/42 | |
| Exposed through MCP (offline `tools/list` with OAuth) | 42/42 | `test_mcp_offline_protocol_e2e.py` |
| Offline / mock tested | 42/42 | `test_apifootball_typed_mcp.py` and `test_apifootball_full_catalog.py` (each tool runs against the mock with MOCK_VERIFIED) |
| Real provider (route level) | 40/40 provider tools | Route inventory run 40211464376 (2026-10-25): 38/39 routes HTTP 200. `/odds` returned 0 results and `/odds/live` was BLOCKED; both were covered later by capability runs 4124xxxx (2026-10-31). **All of these calls went through a raw httpx script, outside the MCP tools and outside the PostgreSQL coordinator.** |
| Real MCP client (narrative docs only, no raw artefact) | 10/40 | bookmakers, bets, live_bets, fixtures, live_odds, odds, fixture_statistics, team_statistics, players (Render `cae48c4b`, 2026-10-25), plus odds_mapping (changelog 2026-10-31). All of it predates `5bd28fe`, `637ac8b` and `218f9b9`. |
| Production verified on current main `342fed6` | **0/42** | No evidence for the current deploy |
| Per-parameter real verification | **0/142** | `full_contract_audit`: certified_routes 0/39, verdict INCOMPLETE. Route-inventory records do not keep the params they used (they can only be reconstructed from the script's `FIXED`/`NEED` tables). |

Cross-cutting rows: AF-043 to AF-053 in the CSV.

## 3. Offline tests run

| Test files | Result |
|---|---|
| `tests/test_api_football_*.py` + `tests/test_apifootball_*.py` (28 files) | **345 passed, 0 failed** (7.4 s) |
| `test_odds_manifest_and_parameter_matrix.py`, `test_mcp_offline_protocol_e2e.py`, `test_mcp_tool_registry_observability.py` | **27 passed** |
| `tests/integration/test_apifootball_*.py` | **11 skipped**: no `TEST_DATABASE_URL` locally. These tests run in CI job `decision-postgres` of `multi-market-tests.yml`. |

## 4. Contracts marked DOCS_UNVERIFIED / PENDING — the labels conflict

- `contracts.py`: 40/40 are `DOCUMENTED_OFFICIAL`; 0 are `PENDING_PARAMETER_RECONCILIATION`. The source is `docs/api_football/official_parameter_matrix_v3.9.3.json` (38 endpoints, 140 params), retrieved 2026-11-01 from the Redoc page rendered in Browser Use. The OpenAPI file returned 403. No raw capture or hash of the page is stored.
- Other places say otherwise:
  - `registry.py`: every parameter is `PENDING_OFFICIAL_VERIFICATION`, with type `UNKNOWN`.
  - `catalog.py`: `EVIDENCE="PENDING_OFFICIAL_RECONCILIATION"`.
  - `docs/api_football/sources.json`: status PENDING.
  - `apifootball_health`: reports `documented_contract: "PARTIALLY_RECONCILED_OFFICIAL"`.
- Five artefacts disagree. Under CLAUDE.md §0 this is a conflict to document, not to settle silently.
- `timezone` is restricted to `UTC` by policy (`TIMEZONE_NOT_CERTIFIED`) on fixtures, injuries, odds, fixture_rounds and headtohead.

## 5. Gaps found in the code and evidence (all reproduced or read in code)

- **G1 (P0, §2.4).** All three live evidence scripts call api-sports through raw `httpx.AsyncClient` with no `PostgresQuota` and no circuit breaker: `scripts/api_football_39_route_inventory.py`, `api_football_capability_probe.py` and `api_football_live_smoke.py`. They are `workflow_dispatch` only and have their own caps, but this conflicts with "ogni chiamata a un provider a quota passa dal coordinatore condiviso". It also means the real evidence does not cover the MCP code path.
- **G2 (P1, §5).** The quota tables `api_football_quota_{counters,leases,attempts}` are created at runtime with `CREATE TABLE IF NOT EXISTS` (`quota.py:_SCHEMA`). There is no versioned migration in `sql/migrations/` (0001–0008 contain none for them), yet the tables already exist live on Neon (narrative 2026-10-25).
- **G3 (P1).** The documentary-status conflict described in §4.
- **G4 (P1).** No real evidence exists after `218f9b9`. Its 36 added parameters, the `/odds/live` manifest (`637ac8b`) and the pagination fix (`5bd28fe`) have never been exercised against the provider or a real MCP client.
- **G5 (P1).** No real fetch of page 2 or later for `/players`, `/players/profiles`, `/odds` or `/odds/mapping`. The changelog itself says odds_mapping stays PENDING until page 2 is verified.
- **G6 (P2).** The MCP error mapping collapses several fixed, safe codes into `PROVIDER_REQUEST_FAILED`: `PROVIDER_HTTP_401/403/429/5xx`, `PROVIDER_CIRCUIT_OPEN`, `PROVIDER_REQUEST_QUEUE_TIMEOUT`, `PROVIDER_REPLY_CONTRACT_INVALID` and `PROVIDER_NO_REPLY`. Reproduced offline. Nothing leaks, but the client cannot tell a 429 from a 401 or an open circuit.
- **G7 (P2, §2.3).** No `Europe/Rome` DERIVED time anywhere in `bridge/api_football/`. Raw UTC is preserved, which is correct.
- **G8 (P2, §4).** Evidence outcome vocabulary is `PASS_DATA`, `PASS_EMPTY_NOT_AVAILABILITY` and `BLOCKED_MISSING_REAL_PREREQUISITES`, while CLAUDE.md §4 requires `HTTP_200`, `DATA_PRESENT`, `EMPTY_VALID`, `PARTIAL`, `ERROR` and `NOT_TESTED`.
- **G9 (P2, §1 scope).** Out-of-scope legacy modules remain in `bridge/api_football/`: `capabilities.py` and `indicators.py` (MatchPilot / TotalCorner), and `gap_matrix.py` / `registry_import.py` (FPT comparison). They are not registered as MCP tools. Removing them is an owner decision; this is not a refactor to do now.
- **G10 (ASSUMED, not verified here).** The current Render env flags (`APIFOOTBALL_ENABLED`, `APIFOOTBALL_LIVE_AUTHORIZED`), the live deploy SHA and Neon state were not checked, because no external calls were allowed.

Already done, no work needed:
- typed contracts with no arbitrary URL;
- pinned origin, no redirects;
- shielded leases with orphan release (`6209eb8`) and schema prepare outside the lease timeout (`b1a0759`);
- 256 KiB cap with a price-free manifest for both `/odds` and `/odds/live`;
- PII stripping on `/status`;
- missing/null preserved (mock tests);
- 140/140 parameters exposed (offline test in both directions).

## 6. Proposed work packages (justified by the evidence only)

**WP-A1. Route evidence probes through the shared coordinator** (P0, G1)
- Files: the 3 scripts above, their workflows, a new test file.
- RED: a test that imports each script with a fake `httpx` transport and asserts that every request first takes a `PostgresQuota` lease. It fails today because the scripts build `httpx.AsyncClient` directly.
- GREEN: the scripts use `ResilientFootballClient` (or the MCP path) against the CI Postgres or Neon coordinator. Scripts that bypass it are disabled.
- Real test: one bounded inventory run of 39 calls or fewer, recorded under `evidence/` with SHA and date.
- Owner authorization needed: the `workflow_dispatch` run, and the coordinator DSN as a GitHub secret (a new secret transfer).
- DONE: no provider call in the repo bypasses the coordinator; evidence is regenerated.

**WP-A2. Versioned migration for the quota tables** (P1, G2)
- Files: `sql/migrations/0009_api_football_quota.sql`, `quota.py`, `tests/integration/`.
- RED: a migration test on a DB that is already initialised (with the tables already present) checks that the migration is tracked. It fails today: no migration exists.
- GREEN: an idempotent `IF NOT EXISTS` migration matching the current DDL exactly (no column changes), plus tracking. The runtime DDL stays only as a fallback or is removed.
- Real test: apply on Neon, verify it is a no-op on the existing tables, and check that the row counts are unchanged.
- Owner authorization needed: applying the migration on the live DB.
- DONE: the CI `decision-postgres` job passes and Neon shows the migration recorded.

**WP-A3. One documentary-status source of truth** (P1, G3)
- Files: `registry.py`, `catalog.py`, `tools.py` (health), `docs/api_football/sources.json`, and a stored hash of the official matrix.
- RED: a test asserting that `registry` status, `contracts` confidence and the health label agree. It fails today.
- GREEN: derive every label from `ENDPOINTS` plus the matrix. Parameters without a recorded source section stay `DOCS_UNVERIFIED`.
- Real test: none (documentation only).
- Owner authorization: none, unless the owner wants the matrix re-captured.
- DONE: one label per contract, test-enforced.

**WP-A4. Real MCP-client certification on the current main SHA** (P1, G4/G5)
- Files: a new evidence file under `evidence/api_football/mcp_client_<date>_<sha>.json`.
- RED: not applicable (evidence work package).
- GREEN: run the steps in `docs/api_football/mcp_client_certification_runbook.md` (15 calls or fewer), extended with page 2 for odds_mapping and players and one call per parameter added in `218f9b9`. Record the client, the SHA, `status`, `network_calls_made` and `cache_status`.
- Real test: yes.
- Owner authorization needed: the live gate on Render and the call budget (within 80/day).
- DONE: each of the 42 rows has an artefact dated on the current SHA, using the §4 vocabulary (G8).

**WP-A5. Keep the fixed error codes in the MCP output** (P2, G6)
- Files: `tools.py`, `tests/test_apifootball_typed_mcp.py`.
- RED: a client raising `ProviderHTTPError(429)` should produce `error.code=="PROVIDER_HTTP_429"`. Today it produces `PROVIDER_REQUEST_FAILED`.
- GREEN: allowlist the fixed codes `PROVIDER_HTTP_{400..599}`, `PROVIDER_CIRCUIT_OPEN`, `PROVIDER_REQUEST_QUEUE_TIMEOUT`, `PROVIDER_REPLY_CONTRACT_INVALID` and `PROVIDER_NO_REPLY`. Still no exception messages.
- Real test: none.
- Owner authorization: none.
- DONE: CI green.

**WP-A6. Europe/Rome rendering as DERIVED** (P2, G7)
- Product decision first: whether the MCP layer or the client renders the time.
- If the MCP layer does it: add a separate `derived` block (raw date kept) marked `DERIVED`.
- RED: a fixture date in UTC should have a `derived.kickoff_europe_rome` field. It is missing today.
- Owner authorization: the product decision.

**WP-A7. Retire out-of-scope modules** (P2, G9)
- Owner decision required: the MatchPilot / FPT / TotalCorner code is out of scope per §1.
- Remove the modules only if the owner agrees, together with their tests. This is a declared behaviour change.
