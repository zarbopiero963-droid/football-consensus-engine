# FCB — Operational work packages (Phase A, mandate FCB-TOTAL-RECONCILIATION-CERTIFICATION-20261102)

Master issue: #130. Baseline: `main` = `342fed64682d8e775bb097d202953618a2ac2d7b`.

**Rules for every package:**
- one application PR active at a time;
- CI and an independent review are required;
- RED before the fix, GREEN after;
- evidence published on #130.

Packages that touch the signer, trading, authentication, secrets, new costs or migrations on `defaultdb` need **owner approval** (CLAUDE.md §2.6 / §3).

The detailed evidence for each gap is in `areas/*.md` and in `FCB_CAPABILITY_MATRIX.csv` (row ids are given in brackets).

## Ordered sequence

| # | ID | P | Title | Depends on | Owner gate |
|---|---|---|---|---|---|
| 1 | FCB-WP-001 | P0 | Browser SSRF class A: validated redirects, fail-closed, read tools re-validate — **DONE** (#165, `a642f3f`, deployed) | — | merge (Render deploy) |
| 2 | FCB-WP-002 | P0 | Browser SSRF class B: every request validated (fetch/xhr/script/css/img/ws), no `network_responses` oracle, service workers blocked — **IN REVIEW** (PR for WP-002) | 001 | merge |
| 3 | FCB-WP-003 | P0 (§2.4) | API-FOOTBALL live scripts through the PostgreSQL coordinator and circuit breaker | — | live runs |
| 4 | FCB-WP-004 | P1 (blocks 005) | Versioned migration for the runtime-only tables (`api_football_quota_*` ×3, `oauth_claude_dcr_clients`) and the 0008 numbering decision | — | **owner decision on 0008**, merge |
| 5 | FCB-WP-005 | P0 (production down) | Restore `defaultdb`: workflow gated by GitHub Environment `aiven-production` (required reviewer: owner), diagnose then apply | 001, 002 (or stop-gap), 004 | **separate authorization**, Environment/secret by the owner |
| 6 | FCB-WP-006 | P1 | `/healthz` and startup report schema readiness (`schema_migrations`, required tables) | 004 | merge |
| 7 | FCB-WP-007 | P1 | OAuth: fixed 503 on storage failure for `/oauth/token` and `/oauth/authorize`; store integration tests on PostgreSQL (code, token, refresh, DCR) | 004 | auth change → owner |
| 8 | FCB-WP-008 | P1 | Database TLS `verify-full` with the Aiven project CA (runtime and workflows) | CA supplied by the owner | secrets/config → owner |
| 9 | FCB-WP-009 | P1 | Least-privilege PostgreSQL roles (`fcb_migrator`, `fcb_app`, `fcb_signer`) | 004, 005 | credentials → owner |
| 10 | FCB-WP-010 | P1 | Browser: DNS pinning for Cloak, no cdpUrl in tracebacks/logs, fixed error codes, CGNAT `is_global` | 002 | merge |
| 11 | FCB-WP-011 | P1 | SX Bet: archive the sanitized evidence of the read-only audit in `evidence/sxbet/`, bound to the SHA (artifact expires 2026-12-01) | — | — |
| 12 | FCB-WP-012 | P1 | Signer: port the 3 ledger fixes (FILLED→SETTLED after purge, late fill on FILLED, absence dedupe) from `test/sxbet-e2e-simulated-engine` | — | **owner merge (§2.6)** |
| 13 | FCB-WP-013 | P1 | Signer service: passkey revocation, rate limit, persistent ledger/exposure across restarts | 012 | **owner merge (§2.6)** |
| 14 | FCB-WP-014 | P1 | API-FOOTBALL: one source of truth for documentation status (contracts/registry/catalog/sources/health), stored capture and hash | — | — |
| 15 | FCB-WP-015 | P1 | Real MCP-client certification on the current SHA: API-FOOTBALL (page ≥2, the 36 new parameters, odds manifest), SX Bet read-only through the PostgreSQL coordinator, Browser smoke per provider | 001, 002, 003, 005 | live quota/costs → owner |
| 16 | FCB-WP-016 | P2 | API-FOOTBALL: fixed codes (429/401/circuit/queue timeout) preserved in the MCP output | — | — |
| 17 | FCB-WP-017 | P2 | Browser: offline tests for the 19 untested tools, atomic Cloak capacity, HTML/screenshot caps, `stop_all` without raw text | 002 | — |
| 18 | FCB-WP-018 | P2 | Browser: sessions bound to the OAuth subject (migration) | 004, 007 | auth/schema → owner |
| 19 | FCB-WP-019 | P2 | CI on PostgreSQL 18 and a post-deploy workflow (SHA, `/healthz`, 401, tool count) | 005 | — |
| 20 | FCB-WP-020 | P2 | Derived Europe/Rome time with the raw value kept (§2.3) | — | **product decision** |
| 21 | FCB-WP-021 | P2 | Remove out-of-scope modules from `bridge/api_football/` (`capabilities`, `indicators`, `gap_matrix`, `registry_import`) | — | **owner decision** |
| 22 | FCB-WP-022 | P3 | Docs (README still says Neon), OAuth revocation endpoint, refresh-token reuse detection, consent password throttling | 007 | auth → owner |
| 23 | FCB-WP-023 | BLOCKED | Signer: wire KMS DIGEST and the Toronto profile into `core`/`gateway`; testnet | 012, 013, legal | **legal + owner** |

**Count:** 3 P0 security/governance packages (001, 002, 003), plus 1 P0 production package (005) that depends on them; 11 P1; 6 P2; 1 P3; 1 BLOCKED.

**Fixed first:** **FCB-WP-001** (Browser SSRF class A), then 002.

## Details

### FCB-WP-001 — Browser SSRF class A (P0)
- **Gap / evidence:**
  - BR-039, BR-003, BR-004, BR-018–020, BR-023;
  - `areas/browser_ssrf_proof.md`, items 1 and 2a (real Chromium): after a redirect to an internal host, the read tools return the internal content.
- **Root cause:**
  - `bridge/browser_use.py:_navigation_guard` is not called on redirect hops;
  - `open_url` (around l.299) raises without unloading the page;
  - `html()` (around l.318), `dom_text`, `screenshot` and `elements` do not re-validate `page.url`.
- **Changes:**
  - handle the redirect chain and validate every hop;
  - on any rejection: `about:blank`, then invalidate the session;
  - re-validate `page.url` and `frame.url` before every read;
  - add a `BROWSER_TOOLS_ENABLED` gate.
- **RED tests (offline, local Chromium, two loopback servers as in the harness):**
  - `test_ssrf_redirect_to_internal_blocks_and_invalidates`;
  - `test_read_tools_revalidate_page_url`;
  - redirect chain;
  - a regression lock on the scenarios already defended (meta refresh, JS, popup, history).
- **Adversarial:** 301/302/303/307/308, mixed http/https, relative Location, chain of 10, timeout during the redirect.
- **Real test:** none against third-party sites. After restore, a smoke test with an owner-controlled public URL.
- **DONE:**
  - every RED test turns GREEN;
  - the harness reports NOT_REPRODUCED on items 1 and 2a;
  - review SAFE;
  - CI green;
  - deploy verified.

### FCB-WP-002 — Browser SSRF class B (P0)
- **Gap / evidence:** BR-041, BR-040 (partial), harness items 3b, 3c and 3d.
- **Changes:**
  - the guard validates every `route.request` by resolved IP;
  - `network_responses` never exposes rejected requests;
  - `service_workers="block"`;
  - WebSocket handled with `page.route_web_socket` or blocked.
- **RED:**
  - `test_ssrf_fetch_subresource_blocked`;
  - `test_ssrf_script_css_img_subresource_blocked`;
  - `test_ssrf_network_responses_no_internal_oracle`;
  - `test_navigation_guard_validates_all_requests`;
  - WebSocket to an internal address.
- **DONE:** the harness reports NOT_REPRODUCED on 3b, 3c and 3d; review SAFE.

### FCB-WP-003 — API-FOOTBALL live scripts through the coordinator (P0, §2.4)
- **Evidence:**
  - `scripts/api_football_39_route_inventory.py`, `api_football_capability_probe.py` and `api_football_live_smoke.py` use `httpx` directly, with 0 references to the coordinator;
  - they are triggered by `workflow_dispatch` workflows with `APIFOOTBALL_API_KEY`.
  - `api-football-contract-check` (PR trigger) uses no secrets.
- **Changes:**
  - the scripts call the `ApiFootballClient` with the PostgreSQL coordinator and the circuit breaker, and refuse to start without `DATABASE_URL` (fail closed);
  - the workflows pass a coordinator database.
- **RED:** a test that fails if a live script opens an `httpx` client toward the provider outside the client/coordinator.
- **Until merged:** no live API-FOOTBALL run.

### FCB-WP-004 — Versioned migration for runtime tables (P1, blocks 005)
- **Evidence:**
  - `bridge/api_football/quota.py:46-60` and `bridge/oauth_store.py:129` use `CREATE TABLE IF NOT EXISTS`, and there is no migration for them;
  - `app/migrations.py:38` requires versions numbered 1..N with no gap, while 0008 (signer) is deliberately excluded from the bridge database.
- **Owner decision:**
  - **(a)** split the bridge and signer migration directories, or
  - **(b)** renumber 0008 as a separate signer series, or
  - **(c)** allow known gaps through an explicit allowlist.
- **Then:** a migration `000N_runtime_tables.sql` with `IF NOT EXISTS`, compatible with databases that already have the tables (Neon/old), and a test on an already-initialized database (§5).
- **Effect on 005:** the restore must apply this migration too, otherwise the database stays under-versioned.

### FCB-WP-005 — Restore `defaultdb` (P0 production)
- **Procedure:** the one published on #130 (comment 6583571331).
- **Additions from Phase A:**
  1. **Before the apply:** WP-001 and WP-002 merged, **or** the owner stop-gap (remove `BROWSER_USE_API_KEY`, Cloak off). Restoring the database makes the Browser tools reachable again.
  2. Include the WP-004 migration.
  3. The "empty schema" precheck must run just before the apply. The first `/oauth/register` creates `oauth_claude_dcr_clients` at runtime, and the precheck then blocks (fail-closed, by design).
  4. Post-deploy:
     - `/mcp` answers 401 without a token;
     - with a real token, no longer 503;
     - `tools/list` returns 122;
     - logs without UndefinedTable.
- **DONE:**
  - the same counts and checksums as run 14, plus the WP-004 tables;
  - OAuth verified from a real client;
  - healthy deploy.

### FCB-WP-006 … FCB-WP-023
See the table above. Each of these packages follows the same template: gap and evidence from the CSV row, files, RED, GREEN, adversarial tests, real test, owner gate and DONE. Each is detailed in the corresponding `areas/*.md` section: Platform WP-2…WP-9, API-FOOTBALL A2–A7, Browser WP-BR-3…7, SX Bet WP1–WP9.
