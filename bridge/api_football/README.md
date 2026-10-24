# API-FOOTBALL Football API v3 — bridge integration

**Scope:** Football only. Owner mandate `FCB-APIFOOTBALL-INTEGRATION-CERT-01`.
**PR-01:** Optional, mock-only foundation. **PR-02:** Hardened offline-testable resilience primitives. **Neither PR authorizes real provider traffic.**

## Fail-closed activation

The module is **disabled by default**. With `APIFOOTBALL_ENABLED=false`
(or unset), `bridge.server` does not import any file from
`bridge.api_football` and the 34 existing `browser_*` tools remain
unchanged.

With `APIFOOTBALL_ENABLED=true`, **42 offline-only typed**
`apifootball_*` tools are registered (38 football paths, `/status`,
coverage plus two diagnostics). No client transport is constructed, no
provider calls are made, and no external service is created.

| Environment variable | Default | Meaning |
|---|---|---|
| `APIFOOTBALL_ENABLED` | `false` | Opt-in flag (only `true` or `1` enables) |
| `APIFOOTBALL_API_KEY` | unset | Optional secret; do **not** set until separately authorized |
| `APIFOOTBALL_BASE_URL` | `https://v3.football.api-sports.io` | Pinned official host; all other values rejected |
| `APIFOOTBALL_TIMEOUT_SECONDS` | 8 | Allowed 0.5–30 s |
| `APIFOOTBALL_MAX_RPM` | 8 | Shared PostgreSQL quota cap **when the PR-02 engine is explicitly used** |
| `APIFOOTBALL_MAX_DAILY_REQUESTS` | 80 | Shared PostgreSQL quota cap **when the PR-02 engine is explicitly used** |
| `APIFOOTBALL_MAX_CONCURRENCY` | 2 | PostgreSQL lease + local semaphore when PR-02 engine used |
| `APIFOOTBALL_CACHE_ENABLED` | true | PR-02 bounded local TTL cache (not active in MCP yet) |
| `APIFOOTBALL_CIRCUIT_BREAKER_ENABLED` | true | PR-02 per-worker breaker (not active in MCP yet) |

**No production environment variables, secrets or permissions are changed
by the PR.** Do not switch on the module on Render before a separately
authorized merge and deploy. Invalid enabled configuration is isolated:
the bridge starts with its original browser tool registry.

## Contracts and security

- Catalog is a **documentary path inventory** from MatchPilot #248, not
  a certified endpoint contract. 38 football GET paths + /status.
- No arbitrary host, URL or endpoint; no credentials in MCP parameters.
- `MockFootballClient` serves only supplied test fixtures and returns
  defensive copies. `RealFootballClient` rejects every actual call.
- The active OAuth middleware, existing scopes, browser session lifecycle,
  rate limits for browser sessions, log contents and `/healthz` are not modified.
- Neither empty response nor missing field is normalized to zero.
- Missing data, PIT guarantees and bookmaker/model provenance remain UNKNOWN.

## PR milestones (one PR at a time, no automatic merge)

1. PR-01 — optional module, config, allowlisted documentary catalog,
   network-free mock and isolated MCP diagnostic **(this PR)**.
2. PR-02 — hardened HTTP transport **injectable only in tests/future trusted code**, shared PostgreSQL quotas, bounded cache, deduplication, timeout, finite retry and breaker. Multiworker simulations on CI PostgreSQL; NO runtime/MCP wiring (this PR).
3. PR-03 — conservative typed endpoint tools and response envelopes (this PR);
   source-verification confidence and unresolved parameters are explicit.
   Network remains disabled; no paid provider calls authorized.
4. PR-04 — coverage diagnostics and MatchPilot field-level comparison
   for all relevant FPT fields, independent empirical validation.



## PR-03 — Optional typed MCP research catalog (NO LIVE DATA)

PR-03 introduces **42 explicit typed MCP tools** (38 football endpoints,
one `/status` account endpoint, `apifootball_coverage_report`,
`apifootball_health`, `apifootball_quota_report`) under
`apifootball_*`. The opt-in flag `APIFOOTBALL_ENABLED` remains OFF by
default. Existing 34 `browser_*` tools, Browser Use, CloakBrowser, OAuth,
session lifecycle and transport are **unchanged**.

The endpoint tool names are:
`apifootball_status`, `apifootball_countries`,
`apifootball_leagues`, `apifootball_seasons`,
`apifootball_fixtures`, `apifootball_fixture_statistics`,
`apifootball_fixture_events`, `apifootball_fixture_lineups`,
`apifootball_teams`, `apifootball_team_statistics`,
`apifootball_players`, `apifootball_injuries`,
`apifootball_predictions`, `apifootball_odds`,
`apifootball_live_odds`, `apifootball_bookmakers`,
`apifootball_bets`, `apifootball_coverage_report`,
`apifootball_timezones`, `apifootball_team_seasons`,
`apifootball_team_countries`, `apifootball_venues`,
`apifootball_standings`, `apifootball_fixture_rounds`,
`apifootball_fixture_headtohead`, `apifootball_fixture_players`,
`apifootball_coaches`, `apifootball_player_seasons`,
`apifootball_player_profiles`, `apifootball_player_squads`,
`apifootball_player_teams`, `apifootball_top_scorers`,
`apifootball_top_assists`, `apifootball_top_yellow_cards`,
`apifootball_top_red_cards`, `apifootball_transfers`,
`apifootball_trophies`, `apifootball_sidelined`,
`apifootball_odds_mapping`, `apifootball_live_bets`.
The diagnostics and additional `/leagues` coverage view make
**42 tools, with 39 unique provider paths** (38 football + /status).

