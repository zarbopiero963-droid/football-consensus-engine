# FCB API-FOOTBALL PR-07 — offline certification matrix

Date: 2026-10-25

This certification is mock/offline only. It does not use an API key, contact
API-FOOTBALL, enable the live gate, or claim provider coverage.

## Results

| Area | Result | Evidence |
|---|---|---|
| Gate OFF / key-only bypass | PASS | tests/test_apifootball_wiring.py |
| Pinned HTTPS origin / redirect rejection | PASS | transport tests |
| 401/403/429/500/502/503/504 | PASS | resilience matrix |
| Timeout/network failure | PASS | resilience matrix |
| Malformed/incomplete JSON | PASS | injected HTTPX transport tests |
| Oversized response | PASS | injected HTTPX transport tests |
| Retry boundedness / circuit breaker | PASS | existing resilience tests + matrix |
| Shared quota failure-closed | PASS | existing PostgreSQL/failure tests |
| SSRF/path/parameter guard | PASS | catalog/transport tests |
| Browser/OAuth isolation | PASS | deployed browser_health; Browser Use create/open/stop |
| Real API-FOOTBALL calls | NOT AUTHORIZED | zero calls and zero key use |
| FPT replacement | NOT CERTIFIED | authentic 325-field export unavailable |

SKIP is never counted as PASS. Any live-provider test remains
BLOCKED/NOT AUTHORIZED until a later owner authorization.

## Required runtime invariant

APIFOOTBALL_ENABLED=true is still insufficient. A real request requires the
server-side APIFOOTBALL_LIVE_AUTHORIZED=true, a secret-store API key, valid
configuration, and a working shared quota coordinator. The deployed service
has not been configured with either live authorization or a provider key.
