# FCB — Master reconciliation (Phase A)

Mandate `FCB-TOTAL-RECONCILIATION-CERTIFICATION-20261102`, master issue #130. Date: 2026-11-02.

**Method:** code-first, contract-first, evidence-first.
- Four read-only reconciliations, one per component, run on `main`.
- One reproducible SSRF proof with real Chromium.
- Every key finding re-checked by hand against the code.

**Phase B has not started.** No production change, no migration on `defaultdb`, no signer, no trading.

## 1. Baseline
| Item | Value | Status |
|---|---|---|
| `main` | `342fed64682d8e775bb097d202953618a2ac2d7b` (merge of #163) | VERIFIED |
| Render | `/healthz` `deploy_commit=342fed64682d`; `/mcp` without a token → 401 | VERIFIED |
| Render (with token) | `/mcp` → 503: `defaultdb` has no schema | VERIFIED (DB 0 tables) |
| Aiven `piero-1` | PostgreSQL 18.6, `do-fra`, developer-1 | VERIFIED |
| `fcb_pg18_test` | 0001–0007 applied by run 14: 29/4/9/54, 61+246 constraints, 7 migrations, exact checksums, idempotent | VERIFIED (temporary database only) |
| `defaultdb` | 0 user tables | VERIFIED |
| PRs | 137 in total: 136 merged, 1 closed without merge, 0 open | VERIFIED |
| Remote branches not merged | 100: 93 already squash-merged, 3 stale, 4 signer branches with work still active | VERIFIED |
| Workflows | 7: 2 on PRs (no secrets), 5 manual (real providers or databases) | VERIFIED |

## 2. Tool matrix (122)
Levels are cumulative: "real provider" also counts the higher levels. Denominator = tools registered in code. The matrix file has 245 rows: tools plus cross-cutting capabilities.

| Component | Tools | Offline/mock tested | Real provider | Real MCP client **on the current SHA** | Production verified |
|---|---|---|---|---|---|
| API-FOOTBALL | 42 | 42/42 | 40/42 (route level, via direct scripts; all 40 provider tools) | 0/42 (10/42 on old code, narrative evidence only) | 0/42 |
| Browser Use / Cloak | 34 | 15/34 (19 untested) | 0/34 | 0/34 | 0/34 |
| SX Bet | 46 | 46/46 | 41/46 (audit run 41320538088, GitHub runner, SHA d569e6e, code unchanged up to 342fed6) | 0/46 | 0/46 (registration only) |
| **Total** | **122** | **103/122** | **81/122** | **0/122** | **0/122** |

**Parameters:**
- API-FOOTBALL: 142 declared over 40 contracts (140 without the duplicated `/leagues` view). Verified one by one against the real provider: 0/142.
- SX Bet: 36 GETs covered with 0 missing parameters. The official POST/DELETE operations, 21 of 57, are deliberately **NOT_EXPOSED**.

**Offline tests run in this pass, all with 0 failures:**

| Area | Passed | Skipped |
|---|---|---|
| API-FOOTBALL | 372 | 11 (PostgreSQL integration; these run in CI) |
| Browser | 70 | 5 |
| SX Bet (main) | 728 | — |
| OAuth / MCP / migrations | 220 | 1 |

**Platform:** 43 rows. 25/41 are OFFLINE_VERIFIED or better; 0 are verified in production.

## 3. P0/P1 findings register
| ID | P | Component | Finding | Evidence | Status |
|---|---|---|---|---|---|
| F-01 | **P0** | Browser | SSRF class A: after a redirect to an internal host, `get_html`/`get_dom`/`get_elements`/`screenshot` read the internal page | `areas/browser_ssrf_proof.md` items 1/2a, real Chromium, reproduced twice | CONFIRMED → WP-001 |
| F-02 | **P0** | Browser | SSRF class B: fetch/XHR/script/css/img/WebSocket to internal hosts are never validated; `network_responses` acts as a port-scan oracle | items 3b/3c/3d | CONFIRMED → WP-002 |
| F-03 | **P0** (§2.4) | API-FOOTBALL | 3 live scripts call the provider with direct `httpx`, outside the PostgreSQL coordinator and the circuit breaker | `scripts/api_football_*`; manual workflows | CONFIRMED → WP-003 |
| F-04 | **P0** | Platform | Production down: `defaultdb` has no schema; the server never runs migrations (`app.db.migrate()` has no caller) | `app/db.py:25`; Aiven read-only | CONFIRMED → WP-005 |
| F-05 | P1 (§5) | Platform / API-FOOTBALL | Tables created only at runtime with no versioned migration: `api_football_quota_*` ×3, `oauth_claude_dcr_clients` | `quota.py:46-60`, `oauth_store.py:129` | CONFIRMED → WP-004 |
| F-06 | P1 | Platform | The migration runner requires versions 1..N with no gap; 0008 (signer) is excluded from the bridge database, so a bridge 0009 needs an owner decision | `app/migrations.py:38` | CONFIRMED → WP-004 |
| F-07 | P1 | Platform | `/healthz` returns `ok:true` with an empty or unreachable database | `bridge/server.py` health | CONFIRMED → WP-006 |
| F-08 | P1 | OAuth | `/oauth/token` and `/oauth/authorize` return a plain 500 on a database error; the OAuth store is never tested on real PostgreSQL; refresh has no test | `areas/platform.md` | CONFIRMED → WP-007 |
| F-09 | P1 | Database | TLS `sslmode=require` does not authenticate the server; no CA pinned | CI-07, DB-12 | CONFIRMED → WP-008 |
| F-10 | P1 | Database | No least-privilege roles (`avnadmin` everywhere) | DB-13 | CONFIRMED → WP-009 |
| F-11 | P1 | Browser | DNS rebinding (Cloak); cdpUrl may appear in tracebacks; raw error text | BR-040/044 | DESIGN GAP / ASSUMED → WP-010 |
| F-12 | P1 | SX Bet | The read-only audit evidence is not in the repo (the GitHub artifact expires on 2026-12-01) | SX-060 | CONFIRMED → WP-011 |
| F-13 | P1 | Signer (main) | 3 ledger defects: FILLED→SETTLED after a purge, late fill on FILLED, absence dedupe leaving UNKNOWN forever. 9 e2e-branch tests fail on main. No impact today (no sending is possible) | SG-017/018/019 | CONFIRMED → WP-012 |
| F-14 | P1 | Signer (branch) | Missing: passkey revocation, request rate limit, ledger/exposure persistent across restarts | SG-007/033 | CONFIRMED → WP-013 |
| F-15 | P1 | API-FOOTBALL | Documentation status contradictory across 5 artifacts; no capture or hash of the official matrix | AF-053 | CONFIRMED → WP-014 |
| F-16 | P1 | All | No real-MCP-client verification on the current SHA; page ≥2, the 36 new parameters and the odds manifest have never been seen live | AF-046/047 | CONFIRMED → WP-015 |

**P2/P3:** WP-016…WP-022. **BLOCKED:** WP-023 (signer testnet/KMS: legal verification for Italy, owner decision).

## 4. Effect on the `defaultdb` restore
1. **Restoring the database reactivates the Browser tools.** Before the apply, WP-001 and WP-002 must be merged, **or** the owner applies the stop-gap: remove `BROWSER_USE_API_KEY` on Render and keep `CLOAKBROWSER_ENABLED` off.
2. The restore has to include the migration for the runtime tables (WP-004). That needs the owner's decision on the 0008 numbering.
3. Run the "empty schema" precheck immediately before the apply. Runtime traffic, such as `/oauth/register`, can create tables first; the precheck then fails closed and blocks the apply.
4. **Run 14 certifies only `fcb_pg18_test`.** It is not a certification of the production database.

## 5. M0–M6 milestones (#130)
Every checkbox in #130 is still unticked. Against the code:
- **Confirmed:**
  - M3: legacy code removed;
  - API-FOOTBALL contracts and the odds manifest: offline only;
  - SX Bet read-only and simulation.
- **Contradicted:**
  - M0 "healthy deploy": production is down;
  - M4: the README still says Neon.
- **Blocked by the database:** M6 and every real-client check.

The detailed reconciliation is in `areas/prs_and_branches.md`.

## 6. Files
- `FCB_CAPABILITY_MATRIX.csv`: 245 rows. Prefixes: AF (API-FOOTBALL), BR (Browser), SX (SX Bet), SG (signer), OA (OAuth), DB (database), CI.
- `FCB_TEST_EVIDENCE_INDEX.json`: real runs and offline suites, with SHA and outcome.
- `FCB_PR_WORKPACKAGES.md`: 23 ordered packages, with dependencies and owner gates.
- `areas/`: detailed reports per component, the SSRF proof, PRs and branches.

## 7. Criteria for the final 100%
The certification is complete only when all of the following hold:
- there are no open P0 or P1 findings;
- 122/122 tools are verified at the level their risk requires, with evidence on the certified SHA;
- production is healthy;
- OAuth and PostgreSQL work in production;
- the owner has ratified the result.

**The read-only/simulation certification is not a certification for real trading.** Real trading has its own separate gate (§2.6).