**No production network wiring yet.** Even with
`APIFOOTBALL_ENABLED=true` AND a dummy/future real API key, endpoint
tools return `REAL_API_TEST_NOT_AUTHORIZED`, `data=null`,
`network_calls_made=0`. Without a key they return
`KEY_NOT_CONFIGURED`. Only unit tests may inject
`MockFootballClient` directly in Python; then the envelope states
`MOCK_VERIFIED` and preserves original null/zero/empty/absent distinctions.
This mechanism is intentionally not an MCP argument or env toggle.

Every registered endpoint tool:
- Has its own fixed, typed parameters (no arbitrary host, path or URL).
- Rejects unbounded fixture/odds scans and oversized page/batch numbers.
- Rejects invalid dates, non-positive IDs, unsupported timezone inputs,
  unknown fields, control characters and booleans in integer slots.
- Returns a structured envelope with UTC acquisition time, source
  version, document-confidence indicator, cache/paging/provenance
  placeholders and a **truthful blocked/no-data status**.
- Limits mocked response payloads to 256 KiB and redacts exceptions.

The underlying catalog inventories **38 football GET paths plus
/status**, and PR-03 now has an explicitly typed tool for **every path**.
This is complete **path coverage**, not proof that every optional
parameter is supported or that all fields are available for every
league. Unreconciled parameter sets remain marked as such. Coverage flags in
/leagues do **not** certify complete match-level field availability.

Official documentation: https://www.api-football.com/documentation_v3
(version 3.9.3 from the provider's published searchable documentation).
The official documentation v3.9.3 was independently examined from the
public indexed provider source; direct bridge-browser fetching is still
Cloudflare-blocked (HTTP 403). Not all optional query combinations were
independently verified and no actual provider call was made. Where exact
parameter conditions are not independently reconciled, the tool
envelope explicitly reports `PENDING_PARAMETER_RECONCILIATION`.
No real provider API or billing test is permitted under this PR.

**Provider query execution must remain fail-closed until separate owner
authorization** for endpoint-specific schema reconciliation, key
configuration, operational limits and a limited real-data sample. Code
review / CI passing cannot be used as permission to switch on paid calls.


## PR-02 resilience layer — OFFLINE by construction in the deployed registry

Added `quota.py`, `cache.py`, `circuit.py`, `errors.py`,
`transport.py`, and `resilience.py`. These are **library classes only**.
No HTTP client or resilience orchestrator is instantiated by
`bridge/server.py`; `RealFootballClient` from PR-01 still raises
`ProviderNotAuthorized`. No new environment variables or expenses.

The `InjectedHTTPXTransport` requires an explicit HTTPX client with the
pinned official `base_url`. It rejects redirects, oversized bodies,
invalid JSON, malformed pagination and provider-declared errors, and
reads the key exclusively from its injected argument for header auth.
No secret is logged, returned or put in a query string. Unit tests use
`httpx.MockTransport` exclusively. The endpoint-specific parameter
contracts are **not yet verified** and this transport **must not** be
wired to MCP until PR-03 and owner-authorized live testing.

**Shared quota is enforced at the PostgreSQL transaction boundary**,
not a process-local counter:
- A per-API-key SHA-256 digest scopes quota counters and concurrency leases.
- `pg_advisory_xact_lock` serializes reservations across all workers using
  the **same database and key**; minute/day counters reserve atomically.
- A failed second quota check rolls back the first debit. Every actual
  attempt consumes one debit, including failed HTTP requests; no refund
  after cancellation or transport failure.
- Concurrency leases expire after 180 s if a worker crashes; ordinary
  completion deletes them.
- Provider-reported `remaining=0` in a recognized quota header can
  freeze that UTC minute/day in the shared table. Attempts older than
  60 seconds are pruned opportunistically for the active key.
- This is **not a guarantee** against independent API clients outside
  this bridge using the same provider key. Stop on uncertainty or denial.

The request coordinator also has bounded local admission/queueing,
short endpoint-specific cache TTLs (never caches `/status`), single-flight
deduplication with cancellation shielding, timeouts, no more than three
attempts, and a per-worker breaker. HTTP 429/401/403 are not retried;
429 freezes the shared minute bucket. All transport errors are stable,
sanitized codes. Response data is preserved, including null, zero and
absent-field differences.

**Not yet certified or enabled:** provider's real header semantics,
sliding rate windows, accuracy of provider limits, dynamic coverage,
endpoint parameters, field provenance, licensing/retention or
production monitoring. A live-key rollout requires another approval.

**Multiworker limitation:** independent workers must point to the same
existing PostgreSQL database and share the exact provider key; otherwise
there is no shared cross-process budget. Changing DB workers,
`WEB_CONCURRENCY` or instances is outside this mandate.

**CI gates:** unit tests with injected HTTPX mocks, plus PostgreSQL tests
for cross-instance atomicity, quota exhaustion, lease recovery and
plaintext-key absence. Tests without `TEST_DATABASE_URL` are **SKIP**,
not PASS, and the `decision-postgres` CI job must run them.


Risk: Render `main` has auto-deploy enabled. Merging a PR is therefore
a deployment action and requires explicit separate owner approval.
