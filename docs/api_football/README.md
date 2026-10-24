# API-FOOTBALL v3 — MCP module and contract registry

Operational issue: #101. Inventory source: MatchPilot #248 (archive). FPT replacement audit: MatchPilot #249.

## Status (2026-10-24)

| Verdict | State |
| --- | --- |
| `GITHUB_READY` | pending CI and review |
| `RENDER_DEPLOY_READY` | not authorized |
| `MCP_DISCOVERY_VERIFIED` | in-process and HTTP test client only; not on Render |
| `CHATGPT_PLUGIN_UPDATED` | **no**, plugin unchanged |
| `BROWSER_REGRESSION_PASS` | in tests: all 34 `browser_*` schemas identical to `main` @ `2d20846` |
| `APIFOOTBALL_REAL_VERIFIED` | **no**, `REAL_API_TEST_NOT_AUTHORIZED` |
| `MATCHPILOT_REPLACEMENT_NOT_YET_CERTIFIED` | yes |

The official documentation (`https://www.api-football.com/documentation_v3`) holds the full contracts. It could
not be acquired from the development environment: Cloudflare returned HTTP 403 (see `sources.json`), and no bypass
was attempted. As a result:

- 39 endpoints are catalogued (MatchPilot #248 baseline).
- 6 endpoints have **owner-verified parameter names** and are exposed as tools: `/fixtures`, `/fixtures/statistics`,
  `/predictions`, `/odds`, `/teams/statistics`, `/odds/live`. Their types, required flags, allowed values and
  response schemas are still `PENDING_OFFICIAL_VERIFICATION`.
- 33 endpoints are `PENDING_OFFICIAL_VERIFICATION`. They have no parameters and no tool: nothing was reconstructed
  from memory.

## Files

| File | Role |
| --- | --- |
| `endpoints.json` | canonical endpoint registry: stable id `AF-EP-NNN`, method, path, category, statuses, tool, client policy |
| `parameters.json` | per-endpoint parameters: official attributes (pending) and provisional client validation |
| `response_schemas.json` | response schemas (all pending) and the client's envelope assumption |
| `sources.json` | provenance, acquisition attempts, client assumptions pending verification |
| `coverage_matrix.json` | generated: endpoint → parameter → source → MCP schema → test → status |
| `reports/coverage_report.md` | generated dashboard with explicit denominators |
| `snapshots/` | normalized official contract snapshots (none yet) |
| `changelog.md` | registry history |

Tools, validators and coverage are derived from these files (`bridge/api_football/registry.py`). Nothing at runtime
depends on the documentation website.

## Architecture and isolation

`bridge/server.py` calls `_register_optional_api_football(mcp)` inside `try/except`. An import error, a corrupt
registry or a registration failure only logs `API-FOOTBALL tools unavailable` and leaves browser tools, OAuth,
sessions, leases, reaper and `/healthz` untouched. Tests cover each of these cases.

- No network I/O at import or startup. `apifootball_health` and `apifootball_quota_report` never call the provider.
- With `APIFOOTBALL_ENABLED` off (the default), tools stay listed (so the ChatGPT tool list is stable) and return
  `DISABLED`.
- Every failure becomes a structured result (`bridge/api_football/errors.py`). Tool bodies catch everything, so
  nothing is raised into MCP.
- Each call has a hard deadline (queue + attempts + bounded backoff), retries are bounded, and redirects are never
  followed.
- Only `https://v3.football.api-sports.io` is allowlisted. Tools build only `GET {base}{registry path}` with validated
  query parameters, so they cannot act as an HTTP proxy.

### Request pipeline

enabled/config/key → validation → cache → in-flight dedup → concurrency slot → per attempt: provider quota reserve,
provider minute block, local daily budget, local per-minute limiter, circuit breaker → HTTP.

## Configuration (Render secret store only)

| Variable | Default | Range |
| --- | --- | --- |
| `APIFOOTBALL_ENABLED` | `false` | bool |
| `APIFOOTBALL_API_KEY` | empty | set by the owner on Render only |
| `APIFOOTBALL_BASE_URL` | `https://v3.football.api-sports.io` | allowlist only |
| `APIFOOTBALL_TIMEOUT_SECONDS` | 15 | 1–60 |
| `APIFOOTBALL_MAX_RPM` | 8 | 1–1200 |
| `APIFOOTBALL_MAX_DAILY_REQUESTS` | 75 | 1–1 500 000 |
| `APIFOOTBALL_MAX_CONCURRENCY` | 2 | 1–16 |
| `APIFOOTBALL_CACHE_ENABLED` | `true` | bool |
| `APIFOOTBALL_CIRCUIT_BREAKER_ENABLED` | `true` | bool |
| `APIFOOTBALL_QUOTA_RESERVE_PERCENT` | 10 | 0–90 |
| `APIFOOTBALL_MAX_RETRIES` | 1 | 0–3 |
| `APIFOOTBALL_QUEUE_TIMEOUT_SECONDS` | 10 | 0–60 |
| `APIFOOTBALL_MAX_RESPONSE_BYTES` | 8 MiB | 1 KiB–32 MiB |
| `APIFOOTBALL_MAX_OUTPUT_BYTES` | 2 000 000 | 1 KiB–16 MiB |

Defaults are close to the provider's free tier, with a margin, so an unknown plan cannot exhaust the key or get the
IP blocked. After the purchase, raise `MAX_RPM` and `MAX_DAILY_REQUESTS` to at most about 80% of the plan's limits.
An invalid value is reported as `CONFIG_INVALID` and the module fails closed.

## Security

- The key is sent only as the `x-apisports-key` header (assumption `ASM-AUTH-HEADER`). It is never put in a URL,
  log, result or error. Exception texts are reduced to their type. As defence in depth, a key echoed by the provider
  is replaced with `[REDACTED]`.
- Client validation is a provisional policy. It requires typed ids, strict dates, bounded lengths, and a character
  set with no `&`, `=`, `?`, `#`, `%` or `/` in free text. An empty call is refused to protect quota.
- Provider responses are data. They never change configuration, the registry or tool registration.

## Rate limiting and quota

- Local counters are **per process** (`limits_scope: PER_PROCESS` in health). The documented start command
  (`uvicorn bridge.server:app`) runs one process. If Render runs more than one worker or instance, local caps
  multiply. The provider's quota headers (global per key) are then the backstop: below the reserve the module stops
  (`QUOTA_RESERVE_REACHED`), and a provider daily-limit error stops calls until the next UTC day.
- A shared Neon-backed coordinator is proposed as a separate, owner-approved PR, needed only if Render shows more
  than one process.
- Retries cover only timeouts, network errors, 429 and 5xx, at most `APIFOOTBALL_MAX_RETRIES` times, with backoff
  ≤ 10 s. A 401/403 opens the breaker for 5 minutes, so a bad key cannot hammer the provider.

## Response contract

Every tool returns these fields:

- **Provenance:** `provider`, `api_version`, `endpoint`, `tool`, `registry_id`, `parameters` (normalized wire
  parameters), `status`, `ok`, `request_started_at_utc`, `acquired_at_utc`, `provider_timestamp_utc` (always `null`:
  the provider's data time is not known), `provider_date_header`, `http_status`, `attempts`, `latency_ms`.
- **Cache and quota:** `cache` (`MISS`/`HIT`/`DEDUPLICATED`/`BYPASS`, age, TTL), `quota` (provider headers; missing
  values stay `null`).
- **Payload:** `results`, `paging`, `provider_errors`, `raw` (the provider envelope, unmodified), `raw_sha256`,
  `bytes_utf8`.
- **Classification:** `data_class` (`MARKET_ODDS` is market data, not model probability; `PROVIDER_PREDICTION` has
  unverified methodology), `contract_status`, `parameter_contract`, `client_validation`,
  `pit_status: NOT_PIT_CERTIFIED`.
- **Diagnostics:** `warnings` (empty response, pagination anomalies, more pages), `error`.

`null`, `0`, `""` and absent keys are preserved exactly as the provider sent them.

## Contract maintenance

```bash
python scripts/check_api_football_contracts.py check            # registry, tools, coverage drift, snapshot hashes
python scripts/check_api_football_contracts.py report --write   # regenerate coverage outputs after a registry PR
python scripts/check_api_football_contracts.py ingest --source saved.html --source-url URL --method "how acquired"
```

`ingest` accepts a locally saved OpenAPI JSON file or a Redoc HTML page. It refuses challenge pages and pages without
a machine-readable contract (`DOCUMENTATION_UNAVAILABLE`, exit 3). It stores only the normalized contract (SHA-256,
no raw page) and writes a semantic diff. Each change is classified `ADDITIVE`, `BREAKING`, `DOCUMENTATION_ONLY` or
`UNKNOWN`; exit 4 means at least one is blocking. It **never edits the registry**: an owner-reviewed PR applies
changes, and a removal needs a documented reason.

### Unblocking official acquisition (owner)

From ChatGPT, using the existing Football Consensus Browser plugin:

1. Run `browser_create`.
2. Run `browser_open_url` on `https://www.api-football.com/documentation_v3`.
3. Run `browser_get_network_responses` to see whether the page loads a separate spec URL.
4. Run `browser_stop`.

Record the real outcome; no custom challenge bypass. If a page or spec loads, save it locally and run `ingest`,
then open a registry PR.

## Render update procedure (after owner authorization)

1. Merge the PR (owner).
2. Render auto-deploys or the owner triggers a deploy. The start command is unchanged.
3. Check `/healthz` (unchanged contract) and `browser_health`.
4. In ChatGPT, call `apifootball_health`: `DISABLED` is expected.
5. Smoke-test `browser_*` tools.
6. The owner sets `APIFOOTBALL_API_KEY` in Render secrets. With authorization, set `APIFOOTBALL_ENABLED=true`
   (this restarts the service).
7. Rollback: set `APIFOOTBALL_ENABLED=false`. No plugin change is needed.

## ChatGPT plugin update procedure (after deploy and owner authorization)

- Keep the plugin `plugin_asdk_app_6ac3e16a331081919657c1fdc0a66d36` (initial release
  `PluginRelease_0af75995cb308191a8602e55db545a63`, v1.0.0). Do not create a second plugin.
- The MCP URL and OAuth (`browser:use offline_access`) are unchanged; the new tools use the same scope.
- Steps:
  1. Refresh the tool list.
  2. Confirm all 34 `browser_*` tools are present and unchanged.
  3. Confirm the 9 `apifootball_*` tools are present.
  4. Publish a new release of the same plugin.
  5. Re-run the browser smoke tests from ChatGPT.
- `CHATGPT_PLUGIN_UPDATED` only after the release is published and verified.

## First-month real verification plan (not authorized yet)

| Step | Calls | Stop condition |
| --- | --- | --- |
| Day 1: headers and quota (`apifootball_fixtures` with one `date`) | ≤ 3 | any `AUTH_FAILED`, any key exposure |
| Week 1: 6 implemented endpoints on 10 fixtures (5 major and 5 minor competitions, from day-1 results) | ≤ 120 | `QUOTA_RESERVE_REACHED`, 429, `CIRCUIT_OPEN` |
| Week 2: repeatability (same fixtures, fresh session), latency, `half` statistics, odds pagination | ≤ 150 | drift without explanation |
| Weeks 3–4: live (`live=all`, `/odds/live`) on ≤ 5 matches, freshness measurement | ≤ 300 | provider minute block |

Results go to issue #101 as `REAL_VERIFIED` per endpoint and parameter, with response hashes. A mock test never
counts as a real test.
