# D — SX Bet component and signer: independent audit (read-only)

- **Target:** `main` = `a8fb2289cb06284f5426d4cd64b6064be57f617c` (#175). The local checkout HEAD is `c892e7a`, which is a different tree (22 files differ: ratelimit.py, migrations tests, etc.). I therefore audited an exact export: `git archive a8fb228` → `/tmp/claude-0/audit_sx/src`. The repository was not modified.
- **Scope:** `bridge/sxbet/` (tools, client, ratelimit, simulation, positions, hedging, rules), `signer/` (core, approval, risk, intent, eip712, orders, gateway, rest_adapter, cancellation, anchor, audit), `sql/migrations/signer/0001`, `docs/sxbet/`, `evidence/sxbet/`, tests, and workflows `sxbet-live-readonly-certification.yml` and `multi-market-tests.yml`.
- **Constraints:** I made no SX Bet calls, no production calls and no DB writes. Only throwaway keys were used (`Account.create()` and `Ed25519PrivateKey.generate()`).
- **Tests run:**
  - `tests/test_sxbet_*.py` + `tests/test_repository_governance.py`: **736 passed** (offline, 9.6 s).
  - On a disposable local PostgreSQL 16 (`127.0.0.1:55439`, now stopped), `tests/integration/test_signer_ledger_anchor_postgres.py`, `test_sxbet_rate_coordinator_postgres.py` and `test_migration_series_postgres.py`: **28 passed**. This was PG16, not the production PG18.
  - Auditor reproductions in `/tmp/claude-0/audit_sx/repro/test_audit_repro.py`: **7/7 reproduce** (R1–R7 below). Run them with `cd /tmp/claude-0/audit_sx/src && SXBET_SIGNER_PROCESS=1 PYTHONPATH=. /tmp/claude-0/venv/bin/python -m pytest --rootdir=/tmp/claude-0/audit_sx/repro /tmp/claude-0/audit_sx/repro/test_audit_repro.py`.

## 0. Governance conflict (recorded, NOT resolved)

The owner decision of 2026-11-06 says the future signer is **AUTONOMOUS**:
- no passkey, WebAuthn, biometric or smartphone approval;
- no manual approval per order and no periodic renewal;
- isolated key custody, automatic risk limits, dedupe, reconciliation, kill switch and audit;
- LIVE stays OFF (D12).

This conflicts with:
- **CLAUDE.md / AGENTS.md §2.6** (lines 79 and 87), which still require "conferma specifica dell'owner … autenticata e fuori banda … verificata dal signer … monouso e a scadenza breve" for **every** order, and state that LIVE cannot be activated until such a signer exists;
- **the code on main.** `signer/core.py::Signer.sign_order` cannot sign without a per-order Ed25519 owner `Approval` (TTL ≤ 120 s). Every cancel needs a per-cancel `CancelApproval`. The LIVE flag has `MAX_FLAG_TTL_SECONDS = 24*3600`, which is a de-facto mandatory **daily renewal**. A recovered ledger needs an owner-signed `LedgerHeadAck` (TTL ≤ 300 s);
- **docs** (§6 list).

Per CLAUDE.md §0: the auditor stops at documenting this. The conservative invariant (LIVE OFF, no real orders) is satisfied either way. Amending §2.6 is a governance-file change (owner merge, AGENTS/CLAUDE kept aligned, `test_repository_governance.py`).

## 1. Capability table

Legend: IMPL = implemented on main and tested offline; SIM = simulation or pure function only; NOT_IMPL = absent on main; BRANCH = only on an unmerged branch.

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Markets / events / fixtures / leagues / sports / teams / search | IMPL read-only, real evidence | `tools.py` GET wrappers; evidence run 41320538088: PASS (live_scores, markets_popular EMPTY_VALID) | Evidence from 2026-11-01 (before the 2026-11-07 DB init, LOCAL_PROCESS coordinator, code SHA d569e6e). No REAL_MCP_CLIENT evidence |
| Orderbook / best odds / liquidity | IMPL read-only + SIM fill walk | `sxbet_orderbook_market/event`, `sxbet_best_odds` PASS (live book EMPTY_VALID); `simulation.simulate_fill` (taker perspective, never invents liquidity) | Fees and betting delay are not modelled in `simulate_fill` (declared) |
| Odds representation and conversion | IMPL (integer) | `percentageOdds` kept as an int at 1e20 scale; round-DOWN onto the ladder with the raw value preserved (`build_intent`, `intent.validate` re-checks); effective odds `filled*1e20//payout` | No decimal/American conversion is exposed (only a unit note). Acceptable, but the user output is raw 1e20 |
| Selections / outcome one-two / "Not" (economic LAY) | IMPL (model) | `positions.py` docstring and code: laying outcome one = backing outcome two; void outcome (0) tracked as `if_void`; signer binding `selection{outcome,label}` cross-checked against `outcomeOneName/TwoName` (`risk.evaluate`) | No Betfair-style LAY order type exists (correct for SX Bet). Maker-side semantics of `percentageOdds` in OBv3 are DOCS-reliant |
| Market vs limit orders | SIM / builder only | Every SX order is a limit at `percentageOdds`; IOC/FOK = taker "market-like", GTC = maker (+ `postOnly` via `SubmitOptions`); `rest_adapter.build_create_orders` | Nothing is ever submitted. `postOnly` is not in the approval binding (declared) |
| Maker / taker | SIM | `simulate_fill` walks the taker side; GTC remainder reported as `resting_base_units`, no maker-fill simulation | Maker fill/queue is not simulated |
| Stake / responsibility (liability) | IMPL | Stake = `total_bet_size` (base units, int); liability = stake for either side; `positions.worst_case_liability_base_units`; signer reserves the full stake | — |
| Full / partial fill | IMPL (ledger) + SIM | `orders.py` PARTIALLY_FILLED/FILLED from `GET /orders-v3` + `/fills-v3`; filled never decreases | **R2:** a PARTIALLY_FILLED order purged from the exchange is reserved for ever |
| Cancel | IMPL (signer, no transport) + MCP preview | `signer/cancellation.py` (by id, market, event, all; per-cancel owner approval; release only on exchange evidence); `sxbet_resting_orders_scope` preview | Needs the live flag + a manual approval per cancel, so it cannot serve as an autonomous kill switch |
| Settlement / realized P&L | IMPL (ledger) / DERIVED (bridge) | `_settlement_pnl` = Σ settleReturn − Σ fill; `positions` capital-efficiency formula | **R1 (SG-017):** a FILLED order whose row is purged never settles, so its loss is never realized. Fix on branch `test/sxbet-e2e-simulated-engine` (not on main) |
| Late fill | IMPL partial | EXPIRED/CANCELLED/EXPIRED_UNCONFIRMED self-transition; REJECTED→executed (H3) | FILLED→FILLED is not allowed (SG-018, fix on branch); R2 |
| Hedge / close position | SIM | `hedging.simulate_hedge`, `sxbet_hedge_simulation`, `sxbet_close_position_preview`; Decimal fee projection | Simulation only; proposals not executable (correct) |
| Reconciliation | IMPL (manual call) | `gateway.reconcile` (order + clientOrderId + fills); `sxbet_reconciliation_diagnostics` (REST vs caller-supplied WS, labelled CALLER_SUPPLIED) | No continuous reconciliation loop or monitor on main (`signer/monitor.py` is branch only). **R7 (SG-019)** dedupe defect |
| Retry after timeout | IMPL | Write-ahead `SIGNED→UNKNOWN(SEND_ATTEMPT)`; re-send only from SIGNED after RECONCILED_ABSENT (404 by id + clientOrderId + empty complete fills + ≥ 60 s), same signed order and same clientOrderId | Exchange behaviour on re-send is DOCS_UNVERIFIED (needs testnet); R7 can block progress |
| Fees | IMPL read + DERIVED | `sxbet_fees` PASS; fee projection with Decimal; signer binds the `expected_fees` snapshot | Fee rounding to base units is DOCS_UNVERIFIED (declared). Provider `null` fee → DERIVED "0" kept apart from the raw null (borderline with §2.3, documented) |
| Numeric precision | IMPL (good) | All money and odds are Python `int` base units / 1e20; Decimal for fee rates; Fraction in positions; floors are conservative; no `float` in any money path (grep). Floats only for data age and timing | `max_betting_delay_ms` accepts float delays from metadata (timing only) |
| Risk / exposure limits | IMPL (signer) | `risk.py`: owner-signed config, default all-zero refuses; stake, market, event, total, open orders, session/daily loss, odds band, slippage, quote age; evaluated inside the ledger lock | **R3:** loss limits ignore matched-but-unsettled worst-case loss. Risk context (market, quote, balance, fees) is **caller-supplied and unauthenticated** (declared provenance gap). Session loss resets on restart. Bridge-side limits are informational zero |
| Positions | IMPL read + DERIVED | `sxbet_positions`, `sxbet_positions_summary`, `sxbet_exposure_pnl` (first page only, PARTIAL flagged) | Proven only on an empty account (EMPTY_VALID) |
| History | IMPL read | `sxbet_trades`, `sxbet_fills`, `sxbet_ledger`, `sxbet_public_trades` | Account history EMPTY_VALID only |
| Append-only audit / ledger | IMPL | Ledger: JSONL, fsync per line, hash chain, GENESIS bound to signer, flock, poison-on-failure, replay through the state machine; optional PG anchor with fencing and triggers (PG16 test pass) | **R5:** `audit.py` (decision audit) has no lock or anchor; tail truncation and a deleted file go undetected (restarts from GENESIS). Anchor-table triggers can be disabled by the table owner (all tables owned by `avnadmin`; no `fcb_signer` role) |
| Idempotency / dedupe | IMPL | key→intent_hash (cache + ledger); nonce single use; same EIP-712 digest under another key refused (`ORDER_ID_ALREADY_AUTHORIZED`); gateway never sends the same clientOrderId twice; evidence dedupe by sha | No semantic or strategy-level dedupe (two keys + two salts = two real orders) once per-order approval is removed. R4 (cache bypasses the approval check, P3). Bridge `SubmissionTracker` / `simulate_cancel` duplicate the logic in memory, used only by tests |
| Modes READ_ONLY / SIMULATION / LIVE_TRADING | IMPL | Bridge: GET-only client, mode strings hard-coded, `write_tools: []`, no mode argument on any tool, bridge never imports signer (`test_sxbet_render_isolation`), `import signer` refuses under RENDER or without `SXBET_SIGNER_PROCESS=1` | — (see §3 LIVE enable paths) |
| EIP-712 signing | IMPL (test keys) | 8-field `Order` struct, domain from metadata, order id = digest; independent keccak/secp256k1 cross-implementation test | No official reproducible vector (OpenAPI example signature does not recover its maker): `DOCS_UNVERIFIED` until testnet. Salt wire format DOCS_UNVERIFIED |
| KMS (simulation) | BRANCH only | `feat/sxbet-kms-digest-signer@86a9d08` (DER→r‖s‖v adapter, not wired to `core`) | NOT_IMPL on main. The key is read from env `SXBET_SIGNER_PRIVATE_KEY` and stays in `os.environ` (R6) |
| Sender / gateway disabled | IMPL (disabled) | `SubmissionGateway` refuses without an owner-signed flag (re-verified per submit); `RestSender` is an abstract Protocol with **no implementation**; no service or entrypoint on main instantiates Signer/Gateway | — |
| Kill switch | NOT_IMPL | No global halt; cancel-all needs flag + manual approval; the live flag's ≤ 24 h expiry is a passive off switch only | Required by the owner decision |
| Heartbeat | NOT_IMPL (documented only) | `HEARTBEAT_TIMEOUT` is recognised as an inactiveReason | — |
| Rate coordinator | IMPL | `PostgresRateCoordinator` (advisory xact lock, sliding windows, documented per-route buckets, 429 cooldown shared, fail-closed 3 s / 10 s); prod `/healthz` POSTGRES_SHARED; PG16 integration pass | Never exercised by real traffic (`sxbet_rate_attempts` = 0 in prod). Evidence run used LOCAL_PROCESS. The signer has no rate limiting (no sender exists) |
| Passkey / WebAuthn approval | BRANCH only | `feat/sxbet-signer-service-approval@7bcb1f8` (`signer/webauthn.py`, `signer_service/*`, PWA static) | Obsolete under the owner decision; references remain on main (§6) |

## 2. Tool classification (46 tools, registered at a8fb228; same list as the evidence `context.sx_tool_names`)

Evidence: `evidence/sxbet/readonly_audit_run41320538088.json` (sha256 `2582808205…7db9b`, matches the README).
- Run 2026-11-01 15:18–15:21 UTC, mainnet, code SHA `d569e6e` (bridge/sxbet unchanged to a8fb228 except the ratelimit DDL→readiness change), LOCAL_PROCESS coordinator, `write_operations_attempted: 0`.
- Counts: 46 PASS / 13 EMPTY_VALID / 3 NOT_TESTED / 1 PARTIAL (tool_coverage 43/46).
- **It predates the 2026-11-07 DB init.** No authenticated MCP or provider call has happened in production since then.

| # | Tool | Class | Route / basis | Evidence outcome |
|---|---|---|---|---|
| 1 | sxbet_health | Local (no network) | — | PASS |
| 2 | sxbet_sports | Read-only real | GET /sports | PASS |
| 3 | sxbet_leagues | Read-only real | GET /leagues[/active] | PASS |
| 4 | sxbet_fixtures_active | Read-only real | GET /fixture/active | PASS |
| 5 | sxbet_fixture_status | Read-only real | GET /fixture/status | PASS |
| 6 | sxbet_live_scores | Read-only real | GET /live-scores | EMPTY_VALID |
| 7 | sxbet_markets_active | Read-only real | GET /markets/active | PASS (prematch p1/p2, live) |
| 8 | sxbet_markets_find | Read-only real | GET /markets/find | PASS |
| 9 | sxbet_markets_popular | Read-only real | GET /markets/popular | EMPTY_VALID |
| 10 | sxbet_metadata | Read-only real | GET /metadata/obv3 | PASS |
| 11 | sxbet_search | Read-only real | GET /search | PASS |
| 12 | sxbet_teams | Read-only real | GET /teams | PASS |
| 13 | sxbet_public_trades | Read-only real | GET /trades-v3/public | PASS |
| 14 | sxbet_orderbook_market | Read-only real (key) | GET /orderbook-v3/snapshot/market | PASS prematch / EMPTY_VALID live |
| 15 | sxbet_orderbook_event | Read-only real (key) | GET /orderbook-v3/snapshot/event | PASS |
| 16 | sxbet_best_odds | Read-only real | GET /orders-v3/odds/best | PASS (prematch, live) |
| 17 | sxbet_balance | Read-only real (account) | GET /user/balance-v3 | EMPTY_VALID |
| 18 | sxbet_fees | Read-only real (account) | GET /user/fees-v3 | PASS |
| 19 | sxbet_positions | Read-only real (account) | GET /positions-v3 | EMPTY_VALID (status open only) |
| 20 | sxbet_open_orders | Read-only real (account) | GET /orders-v3 | EMPTY_VALID |
| 21 | sxbet_trades | Read-only real (account) | GET /trades-v3 | EMPTY_VALID |
| 22 | sxbet_fills | Read-only real (account) | GET /fills-v3 | EMPTY_VALID |
| 23 | sxbet_ledger | Read-only real (account) | GET /ledger-v3 | EMPTY_VALID |
| 24 | sxbet_simulate_order | **Simulation** (3 GET + pure) | metadata, markets/find, orderbook (+ balance) | PASS (SIMULATED, signed=false) |
| 25 | sxbet_order | Read-only real (account) | GET /orders-v3/{orderId} | PASS **404 path only** (no data row ever seen) |
| 26 | sxbet_order_by_client_id | Read-only real (account) | GET /orders-v3/client/{id} | PASS **404 path only** |
| 27 | sxbet_trade | Read-only real (account) | GET /trades-v3/{tradeId} | PASS **404 path only** |
| 28 | sxbet_public_market_orders | Read-only real | GET /orders-v3/public | EMPTY_VALID |
| 29 | sxbet_public_user_orders | Read-only real | GET /orders-v3/public/user | **NOT_TESTED** |
| 30 | sxbet_public_positions | Read-only real (key) | GET /positions-v3/public | **NOT_TESTED** |
| 31 | sxbet_privacy | Read-only real (account) | GET /user/privacy-v3 | PASS |
| 32 | sxbet_proxy_wallet | Read-only real (account) | GET /user/proxy | PASS |
| 33 | sxbet_proxy_deploy_pending | Read-only real (account) | GET /user/pending-deploy-proxy | PASS |
| 34 | sxbet_proxy_funding_pending | Read-only real (account) | GET /user/transfer-to-proxy/pending | EMPTY_VALID |
| 35 | sxbet_proxy_funding_status | Read-only real (account) | GET /user/transfer-to-proxy/status | **NOT_TESTED** |
| 36 | sxbet_proxy_address_check | Read-only real (account) | GET /user/multisig/proxy/{addr} | PASS |
| 37 | sxbet_safe_info | Read-only real (account) | GET /user/multisig/{safe}/info | PASS |
| 38 | sxbet_order_status | Derived read (GET + pure) | orders-v3/{id} + fills-v3 | PASS (not-found only) |
| 39 | sxbet_positions_summary | Derived read | 7 GET + positions.py | PASS (DERIVED, empty account) |
| 40 | sxbet_exposure_pnl | Derived read | 2 GET + positions.py | PASS (empty account) |
| 41 | sxbet_resting_orders_scope | Derived read (cancel preview) | 2 GET | PASS (empty) |
| 42 | sxbet_hedge_simulation | **Simulation** | 7 GET + hedging.py | PASS (POSITION_ALREADY_BALANCED, i.e. no position) |
| 43 | sxbet_close_position_preview | **Simulation** | 7 GET + hedging.py | PASS (no position) |
| 44 | sxbet_rules_evaluate | **Simulation** | 7 GET + rules.py | PASS (NO_ACTION) |
| 45 | sxbet_risk_status | Local (no network) | — | PASS |
| 46 | sxbet_reconciliation_diagnostics | Derived read (+ caller-supplied WS) | 5 GET + positions.py | PASS (empty) |

Totals:
- 35 read-only real GET tools (`write_tools: []`):
  - effectively with data: 21 PASS-with-data;
  - EMPTY_VALID: 9;
  - 404-only: 3;
  - NOT_TESTED: 3 (public_user_orders, public_positions, proxy_funding_status).
- 5 derived-read tools;
- 4 simulation tools;
- 2 local tools.
- **0 write, sign, submit or cancel tools.**
- Every derived and simulation tool is proven only on an empty account.

## 3. LIVE_TRADING enable paths (each path checked)

| Path | Result |
|---|---|
| MCP tool argument | Impossible. No tool has a mode/flag/key/signature/approval parameter (the `mode` of `sxbet_hedge_simulation` is FULL/PARTIAL). Mode strings are literals. The client exposes `get()` only |
| Bridge env | No LIVE env var in `bridge/`/`app/`. Settings has only enabled/network/api_key/max_rpm/coordinator |
| Bridge → signer import | No `bridge/` module imports `signer`. `signer/__init__` raises ImportError under `RENDER` or without `SXBET_SIGNER_PROCESS=1` (tests pass) |
| Signer process | No service or entrypoint on main constructs `Signer`/`SubmissionGateway`. `RestSender` has no implementation, so no network transport exists in the repo |
| Gateway flag | Needs an owner Ed25519-signed file bound to signer_id + network, TTL ≤ 24 h, re-verified on every submit. A direct `LiveTradingFlag(...)` raises. Not a boundary against in-process code (documented) |
| CI / workflows | `sxbet-live-readonly-certification.yml`: manual dispatch, `sxbet-live-readonly` environment, API key only, GET + WS read. `multi-market-tests` installs signer deps only for tests. No signing key in any workflow |

**Verdict:** LIVE cannot be enabled on main by any path (VERIFIED in code and tests). No P0.

## 4. Findings (P0–P3) with reproduction

| ID | P | Finding | Reproduction | Status vs branches |
|---|---|---|---|---|
| F-0 | P1 (governance/design) | The signer architecture on main requires manual per-order and per-cancel owner Ed25519 approvals and a ≤ 24 h LIVE flag renewal. This is incompatible with the owner decision (autonomous, no per-order approval, no periodic renewal). It also conflicts with CLAUDE.md/AGENTS.md §2.6 | `signer/core.py:sign_order` steps 1–3; `approval.MAX_TTL_SECONDS=120`; `gateway.MAX_FLAG_TTL_SECONDS=86400`; `cancellation.py` per-cancel approval | Not addressed anywhere. Needs an owner governance amendment plus a redesign WP |
| F-1 | P1 (for autonomous) / P2 now | Risk context (market row, quote, liquidity, balance, fees) is supplied by the caller and not authenticated. Today the owner approval compensates; once it is removed, the limits rely on unauthenticated input | `risk.py` docstring "provenance is a documented remaining gap"; feed any context dict to `sign_order` | Open |
| F-2 | P1 (for autonomous) / P2 now | Session/daily loss limits count only realized P&L + this stake. Matched-but-unsettled worst-case losses are ignored, so concurrent open bets can lose N× the daily limit | **R3:** daily limit 5 USDC, 3 orders × 5 USDC all signed; exposure 15 USDC | Open (not on any branch) |
| F-3 | P2 | FILLED order whose exchange row is purged (404) never reaches SETTLED. The loss is never realized, so the loss limits never see it, and the matched exposure stays for ever | **R1** | SG-017; fix on `test/sxbet-e2e-simulated-engine` (not merged) |
| F-4 | P2 | PARTIALLY_FILLED order purged after expiry with no new fills: the unfilled remainder stays reserved for ever (no EXPIRED_UNCONFIRMED path from PARTIALLY_FILLED) | **R2** | **New**: not fixed by the e2e branch diff |
| F-5 | P2 | Evidence dedupe by content hash ignores observation time and send attempt: identical "absent" bytes after a re-send or after expiry are dropped, so the order stays UNKNOWN (reserved) for ever | **R7** | SG-019; fix on the e2e branch (`_context_sha`) |
| F-6 | P2 (for autonomous custody) | The signing key is read from the process env and left in `os.environ` (and `/proc/<pid>/environ`). It is reachable in-process (`_Signer__account`). No KMS/HSM on main | **R6** | KMS adapter on branch only, not wired |
| F-7 | P2 | No kill switch, no continuous reconciliation loop and no heartbeat sender | Code search (`kill`, `monitor`, `heartbeat`): none in `signer/` | `signer/monitor.py` on the e2e branch only |
| F-8 | P3 | `audit.py` decision audit: no flock or anchor; tail truncation and a deleted file go undetected (silently restarts from GENESIS); multiple writers would break the chain; the timestamp uses the wall clock, not the injected clock | **R5** | Open |
| F-9 | P3 | Idempotency cache returns a previously released signature without re-verifying any approval or context (same key + same intent). It does not create a new order (same digest), but it bypasses the stated "approval then sign" order | **R4** (`sign_order(it, None, None)` returns the cached signature) | Open |
| F-10 | P3 | No strategy-level dedupe: two client keys with different salts and the same economics give two signable orders. Today each needs its own approval nonce; under autonomy a dedupe or decision-id is required | Design review | Open |
| F-11 | P3 | A SIGNED order whose signature was lost (restart before submit: the cache is in memory) stays reserved until expiry + 404 + 24 h confirm window (capital lock, safe direction) | `orders.py` EXPIRY_CONFIRM_SECONDS path | Open |
| F-12 | P3 | Bridge `simulation.SubmissionTracker` / `simulate_cancel` are an in-memory duplicate of the signer state machine, used only by tests (drift risk) | grep: no runtime caller | Open |
| F-13 | P3 | The signer anchor DB's append-only guarantees rely on triggers that the table owner can drop or disable. Prod has only `avnadmin` (no `fcb_signer`). The signer migration is correctly NOT applied on prod (`signer_schema_migrations` absent) | SQL review | WP-009 roles |
| F-14 | P3 | `sxbet_order`/`_by_client_id`/`_trade`/`order_status` have only 404-path live evidence. 3 tools NOT_TESTED. All derived tools were proven on an empty account. The PG coordinator was never exercised by real traffic | Evidence JSON | WP7/WP8 of areas/sxbet.md |

**No finding of:**
- LIVE enable bypass;
- key exposure to MCP, model, logs or audit (allowlist enforced; repr hides the key);
- approval binding bypass (`binding_mismatch`, `_binding_well_formed`, risk event/label/fees cross-checks and `intent_hash` all bind every intent field);
- double signing under concurrency (single lock + ledger nonce/key/order-id checks);
- float rounding in money.

## 5. WP gap table (docs/certification/FCB_PR_WORKPACKAGES.md)

| WP | Requirement | Implemented on main | Concrete gaps | Acceptance criteria |
|---|---|---|---|---|
| WP-011 (marked DONE_TESTED #170) | Archive sanitized read-only audit evidence bound to SHA | Yes: JSON present, sha256 `2582808…` matches README; `test_sxbet_evidence_archive.py` pins hashes and leak checks | Evidence is from 2026-11-01 (LOCAL_PROCESS, pre-DB-init, SHA d569e6e); 3 NOT_TESTED; 3 tools 404-only; no production or REAL_MCP_CLIENT run; PG coordinator not exercised | New read-only run (owner dispatch) on the current main SHA through Render/OAuth with `rate_coordinator=POSTGRES_SHARED` in the report and `sxbet_rate_attempts` > 0 afterwards; 46/46 exercised or NOT_TESTED with a fixed reason; archived with SHA + leak test |
| WP-012 | Signer dev/test: ledger fixes, dedupe, settlement, late fills, persistence and recovery; no live creds/sender/funds | Persistence and recovery (replay, flock, poison, PG anchor + fencing) **done**; nonce/key/order-id/evidence dedupe **done**; settlement **partial**; late fills **partial** | SG-017 (R1), SG-018, SG-019 (R7) fixes only on `test/sxbet-e2e-simulated-engine`; **F-4/R2 not fixed anywhere**; F-2 loss limit; F-8 audit truncation; F-9 cache order | RED→GREEN: R1, R2, R7, FILLED→FILLED late fill, R3 (loss limit counts matched + reserved worst case), R5 (audit refuses truncated or missing log, or anchors it); full suite + PG integration green on PG18; owner merge (§2.6) |
| WP-013 | Signer service: passkey revocation, rate limit, persistent ledger/exposure, simulated KMS, reconciliation | Core ledger exposure persists (JSONL). Nothing of the *service* exists on main | Passkey revocation is **obsolete** under the owner decision (re-scope the WP). Missing: service with request rate limit + auth; KMS simulation wired into `core` (branch `feat/sxbet-kms-digest-signer` unwired); continuous reconciliation and heartbeat (F-7); kill switch; autonomous policy engine replacing per-order approval (F-0); authenticated risk-context provenance (F-1) | Signer signs with a KMS-simulated digest signer only (no env key, R6 negative test); a flood returns 429 with a fixed code; a restart preserves exposure, loss and session; reconciliation loop resolves R1/R2/R7 scenarios automatically on a SimExchange; kill switch halts signing + issues cancel-all without manual approval and is audited; no passkey/WebAuthn code; LIVE still `UNAVAILABLE` |
| WP-023 (BLOCKED) | Non-operative signer components: simulated KMS, testnet only when legally/technically authorized; real trading OFF | Nothing on main (KMS and Toronto profile are branch-only, unwired) | Legal/jurisdiction BLOCKED; EIP-712 official vector, salt format and re-send semantics DOCS_UNVERIFIED (need testnet); no sender | Owner legal decision recorded; testnet profile guard wired into core/gateway (RED: a testnet intent with a mainnet escrow is refused); KMS-simulated end-to-end on SimExchange; any testnet order only under a separate owner mandate, never mainnet |
| **Proposed new WP** (owner decision 2026-11-06) | Autonomous signer design | — | Governance amendment of §2.6 (CLAUDE.md + AGENTS.md, owner merge); remove the per-order `Approval`/`CancelApproval` and the LIVE flag ≤ 24 h renewal; replace them with owner-signed static policy + automatic limits + kill switch + dedupe + reconciliation + isolated custody | Design doc + tests; F-0..F-2, F-6, F-7 closed; D12 LIVE OFF preserved; `test_repository_governance.py` green |

## 6. Passkey / manual-approval references to remove (owner request)

**Main (a8fb228) — passkey, WebAuthn, biometric, phone or device approval:**
- `docs/sxbet/isolated_signer_architecture.md`: :73 (fuori banda), :82 (telefono, app di approvazione, PWA), :83 (gesto biometrico, passkey WebAuthn P-256), :90 (app di approvazione), :91 (verificatore WebAuthn).
- `docs/sxbet/mcp_chat_trading_workflow.md`: :15 (app sul telefono), :69, :125 (PWA o passkey), :139, :140, :142 (Conferma ordine per ordine sul dispositivo), :155, :156.
- `docs/sxbet/wallet_and_signing.md`: :30, :36 (dispositivo o app di approvazione), :44, :75, :76, :92.
- `docs/sxbet/README.md`: :10, :90, :150, :234 (dispositivo di approvazione dell'owner).
- `docs/sxbet/activation_readiness.md`: :86 (dispositivo di approvazione); :33 (Trezor "conferma sul dispositivo": hardware-wallet context, review).
- `docs/sxbet/magic_and_proxy_wallet.md`: :26, :35 (gesto fuori banda).
- `docs/sxbet/official_api_inventory.json`: 21 entries "NOT_EXPOSED (… per-operation out-of-band confirmation)".
- `docs/certification/FCB_PR_WORKPACKAGES.md`: :34 (WP-013 "passkey revocation").
- `docs/certification/FCB_MASTER_RECONCILIATION.md`: :66 (F-14 passkey revocation).
- `docs/certification/FCB_CAPABILITY_MATRIX.csv`: rows SG-003 (WebAuthn verifier), SG-004 (Android/PWA), SG-005 (unlock passkey), SG-007 (passkey revoke).
- `docs/certification/areas/sxbet.md`: :63, :69, :86, :110 (WP4 passkey revocation, Android).
- `docs/certification/areas/prs_and_branches.md`: :182 (signer/webauthn.py branch).
- `signer/README.md`: :30 (dispositivo dell'owner), plus the whole "Binding dell'approvazione" / "Perché un testo in chat" sections.
- `signer/approval.py`: :1, :3, :80 (owner's approval device, out-of-band).
- `signer/cancellation.py`: :116 (owner's approval device).
- `bridge/sxbet/tools.py`: :179 and :1111 (`sxbet_health` / `sxbet_risk_status` "per-order confirmation" — user-visible MCP output), :416 (`sxbet_simulate_order` docstring "out-of-band owner approval" — MCP schema text).
- `bridge/sxbet/simulation.py`: :5; `bridge/sxbet/rules.py`: :5 (out-of-band, signer-verified).
- `tests/test_sxbet_ledger_anchor.py`: :133 ("owner's device signs").
- Tests built on per-order approval (whole modules): `tests/sxbet_signer_kit.py::approve`, `test_sxbet_signer.py`, `test_sxbet_signer_transactional.py`, `test_sxbet_cancellation.py`, `test_sxbet_gateway.py` (live flag TTL).
- `CLAUDE.md`/`AGENTS.md` :79, :87: governance, owner-only (see §0).

**Branch-only (not on main):** `origin/feat/sxbet-signer-service-approval@7bcb1f8`: `signer/webauthn.py`, `signer_service/*` (incl. `static/approve.*`, `enroll.*`, `manifest.webmanifest`), `tests/test_sxbet_webauthn.py`, `tests/sxbet_webauthn_kit.py`, `tests/test_signer_service*.py`, plus edits to `signer/approval.py`, `signer/core.py` and the docs. Recommend closing the branch without merge.

## 7. VERIFIED vs ASSUMED

- **VERIFIED:**
  - code at a8fb228;
  - 736 offline tests + 28 PG16 integration tests pass;
  - R1–R7 reproduce;
  - 46 tools registered, matching the evidence list;
  - evidence file hash;
  - no write tools and no signer import from the bridge.
- **ASSUMED / not verified here:**
  - PG18 behaviour of the anchor triggers (tested on PG16 only);
  - production state (taken from PHASE0);
  - SX Bet semantics marked DOCS_UNVERIFIED in the repo (salt format, fillAmount units, re-send behaviour, EIP-712 official vector).
