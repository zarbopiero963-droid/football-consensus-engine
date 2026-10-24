# FCB-APIFOOTBALL-INTEGRATION-CERT-01 — PR-02 Resilience and Security

Date: 2026-10-24. Football only. Budget €0. NO LIVE API CALLS.

## D02/D04/D05 — architecture, isolation and threat model

Baseline main for PR-02:
`d0196e090f6cc6b9ed3ca07900bca7efa9c36c09` (PR #105 merged).
Current production after PR #105: Render
`football-consensus-browser-bridge`, deployment
`dep-dbei2067bikc73ba6ftg`, health and CloakBrowser navigation PASS.
The previous version `dep-dbeerbek1f9s73f8mdcg` is recorded for rollback.

The 34 existing browser MCP tools, OAuth, session leases, reaper,
CloakBrowser, Browser Use and health checks are **not modified** in PR-02.

```text
existing Starlette MCP --> unchanged 34 browser_* tools
                     \--> optional mock-only apifootball_health
                           (no wiring to HTTP transport)

isolated library (not instantiated by the MCP registry):
  validated_path + bounded query args
      -> in-process TTL cache
      -> per-key async singleflight
      -> bounded local queue / semaphore
      -> circuit breaker
      -> shared PostgreSQL Quota(acquire)
      -> injected HTTPX transport (tests/mock only)
      -> Quota(release)
      -> provider-data validation / provenance
```

No outbound traffic is possible via the registered `apifootball_*` MCP
tools in this PR. The disabled `RealFootballClient` from PR-01 is unchanged.
The injected transport is **not a deployed entry point**, and tests supply
`httpx.MockTransport`. No API key, paid plan or provider call was used.

### Security boundaries

- Only `GET` against an exact catalog path; arbitrary paths and raw
  URLs are rejected.
- Single allowed origin
  `https://v3.football.api-sports.io`; reject alternate hosts, IPs,
  protocol, credentialed URLs and redirects.
- Header-based auth `x-apisports-key`, never query/body; API key is
  not returned in MCP outputs.
- At most 16 bounded scalar query parameters accepted at this lower-level
  boundary. **Endpoint-specific names and requirements are not verified**;
  PR-03 will introduce source-verified schemas before user-facing tools.
- No response body logged; errors use fixed class/code instead of
  upstream body, traceback, DSN or secret value.
- Strict total response cap 512 KiB and validation of JSON/pagination.
- No normalization of null/empty/zero/absent fields. Acquisition timestamps
  are not asserted as provider publication timestamps or PIT evidence.

### Shared request budget

`PostgresQuota` operates on **the existing bridge PostgreSQL database**,
without new paid services. Lazy-created isolated tables:
`api_football_quota_counters`, `api_football_quota_leases`.

- Per-key SHA-256 fingerprint (not plaintext secret) scopes all counters.
- Transaction-scoped advisory lock serializes reservations across
  independent workers/processes sharing the same key and DB.
- Atomic UTC minute and UTC day buckets; if either cap is exhausted,
  neither counter advances and no HTTP request is dispatched.
- Every actual attempt consumes one reservation: errors, timeouts and
  cancellations do not refund prior unknown work.
- Concurrent-request leases expire after 180s for crash recovery.
- Provider-reported zero remaining can freeze a window globally.
- Readable quota snapshots exclude the key.
- DB failure -> fail-closed `QUOTA_COORDINATOR_UNAVAILABLE`.
- NOTE: these are fixed UTC windows, **not guaranteed rolling windows**.
  Header semantics and real provider plan limits require verification
  before any actual use.
- NOTE: independent applications outside this bridge sharing a key
  are not counted, and are therefore not certified safe in aggregate.
- NOTE: PostgreSQL counters require retention/cleanup planning for long
  production operation; this research PR is never activated.

### Bounded request policy

- Async queue admission max `4 * max_concurrency` with 0.1s admission
  timeout; local dispatch concurrency max `max_concurrency`, max 5s wait.
- Shared concurrency validated by the PostgreSQL atomic lease.
- Identical in-flight calls share one task and one request budget; cancelled
  waiter cannot cancel other waiters' work. In-flight operation remains
  bounded by the finite attempt and timeout policy.
- Endpoint TTL cache max 128 entries, 2 MiB total, 256 KiB per item;
  never caches `/status`.
- Retry is GET-only (all catalog endpoints GET), at most 3 attempts;
  jittered backoff max 2s. 401/403/429 are not retried; 429 freezes
  minute quota. Per-attempt budget debit.
- Per-worker circuit breaker defaults to 2 failed operations / 30s
  cooldown, one half-open probe. Shared quota remains global.
- Transport timeout settings validated 0.5–30 seconds.
- Real-time provider response frequency and latency remain UNKNOWN.

## D07/D08 — acceptance matrix

| Scenario | Verification path | Status |
|---|---|---|
| Browser/OAuth registry unchanged | PR diff + pre-existing suite | CI PENDING |
| Adapter off, key absent | PR-01 existing baseline tests | CI PENDING |
| Bad host/URL/params | New mock unit tests | CI PENDING |
| Provider 301/401/403/429/500/502 | HTTPX mock tests | CI PENDING |
| Malformed body, pagination and oversize | HTTPX mock tests | CI PENDING |
| Cache null/zero fidelity and expiry | Unit tests | CI PENDING |
| Dedup of 8 concurrent callers | Unit test | CI PENDING |
| Retry 503/502 bounded by 3 debits | Unit test | CI PENDING |
| 429 reconciles shared budget | Mock and PostgreSQL tests | CI PENDING |
| Breaker open / half-open | Unit tests | CI PENDING |
| Caller cancellation | Unit test | CI PENDING |
| PostgreSQL shared budget across workers | Dedicated CI PostgreSQL | CI PENDING |
| PostgreSQL crash lease expiry | Dedicated CI PostgreSQL | CI PENDING |
| Cross-key quota isolation | Hash/unique key tests | CI PENDING |
| Real provider header/latency/coverage | Needs owner key + approval | BLOCKED |
| Official endpoint parameter reconciliation | 403 / separate audit | UNKNOWN |
| Live ChatGPT discovery for new endpoints | Endpoint tools not wired | NOT IMPLEMENTED |
| MatchPilot provider replacement | Empirical mapping later | NOT CERTIFIED |

A test marked SKIP remains SKIP. CodeRabbit Free summaries are not
equivalent to comprehensive independent line-by-line APPROVED reviews;
any waiver must be explicitly recorded by the owner.

## D09 — MCP compatibility

No new MCP tool in PR-02. `apifootball_health` remains the offline
diagnostic from PR-01 and remains disabled by default. OAuth/session
interfaces and browser registry are unchanged. Therefore PR-02 makes
no claim of live ChatGPT API-FOOTBALL tool readiness.

## D10/D11/D12 — MatchPilot and owner release gates

- MatchPilot is READ ONLY. FutPythonTrader is owner-reported
  unavailable for future collection; its 325 historical raw fields and
  archived PIT snapshots are not modified.
- TotalCorner remains SUSPENDED; no provider subscription or renewal.
- Plan for the next authorized phase: PR-03 source-verified typed endpoint
  contracts and MCP schemas, then PR-04 field-level mapping/coverage.
- First-month spending authorized €0, real API calls 0. Default limits
  8/minute and 80/day are design values, **not proof of provider quota**.
- D12 status: `PR02_MOCK_RESEARCH_LAYER` pending CI/review,
  `MCP_INTEGRATION_READY=NO`,
  `REAL_API_TEST_NOT_AUTHORIZED=YES`,
  `MATCHPILOT_REPLACEMENT_NOT_YET_CERTIFIED=YES`.
- No merge/deploy of PR-02 without explicit new owner approval.
  Render auto-deploy on main makes merge a deployment action.

## Code map

- `bridge/api_football/quota.py`: atomic shared quota and lease recovery.
- `bridge/api_football/transport.py`: injected HTTPX, pinned host,
  validation, bounded body.
- `bridge/api_football/resilience.py`: queue, single-flight,
  breaker, retries and orchestration.
- `bridge/api_football/{cache,circuit,errors}.py`: supporting primitives.
- `tests/test_apifootball_resilience.py`: unit and adversarial mock cases.
- `tests/integration/test_apifootball_quota.py`: PostgreSQL multi-worker
  budget and crash recovery.

## Deliberate non-goals

No endpoint-specific official schema claims, no live HTTP calls, no
secret management changes, no MatchPilot updates, no OAuth changes,
no migrations of hosting or database, no automated deployments.
