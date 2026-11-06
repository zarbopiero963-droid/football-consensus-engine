# FCB — Operational work packages

Active mandate: `FCB-OWNER-FINAL-COMPLETION-20261106` (D01–D14 RATIFIED). Previous reconciliation mandate: `FCB-TOTAL-RECONCILIATION-CERTIFICATION-20261102`.

Master issue: #130. Current verified baseline: `main` = `7f9ee53c295aa459384378bae39e60fb7ec578df` (2026-11-06). Render deploy `dep-dbl63tp5efls73cmo1u0` is live on that SHA. Production MCP is not healthy: current logs show `UndefinedTable` and authenticated `POST /mcp` responses with 503.

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
| 2 | FCB-WP-002 | P0 | Browser SSRF class B: every request validated (fetch/xhr/script/css/img/ws), no `network_responses` oracle, service workers blocked — **PARTIAL** (#166, owner decision "OPTION 1": merged as a partial hardening, Browser kept disabled by `BROWSER_TOOLS_ENABLED=false`) | 001 | merge |
| 2b | FCB-WP-002b | P0 | Browser SSRF residuals: WebSocket opened inside dedicated/shared workers (**P0**), speculation-rules prefetch/prerender (**P1**), WebRTC/WebTransport (**P2**), `fec0::/10` + verdict-cache TOCTOU window (**P3**); Browser Use cloud limits, egress/proxy policy, positive tests for legitimate public features; fail-closed block of every uncertified capability — **PARTIAL** (PR #172: worker WS/WebSocketStream fixed fail-closed on both paths; sandboxed-srcdoc WebSocket/WebSocketStream/workers on the remote path fixed by a browser-enforced document CSP; `fec0::/10` fixed. OPEN, need an egress proxy (WP-024): speculation-rules prefetch/prerender, WebRTC TURN/STUN and WebTransport from a sandboxed srcdoc on the remote path, DNS rebinding) | 002 | merge; re-enabling Browser = owner |
| 3 | FCB-WP-003 | P0 (§2.4) | API-FOOTBALL live scripts through the PostgreSQL coordinator and circuit breaker — **DONE (code)** (#167, `df76738`, deployed); live runs need the owner-created secret `APIFOOTBALL_COORDINATOR_DATABASE_URL` in environment `api-football-live` | — | live runs; coordinator secret = owner |
| 4 | FCB-WP-004 | P1 (blocks 005) | Versioned migration for the runtime-only tables (`api_football_quota_*` ×3, `oauth_claude_dcr_clients`) and the 0008 numbering decision — **DONE (code)** (#168, `43c7926`; option A: bridge/signer series split) | — | merge |
| 5 | FCB-WP-005 | P0 (production down) | Restore `defaultdb`: workflow gated by GitHub Environment `aiven-production`, diagnose then apply — **DONE_CODE / BLOCKED_OWNER / SEQUENCE_UNDER_TECHNICAL_EVALUATION**. Historical diagnose #41710920087 passed on 2026-11-03, but current Render logs still show `UndefinedTable` and MCP 503. D02 (`verify-full`) and D03 (least privilege) remain mandatory for final certification; their chronological precedence over the schema apply is not presumed. Compare path A (schema first, hardening after) with path B (hardening first) using isolated PostgreSQL 18 evidence. No apply before the owner approves the definitive workflow, protected Environment and exact confirmation phrase. | 001, 002 (stop-gap), 004; sequencing with 008/009 TBD by evidence | **D01 owner-gated apply**; no production credentials/roles/config changes without owner approval |
| 6 | FCB-WP-006 | P1 | `/healthz` and startup report schema readiness — **PRODUCTION_VERIFIED** (#171, `25a1d8e`); production correctly reported `EMPTY`. This does not certify WP-005 or MCP operation. | 004 | — |
| 7 | FCB-WP-007 | P1 | OAuth: fixed error codes and DB-unavailable handling; authorization code, token, refresh and DCR persistence; real PostgreSQL regression tests and rollback | 004, 005 | **D08 auth-critical merge → owner** |
| 8 | FCB-WP-008 | P1 | Database TLS `verify-full` with the authentic Aiven project CA in runtime and workflows; hostname/chain/certificate verification and fail-closed negative tests | authenticated CA source | **D02 approved development; production CA/secret config → owner** |
| 9 | FCB-WP-009 | P1 | Least-privilege PostgreSQL roles (`fcb_migrator`, `fcb_app`, `fcb_signer`), revocation of excess grants and runtime compatibility tests | 004 | **D03 approved preparation; role creation/production credentials → owner** |
| 10 | FCB-WP-010 | P1 | Browser: DNS pinning for Cloak, no cdpUrl in tracebacks/logs, fixed error codes, CGNAT `is_global` | 002 | merge |
| 11 | FCB-WP-011 | P1 | SX Bet: archive the sanitized evidence of the read-only audit in `evidence/sxbet/`, bound to the SHA (artifact expires 2026-12-01) — **DONE_TESTED** (#170, `738e7da`) | — | — |
| 12 | FCB-WP-012 | P1 | Signer development/test only: ledger fixes, deduplication, settlement, late fills, persistence and recovery; no live credentials, sender or funds | — | **D11 owner merge (§2.6)** |
| 13 | FCB-WP-013 | P1 | Signer service development/test only: passkey revocation, rate limit, persistent ledger/exposure across restarts, simulated KMS and reconciliation | 012 | **D11 owner merge (§2.6)** |
| 14 | FCB-WP-014 | P1 | API-FOOTBALL: one source of truth for documentation status (contracts/registry/catalog/sources/health), stored capture and hash | — | — |
| 15 | FCB-WP-015 | P1 | Real MCP-client certification on the current SHA: API-FOOTBALL (page ≥2, the 36 new parameters, odds manifest), SX Bet read-only through the PostgreSQL coordinator, Browser smoke per provider | 001, 002, 003, 005 | live quota/costs → owner |
| 16 | FCB-WP-016 | P2 | API-FOOTBALL: fixed codes (429/401/circuit/queue timeout) preserved in the MCP output | — | — |
| 17 | FCB-WP-017 | P2 | Browser: offline tests for the 19 untested tools, atomic Cloak capacity, HTML/screenshot caps, `stop_all` without raw text | 002 | — |
| 18 | FCB-WP-018 | P2 | Sessions bound to the OAuth subject; negative cross-user tests and real PostgreSQL regression | 004, 007 | **D08 auth/schema → owner** |
| 19 | FCB-WP-019 | P2 | CI on PostgreSQL 18 and a post-deploy workflow (SHA, `/healthz`, 401, tool count) | 005 | — |
| 20 | FCB-WP-020 | P2 | UTC and original timestamps preserved; Europe/Rome derived with DST, ambiguous time and offset tests; reversible/documented transforms — **APPROVED TO DEVELOP (D09)** | — | ordinary SAFE merge |
| 21 | FCB-WP-021 | P2 | Remove only demonstrably inactive out-of-scope modules after import/runtime/tool-registry/public-API analysis, regression tests and rollback — **APPROVED WITH TESTS (D10)** | — | ordinary SAFE merge; destructive/data removal remains owner-gated |
| 22 | FCB-WP-022 | P3 | Docs correction plus OAuth revocation, refresh-token reuse detection and consent throttling; fixed codes and rollback | 007 | **D08 OAuth merge → owner** |
| 23 | FCB-WP-023 | BLOCKED | Prepare non-operative signer components only: simulated KMS and testnet only when legally/technically authorized. Real trading remains OFF. | 012, 013, legal | **D11/D12 legal + owner; no live finance** |
| 24 | FCB-WP-024 | P1 | **APPROVED PROTOTYPE ISOLATED, ZERO COST (D06):** mandatory proxy/DNS, resolved-IP validation and pinning, reject non-public IPs, no DIRECT fallback, UDP/QUIC control, real Chromium RED/GREEN for WebSocket/WebRTC/WebTransport/speculation, DNS rebinding, recovery and proxy-down. Browser Use cloud feasibility only; production stays disabled. | 002b | prototype approved; any cost, production infra or re-enable → owner |

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
- **DONE criteria (original):** the harness reports NOT_REPRODUCED on 3b, 3c and 3d; review SAFE.
- **Status: PARTIAL.** Verified by real Chromium tests (CI job `browser-ssrf-chromium`): the 15 vectors `fetch`, `xhr`, `img`, `script`, `css`, `font`, `subresource_redirect`, `iframe`, `websocket`, `beacon`, `eventsource`, `<link rel=prefetch>`, `worker` script load, `service_worker`, `form_post` to internal hosts refused at the CDP Fetch request stage (browser-level auto-attach, popups included); page-level WebSocket refused via `route_web_socket` (close 1008); no rejected request in `network_responses`; service workers blocked; pre-existing internal pages quarantined; guard missing ⇒ fail closed; credentials/non-http schemes refused; fixed error messages.
- **Not protected (moved to WP-002b):** WebSocket from inside a worker (the request-stage Fetch domain does not see it and `route_web_socket` only covers page contexts; pausing workers with `waitForDebuggerOnStart` froze them and was rejected — a frozen worker is not a protection), speculation-rules prefetch, WebRTC/WebTransport.

### FCB-WP-002b — Browser SSRF residuals (P0)
- **Scope:** worker WebSocket (P0), speculation-rules prefetch/prerender (P1), WebRTC ICE/WebTransport (P2), `fec0::/10` and the 10 s verdict cache window (P3).
- **Must also evaluate:** Browser Use cloud limits (what CDP allows remotely), network/proxy/egress or provider-policy controls, and positive tests proving legitimate public features still work.
- **Rule:** a capability that cannot be made safe on the cloud provider is documented as a limit and blocked fail-closed (e.g. disabled by launch flag/CDP or the provider refused); a frozen or hung component does not count as a protection.
- **Until DONE:** `BROWSER_TOOLS_ENABLED=false` in production; re-enabling Browser requires the owner.
- **Implementation:** RED reproduced on real Chromium, on both the local provider and remote CDP.

| Residual | RED | Status |
|---|---|---|
| WebSocket / `WebSocketStream` inside a dedicated or shared worker, from page, iframe, late iframe, srcdoc, popup or blob realms (P0) | Handshake reached the internal host on both paths, 9/9 vectors (locked by a RED control with the guards off, `test_control_every_worker_vector_leaks_without_the_guards`) | **FIXED.** Layer 1: `Worker`, `SharedWorker`, `RTCPeerConnection`, `webkitRTCPeerConnection`, `RTCDataChannel`, `WebTransport` and `WebSocketStream` read as `undefined`, set non-configurable by a context init script, in every realm the script reaches. Layer 2 below. GREEN 9/9 on both paths. |
| Sandboxed `srcdoc` iframe on the remote (Browser Use / CDP) path: `WebSocket`, `WebSocketStream`, worker (P0, independent re-review of #172) | Reached the internal host 3/3. The frame commits in its own process before any DevTools client attaches (`waitingForDebugger=false`, verified with raw CDP), so neither the init script nor Playwright's WebSocket route reach it | **FIXED by layer 2.** Every document response is re-served with `Fetch.fulfillRequest` carrying an added `Content-Security-Policy: worker-src 'none'; connect-src http: https: data: blob:`. It is enforced by the browser and inherited by srcdoc, about:blank, blob: and data: documents, sandboxed or not. `Fetch.continueResponse` was tried and rejected: Chromium does not enforce a CSP added that way. Fail closed: a body that cannot be read in time, exceeds 4 MiB, or exceeds the per-session or global buffer budget fails the document. RED locked by `test_control_sandboxed_srcdoc_escapes_the_init_script_on_the_remote_path`; GREEN 3/3. |
| WebRTC / WebTransport (P2) | UDP/QUIC outside the Fetch domain | **FIXED in realms the init script reaches** (both paths, and every realm on the local path). |
| WebRTC (TURN over TCP, STUN over UDP) and WebTransport from a sandboxed `srcdoc` frame on the remote path (P1) | TURN/STUN bytes and QUIC packets reached the internal host | **OPEN.** The constructors are still defined in that frame. Chromium has no CSP directive for WebRTC (`webrtc 'block'` tried, not enforced). WebTransport URLs are `https:` and match `connect-src` like `fetch` does. Not HTTP to the internal host. Locked by strict xfails: `test_webrtc_from_a_sandboxed_srcdoc_residual_is_still_open`, `test_webrtc_udp_from_a_sandboxed_srcdoc_residual_is_still_open`, `test_webtransport_from_a_sandboxed_srcdoc_residual_is_still_open`. Clean on the local path, with a RED control proving the vectors fire there without the guards. |
| `fec0::/10` (P3) | Accepted by the validator | **FIXED.** Refused (`is_site_local`). |
| Arming | — | A session without the capability guard is guard-inactive (fail closed). |
| Speculation-rules prefetch/prerender (P1) | Browser-process `GET` reached the internal host | **OPEN.** Probably wider than a blind GET: an activated prerendered document may carry neither the CSP nor the init script (not verified; to be covered by WP-024). CDP Fetch never pauses it. `Page.setPrerenderingAllowed`, the data-saver override and the Chromium launch flags tried (`--disable-blink-features=SpeculationRules`, `--disable-features=Prerender2,...`) do not stop it. Text and DOM filtering was bypassed by character references, split `document.write` arguments and blob: documents (independent review of #172), so it was not shipped. Locked by a strict xfail: `test_speculation_rules_residual_is_still_open`. |
| DNS rebinding (P3) | A name the bridge validates as public, resolved by Chromium to the internal host, reached it on both paths | **OPEN.** The request stage does not expose the connected IP. Locked by a strict xfail: `test_dns_rebinding_residual_is_still_open` (Chromium `--host-resolver-rules`, test only). |

- **Test profiles (TEST ENVIRONMENT ONLY):** the WP-002b suite runs twice in CI.
  - Applies to `tests/test_browser_capability_guard_chromium.py`; the older WP-001/WP-002 Chromium suites run in the native profile only.
  - `lna_off`: Chromium's Local Network Access checks are disabled so the bridge's guards are proven on their own; the RED controls (guards off: every vector fires) and the residual xfails run here.
  - `native`: the unmodified browser; every GREEN test must hold as well.
  - Production Chromium flags, Render and Browser Use are never changed. The full vector × provider × guards matrix is reproducible with `scripts/wp002b_browser_matrix.py`.
- **Document re-serve (layer 2), checked on both paths:** redirects, `Set-Cookie` (two cookies), the site's own CSP still enforced, `iso-8859-1` charset, gzip, a 404 status, a reload of a `Cache-Control: max-age` document (each navigation reaches the site again, with or without the re-serve, measured, so no cache regression and no cache-served document is claimed), and a page after an oversized one: `test_re_serving_documents_keeps_navigation_semantics`.
- **Fail closed without false SSRF verdicts:** a document that cannot be re-served is failed with `Fetch.failRequest`. That covers a body unreadable within 30 s, over the 4 MiB per-document cap, or over the buffer budgets (12 MiB per session, so one session cannot starve the others, and 32 MiB across all sessions; raw body and base64 copy are both counted until the fulfil returns). The navigation raises the fixed code `DOCUMENT_REFUSED`, never `REDIRECT_BLOCKED`. A cancelled frame no longer spills into the next navigation (regression test). `open_url` reports `documents_refused`.
- **Way forward for the OPEN rows:** an egress proxy that resolves DNS and validates every resolved IP before connecting (FCB-WP-024).
  - Cloak: `--proxy-server`, plus WebRTC policy `disable_non_proxied_udp`.
  - Browser Use cloud: only if its API accepts a custom proxy, which is not verified.

  This covers browser-process prefetch, WebRTC/WebTransport from unguarded frames and rebinding.
- **Until then:** `BROWSER_TOOLS_ENABLED=false` stays on in production. Re-enabling Browser requires the owner, who accepts the open P1.
- **Declared limits of the fix (compatibility):**
  - sites that need web workers, WebRTC, WebTransport or **WebSockets** do not work. The document CSP refuses every `ws:`/`wss:` socket, including on the page itself, so live pages that push updates over WebSocket only show the state at load. Feature detection sees `undefined` and degrades instead of throwing;
  - a document renders only once its whole body has arrived (no progressive rendering; streaming or forever-frame pages wait until timeout, and `wait_until="commit"` waits for the body);
  - documents over 4 MiB fail closed with `DOCUMENT_REFUSED`;
  - one CDP round trip per 64 KiB of every document.

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
- **Implementation:** `bridge/api_football/live_harness.py` (`coordinated_client`) is the only provider door for the scripts: one `PostgresQuota` lease per attempt (counted even on failure), circuit breaker (5xx/429/transport), provider zero written back with `mark_exhausted`, fixed refusal codes, `ValueError(COORDINATOR_DATABASE_REQUIRED)` without `DATABASE_URL`. The 3 workflows pass `DATABASE_URL` from `secrets.APIFOOTBALL_COORDINATOR_DATABASE_URL` and fail closed before any live step when it is empty.
- **Shared-budget semantics (declared):** each job checks the key's shared day/minute counters (runtime and every workflow together) against its own `APIFOOTBALL_MAX_*`. The smoke job (4/day) is therefore refused once the key has made 4 calls that UTC day, and the capability probe (60) gets only `60 − used`. This fails closed. Raising the workflow limits is an owner decision (§2.4).
- **Provider zero:** a provider-reported zero now raises the shared counters to the configuration upper bound, and a rolling marker blocks the next 60 s. Every consumer is blocked, whatever its own limits.
- **Owner gate:** which database backs the coordinator (it must be the one Render uses, so that CI and runtime share the same budget) and the creation of that GitHub Environment secret (§2.2: a GitHub secret does not reach Render, and vice versa).

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
- **Implementation (option A):**
  - **Bridge series:** `sql/migrations/bridge/0001–0007` are byte-identical moves (same names and SHA-256), tracked in `schema_migrations`. The Aiven 0001–0007 script still regenerates to SHA `b73ed5c2…`.
  - **New `bridge/0008_runtime_tables.sql`:** exactly the runtime DDL, `IF NOT EXISTS`, additive.
  - **Signer series:** `sql/migrations/signer/0001_signer_ledger_anchor.sql` (bytes of the former 0008), tracked in `signer_schema_migrations` under its own advisory lock. The bridge runner (`app.db.migrate()`) never applies it.
- **Pre-split database:** a database already carrying `schema_migrations` v8 = `0008_signer_ledger_anchor.sql` is refused with a checksum/name drift error and its history is left untouched. Only test databases are known to be in this state: `defaultdb` is empty, `fcb_pg18_test` has 0001–0007, and Neon was deleted.
- **Rollback:**
  - Reverting the PR is code only. The runtime never runs migrations at boot, so a revert touches no database.
  - After bridge 0008 has been applied, an old checkout's runner would refuse that database (v8 name drift). That is fail-closed, and no data is lost.
  - Inside a run, the upgrade is a single transaction, so a failure rolls everything back.

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

### FCB-WP-024 — Browser egress proxy, fail closed (P1, proposal)
- **Why:** the WP-002b residuals cannot be closed inside the browser. Speculation-rules requests leave from the browser process, and WebRTC/WebTransport from a sandboxed srcdoc on the remote path escape both the init script and the CSP. DNS rebinding happens between the bridge's resolver and Chromium's.
- **Design (to be validated):**
  - every browser connection (TCP and UDP) goes through a proxy owned by the bridge;
  - the proxy resolves DNS itself, refuses any resolved non-public IP (same rules as `bridge/security.py`), and pins the connection to the validated IP;
  - UDP and QUIC are refused (no WebRTC, no WebTransport, no HTTP/3).
  - Fail closed: no proxy means no navigation.
- **Per provider:**
  - Cloak (local): `--proxy-server` plus WebRTC IP handling `disable_non_proxied_udp`.
  - Browser Use cloud: only if its API accepts a custom egress proxy and UDP blocking. Not verified; if it cannot, Browser Use stays disabled for untrusted pages.
- **Tests:** every OPEN row of WP-002b turns from strict xfail into GREEN on both providers, in both Chromium profiles, plus positive public navigation.
- **Gate:** new infrastructure and possible cost, so the owner decides. Until it is done, `BROWSER_TOOLS_ENABLED=false` and `CLOAKBROWSER_ENABLED=false` stay in production.

## Owner gate matrix — mandate FCB-OWNER-FINAL-COMPLETION-20261106

| Decision | Scope | Development/test | Merge/apply/production gate |
|---|---|---|---|
| D01 | defaultdb bridge 0001–0008 | preflight, isolated PG18 tests and A/B sequencing analysis allowed | apply only through the owner-approved definitive workflow, protected Environment and exact confirmation; no signer migration |
| D02 | Aiven TLS | verify-full implementation and negative tests approved; mandatory for final certification | authentic CA and production secret/config owner-gated; chronological order relative to WP-005 decided from A/B evidence |
| D03 | PostgreSQL roles | SQL/runbook/tests approved; mandatory for final certification | create roles and distribute credentials owner-gated; chronological order relative to WP-005 decided from A/B evidence |
| D04 | API-FOOTBALL live read-only | approved within existing budget after coordinator/credentials work | live run only when shared coordinator and dedicated credentials are verified; no plan upgrade |
| D05 | Browser Use cloud | feasibility checks only | disabled; re-enable owner-gated |
| D06 | Cloak/proxy WP-024 | isolated zero-cost prototype approved | production/cost/infra/re-enable owner-gated |
| D07 | Costs | analysis/proposal only | every new cost owner-gated |
| D08 | OAuth/session | development/review approved | auth-critical merge and production config owner-gated |
| D09 | Europe/Rome | approved | ordinary SAFE merge |
| D10 | legacy removal | approved with dependency/regression evidence | ordinary SAFE merge unless destructive/data scope |
| D11 | signer | development, simulation and tests only | signer merge owner-gated; no real credentials/funds |
| D12 | real SX Bet | prohibited | separate future mandate required |
| D13 | 122-tool certification | controlled testing approved | disabled Browser tools = BLOCKED; no budget overrun |
| D14 | autonomy | ordinary non-sensitive SAFE PRs | sensitive or mixed PR uses the strictest owner gate |

**Invariant current status:** Browser cloud OFF; Cloak production OFF; SX Bet real trading OFF; defaultdb apply owner-gated. A skipped/xfail/infra error is never PASS.

## Owner clarification — WP-005 / D01–D03 (2026-11-06)

D02 and D03 are not weakened or removed, but they are **not automatic chronological prerequisites** to the bridge 0001–0008 apply. Before requesting an apply decision, compare:

- **Path A — schema first:** use the already tested owner-gated workflow with its current encrypted TLS/role model, then migrate to `verify-full` and dedicated roles.
- **Path B — hardening first:** prepare and validate `verify-full` plus dedicated roles, then restore the bridge schema with the hardened identity and trust configuration.

For both paths the technical decision must include dependencies, ownership/permissions, Render and workflow compatibility, isolated PostgreSQL 18 tests, backup/rollback, intermediate-failure consequences and required workflow changes. The recommendation must be an evidence-backed GO/NO-GO. No plaintext TLS, no disabled certificate verification on the chosen path, no production credential/role/config mutation and no `defaultdb` apply without owner approval.
