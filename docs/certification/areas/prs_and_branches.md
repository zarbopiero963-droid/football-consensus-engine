# Phase A - PR / branch inventory and #130 reconciliation

Repo `zarbopiero963-droid/football-consensus-engine`, `origin/main` = `342fed6` (merge of #163). Source: GitHub REST `pulls?state=all` (2 pages) + `git branch -r --no-merged origin/main` (local clone is shallow, 281 commits on main; merge SHAs older than that are not resolvable locally but come from the API).

## 1. Pull requests

Total PRs: **137**; merged: **136**; closed unmerged: **1** (#124); open: **0**. Numbers absent from the PR list are issues (e.g. #130).

| # | title | merged | merged_at (UTC) | merge SHA | head branch |
|---|---|---|---|---|---|
| 163 | fix(ci): PG18 workflow takes the port from pinned config; fail-closed target policy | yes | 2026-11-02 17:07:55 | 342fed6468 | `claude/api-football-pr-125-hardening-d9x0pz` |
| 162 | fix(ci): PG18 workflow retries the PGDG key download and apt (fingerprint check unchanged) | yes | 2026-11-02 16:37:32 | 663d00503d | `claude/api-football-pr-125-hardening-d9x0pz` |
| 161 | fix(ci): PG18 probe reports where the public port sits in the URI; explicit :14274 tests | yes | 2026-11-02 15:56:13 | 2aeb38f284 | `claude/api-football-pr-125-hardening-d9x0pz` |
| 160 | fix(ci): PG18 probe reports whether the secret's host/port match the Aiven service | yes | 2026-11-02 15:34:02 | b5fffccba5 | `claude/api-football-pr-125-hardening-d9x0pz` |
| 159 | fix(ci): PG18 test diagnoses connection failures without leaking connection data | yes | 2026-11-02 15:19:18 | 9731ed80ac | `claude/api-football-pr-125-hardening-d9x0pz` |
| 158 | fix(ci): i precheck del test PG18 riportano una categoria d'errore invece di inghiottirla | yes | 2026-11-02 14:48:57 | f44073754a | `fix/fcb-pg18-test-precheck-diagnostics` |
| 157 | fix(ci): diagnostica non sensibile quando FCB_PG18_TEST_URL punta a un altro database | yes | 2026-11-02 14:15:44 | ba4f4c1b42 | `fix/fcb-pg18-test-uri-diagnostics` |
| 156 | ci(aiven): test manuale migration 0001-0007 su fcb_pg18_test (PostgreSQL 18) | yes | 2026-11-02 12:26:22 | 57b0ecd42f | `ci/fcb-aiven-pg18-test` |
| 155 | docs(sxbet): audit del wallet Magic dell'account esistente, architettura del signer isolato, workflow di trading dalla chat | yes | 2026-11-01 15:15:37 | d569e6e65f | `docs/sxbet-magic-wallet-audit` |
| 154 | fix(sxbet-audit): retry dopo una finestra completa di 60 s sul rate limit locale | yes | 2026-11-01 15:10:40 | 627225b7bf | `fix/sxbet-audit-composite-pacing` |
| 153 | test(sxbet-signer): EIP-712 OBv3 independent vectors, ecrecover and EOA key-format proofs | yes | 2026-11-01 15:07:45 | e60c3d4132 | `test/sxbet-eip712-official-vectors` |
| 152 | feat(sxbet-signer): official REST adapter (no sender), owner-approved cancellation, PostgreSQL ledger anchor + lease, REJECTED→filled | yes | 2026-11-01 15:12:02 | 54b2cb75e6 | `feat/sxbet-signer-adapter-cancel-ledger` |
| 151 | feat(sxbet): position manager, hedge simulator, rule engine + 9 DERIVED read-only tools; healthz sxbet_rate_coordinator | yes | 2026-11-01 14:56:38 | 3ddec6284a | `feat/sxbet-positions-hedging-rules` |
| 150 | docs(sxbet): activation readiness (Italy BLOCKED_LEGAL_VERIFICATION, wallets, scopes, limits proposal, checklists) | yes | 2026-11-01 14:19:17 | b8cf18fa71 | `docs/sxbet-activation-readiness` |
| 149 | feat(sxbet-signer): FIX 1/2/4, risk engine, approval binding v2, disabled submission gateway | yes | 2026-11-01 13:46:02 | a4e46d7366 | `feat/sxbet-signer-hardening-risk-gateway` |
| 148 | fix(sxbet): shared PostgreSQL rate coordinator for SX Bet (FIX 3, governance §2.4) | yes | 2026-11-01 13:17:36 | 72c2bcc1b1 | `feat/sxbet-shared-rate-coordinator` |
| 147 | docs(sxbet): Magic wallet compatibility, official signing paths, proxy wallet lifecycle | yes | 2026-11-01 13:10:42 | 49395a9a57 | `docs/sxbet-magic-proxy-wallet` |
| 146 | chore(sxbet): SXBET_API_KEY now in the main-only environment; pin it with a test | yes | 2026-11-01 12:19:09 | eb95213f64 | `chore/sxbet-env-secret-done` |
| 145 | fix(sxbet-audit): use the documented proxy/Safe address fields | yes | 2026-11-01 10:28:07 | b300491df0 | `fix/sxbet-audit-proxy-safe-fields` |
| 144 | fix(sxbet-audit): wait on the bridge's local rate limit instead of reporting FAIL | yes | 2026-11-01 10:19:32 | 357fe1f8ee | `fix/sxbet-audit-pacing` |
| 143 | [OWNER MERGE ONLY] feat(sxbet): SIMULATION mode + isolated EIP-712 signer (no submit path, LIVE_TRADING still impossible) | yes | 2026-11-01 12:19:40 | 161e1679cb | `feat/sxbet-simulation-isolated-signer` |
| 142 | feat(sxbet): all 36 official GET operations as MCP tools + official 57-operation inventory | yes | 2026-11-01 10:09:06 | 7177b07cd2 | `feat/sxbet-remaining-read-routes` |
| 141 | fix(apifootball): expose all 140 officially documented parameters (v3.9.3) and close the 6 pending contracts | yes | 2026-11-01 10:01:24 | 481cec22b0 | `fix/apifootball-official-parameter-reconciliation` |
| 140 | feat(sxbet): manual live READ-ONLY certification workflow (roadmap #130) | yes | 2026-11-01 09:53:43 | 1f0e1a64d1 | `feat/sxbet-live-readonly-certification` |
| 139 | fix(mcp): published tool catalogue provable per process (SX Bet ChatGPT discovery P0) | yes | 2026-11-01 09:39:45 | 94ca791515 | `fix/mcp-tool-registry-observability` |
| 138 | feat(sxbet): READ_ONLY SX Bet module (23 sxbet_* tools) from the official V3 OpenAPI (roadmap #130 M2B) | yes | 2026-11-01 00:40:28 | 04e11b4eab | `feat/sxbet-read-only` |
| 137 | docs(governance): AGENTS.md/CLAUDE.md describe only the MCP product (roadmap #130) | yes | 2026-11-01 00:25:17 | b92549286f | `docs/governance-mcp-only` |
| 136 | test(odds): fix intermittent price-leak assertion matching timestamp microseconds | yes | 2026-11-01 00:08:16 | f03e0cb342 | `test/fix-timestamp-flaky-price-assertions` |
| 135 | chore(bridge): remove legacy /internal/pinnacle routes and json_document (roadmap #130 M3) | yes | 2026-10-31 23:55:29 | 3f15cac303 | `chore/remove-internal-pinnacle-routes` |
| 134 | docs(readme): MCP-only README + Browser Use/CloakBrowser docs (roadmap #130 M4) | yes | 2026-10-31 23:48:10 | 23b02dca4e | `docs/readme-mcp-only` |
| 133 | fix(security): browser_create no longer returns Browser Use liveUrl (embeds CDP host) | yes | 2026-11-01 00:12:15 | 193f208d9a | `fix/browser-use-redact-live-url` |
| 132 | chore(repo): remove legacy consensus engine, collectors, pilots, backtests (roadmap #130 M3) | yes | 2026-10-31 23:32:07 | 5fa516ebd6 | `chore/remove-legacy-consensus-engine` |
| 131 | feat(api-football): navigable price-free manifest for oversized /odds/live (roadmap #130 M1-B) | yes | 2026-10-31 23:14:13 | 50c4d9890c | `feat/af-live-odds-manifest` |
| 129 | fix(api-football): real-reply pagination, /odds/mapping page, accurate warnings (from real MCP client run) | yes | 2026-10-31 23:05:20 | 13e7ee882a | `fix/af-mcp-real-pagination-and-mapping-page` |
| 128 | fix(api-football): first call after process start no longer fails with QUOTA_COORDINATOR_UNAVAILABLE | yes | 2026-10-31 22:19:09 | 8fdf8ab084 | `fix/af-quota-schema-init-outside-lease-timeout` |
| 127 | test(mcp): offline protocol E2E + ChatGPT/Claude.ai certification runbook | yes | 2026-10-31 22:59:21 | 036621abe4 | `test/mcp-offline-protocol-e2e` |
| 126 | fix(api-football): release orphan quota lease after coordinator timeout (QUOTA_COORDINATOR_UNAVAILABLE → LOCAL_CONCURRENCY_BUSY) | yes | 2026-10-31 21:48:46 | d667d7884e | `fix/af-quota-orphan-lease-on-timeout` |
| 125 | fix(api-football): bounded massive pre-match odds handling and auditable parameter matrix | yes | 2026-10-31 22:45:03 | 4cdbd28a04 | `fix/af-odds-payload-contract-20261025` |
| 124 | FCB-MCP-GLOBAL-E2E-CERT-01 — certificazione globale MCP (fail-closed) | no | - | - | `cert/fcb-mcp-global-e2e-01` |
| 123 | API-FOOTBALL — One PR: full bookmaker, markets, statistics, LIVE and coverage certification | yes | 2026-10-25 18:21:24 | ee4cabd1cd | `feat/apifootball-five-point-certification` |
| 122 | Fix API-FOOTBALL MCP LIVE /status, quota reporting and safe mode classification | yes | 2026-10-25 16:21:26 | cae48c4b69 | `fix/apifootball-live-status-gate-20261025` |
| 121 | Diagnose missing API-FOOTBALL MCP tool discovery without exposing secrets | yes | 2026-10-25 15:52:42 | 3aa06df3d8 | `mcp-apifootball-discovery-diagnostics` |
| 120 | API-FOOTBALL: exhaustive coverage ledger for bookmakers, markets, statistics and live | yes | 2026-10-25 15:06:57 | b203b58546 | `api-football-full-coverage-certification` |
| 119 | API-FOOTBALL: persist complete observed route and catalog evidence files | yes | 2026-10-25 14:51:17 | 14bee0ad21 | `api-football-persistent-evidence-audit` |
| 118 | API-FOOTBALL PR 3/3: bookmaker, market and statistics capability inventory | yes | 2026-10-25 14:30:15 | e8263aef24 | `api-football-markets-statistics-inventory` |
| 117 | API-FOOTBALL PR 2/3: bounded live 39-path inventory and evidence | yes | 2026-10-25 14:25:48 | 0713172f17 | `api-football-39-route-live-evidence` |
| 116 | API-FOOTBALL PR 1/3: reconcile official fixture multi-IDs and missing filters | yes | 2026-10-25 14:21:48 | b4a05b0b00 | `api-football-contract-reconcile-next` |
| 115 | API-FOOTBALL: publish live smoke evidence without Actions artifact storage | yes | 2026-10-25 14:11:52 | aafdbe382b | `api-football-live-summary-evidence` |
| 114 | API-FOOTBALL: bounded opt-in live smoke test with quota and secret protection | yes | 2026-10-25 14:04:50 | 2c10c7b430 | `api-football-live-contract-cert-114` |
| 113 | FCB API-FOOTBALL fail-closed evidence gap gate | yes | 2026-10-25 13:51:04 | bf8c642a9e | `api-football-evidence-gap-gate` |
| 112 | FCB API-FOOTBALL permanent contract registry and change detector | yes | 2026-10-25 12:28:02 | 3cc19a4147 | `contracts-registry-final` |
| 111 | FCB API-FOOTBALL PR-07: offline adversarial certification | yes | 2026-10-25 11:49:05 | 10239ef11a | `feat/fcb-apifootball-pr07-certification` |
| 110 | FCB API-FOOTBALL PR-06: gated MCP client wiring | yes | 2026-10-25 11:43:36 | 2c92fc1fb2 | `feat/fcb-apifootball-pr06-wiring-gate` |
| 108 | FCB API-FOOTBALL PR-04: MatchPilot 325-field gap and 37-indicator offline audit | yes | 2026-10-25 11:38:19 | 7086f97762 | `feat/fcb-apifootball-pr04-matchpilot-gap-20261025` |
| 107 | FCB API-FOOTBALL PR-03: all 38 football routes + status, 42 typed offline MCP tools | yes | 2026-10-24 22:46:45 | 27d2e5ae8a | `feat/fcb-apifootball-pr03-typed-mcp-20261025` |
| 106 | FCB API-FOOTBALL PR-02: shared quota resilience, safe mock transport and regression tests | yes | 2026-10-24 22:01:30 | 4228bfc475 | `feat/fcb-apifootball-pr02-resilience-20261024` |
| 105 | FCB API-FOOTBALL PR-01: optional offline foundation, fail-closed isolation and tests | yes | 2026-10-24 21:19:58 | d0196e090f | `feat/fcb-apifootball-pr01-foundation-20261024` |
| 104 | Fix Claude MCP OAuth with secure DCR fallback and redacted diagnostics | yes | 2026-10-24 17:40:58 | 9a21c56bce | `fix/claude-oauth-safe-diagnostics-20261024` |
| 103 | Support Claude.ai published OAuth identity safely | yes | 2026-10-24 17:02:02 | 91e97e6f77 | `fix/claude-oauth-cimd` |
| 100 | Handle browser stop and reaper race safely | yes | 2026-10-05 22:18:03 | 2d208462dd | `fix/browser-stop-reaper-race` |
| 99 | Prevent Cloak DOM reads and stop locks from wedging the bridge | yes | 2026-10-05 22:05:37 | f45eaeb815 | `fix/cloak-dom-stop-lock` |
| 98 | Prewarm CloakBrowser runtime before service readiness | yes | 2026-10-05 21:48:48 | c80865b7b7 | `fix/cloak-startup-prewarm` |
| 97 | Make Cloak shutdown resilient to hung browser close | yes | 2026-10-05 21:43:03 | 906d07e8f8 | `fix/cloak-stop-resilience` |
| 96 | Harden browser bridge against Cloak hangs and memory pressure | yes | 2026-10-05 21:35:19 | 80291ef87b | `fix/browser-bridge-resilience` |
| 95 | Fix CloakBrowser live click/fill fallback | yes | 2026-10-05 20:22:51 | cc7f6f81ed | `fix/cloak-actions-live-cert` |
| 94 | Fix CloakBrowser interactive actions on ScoreTrend | yes | 2026-10-05 19:42:02 | fdfb613aba | `fix/cloak-scoretrend-actions` |
| 93 | feat(browser): full interaction controls for Cloak and Browser Use | yes | 2026-10-05 17:59:20 | 4635685c42 | `feat/browser-interaction-tools` |
| 92 | FEAT: browser lifecycle inventory, health and cleanup controls | yes | 2026-10-04 23:46:40 | dcec015ef0 | `feature/browser-lifecycle-controls-91` |
| 90 | FEAT: provider CloakBrowser opzionale nel Browser MCP bridge | yes | 2026-10-04 22:59:31 | 2182026c2b | `feature/cloakbrowser-bridge` |
| 88 | Qualifica Footiqo match-level e audit finale pilot 48H | yes | 2026-10-02 23:23:49 | 4d5a214865 | `work/pilot-final-audit-footiqo` |
| 86 | fix: seal truthful pilot capture status and admission audit | yes | 2026-10-02 07:59:57 | ff4dd0c7b6 | `fix/pilot-run-outcome-status` |
| 85 | fix: reject ambiguous and unaudited Pinnacle moneylines | yes | 2026-10-02 00:11:22 | d777f80b97 | `fix/pinnacle-moneyline-validation` |
| 84 | fix: score frozen shown tickets and seal prospective evaluation criteria | yes | 2026-10-01 23:38:35 | f46495a229 | `fix/frozen-ticket-evaluation` |
| 82 | fix: verify result pilot lifecycle, native UTC and competition | yes | 2026-10-01 22:01:09 | d7b8f518cc | `fix/result-pilot-context-lifecycle` |
| 81 | Fix pilot UTC timestamps, reject live Opta vectors and recover ClubElo fixtures | yes | 2026-10-01 21:29:30 | 951a05ac6c | `fix/structured-pilot-source-times` |
| 80 | M17: persist strict market matching rejection audit | yes | 2026-10-01 19:15:47 | f0f27115db | `fix/m17-market-match-audit` |
| 79 | M27: replay observed Pinnacle FT totals with exact market and line gates | yes | 2026-10-01 18:27:33 | e4d622521d | `work/m27-pinnacle-same-market` |
| 78 | fix: handle pending browser routes during session shutdown | yes | 2026-10-01 17:54:44 | 137a7de496 | `fix/browser-route-shutdown` |
| 77 | fix: suspend failing pilot sources and gate result fallback | yes | 2026-10-01 17:43:47 | 451961c677 | `fix/pilot-circuit-and-fallback` |
| 76 | fix: bound public source retries and require browser stop | yes | 2026-10-01 16:08:00 | c6eeabfac5 | `fix/pilot-navigation-retries` |
| 75 | fix: gate pilot ingestion on bounded content readiness | yes | 2026-10-01 15:49:15 | 4da23de453 | `fix/pilot-content-readiness` |
| 74 | fix: audit Pinnacle bridge failures and retry only transient HTTP 503 | yes | 2026-10-01 15:18:06 | 8f90768170 | `fix/bridge-startup-retry` |
| 73 | fix: report pilot HTTP failures and collect Opta from observed homepage | yes | 2026-10-01 15:08:14 | 920b3be211 | `fix/pilot-http-quality` |
| 72 | fix: AIPredict public AI layer without treating ODDS bars as models | yes | 2026-10-01 14:39:49 | 19f7242de5 | `fix/aipredict-public-model-layer` |
| 71 | fix: Pinnacle league fallback with complete bounded JSON and audited browser cleanup | yes | 2026-10-01 14:28:16 | ad098d7e78 | `fix/pinnacle-league-browser-fallback` |
| 70 | feat: proper scoring read-only with explicit policy and common fixture set (M24 code) | yes | 2026-10-01 14:16:54 | 0aab7f0cb5 | `feature/read-only-proper-scoring` |
| 69 | Preserve future source timezone proof and server-seal pilot evidence | yes | 2026-10-01 14:04:18 | 4b352df937 | `fix/prospective-source-audit-sealing` |
| 68 | M28: read-only competition coverage and verification gaps | yes | 2026-10-01 13:53:00 | 13e66c3cc9 | `feat/m28-competition-coverage` |
| 67 | M26: normalize native OU/BTTS from verified pilot raw in staging | yes | 2026-10-01 13:43:11 | bf896a9361 | `feat/m26-native-market-staging` |
| 66 | M25: explicit native market vector contract | yes | 2026-10-01 13:27:35 | 039de02449 | `feat/m25-market-vector-contract` |
| 65 | M18/M19: read-only consensus robustness harness and frozen fixtures | yes | 2026-10-01 13:17:10 | 6433d940b0 | `feat/m18-robustness-harness` |
| 64 | M17: observed opening/closing references and strict quote joins | yes | 2026-10-01 12:38:03 | 3133c847a4 | `feat/m17-market-reference` |
| 63 | M16 Add versioned canonical team and competition identities | yes | 2026-10-01 12:25:25 | 960a0a2bc4 | `feat/m16-canonical-entity-registry` |
| 62 | M15 expose read-only runtime health and invariant gates | yes | 2026-10-01 11:02:49 | 77303600f5 | `feat/m15-runtime-invariant-health` |
| 61 | M14 exercise collect, freeze, settle and result consensus on real PostgreSQL | yes | 2026-10-01 10:57:18 | 252b9dfb70 | `test/m14-collect-settle-postgres-smoke` |
| 60 | M13 clean response listeners and reinstall navigation guard on reconnect | yes | 2026-10-01 10:53:55 | a4d7e54f57 | `fix/m13-browser-listener-cleanup` |
| 59 | M12 recover orphan browser sessions without stopping active work | yes | 2026-10-01 10:48:39 | e3a428c0b8 | `fix/m12-owned-browser-reaper` |
| 58 | M08 report explicit coverage and matching rejection reasons | yes | 2026-10-01 10:39:08 | d2da7e858c | `fix/m08-coverage-reasons` |
| 57 | M11 prevent duplicate decisions and stakes on POST retries | yes | 2026-10-01 10:34:38 | 37ac508608 | `fix/m11-decision-post-idempotency` |
| 56 | M10 add transactional versioned Neon migrations | yes | 2026-10-01 10:23:26 | a4bb941b52 | `feat/m10-versioned-migrations` |
| 55 | M09 report the actual snapshot insert count | yes | 2026-10-01 10:17:08 | 13f854b3e3 | `fix/m09-honest-insert-count` |
| 54 | M08 harden FIFA fetch and match safety | yes | 2026-10-01 10:13:25 | 3132d42cdc | `fix/m08-fifa-matching-hardening` |
| 53 | M07 make result observations idempotent | yes | 2026-10-01 09:53:31 | 618b18582c | `fix/m07-result-observation-idempotency` |
| 52 | M06b revert stale unverified market settlements | yes | 2026-10-01 09:48:37 | c4dc8188cc | `fix/m06b-revert-unverified-market-status` |
| 51 | M06 require VERIFIED fields before settlement | yes | 2026-10-01 09:45:33 | a9a3f9ca84 | `fix/m06-verified-field-settlement` |
| 50 | M05 normalize real result-source observations | yes | 2026-10-01 09:41:00 | 92a9894d5c | `feat/m05-real-result-observations` |
| 49 | M04 keep StatsUltra Under 2.5 missing | yes | 2026-10-01 09:34:21 | 5aff50dcc8 | `fix/m04-statsultra-no-inferred-under` |
| 46 | M03 stop inferring ProSoccer timezone | yes | 2026-09-30 23:41:52 | 45cc2a8b86 | `fix/m03-prosoccer-timezone` |
| 45 | M02 preserve zero values in persistence | yes | 2026-09-30 23:38:41 | 72daf39e59 | `fix/m02-preserve-zero-values` |
| 44 | M01 isolate pilot sources from production tickets | yes | 2026-09-30 23:34:27 | 0b76010cc1 | `fix/m01-pilot-production-isolation` |
| 41 | Document and enforce repository governance contracts | yes | 2026-09-30 22:52:14 | b163d59d0f | `docs/repository-contracts` |
| 34 | Fix Issue #29 zero-row source parsers | yes | 2026-09-30 22:12:47 | 43ad7fcb2a | `fix/issue29-zero-row-parsers` |
| 33 | Fix settlement kickoff lookup | yes | 2026-09-30 22:09:53 | 08a5f42b88 | `fix/settlement-kickoff-join` |
| 32 | Issue #29: add isolated 48h shortlist pilot | yes | 2026-09-30 22:00:31 | 34041cf7cb | `feature/issue29-48h-pilot` |
| 31 | Fix ProSoccer 48h pilot future-window parsing | yes | 2026-09-30 21:22:40 | fe74a48771 | `fix/prosoccer-pilot-future-window` |
| 28 | Issue #20: add worldfootball.net to personal-use result pilot | yes | 2026-09-30 17:05:34 | 26fbdc1e5f | `issue20-add-worldfootball-pilot` |
| 27 | Issue #20: add isolated 48h result-source pilot | yes | 2026-09-30 16:37:45 | 215b39fcfe | `issue20-result-source-pilot48` |
| 26 | Issue #20 staging: 365Scores adapter and integration plan | yes | 2026-09-30 16:28:09 | 65d1ac4100 | `issue20-source-adapters-staging` |
| 25 | Fix ProSoccer pilot datetime serialization | yes | 2026-09-30 14:57:37 | 8aba4d0f22 | `fix-prosoccer-pilot-json` |
| 24 | Fix Render source pilot import path | yes | 2026-09-30 14:36:36 | e8a69fd89b | `fix-source-pilot-import` |
| 23 | Collector: report Pinnacle fallback as transport, not error | yes | 2026-09-30 13:31:03 | d480652e79 | `fix-pinnacle-fallback-telemetry` |
| 22 | Issue #9 final fixes: timezone + Pinnacle collector fallback | yes | 2026-09-30 13:28:28 | a4ed5c3382 | `fix-issue9-final-gaps` |
| 19 | Cleanup: remove paid result-provider integrations | yes | 2026-09-30 11:49:26 | 122a335254 | `remove-paid-result-providers` |
| 18 | Results: run external validators from existing settlement cron | yes | 2026-09-30 11:14:13 | 69b68014c7 | `external-result-throttle` |
| 17 | Results validation: FIFA + API-Football + FootyStats consensus | yes | 2026-09-30 11:09:30 | 9736c16e9a | `result-validation-layer` |
| 16 | ProSoccer adapter: real O/U, HT, correct score and corners feeds | yes | 2026-09-30 10:59:54 | 7424b5dff0 | `prosoccer-multimarket-adapter` |
| 15 | Multi-market engine: DC, O/U, GG-NG, HT, correct score and corners | yes | 2026-09-30 10:48:14 | 09bed19c21 | `multi-market-engine` |
| 14 | Strategy accounting: automatic synchronized €1 simulation ledger | yes | 2026-09-30 10:29:27 | 18f29f7cef | `pr12-strategy-accounting` |
| 11 | Issue #1: persistent 48h pilots for ProSoccer, StatsUltra and Elofoot | yes | 2026-09-30 09:55:28 | 8c1a23434e | `issue1-pilot48` |
| 10 | PR8: normalize all user-facing times to Europe/Rome | yes | 2026-09-30 09:30:53 | a7f93f3f9e | `pr8-italy-time-contract` |
| 8 | PR7: freeze user-visible decision tickets | yes | 2026-09-30 08:58:47 | 23efee9df5 | `pr7-decision-tickets` |
| 7 | PR6: add ChatGPT-compatible OAuth 2.1 for MCP bridge | yes | 2026-09-29 17:21:03 | 3f8fa5a383 | `pr6-chatgpt-oauth21` |
| 6 | PR5: fix MCP mount path and session lifespan | yes | 2026-09-29 17:01:22 | 20aad1485d | `pr5-fix-mcp-mount-lifespan` |
| 5 | PR4: secure and deploy-ready Browser Use MCP bridge | yes | 2026-09-29 16:58:59 | c5623493e0 | `pr4-secure-mcp-render` |
| 4 | PR3: add Browser Use MCP bridge over CDP | yes | 2026-09-29 16:54:40 | d5d25a1564 | `pr3-browser-use-mcp-bridge` |
| 3 | PR2: dual-write collector and settlement state to Neon | yes | 2026-09-29 16:40:45 | 38aa3551ec | `pr2-neon-dual-write` |
| 2 | PR1: add Neon persistence foundation and read-only query API | yes | 2026-09-29 16:36:55 | 0db12a7342 | `pr1-neon-read-api` |

Note: the MCP `list_pull_requests` tool returns `merged:false` for every PR; `merged_at` / `merge_commit_sha` (REST) are authoritative. Squash merges explain why most head branches still show as unmerged.

## 2. Remote branches not merged into origin/main (100 of 140)

Classification rule (evidence): a branch whose PR is merged and that has **0 commits after the PR's merged_at** is `superseded/merged-equivalent` (squash-merged; tip == PR head). Branches without a merged PR are reviewed individually.

| branch | last commit | commits not on main | PR(s) | class | note |
|---|---|---|---|---|---|
| `ccr-f4e29202-nscnhw` | 2026-10-24 | 1 | - | stale | No PR. Base 2d20846 (2026-10-06), 251 commits behind main; 1 commit / 30 files (+7251) = early API-FOOTBALL module, superseded by #105-#111 on main. |
| `cert/fcb-mcp-global-e2e-01` | 2026-10-25 | 58 | #124(closed-unmerged) | stale (closed-unmerged #124) | 58 commits, 29 files incl. bridge/oauth.py, oauth_store.py, oauth_refresh_vault.py, oauth_test_client.py + 2 workflows. Touches OAuth (owner-merge class). 100 commits behind main. Do not revive without new PR + owner approval. |
| `ci/fcb-aiven-pg18-test` | 2026-11-02 | 3 | #156(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(ci): harden PG18 test workflow per independent review (P |
| `claude/api-football-pr-125-hardening-d9x0pz` | 2026-11-02 | 15 | #159(merged) #160(merged) #161(merged) #162(merged) #163(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(ci): target_check validates the raw secret value (no str |
| `docs/repository-contracts` | 2026-10-01 | 33 | #41(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add mandatory PR verification checklist |
| `docs/sxbet-activation-readiness` | 2026-11-01 | 1 | #150(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: docs(sxbet): activation readiness (Italy eligibility BLOCKED |
| `docs/sxbet-magic-wallet-audit` | 2026-11-01 | 2 | #155(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: docs(sxbet): update PR status (#152, #153 merged) in chat tr |
| `external-result-throttle` | 2026-09-30 | 18 | #18(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Tests: external result validation skips safely without keys |
| `feat/browser-interaction-tools` | 2026-10-05 | 3 | #93(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test(browser): cover shared actions and secret-safe results |
| `feat/fcb-apifootball-pr01-foundation-20261024` | 2026-10-24 | 10 | #105(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: docs(apifootball): record PR-01 CI evidence and independent  |
| `feat/fcb-apifootball-pr02-resilience-20261024` | 2026-10-24 | 25 | #106(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: docs(apifootball): record fail-closed unexpected error sanit |
| `feat/fcb-apifootball-pr03-typed-mcp-20261025` | 2026-10-25 | 16 | #107(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(apifootball): prevent boolean current filter from collid |
| `feat/fcb-apifootball-pr04-matchpilot-gap-20261025` | 2026-10-25 | 10 | #108(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: docs(apifootball): add 34 critical MatchPilot capability gap |
| `feat/fcb-apifootball-pr06-wiring-gate` | 2026-10-25 | 6 | #110(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(api-football): preserve mock payload error contract |
| `feat/fcb-apifootball-pr07-certification` | 2026-10-25 | 2 | #111(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test(api-football): add PR-07 offline adversarial matrix |
| `feat/m05-real-result-observations` | 2026-10-01 | 43 | #50(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test pilot result isolation from production consensus |
| `feat/m10-versioned-migrations` | 2026-10-01 | 45 | #56(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: M10 apply immutable SQL migrations atomically with tracking |
| `feat/m15-runtime-invariant-health` | 2026-10-01 | 51 | #62(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Expose read-only runtime invariant gates and honest degraded |
| `feat/m16-canonical-entity-registry` | 2026-10-01 | 53 | #63(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Exercise immutable registry truncate triggers with FK depend |
| `feat/m17-market-reference` | 2026-10-01 | 53 | #64(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add observed market references and strict pre-kickoff quote  |
| `feat/m18-robustness-harness` | 2026-10-01 | 54 | #65(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add read-only robustness harness and frozen synthetic fixtur |
| `feat/m25-market-vector-contract` | 2026-10-01 | 55 | #66(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: feat: add explicit native market vector contract (M25) |
| `feat/m26-native-market-staging` | 2026-10-01 | 56 | #67(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: feat: normalize hash-verified native OU and BTTS in staging  |
| `feat/m28-competition-coverage` | 2026-10-01 | 1 | #68(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: feat: report competition coverage with explicit data gates ( |
| `feat/sxbet-kms-digest-signer` | 2026-11-01 | 1 | - | active-unmerged-work (owner-merge §2.6) | No PR. 1 commit, signer/kms.py + tests (+611). Base d569e6e, 8 behind main. |
| `feat/sxbet-signer-service-approval` | 2026-11-01 | 3 | - | active-unmerged-work (owner-merge §2.6) | No PR. 3 commits, 28 files (+4105): signer_service/, signer/webauthn.py, approval.py. Out-of-band confirmation work. |
| `feat/sxbet-testnet-toronto-profile` | 2026-11-01 | 2 | - | active-unmerged-work (owner-merge §2.6) | No PR. 2 commits: signer/network_profile.py + docs (+903). |
| `feature/browser-lifecycle-controls-91` | 2026-10-05 | 6 | #92(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: docs: document browser lifecycle controls |
| `feature/cloakbrowser-bridge` | 2026-10-05 | 12 | #90(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: CLEANUP: remove temporary Cloak smoke instrumentation |
| `feature/issue29-48h-pilot` | 2026-09-30 | 28 | #32(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Run Issue 29 pilot from existing 3h cron |
| `feature/read-only-proper-scoring` | 2026-10-01 | 1 | #70(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: feat: add read-only proper scoring with explicit common samp |
| `fix-issue9-final-gaps` | 2026-09-30 | 22 | #22(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Tests: protect internal Pinnacle bridge route |
| `fix-pinnacle-fallback-telemetry` | 2026-09-30 | 19 | #23(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Collector: distinguish successful Pinnacle fallback from rea |
| `fix-prosoccer-pilot-json` | 2026-09-30 | 22 | #25(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test datetime serialization in source pilot payload |
| `fix-source-pilot-import` | 2026-09-30 | 20 | #24(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Fix source pilot imports when run as Render script |
| `fix/aipredict-public-model-layer` | 2026-10-01 | 1 | #72(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: capture only public AI distributions from AIPredict foo |
| `fix/bridge-startup-retry` | 2026-10-01 | 1 | #74(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: audit Pinnacle bridge failures and bound HTTP 503 retri |
| `fix/browser-bridge-resilience` | 2026-10-05 | 3 | #96(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test: cap local Cloak session fan-out |
| `fix/browser-route-shutdown` | 2026-10-01 | 1 | #78(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: handle route cancellation only during owned browser shu |
| `fix/browser-stop-reaper-race` | 2026-10-06 | 2 | #100(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test: stop tolerates existing stopping lease |
| `fix/claude-oauth-safe-diagnostics-20261024` | 2026-10-24 | 8 | #104(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(oauth): bound DCR JSON body while streaming before alloc |
| `fix/cloak-actions-live-cert` | 2026-10-05 | 2 | #95(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test: cover Cloak DOM action fallback |
| `fix/cloak-dom-stop-lock` | 2026-10-06 | 4 | #99(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test: stop recovers from wedged operation lock |
| `fix/cloak-scoretrend-actions` | 2026-10-05 | 2 | #94(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test: cover Cloak page-level actions |
| `fix/cloak-startup-prewarm` | 2026-10-05 | 3 | #98(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: test: prewarm Cloak binary before startup |
| `fix/cloak-stop-resilience` | 2026-10-05 | 3 | #97(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: preserve Browser Use cleanup error semantics |
| `fix/fcb-pg18-test-precheck-diagnostics` | 2026-11-02 | 1 | #158(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(ci): PG18 test prechecks report a fixed failure category |
| `fix/fcb-pg18-test-uri-diagnostics` | 2026-11-02 | 1 | #157(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(ci): non-sensitive diagnostics when FCB_PG18_TEST_URL po |
| `fix/frozen-ticket-evaluation` | 2026-10-02 | 1 | #84(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: evaluate frozen shown tickets under prospective sealed  |
| `fix/issue29-zero-row-parsers` | 2026-10-01 | 29 | #34(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test AIPredict and Vitisport pilot parsers |
| `fix/m01-pilot-production-isolation` | 2026-10-01 | 36 | #44(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add regression test for pilot production isolation |
| `fix/m02-preserve-zero-values` | 2026-10-01 | 37 | #45(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add zero persistence regression tests |
| `fix/m03-prosoccer-timezone` | 2026-10-01 | 39 | #46(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Remove browser-timezone assumptions from ProSoccer tests |
| `fix/m04-statsultra-no-inferred-under` | 2026-10-01 | 39 | #49(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test StatsUltra missing Under remains missing |
| `fix/m06-verified-field-settlement` | 2026-10-01 | 44 | #51(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test decision tickets require verified FT results |
| `fix/m06b-revert-unverified-market-status` | 2026-10-01 | 43 | #52(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test reset of stale unverified market settlements |
| `fix/m07-result-observation-idempotency` | 2026-10-01 | 43 | #53(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test semantic result observation idempotency |
| `fix/m08-coverage-reasons` | 2026-10-01 | 47 | #58(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: M08 distinguish missing coverage from unsafe matches and fai |
| `fix/m08-fifa-matching-hardening` | 2026-10-01 | 45 | #54(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: M08 reject unsafe FIFA pairs and incomplete feed windows |
| `fix/m09-honest-insert-count` | 2026-10-01 | 44 | #55(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: M09 count only snapshots actually inserted |
| `fix/m11-decision-post-idempotency` | 2026-10-01 | 46 | #57(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: M11 freeze decision requests atomically and replay concurren |
| `fix/m12-owned-browser-reaper` | 2026-10-01 | 48 | #59(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: M12 reap owned idle browsers with durable leases and retry f |
| `fix/m13-browser-listener-cleanup` | 2026-10-01 | 49 | #60(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Fix response listener lifecycle and reinstall URL guard on C |
| `fix/m17-market-match-audit` | 2026-10-01 | 1 | #80(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Explain strict Pinnacle matching exclusions without changing |
| `fix/pilot-circuit-and-fallback` | 2026-10-01 | 6 | #77(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: report only newly inserted result observations |
| `fix/pilot-content-readiness` | 2026-10-01 | 1 | #75(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: gate pilot ingestion on bounded content readiness |
| `fix/pilot-http-quality` | 2026-10-01 | 1 | #73(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: distinguish pilot HTTP quality and use observed Opta ho |
| `fix/pilot-navigation-retries` | 2026-10-01 | 1 | #76(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: bound public source retries and require browser stop |
| `fix/pilot-run-outcome-status` | 2026-10-02 | 1 | #86(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: seal truthful pilot capture status and admission audit |
| `fix/pinnacle-league-browser-fallback` | 2026-10-01 | 1 | #71(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: recover Pinnacle league feeds through bounded browser J |
| `fix/pinnacle-moneyline-validation` | 2026-10-02 | 2 | #85(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: preserve frozen four-decimal Pinnacle price conversion |
| `fix/prosoccer-pilot-future-window` | 2026-09-30 | 26 | #31(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add ProSoccer pilot parser regression tests |
| `fix/prospective-source-audit-sealing` | 2026-10-01 | 1 | #69(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: preserve future source time proof and server-seal pilot |
| `fix/result-pilot-context-lifecycle` | 2026-10-01 | 2 | #82(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix: retain result raw and reject rows on parser audit fault |
| `fix/settlement-kickoff-join` | 2026-10-01 | 28 | #33(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test settlement kickoff join |
| `fix/structured-pilot-source-times` | 2026-10-01 | 2 | #81(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Reject overflowing source datetimes without discarding neigh |
| `fix/sxbet-audit-composite-pacing` | 2026-11-01 | 1 | #154(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: fix(sxbet-audit): retry after a full 60 s window on local ra |
| `issue1-pilot48` | 2026-09-30 | 12 | #11(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Issue #1: test source pilot parsers |
| `issue20-add-worldfootball-pilot` | 2026-09-30 | 25 | #28(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Measure worldfootball result-pilot capabilities |
| `issue20-result-source-pilot48` | 2026-09-30 | 26 | #27(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Test result-source pilot inspection contract |
| `issue20-source-adapters-staging` | 2026-09-30 | 25 | #26(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Document Issue 20 result-source integration plan |
| `multi-market-engine` | 2026-09-30 | 21 | #15(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Tests: align bridge health check with OAuth contract |
| `pr1-neon-read-api` | 2026-09-29 | 12 | #2(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR1: use compatible dependency ranges |
| `pr12-strategy-accounting` | 2026-09-30 | 19 | #14(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Strategy accounting: document automatic synchronized virtual |
| `pr2-neon-dual-write` | 2026-09-29 | 10 | #3(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR2: test disabled persistence path |
| `pr3-browser-use-mcp-bridge` | 2026-09-29 | 12 | #4(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR3: document Browser Use MCP bridge contract |
| `pr4-secure-mcp-render` | 2026-09-29 | 8 | #5(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR4: test bridge health and bearer gate |
| `pr5-fix-mcp-mount-lifespan` | 2026-09-29 | 7 | #6(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR5: document public MCP host |
| `pr6-chatgpt-oauth21` | 2026-09-29 | 13 | #7(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR6: test OAuth metadata and PKCE |
| `pr7-decision-tickets` | 2026-09-30 | 13 | #8(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR7: make frozen-odds profit assertion robust |
| `pr8-italy-time-contract` | 2026-09-30 | 13 | #10(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: PR8: expose settlement timestamp in Italy time |
| `prep/fcb-apifootball-pr04-audit-20261025` | 2026-10-25 | 20 | - | stale | No PR. MatchPilot gap prep (out of scope per CLAUDE.md §1); 246 commits behind. |
| `prosoccer-multimarket-adapter` | 2026-09-30 | 18 | #16(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Tests: cover ProSoccer goal lines, HT, corners and HT/FT pro |
| `remove-paid-result-providers` | 2026-09-30 | 20 | #19(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Tests: remove paid provider-specific fixtures |
| `result-validation-layer` | 2026-09-30 | 23 | #17(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Results: rebuild FIFA consensus before every market settleme |
| `test/m14-collect-settle-postgres-smoke` | 2026-10-01 | 50 | #61(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add real PostgreSQL collector-to-settlement integration smok |
| `test/sxbet-e2e-simulated-engine` | 2026-11-01 | 2 | - | active-unmerged-work (owner-merge §2.6) | No PR. 2 commits, 18 files (+3229): signer/monitor.py, orders.py, simulated exchange + PG e2e test. |
| `test/sxbet-eip712-official-vectors` | 2026-11-01 | 2 | #153(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Merge remote-tracking branch 'origin/main' into test/sxbet-e |
| `work/m27-pinnacle-same-market` | 2026-10-01 | 1 | #79(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Add read-only Pinnacle same-market FT totals adapter (M27) |
| `work/pilot-final-audit-footiqo` | 2026-10-03 | 4 | #88(merged) | superseded/merged-equivalent | tip has 0 commits after PR merge: Qualify Footiqo and record immutable pilot audit |

Counts: stale=3, superseded/merged-equivalent=93, active-unmerged-work=4. The local working branch `claude/api-football-pr-125-hardening-d9x0pz` (tip 11729cb) == head of merged #163: merged-equivalent.

## 3. Issue #130 milestone reconciliation (body checklist is entirely unticked `[ ]`; status below is from code/CI evidence on 342fed6)

Legend: CONFIRMED = code/CI evidence supports the item; PARTIAL; CONTRADICTED = code/state contradicts a claim or the gate; OPEN = no evidence.

| M | checklist item (short) | verdict | evidence |
|---|---|---|---|
| M0 | Map runtime modules, MCP entrypoint, tools, deps, workflows, DB | PARTIAL | This phase-A pass (platform.csv). No KEEP/REMOVE/REFACTOR matrix committed in repo. |
| M0 | Shared deps: OAuth, quota coordinator, host/redirect guards, logging, CI, deploy | PARTIAL | OAuth (bridge/oauth*.py), shared PG quota (api_football/quota.py), SX Bet PG coordinator (sxbet/ratelimit.py), SSRF (bridge/security.py) exist. **Contradiction:** quota + DCR tables are runtime DDL only, not in sql/migrations (§5). |
| M0 | Baseline main SHA, deployed SHA, Render health, CI, tool inventory | CONTRADICTED (current) | main=342fed6; tool counts 34/76/122 by flag (offline). Production /mcp 503 (defaultdb 0 tables) and /healthz still reports ok:true (config-only) -> "healthy deploy" precondition of §3 not met. |
| M0 | No blind removals | CONFIRMED | Legacy tables kept in 0001-0006; no destructive migration. |
| M1 | All API-FOOTBALL endpoints/params reconciled | PARTIAL (out of platform scope) | #141 "all 140 documented params (v3.9.3)"; 42 apifootball tools; ChatGPT visibility not verifiable now (prod down). |
| M1 | Six open contracts | CONFIRMED in code (#141 "close the 6 pending contracts") | real-client proof BLOCKED. |
| M1 | /odds/live > 256 KiB manifest | CONFIRMED offline (#131 PARTIAL_ODDS_MANIFEST) | real re-test BLOCKED. |
| M1 | Real pre-match/live tests | PARTIAL | capability/route inventory workflows succeeded (Oct 25/31); live-smoke workflow only run failed (2026-10-25). |
| M1 | QUOTA_COORDINATOR_UNAVAILABLE / cold start | CONFIRMED offline+integration (#126, #128) | **Contradicted in prod**: quota tables need DB; defaultdb empty and app role CREATE rights unverified. |
| M2 | Browser Use / Cloak inventory & E2E | PARTIAL | 34 browser tools; leases need 0003 table -> **broken in prod** until DB-09 fixed. Real client proof BLOCKED. |
| M2B | SX Bet official inventory (57 ops), 36 GET tools, read-only cert | CONFIRMED read-only (#142, #140; cert workflow 11 runs, last success) | 46 sxbet tools when enabled. |
| M2B | READ_ONLY/SIMULATION/LIVE_TRADING separation, signer isolated | PARTIAL | SIMULATION + isolated signer on main (#143, #149, #152, #153); LIVE_TRADING impossible by design. Out-of-band confirmation signer work only on 4 unmerged branches (owner-merge). |
| M2B | Real trading | OPEN / correctly blocked (§2.6) | No submit path on main; jurisdiction BLOCKED_LEGAL_VERIFICATION (#150). |
| M3 | Remove out-of-scope code | CONFIRMED (code) | #132, #135 removed legacy engine and /internal/pinnacle routes; residual legacy **schema** remains in migrations (must stay). |
| M4 | README/docs | PARTIAL / CONTRADICTED | README line 34 still says "PostgreSQL (Neon)"; runtime DB is now Aiven PG 18.6. |
| M5 | Legacy issue triage/closure | OPEN | not evaluated here (owner-only closure, §6). |
| M6 | Final certification ChatGPT/Claude -> MCP -> tools | BLOCKED | /mcp 503; CI-08 (post-merge deploy verification) not automated. |
