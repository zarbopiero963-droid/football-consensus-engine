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
      -> shared PostgreSQL Quota(acquire) + rolling 60-second history
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
- Atomic UTC minute/day buckets **plus exact shared rolling 60-second
  per-key attempt history**, preventing provider bursts at minute turnover.
  If any cap is exhausted, neither counter advances and no HTTP request
  is dispatched. Stale attempt rows are pruned during subsequent
  reservations for the same key.
- Every actual attempt consumes one reservation: errors, timeouts and
  cancellations do not refund prior unknown work.
- Concurrent-request leases expire after 180s for crash recovery.
- Provider-reported zero remaining can freeze a window globally.
- Readable quota snapshots exclude the key.
- DB failure -> fail-closed `QUOTA_COORDINATOR_UNAVAILABLE`.
- NOTE: **daily** limits remain UTC calendar buckets; **minute** admission
  requires both UTC bucket and sliding 60-second limit.
  Header semantics and real provider limits require verification
  before any actual use. Expired-attempt retention for inactive keys
  needs later operational cleanup.
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
| Browser/OAuth registry unchanged | PR diff + pre-existing suite | MOCK/CI PASS |
| Adapter off, key absent | PR-01 existing baseline tests | MOCK/CI PASS |
| Bad host/URL/params | New mock unit tests | MOCK/CI PASS |
| Provider 301/401/403/429/500/502 | HTTPX mock tests | MOCK/CI PASS |
| Malformed body, pagination and oversize | HTTPX mock tests | MOCK/CI PASS |
| Cache null/zero fidelity and expiry | Unit tests | MOCK/CI PASS |
| Dedup of 8 concurrent callers | Unit test | MOCK/CI PASS |
| Retry 503/502 bounded by 3 debits | Unit test | MOCK/CI PASS |
| 429 reconciles shared budget | Mock and PostgreSQL tests | MOCK/CI PASS |
| Breaker open / half-open | Unit tests | MOCK/CI PASS |
| Caller cancellation | Unit test | MOCK/CI PASS |
| PostgreSQL shared budget across workers | Dedicated CI PostgreSQL | MOCK/CI PASS |
| PostgreSQL crash lease expiry | Dedicated CI PostgreSQL | MOCK/CI PASS |
| Rolling 60-second cap at UTC minute boundary | Dedicated CI PostgreSQL | CI PENDING |
| Cross-key quota isolation | Hash/unique key tests | MOCK/CI PASS |
| Real provider header/latency/coverage | Needs owner key + approval | BLOCKED |
| Official endpoint parameter reconciliation | 403 / separate audit | UNKNOWN |
| Live ChatGPT discovery for new endpoints | Endpoint tools not wired | NOT IMPLEMENTED |
| MatchPilot provider replacement | Empirical mapping later | NOT CERTIFIED |

A test marked SKIP remains SKIP. CodeRabbit Free summaries are not
equivalent to comprehensive independent line-by-line APPROVED reviews;
any waiver must be explicitly recorded by the owner.

## D08.1 — Actual CI outcomes and reproduced concurrency defect

First run [40134730425](https://github.com/zarbopiero963-droid/football-consensus-engine/actions/runs/40134730425):
- General Python suite: **850 passed, 99 skipped**.
- PostgreSQL integration: **1 failed, 97 passed**. The 2-worker
  simultaneous first-use quota test reproduced a **real DDL/advisory
  lock-order deadlock**. This is a FAILED finding, not SKIP or PASS.

Fix applied in this same PR: `PostgresQuota._ensure_schema()` uses a
dedicated globally serialized transactional schema lock, completes
all DDL and commits **before** acquiring the per-key quota lock.
A process-local thread lock also prevents duplicate same-instance
bootstrap. No schema DDL runs while the per-key advisory lock is held.

Re-run [40134908029](https://github.com/zarbopiero963-droid/football-consensus-engine/actions/runs/40134908029):
- General Python suite: **850 passed, 99 skipped** — SUCCESS.
- Dedicated PostgreSQL suite: **98 passed** — SUCCESS.
- The previously failing multi-worker budget test now passes.

Follow-up hardening on this same PR: added a per-key PostgreSQL rolling
60-second attempt history to reject requests that would otherwise fit
into a new UTC minute bucket but still exceed a rolling provider rate
limit. Added integration tests for recent-history blocking and stale
history expiry. **The amended HEAD requires another successful CI run**;
prior PASS evidence is not automatically transferable.

Any subsequent documentation-only commit will require latest-head CI
green again. A CodeRabbit summary alone is not an independent full
code review; review remains a separate release gate.


## D08.2 — Additional manual security review (same PR)

A manual author-side code inspection found two **real gaps** despite green
mock CI:

1. `PostgresQuota.snapshot()` was reporting only the UTC-calendar-minute
   remaining balance. At a minute boundary, it could report positive
   allowance while the strict rolling 60-second ledger rejected requests.
   Fixed by computing the **minimum** of calendar and rolling capacity.
   Added DB regressions for effective remaining-capacity reporting.
2. `ResilientFootballClient.get()` spawned an unbounded number of unique
   `asyncio.Task` instances **before** entering its bounded downstream
   semaphore. The queue protected work execution, not task allocation.
   Fixed by rejecting additional distinct inflight keys while the
   `4 * max_concurrency` queue is full. Duplicate callers may still
   await the already-running identical request without creating a new one.
   Added a concurrent overload/capacity test.

This is an **author-side review**, not an independent APPROVED review, and
cannot waive the mandatory independent reviewer gate. Results are PENDING
fresh CI on the amended head.

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
- D12 status: `PR02_MOCK_RESEARCH_LAYER` CI PASS / review pending,
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
