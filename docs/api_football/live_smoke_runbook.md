# API-FOOTBALL live smoke: controlled execution

This is **not** full endpoint or live-odds certification. It tests only four read-only routes:
`/status`, `/countries`, `/leagues/seasons`, `/odds/bookmakers`.

## Preconditions
- Execute **inside an approved server-side environment** where the real `APIFOOTBALL_API_KEY` is already injected as a secret. Do not paste the key into terminals, CI variables, PRs or logs.
- `APIFOOTBALL_ENABLED=true` and `APIFOOTBALL_LIVE_AUTHORIZED=true` must be explicitly authorized and already configured.
- `APIFOOTBALL_MAX_DAILY_REQUESTS >= 4`. The test makes at most four GET requests, serially, **with no retries**.
- Do not run against an externally controlled host or use test mocks as live certification.
- Verify remaining provider quota first (the runner stops if a response reports zero remaining); treat absent headers as **unknown**, not as guaranteed quota.
- The test does not alter server config, database, state or deployment and does not invoke any order-placement functionality.

## Invocation

From the repository root, in a secure server-side shell with existing secret injection:

```sh
python scripts/api_football_live_smoke.py --authorize-live-test --output /tmp/api_football_live_smoke.json
```

The report contains only route, HTTP status, limited numerical quota metadata, numerical result count, payload shape, timestamps and outcomes, never response bodies or credentials. Store it as controlled evidence only after reviewing output. Exit 0 means all four checks passed; exit 2 means partial/failed; other exit codes indicate preflight or runtime errors.

## Scope and evidence classification

A `PASS` here proves one successful, authenticated response for a selected endpoint **at this time**, not complete schema, league/plan, fixture, all-parameter, odds-live, or MCP end-to-end validation. Calls to these direct provider endpoints do not certify that ChatGPT can discover or invoke the `apifootball_*` MCP tools. The remaining endpoint-by-endpoint matrix, full official contract reconciliation, adversarial integration, and independent review must be completed separately.

**Do not enable this test in default CI or production startup.** The runner does not trigger itself. Run only through a secure approved execution mechanism and within configured quotas.
