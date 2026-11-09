# B — Per-tool inventory of the MCP registry on main a8fb228

Auditor: independent, read-only. Date: 2026-11-09. Machine-readable inventory: `B_tools.csv` (122 rows, columns tool,component,provider_endpoint,impl,flag,nature,positive_test,negative_test,real_evidence,classification,notes).

## Method

1. **Source = a8fb228, not the working tree.** The working tree at /home/user/football-consensus-engine is checked out at `c892e7a` (branch `claude/api-football-pr-125-hardening-d9x0pz`, 22 files differ from main). I exported main with `git archive a8fb228 | tar -x -C /tmp/claude-0/audit_tools/src`, and every import, test run and line number below comes from that export. The repo was not modified.
2. **Real registry, offline.** I imported `bridge.server` with `/tmp/claude-0/venv/bin/python -I`, using production-like flags: `APIFOOTBALL_ENABLED=true`, `SXBET_ENABLED=true`, dummy non-secret keys, `APIFOOTBALL_LIVE_AUTHORIZED=false`, `BROWSER_TOOLS_ENABLED=false` and no `DATABASE_URL`. Then I called `await mcp.list_tools()`, which is the same catalogue `/healthz` counts. Script: `/tmp/claude-0/audit_tools/dump_tools.py`; dump: `/tmp/claude-0/audit_tools/tools.json` (names, descriptions, input schemas).
   - Result: **122 tools = browser 34 + apifootball 42 + sxbet 46 + other 0**. Both registration error types were `None`.
   - This matches the production `/healthz` from PHASE0 exactly (122 / 34 / 42 / 46).
   - Offline the SX Bet coordinator is `LOCAL_PROCESS` because there is no `DATABASE_URL`. Production reports `POSTGRES_SHARED`.
3. **Schema vs contract.**
   - I ran the offline E2E test `tests/test_mcp_offline_protocol_e2e.py` plus the related suites (full_catalog, typed_mcp, registry_observability, official_parameter_matrix, full_contract_audit, render_isolation): **108 passed**.
   - I also ran my own independent diff of `contracts.ENDPOINTS[*].fields` and `required` against each MCP `inputSchema` (`from`/`to` are exposed as `from_date`/`to_date`).
   - Result: **40/40 contracts and 142/142 params exposed, 0 mismatches**, required sets equal.
   - No tool exposes an `api_key`, CDP, private-key or signing parameter. The only `url` parameters are `browser_open_url` and `browser_new_page`, both behind the SSRF guard. The `*_key` parameters are pagination cursors (`next_key`, `pagination_key`), a keyboard key (`browser_press`) or a simulation `idempotency_key`.
4. **Full offline suite at a8fb228.** Run excluding `tests/integration`: **1578 tests, 0 failed, 0 errors, 7 skipped**.
   - 6 of the skips are xfails for the WP-002b browser residuals: WebRTC TCP and UDP, WebTransport, speculation rules, DNS rebinding local/remote.
   - The 7th is a PostgreSQL DCR test that needs a database.
   - `tests/integration` (PostgreSQL) was not run because no database is available locally.
5. **Positive/negative tests per tool are traced, not guessed.**
   - A pytest plugin (`/tmp/claude-0/audit_tools/trace_plugin2.py`) used `sys.setprofile` to record every call into each tool function (`bridge/*/tools.py`, `bridge/server.py`) and each `BrowserUseBridge` method behind a browser tool, together with the returned `status`. Results are in `/tmp/claude-0/audit_tools/trace.json`.
   - Statuses `INVALID_INPUT`, `KEY_NOT_CONFIGURED`, `LIVE_NOT_AUTHORIZED`, `PROVIDER_ERROR`, `PAYLOAD_TOO_LARGE`, raised exceptions, `*UNAVAILABLE`/`*UNCERTAIN`/`REFUSED` count as negative. Everything else counts as positive.
   - The 3 Chromium files ran too slowly under the profiler, so I mapped them statically (AST): `*_never_*`/`*block*`/`*internal*` tests count as negative.
   - Caveat: `apifootball_health` returning `LIVE_NOT_AUTHORIZED` is counted as negative by this rule even though it is a valid health answer.
6. **Real-provider evidence: only artefacts in the repo count.**
   - SX Bet: `evidence/sxbet/readonly_audit_run41320538088.json` (2026-11-01, GitHub Actions, in-process MCP, LOCAL_PROCESS coordinator, code d569e6e). `bridge/sxbet/tools.py` and `client.py` are unchanged from d569e6e to a8fb228; only `ratelimit.py` changed.
   - API-FOOTBALL: route inventory runs 40211464376 and 40205480029 (2026-10-25; `.b64` zip, decoded and read), plus capability runs 40218205457, 41240887865, 41242779991 and 41243344579 for `/odds` and `/odds/live`.
   - Browser: no artefact at all.

