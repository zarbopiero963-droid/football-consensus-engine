# API-FOOTBALL Football API v3 — bridge integration

**Scope:** Football only. Owner mandate `FCB-APIFOOTBALL-INTEGRATION-CERT-01`.
**PR-01:** Optional, mock-only foundation. **Do not claim real API integration.**

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
| `APIFOOTBALL_MAX_RPM` | 8 | PR-02 planning value, **not yet enforced** |
| `APIFOOTBALL_MAX_DAILY_REQUESTS` | 80 | PR-02 planning value, **not yet enforced** |
| `APIFOOTBALL_MAX_CONCURRENCY` | 2 | PR-02 planning value, **not yet enforced** |
| `APIFOOTBALL_CACHE_ENABLED` | true | PR-02 planning value, not yet implemented |
| `APIFOOTBALL_CIRCUIT_BREAKER_ENABLED` | true | PR-02 planning value, not yet implemented |

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
2. PR-02 — real transport only after owner authorization: bounded
   client, shared quotas, cache, deduplication, timeout, retry and breaker.
   Verify per-process vs multi-instance rate enforcement.
3. PR-03 — source-verified typed endpoint tools and response envelopes;
   never invent parameters. No paid provider calls until authorized.
4. PR-04 — coverage diagnostics and MatchPilot field-level comparison
   for all relevant FPT fields, independent empirical validation.

Risk: Render `main` has auto-deploy enabled. Merging a PR is therefore
a deployment action and requires explicit separate owner approval.
