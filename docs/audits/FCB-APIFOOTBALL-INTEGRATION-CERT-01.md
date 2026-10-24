# FCB-APIFOOTBALL-INTEGRATION-CERT-01 — Phase 0 / PR-01

Date: 2026-10-24 · Owner mandate · **football only** · **€0** · **NO MERGE / NO DEPLOY**

## Evidence levels

- `REPO_VERIFIED`: inspected actual main GitHub source.
- `PROD_OBSERVED`: remote bridge/Render read-only inspection.
- `DOCUMENTARY_ONLY`: provider/MatchPilot docs, **not** real API response.
- `MOCK_VERIFIED`: deterministic fixtures in tests, **not** provider coverage.
- `UNKNOWN`: unavailable proof; never promoted to PASS.

## D01 — Baseline repository / MCP

- Repository: `zarbopiero963-droid/football-consensus-engine`.
- Baseline main SHA: `9a21c56bce40bf91da0a25df534274a7b76ce980` (#104 merged).
- Phase-0 GitHub PR scan: **zero open PRs**; unique PR-01 branch is isolated.
- Governance read: complete `AGENTS.md`, `CLAUDE.md`, `README.md`.
  Files semantically aligned; repository requires no leakage, append-only
  ledger, missing≠zero, model≠market, UTC canon, no production source promotion.
- Bridge: `bridge/server.py`, `bridge/browser_use.py`, OAuth modules,
  existing `bridge/security.py`, session leases.
- Stack: Python 3.11 CI; `mcp[cli]>=2,<3`, Starlette, HTTPX, Playwright,
  CloakBrowser, `psycopg`; no new runtime dependency in PR-01.
- MCP: `MCPServer` with `streamable_http_app(json_response=True,
  stateless_http=True)`; mounted at `/mcp` with OAuth bearer middleware,
  OAuth metadata, registration, authorize/token endpoints; session manager
  and reaper share the Starlette lifespan.
- **Exactly 34 `@mcp.tool()` browser tools** on baseline main:

```text
browser_create, browser_create_cloak, browser_open_url,
browser_get_html, browser_get_dom, browser_screenshot, browser_click,
browser_fill, browser_type, browser_press, browser_select_option,
browser_set_checked, browser_hover, browser_focus, browser_scroll,
browser_wait_for_selector, browser_wait, browser_back, browser_forward,
browser_reload, browser_get_elements, browser_list_pages,
browser_new_page, browser_switch_page, browser_close_page,
browser_get_network_responses, browser_stop, browser_list_sessions,
browser_session_status, browser_reap, browser_stop_all_idle,
browser_stop_all, browser_health, browser_cleanup_report
```

**All signatures and response contracts stay defined by original functions
in `bridge/server.py` and `bridge/browser_use.py`.** No browser function
is changed by PR-01. Regression test asserts original count and key names;
GitHub PR diff proves no body changes.

Runtime baseline read-only:
- Render web service `football-consensus-browser-bridge`,
  region Frankfurt, **Free**, `numInstances=1`, branch main,
  autoDeploy **YES**, URL
  `https://football-consensus-browser-bridge.onrender.com/mcp`.
- Running commit `9a21c56b...` confirmed through Render deploy metadata.
- `browser_health` returned `ok=true`, `database_ok=true`,
  Browser Use configured, Cloak enabled/runtime available, zero local sessions.
- Browser state and OAuth remain independent of optional adapter.
- Last known **pre-PR-01** GitHub `pytest` log: **790 passed, 93 skipped**;
  `decision-postgres` job SUCCESS. This is previous PR CI, **not** PR-01 CI.
- Render/Neon dependencies are project-specific; this mandate neither
  migrates nor modifies hosting, database, deployments or secrets.

## D02 — Architecture / failure domains

```text
ChatGPT + Claude.ai --OAuth--> Starlette /mcp --> existing 34 browser_* tools
                                          | 
                                          +-- [APIFOOTBALL_ENABLED=true only]
                                               bridge.api_football
                                               optional apifootball_health
                                               (offline / mock only in PR-01)
```

Default: `APIFOOTBALL_ENABLED=false`. No optional package import, no tool
addition, no client initialization and no HTTP from this module. The flag
must explicitly be true/1; malformed enabled config or missing module is
logged using a fixed non-sensitive error code and does **not** prevent MCP
startup. With enabled config, a single offline health tool can report
`KEY_NOT_CONFIGURED` or `MOCK_ONLY`; it cannot contact provider.

PR-02 will introduce an independent bounded HTTP client and protections
before any paid/trial calls. If multiple worker instances are introduced,
**in-memory** quota accounting is insufficient: requires an owner-approved
shared coordinator (for example an already available PostgreSQL table with
atomic reservations and tests, not a new paid service). No shared quota
implementation is claimed here.

## D03 — API-FOOTBALL documentary catalog

Source MatchPilot [#248](https://github.com/zarbopiero963-droid/MatchPilot/issues/248);
official document URL:
https://www.api-football.com/documentation_v3

38 football GET paths + `/status` are indexed in
`bridge/api_football/catalog.py` with
`PENDING_OFFICIAL_RECONCILIATION`. A fresh browser check of official
documentation returned Cloudflare HTTP 403; no CAPTCHA bypass attempted.
Parameters, paging, response fields and bookmaker IDs **not certified**
from this check. There is **no** generic user-supplied URL tool. No endpoint
tool (apart from offline health) is activated in PR-01.

Provider claims (price, coverage, limits and predictions) are not
contractual proof. Distinguish:
DOCUMENTED / REAL_VERIFIED / MOCK_ONLY / UNKNOWN.
Do not infer xG/xGoT/attacks or arbitrary HT/2T stats.

## D04 — Security

- Secret `APIFOOTBALL_API_KEY` **never committed or printed**.
  `Settings.api_key` excluded from repr and comparisons.
- Allowed base URL exactly
  `https://v3.football.api-sports.io`; reject all other origins, paths,
  query strings, embedded credentials or ports.
- Mock client accepts only exact catalog paths, validates mock params,
  deep-copies payloads and opens no sockets.
- Real client in PR-01 **rejects** every call, even with a dummy or future key.
- Existing OAuth clients, scopes, token persistence, HTTP middleware and
  browser network guard remain unchanged.
- Do not infer `0` from missing/null or modify data contract semantics.
- No arbitrary HTTP request, MCP-triggered key entry, paid call or external
  registration. Authenticated MCP access remains obligatory.

## D05 — Quotas / request planning (NOT IMPLEMENTED YET)

Conservative opt-in planning defaults:
`APIFOOTBALL_MAX_RPM=8`,
`APIFOOTBALL_MAX_DAILY_REQUESTS=80`,
`APIFOOTBALL_MAX_CONCURRENCY=2`,
`APIFOOTBALL_TIMEOUT_SECONDS=8`.
These are **configuration only**; they are not a functioning limiter.
In PR-01 API call budget and actual usage both **zero**.

Before PR-02 / runtime authorization:
- Read provider `/status` headers and actual per-minute/day quota;
  design atomic `reservation/commit/reconcile` semantics.
- Shared per-key budget across processes and restarts; stop below a
  configurable safety margin. Account for all callers sharing provider key.
- Coalesce identical inflight queries, bounded per-endpoint TTL cache,
  finite retries with jitter only when safe, circuit breaker, cancellation,
  timeout, `429`/auth/5xx isolation, paging limits, memory/payload caps.
- Test across at least two workers before certifying global quota.

## D06 — Implementation per PR

PR-01 contains only:
`bridge/api_football/{__init__,config,catalog,client,tools,README}.py`
(where the README has a `.md` extension),
one guarded optional registration in `bridge/server.py`,
plus mocks/regression tests and this dossier.
**No collector, ledger, scheduler, matching, settlement, OAuth or strategy edits.**

## D07 — Non-regression matrix

- Default disabled: 34 original tools and no apifootball tool.
- Optional module missing or invalid enabled config: import/registry
  isolation; OAuth and browser remain available.
- Enabled: only `apifootball_health` / no provider requests.
- Explicit URL/host/parameter reject; secret redaction; no outbound mock.
- No change to browser implementation; existing browser tests must remain
  PASS in GitHub CI.
- Real API, retries, quotas and data coverage **BLOCKED / NOT IMPLEMENTED**,
  not hidden as PASS.

## D08 — MCP test evidence

Status until PR CI finishes: **PENDING**. No live provider tests authorized.
Previous unrelated PR baseline: 790 PASS, 93 SKIP (not attributable to PR-01).
Mock results will not be promoted to `REAL_VERIFIED`.

## D09 — ChatGPT / Claude.ai

Existing OAuth/registration/discovery retained. OAuth has been validated
on ChatGPT and Claude web in previous owner-authorized testing. PR-01
disables the new module by default; no extra tools in production without
a future deployment/activation. No connector settings are modified.

## D10 — MatchPilot field substitution (read-only baseline)

Sources:
[#12](https://github.com/zarbopiero963-droid/MatchPilot/issues/12),
[#20](https://github.com/zarbopiero963-droid/MatchPilot/issues/20),
[#29](https://github.com/zarbopiero963-droid/MatchPilot/issues/29),
[#34](https://github.com/zarbopiero963-droid/MatchPilot/issues/34),
[#40](https://github.com/zarbopiero963-droid/MatchPilot/issues/40),
[#97](https://github.com/zarbopiero963-droid/MatchPilot/issues/97),
[#102](https://github.com/zarbopiero963-droid/MatchPilot/issues/102),
[#248](https://github.com/zarbopiero963-droid/MatchPilot/issues/248),
[#249](https://github.com/zarbopiero963-droid/MatchPilot/issues/249).

Per owner #249, FPT is owner-reported unavailable for future operation,
not empirically independently confirmed. Historical FPT remains immutable.
The report mentions **325 FPT raw fields**, including 188 odds,
28 shots, 26 xG, 24 discipline, 18 defense, 18 possession, six corners
and additional fields. **No field-by-field 325-row audit completed in PR-01.**

| Critical capability | Candidate AF source | Status against MatchPilot |
|---|---|---|
| Fixture ID, teams, kickoff, FT score | /fixtures | UNKNOWN_EMPIRICAL; mapping needed |
| HT/2T stats, period semantics | /fixtures/statistics | UNKNOWN; half flag not validated |
| Shots, corners, possession, fouls, cards | /fixtures/statistics | UNKNOWN per field/league/season |
| Events, red/yellow, goals, substitutions | /fixtures/events | UNKNOWN temporal coverage |
| Lineups, coaches, players, injuries | /fixtures/lineups, /injuries | UNKNOWN |
| Odds and movements / closing PIT | /odds, /odds/live | PARTIAL_CANDIDATE; historical PIT not proven |
| xG, xGoT, big chances | no confirmed equivalent | UNKNOWN / GAP CANDIDATE |
| Attacks, dangerous attacks | no confirmed equivalent | UNKNOWN / TC candidate; TC SUSPENDED |
| Asian Corner lines | /odds catalog unverified | UNKNOWN / TC candidate; TC SUSPENDED |
| Provider predictions | /predictions | UNKNOWN_MODEL_CONTRACT |
| Historical PIT / preserved FPT archive | none retroactively | UNAVAILABLE to reconstruct past observations |

No row is certified `DIRECT_REPLACEMENT` or
`REPLACEMENT_WITH_TRANSFORMATION` absent real data.

Scenario statuses: AF-only=UNKNOWN; AF+TC=BLOCKED_TC_SUSPENDED;
AF+legacy FPT=HISTORICAL_ONLY; AF+TC+legacy=BLOCKED_TC_SUSPENDED;
no replacement=SAFE_CURRENT_BASELINE. Never modify MatchPilot, archive #40,
FutPythonTrader, trading risk, strategies or TotalCorner status here.

## D11 — First-month plan (proposed; **€0 spending**, zero requests now)

- Month 0: PR-01 offline unit/CI only, no key, zero calls.
- After authorized PR-02/03 and owner-authorized free API account:
  estimate a strictly capped daily pilot **up to 80 requests/day**
  (8 per minute local cap initially), 14-day max 1,120 requests *only if*
  free tier actually grants compatible quota; revalidate plan first.
- Choose a reproducible sample of major/minor leagues and match phases.
  Record HTTP, original body hash when licensing permits, timestamps,
  missing-field semantics, headers, per-field coverage/quality.
- Stop on quota/429, bad license, secret leak, unverified network cost,
  loss of browser availability or data-contract inconsistency.
- This plan is **not** authorization to sign up, spend or run real calls.

## D12 — Verdict (as of PR-01)

- `MCP_INTEGRATION_READY`: **NO** (endpoint tools / real transport not built).
- `MCP_INTEGRATION_BLOCKED`: **NO** for offline foundation; **YES**
  for claim of complete API integration.
- `REAL_API_TEST_NOT_AUTHORIZED`: **YES**.
- `REAL_API_TEST_READY`: **NO**.
- `MATCHPILOT_REPLACEMENT_NOT_YET_CERTIFIED`: **YES**.

Release gate: owner must explicitly authorize **merge and deploy**.
Render auto-deploys on main; opening PR does **not** deploy.
No next PR before PR-01 owner closure. No further provider or infra action.
