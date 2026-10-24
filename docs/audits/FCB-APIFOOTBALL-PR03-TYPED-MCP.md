# FCB-APIFOOTBALL-INTEGRATION-CERT-01 — PR-03 typed MCP endpoints

Date: 2026-10-25 (Europe/Rome). Football-only, €0, no live API authorization.

## Baseline

- Branch base: `main`, commit `4228bfc4758d2c035efaf97eb9f41d19ba656891`.
- PR #105 Foundation and PR #106 Resilience are merged.
- PR #106 approved by an owner-specific review waiver, CI was 852 PASS,
  102 SKIP in Python and 101 PASS in PostgreSQL. This waiver is **not**
  transferable to PR-03.
- Render deploy of #106 `dep-dbeilf49v7es73ciuit0`: LIVE, `browser_health`
  and actual CloakBrowser create/navigate/stop PASS.
- Original 34 `browser_*` tools and OAuth are not touched by PR-03.

## Goals and non-goals

Develop typed football research contracts while **not** connecting any
real API key or provider network call. PR-03 adds 20 optional
`apifootball_*` MCP tools (18 endpoint-related + health + quota-report)
under `bridge/api_football/`, registered only when
`APIFOOTBALL_ENABLED=true`. With the flag absent, startup and browser
registry are identical to baseline. With the flag enabled, every endpoint
tool still refuses live querying, including if a future key is present.

No runtime wiring to the PR-02 HTTPX client or resilience orchestrator,
no external service or migration, no new env settings, no modifications
to OAuth/session manager/reaper, no MatchPilot writes, no TotalCorner
subscription changes and no private provider calls.

## Official documentation and limits of evidence

Reference: https://www.api-football.com/documentation_v3
Provider docs index: API-FOOTBALL **v3.9.3**. The reference documents
GET-only endpoints, `x-apisports-key` header, statistics,
fixtures, predictions, odds/live and odds pre-match.

- At the time of this PR, the connected cloud browser encountered
  Cloudflare HTTP 403 on the documentation and YAML path.
- Search-indexed official provider documentation and provider posts
  were available. Core path names and some parameter names can be
  corroborated publicly, but the full OpenAPI YAML and all cross-field
  constraints have **not** been independently ingested.
- Endpoint tool `source_contract` is either
  `DOCUMENTED_OFFICIAL` for a curated common parameter subset or
  `PENDING_PARAMETER_RECONCILIATION` where full typing is not attested.
- `DOCUMENTED_OFFICIAL` here does NOT mean `REAL_VERIFIED`, nor that
  a league exposes every response field or every bookmaker market.
- The full MatchPilot #248 documentary inventory remains 38 GET paths
  + /status; this PR implements **18 endpoint-type tools**, not all 38.

## Structured tool catalog

| MCP tool | Provider path | Status |
|---|---|---|
| apifootball_status | /status | Documentation only |
| apifootball_countries | /countries | Documentation only |
| apifootball_leagues | /leagues | Documentation only |
| apifootball_seasons | /leagues/seasons | Documentation only |
| apifootball_fixtures | /fixtures | Documentation only |
| apifootball_fixture_statistics | /fixtures/statistics | Documentation only |
| apifootball_fixture_events | /fixtures/events | Documentation only |
| apifootball_fixture_lineups | /fixtures/lineups | Documentation only |
| apifootball_teams | /teams | Documentation only |
| apifootball_team_statistics | /teams/statistics | Documentation only |
| apifootball_players | /players | Parameter reconciliation pending |
| apifootball_injuries | /injuries | Parameter reconciliation pending |
| apifootball_predictions | /predictions | Documentation only |
| apifootball_odds | /odds | Documentation only |
| apifootball_live_odds | /odds/live | Documentation only |
| apifootball_bookmakers | /odds/bookmakers | Parameter reconciliation pending |
| apifootball_bets | /odds/bets | Parameter reconciliation pending |
| apifootball_coverage_report | /leagues | Coverage flag only |
| apifootball_health | local only | Offline |
| apifootball_quota_report | local only | Configured caps only |

No new tool takes a raw URL, raw provider path or user-supplied API key.
Each query parameter is bounded by type and range. Fixtures and odds
require targeted filters. Page is capped at 100, dates must be ISO
calendar dates, timezone is currently limited to UTC, IDs positive.
These are conservative **local safeguards**, not claims about provider
maxima. The /fixtures/statistics `half` optional filter is held back
until its accepted type and semantics are reconciled against the
official machine-readable contract.

## Data envelope and provenance

Endpoint responses return:
`provider`, `tool`, `endpoint`, `parameters`, `status`,
`source_contract`, `documentation_url`, `documentation_version`,
`retrieved_at_utc`, `provider_timestamp_utc` (null until genuinely
known), `cache_status`, `quota_remaining` (null until known),
`pagination`, `data` (null until authorized), `network_calls_made`.
Error responses use stable codes. Results from explicitly injected
unit-test mocks are `MOCK_VERIFIED`, **never** `REAL_VERIFIED`.
Raw values including null, zero, empty and absent keys are preserved.
A 256 KiB bound stops mock output expansion; larger real-data payloads
will require additional governed pagination / projection design.

Provider `/predictions` is NOT bookmaker-implied probabilities:
official documentation says it derives from algorithms including
Poisson/team-form comparisons, not bookmaker odds. No prediction
timestamp/PIT guarantee is inferred. Prematch odds archival availability
differs from in-play odds (no in-play history according to provider).
Those are **documented**, not empirical assertions.

## Security and resilience regression gates

- Disabled API module: zero registered new tools and original 34 browsers.
- Enabled module: +20 only, no network even with a test key.
- Typed signatures, no arbitrary URL, safe validation, rejection of
  oversized/unbounded scans, no raw upstream exception leakage.
- Mock response equality preserves missing/null/zero/empty.
- Existing OAuth, browser sessions, leases, reaper, SSRF checks unchanged
  at code level.
- PR-02 rate limiter remains **library only**, so it is not valid to
  claim that new tools already enforce operational provider quota.
- Test suites `pytest` and `decision-postgres` mandatory at final SHA.
- Independent review required, or a separate owner waiver specifically
  for this PR. CodeRabbit Free status checks are not a substitute.

## MatchPilot source replacement

Still **NOT CERTIFIED**. MatchPilot #248/#249 and raw 325-field FPT
inventory are read-only; no runtime data, coverage by league or PIT
comparison executed. TotalCorner stays suspended. The endpoint catalog
supports planning of a later comparison but proves no xG, xGoT,
dangerous attacks, corners markets or historical opening/closing odds.

## D12 verdict at PR preparation

- `PR03_TYPED_MCP_OFFLINE_IMPLEMENTED`: pending GitHub CI/review.
- `MCP_INTEGRATION_READY`: NO for real provider reads.
- `REAL_API_TEST_NOT_AUTHORIZED`: YES.
- `REAL_API_TEST_READY`: NO.
- `MATCHPILOT_REPLACEMENT_NOT_YET_CERTIFIED`: YES.
- `MERGE_DEPLOY`: requires separate owner approval.

No further PR should be opened while PR-03 remains active.