## Totals

### Per classification

| Classification | Count |
|---|---|
| PASS_REAL | **77** |
| DISABLED_BY_POLICY | **26** |
| PASS_OFFLINE | **14** |
| PASS_SIMULATION | **4** |
| IMPLEMENTED_UNVERIFIED | **1** |
| BLOCKED_EXTERNAL, FAIL, NOT_TESTED, NOT_IMPLEMENTED | 0 |
| **Total** | **122** |

### Per component

| Component | Tools | PASS_REAL | PASS_SIMULATION | PASS_OFFLINE | DISABLED_BY_POLICY | IMPLEMENTED_UNVERIFIED |
|---|---|---|---|---|---|---|
| browser | 34 | 0 | 0 | 7 | 26 | 1 (`browser_stop_all`) |
| apifootball | 42 | 40 | 0 | 2 (`health`, `quota_report`) | 0 | 0 |
| sxbet | 46 | 37 | 4 | 5 | 0 | 0 |

The 5 SX Bet PASS_OFFLINE rows:
- 2 local diagnostics: `sxbet_health`, `sxbet_risk_status`;
- 3 tools the real run marked NOT_TESTED: `public_user_orders`, `public_positions`, `proxy_funding_status`.

The 4 SX Bet PASS_SIMULATION rows: `simulate_order`, `hedge_simulation`, `close_position_preview`, `rules_evaluate`.

### How strong PASS_REAL is (read before quoting the 77)

**Every PASS_REAL is historical.** All real evidence predates the DB re-initialisation of 2026-11-07. PHASE0 shows no OAuth flow and no authenticated MCP call since then. **Production-verified on a8fb228 = 0/122.**

