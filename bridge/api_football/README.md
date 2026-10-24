# API-FOOTBALL Football API v3 — bridge integration

**Scope:** Football only. Owner mandate `FCB-APIFOOTBALL-INTEGRATION-CERT-01`.
**PR-01:** Optional, mock-only foundation. **PR-02:** Hardened offline-testable resilience primitives. **Neither PR authorizes real provider traffic.**

## Fail-closed activation

The module is **disabled by default**. With `APIFOOTBALL_ENABLED=false`
(or unset), `bridge.server` does not import any file from
`bridge.api_football` and the 34 existing `browser_*` tools remain
unchanged.

With `APIFOOTBALL_ENABLED=true`, `apifootball_health` is registered as an
**offline-only** MCP diagnostic; no client transport, authentication attempt,
provider call, rate limiter or new external service is created.

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
3. PR-03 — source-verified typed endpoint tools and response envelopes;
   never invent parameters. No paid provider calls until authorized.
4. PR-04 — coverage diagnostics and MatchPilot field-level comparison
   for all relevant FPT fields, independent empirical validation.


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