**API-FOOTBALL (40):**
- The evidence is route-level only. It came from raw httpx scripts, outside the MCP tool path and outside the PostgreSQL coordinator; WP-003 (#167) moved those scripts behind the coordinator later.
- Per-parameter verification is 0/142: `full_contract_audit` reports certified_routes=0 and verdict INCOMPLETE.
- The 36 parameters added in 218f9b9 have never been sent live.
- In route inventory 40211464376, `/odds` was `PASS_EMPTY_NOT_AVAILABILITY` and `/odds/live` was `BLOCKED_MISSING_REAL_PREREQUISITES`. Both have real prices only in the 2026-10-31 capability runs (10066 pre-match and 382–507 live values).

**SX Bet (37):**
- The evidence ran through MCP, but in-process in GitHub Actions, not Render, and with the LOCAL_PROCESS coordinator.
- 10 of the 37 are `EMPTY_VALID` only: no data rows were observed. The run has 13 EMPTY_VALID results in total; the other 3 are live/realtime variants of tools that passed elsewhere.
- `sxbet_order`, `order_by_client_id`, `trade` and `order_status` have only the negative 404 path evidenced.
- The 5 DERIVED tools are `DERIVED_FROM_REAL_API`.

## Defects and findings

**D1 (P2, CLAUDE.md §2.1): `browser_stop_all` returns the raw exception text.**
- Where: `bridge/browser_use.py:1439`, `failed.append({"session_id": ..., "error": str(exc)})`.
- Every other path uses fixed codes or `type(exc).__name__`; compare `stop_error` handling at :227–229 and `reap`.
- The tool is not gated by the kill switch (see D2), so it is reachable in production today. It is low impact while no sessions exist.
- Offline check: exceptions *raised* from tools are masked by the SDK as `UnexpectedToolError "Error executing tool X"`. A *returned* string is not masked.

**D2 (observation): 8 browser tools are not behind `BROWSER_TOOLS_ENABLED`.**
- The tools: `browser_stop`, `list_sessions`, `session_status`, `reap`, `stop_all_idle`, `stop_all`, `health`, `cleanup_report`.
- `_require_enabled()` gates only `create`, `create_cloak` and the 24 `@session_operation` methods.
- This looks intentional, so cleanup and diagnostics keep working, but `/healthz` showing `browser_tools_enabled=false` does not mean all 34 browser tools are inert.

**D3 (duplicate tool): `browser_stop_all_idle` is an exact alias of `browser_reap`.**
- `stop_all_idle()` just returns `await self.reap(idle_seconds)` (`browser_use.py:1414`).
- Two MCP names do one operation.

**D4 (duplicate route): `apifootball_coverage_report` is a second view of `GET /leagues` (`id`, `season`).**
- It overlaps `apifootball_leagues`. This is documented and asserted (40 contracts over 39 paths), but it inflates the tool count.

**D5 (test gaps, traced): 18 tools have NO offline tool-level test, neither positive nor negative.**
- Browser (11): `press`, `select_option`, `set_checked`, `hover`, `focus`, `scroll`, `wait_for_selector`, `wait`, `forward`, `close_page`, `stop_all`.
- SX Bet (7): `fixtures_active`, `live_scores`, `orderbook_market`, `orderbook_event`, `best_odds`, `open_orders`, `fills`.
  - Their only coverage is the pinned name list in `test_mcp_tool_registry_observability.py` and the 2026-11-01 real run.
  - The derived tools reach the same routes through `client.get` (`market_bundle`), not through these tool functions.
- Further: 25 tools have only negative traced tests and 19 only positive (CSV `notes`, "TEST GAP").
- API-FOOTBALL:
  - Each of the 18 "original" contract tools (countries, leagues, seasons, teams, predictions and so on) is invoked only on the not-authorized/key-missing path. Their positive path is covered only through the shared `execute()` and contract-level `validate()` tests.
  - The `areas/apifootball.md` claim "Offline / mock tested 42/42 (each tool runs against the mock with MOCK_VERIFIED)" is **not reproduced**: `test_mock_full_endpoint_catalog_preserves_zero_null_empty_absent` covers the 22 `NEW_CASES` only.

**D6 (documentary conflict, §0, already known as G3, not fixed on a8fb228).**
- `contracts.py` marks 40/40 as `DOCUMENTED_OFFICIAL`.
- `registry.py`, `catalog.py` and `sources.json` say PENDING.
- `apifootball_health` reports `PARTIALLY_RECONCILED_OFFICIAL`.

**D7 (stale text): the success path of `apifootball_*` tools still starts with a warning "Offline PR-03 tool: never claim real coverage..."**
- It is replaced only when the reply is `REAL_API_RESPONSE` (`tools.py:65-68,123-127`).
- The module docstring still says "Without an injected test-only mock client every endpoint returns an explicit non-network authorization gate", which is false when `APIFOOTBALL_LIVE_AUTHORIZED=true` (production).

**D8 (coordinator fallback, §2.4 note).**
- `SXBetSettings.from_env` silently picks `rate_coordinator="local"` when `DATABASE_URL` is empty (`bridge/sxbet/client.py:78`).
- Production has the database and reports `POSTGRES_SHARED`, so this is only a risk if the DSN is lost. It is observable via `/healthz.sxbet_rate_coordinator`.

**D9 (browser SSRF residuals).**
- `browser_open_url` and `browser_new_page` carry the 6 documented WP-002b residuals (xfail): WebRTC TCP/UDP, WebTransport, speculation rules, DNS rebinding.
- Mitigated only by the kill switch (DISABLED_BY_POLICY). They need an egress proxy before re-enabling.

## Scope, funds and signer checks

**Out-of-scope / legacy (§1):**
- No MCP tool is out of scope: all 122 belong to browser, API-FOOTBALL or SX Bet, and `other=0`.
- Out-of-scope legacy *modules* remain in `bridge/api_football/` but are **not registered**: `capabilities.py`, `indicators.py` (MatchPilot/TotalCorner), `gap_matrix.py`, `registry_import.py` (FPT).

**Can any tool write or move funds?** **No.**
- `SXBetReadClient` exposes only `get`; `_fetch` hard-codes `"GET"`.
- `test_no_write_capability_exists` checks the method list and bans tool names containing `place`, `cancel`, `approve`, `transfer`, `withdraw`, `deposit`, `sign` and similar.
- No tool argument can select `LIVE_TRADING`: the hedge `mode` is FULL/PARTIAL/REDUCE only, and `sxbet_health` reports `LIVE_TRADING: UNAVAILABLE`.
- `signer/` is not imported by `bridge/`; that is checked in a subprocess by `test_sxbet_render_isolation.py`.

**Sensitive account reads (no fund movement):**
- `sxbet_balance`, `ledger`, `positions`, `open_orders`, `trades`, `fills`, `privacy`, `proxy_wallet`, `proxy_deploy_pending`, `proxy_funding_pending`, `proxy_funding_status`;
- `public_positions` and `public_user_orders`, which take a third-party wallet address.

**Auth:** all 122 tools are served only under `/mcp`.
- `OAuthBearerMiddleware` (`bridge/server.py:347`) returns 401 without a valid token. PHASE0 confirms this live.
- Tools have no per-tool scope: one scope, `browser:use`, covers browser, API-FOOTBALL and SX Bet account reads.

## Files

- `/tmp/claude-0/-home-user-football-consensus-engine/a0651bc2-1244-54e7-b9ad-d78e2b3e8920/scratchpad/audit/B_tools.csv`
- Tools: `/tmp/claude-0/audit_tools/dump_tools.py`, `build_csv.py`, `trace_plugin2.py`, `errprobe.py`
- Data: `/tmp/claude-0/audit_tools/tools.json`, `trace.json`, `junit.xml`, and the decoded evidence in `/tmp/claude-0/audit_tools/ev/`
