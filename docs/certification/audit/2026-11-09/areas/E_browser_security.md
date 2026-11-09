# E — Browser (Browser Use + CloakBrowser) and whole-repo security / supply chain audit

- Auditor: independent, read-only. Date 2026-11-09.
- **Audited tree: `main` = a8fb2289cb06 (#175)**, exported with `git archive a8fb228` to `/tmp/claude-0/audit_sec/src`. The working tree `/home/user/football-consensus-engine` is at c892e7a (an older tree, not main) and was **not** used for any conclusion. The repo was not modified (HEAD and status unchanged).
- Probes and scratch files: `/tmp/claude-0/audit_sec/probes/` (`p_ssrf.py`, `p_routes.py`, `p_block.py`, `p_cdp.py`, `stall.py`).
- Toolchain: venv Python 3.13.18, mcp 2.3.0, playwright 1.63.0, starlette 1.10.0, cloakbrowser 0.5.11, real Chromium.
- Legend: VERIFIED = reproduced here. ASSUMED = inferred from code or docs and not reproducible offline. KNOWN = already in the WP register or the area docs. NEW = not registered.

---

## 1. Test results (offline, a8fb228)

| Suite | Env | Result |
|---|---|---|
| test_browser_actions, _create_no_capability_leak, _listeners, _reaper, _route_shutdown, test_cloak_bridge, test_bridge_security, test_bridge_auth, test_bridge_config, test_oauth_metadata, test_mcp_offline_protocol_e2e, test_mcp_tool_registry_observability, test_browser_ssrf_redirect_chromium, test_browser_ssrf_subresources_chromium | `FCB_REQUIRE_CHROMIUM=1` | **183 passed, 1 skipped** (test_oauth_metadata.py:539 needs the CI DATABASE_URL) |
| test_browser_capability_guard_chromium.py | `FCB_CHROMIUM_PROFILE=lna_off` | **25 passed, 6 xfailed.** The xfails are strict and correspond to the documented OPEN residuals: speculation rules; WebRTC TURN/TCP, WebRTC STUN/UDP and WebTransport from a sandboxed srcdoc frame on the remote path; DNS rebinding on the local and remote paths |
| test_browser_capability_guard_chromium.py | `FCB_CHROMIUM_PROFILE=native` | **21 passed, 10 skipped.** The RED controls and residuals run only in lna_off, by design |
| tests/integration/test_browser_leases_postgres.py | throwaway local PostgreSQL 16 (`TEST_DATABASE_URL`) | **5 passed** |
| pip-audit 2.10.1 (requirements.txt resolved today, plus the frozen venv) | network OK | **1 package: pytest 8.4.2 PYSEC-2026-1845 (fix 9.0.3).** It cannot be fixed because requirements.txt pins `pytest>=8,<9`. signer/requirements.txt: no known vulnerabilities |

A skipped or xfailed test is not counted as a PASS. The "remote" path in the Chromium suites is a **local** Chromium driven over CDP. It is **not** the real Browser Use cloud.

---

## 2. Kill-switch verification (VERIFIED, probe `p_routes.py`)

I called all 34 `browser_*` tools through `mcp.call_tool` with `BROWSER_TOOLS_ENABLED=false` and every `session_leases` function stubbed to raise.

| Group | Tools | Behaviour with the switch off |
|---|---|---|
| Session create | `browser_create`, `browser_create_cloak` | **Blocked** (`_require_enabled` runs before any provider call). Cloak additionally needs `CLOAKBROWSER_ENABLED` truthy, which is fail-closed by default |
| Session operations (24) | open_url, get_html, get_dom, screenshot, click, fill, type, press, select_option, set_checked, hover, focus, scroll, wait_for_selector, wait, back, forward, reload, get_elements, list_pages, new_page, switch_page, close_page, get_network_responses | **Blocked** by the `session_operation` decorator, before the lease renew or any CDP or provider call |
| Lifecycle and cleanup (not gated, by design) | `browser_stop`, `browser_reap`, `browser_stop_all_idle`, `browser_stop_all`, background `reaper_loop`, shutdown `stop_all` | **Not gated.** They read and write the lease DB and may call Browser Use `PATCH /browsers/{id}` (stop only). They cannot create a session or navigate. I consider this correct: a kill switch must not stop cleanup |
| Observability (not gated) | `browser_list_sessions`, `browser_session_status`, `browser_health`, `browser_cleanup_report` | Read only (DB plus in-memory state). They return no capability |

- **Totals:** 26/34 tools gated, and 8/34 ungated lifecycle and observability tools. **No path creates a session or navigates while the switch is off.**
- The switch is read on every call, so no restart is needed. No MCP argument can toggle it.
- **Weakness (NEW, F-03):** the parser fails open. If the variable is unset, empty, `disabled`, `none` or `false;`, the tools are **enabled**. Only `0/false/no/off` disable them.
- Production `/healthz` shows `browser_tools_enabled=false` and `cloak_enabled=false` (Phase 0).

---

## 3. Browser capability / residual table (a8fb228)

| Capability / vector | Mechanism | Local (Cloak-like) | Remote CDP (offline stand-in) | Real Browser Use cloud | Status |
|---|---|---|---|---|---|
| Literal private/loopback/link-local/metadata 169.254.169.254, IPv6 ULA/link-local, `fec0::/10`, NAT64, 6to4, Teredo, IPv4-mapped/compatible, CGNAT 100.64/10 (incl. 100.100.100.200), 198.18/15, 240/4, broadcast, multicast, doc ranges, decimal/hex/octal/short IPv4, `localhost.` | `security.validate_public_http_url` + `not is_global` | refused | refused | n/a (validator) | **VERIFIED** (p_ssrf.py, 45 cases). One exception: `192.88.99.1` (deprecated 6to4 relay anycast) is ALLOWED. P3 KNOWN (W4 text) |
| Redirect hops (301/302/303/307/308, chains, meta, JS, popup, history) | CDP Fetch request stage (browser-level, flat auto-attach) + post-navigation re-validation + quarantine | blocked | blocked | NOT VERIFIED | GREEN (WP-001) |
| Sub-resources (fetch/xhr/img/script/css/font/beacon/EventSource/prefetch link/form post/iframe) | Fetch request stage | blocked | blocked | NOT VERIFIED | GREEN (WP-002) |
| Page WebSocket | `route_web_socket` + document CSP (no ws:/wss:) | blocked | blocked | NOT VERIFIED | GREEN |
| Worker / SharedWorker / WebSocketStream / service worker | init script (undefined, non-configurable) + CSP `worker-src 'none'` | blocked | blocked | NOT VERIFIED | GREEN (WP-002b) |
| Sandboxed srcdoc: WebSocket/WebSocketStream/worker | document CSP re-served via `Fetch.fulfillRequest` | blocked | blocked | NOT VERIFIED (depends on the provider allowing Fetch response interception and fulfil) | GREEN |
| WebRTC / WebTransport in reachable realms | init script | blocked | blocked | NOT VERIFIED | GREEN |
| WebRTC TURN/TCP and STUN/UDP from sandboxed srcdoc | none (no CSP directive) | clean | **LEAK** | NOT VERIFIED | **OPEN** strict xfail, KNOWN → WP-024 |
| WebTransport from sandboxed srcdoc | none (`https:` matches connect-src) | clean | **LEAK** (lna_off) | NOT VERIFIED | **OPEN** KNOWN → WP-024 |
| Speculation-rules prefetch/prerender | browser process, outside Fetch | **LEAK** | **LEAK** | NOT VERIFIED | **OPEN** KNOWN (P1) → WP-024 |
| DNS rebinding / resolver TOCTOU (+10 s verdict cache) | none (bridge resolver ≠ Chromium resolver) | **LEAK** | **LEAK** | NOT VERIFIED | **OPEN** KNOWN → WP-024 / WP-010 |
| iframes (cross-origin, OOPIF), popups | auto-attach + frame URL re-validation before reads | blocked | blocked | NOT VERIFIED | GREEN |
| Pre-existing pages at CDP attach | `_quarantine_preexisting` → about:blank | covered | covered | NOT VERIFIED | GREEN |
| Guard arming failure | `_require_guard` fails closed | covered | covered | — | GREEN |
| Documents >4 MiB / budgets (12 MiB per session, 32 MiB global) / 30 s read | fail closed `DOCUMENT_REFUSED` | covered | covered | — | GREEN |
| cdpUrl / liveUrl in the MCP result | never returned; liveUrl only with `BROWSER_USE_EXPOSE_LIVE_URL` | — | — | — | GREEN (tested) |
| cdpUrl in **logs** | Playwright `connect_over_cdp` errors embed the full wss URL; mcp SDK `logger.exception` logs it | — | **VERIFIED leak** (p_cdp.py: "contains_secret: True") | ASSUMED | **OPEN** KNOWN WP-010 (W5) |
| HTML / screenshot payload caps | none; `dom_text` capped at 500 000 chars (> the 256 KiB MCP cap) | — | — | — | **OPEN** KNOWN WP-017 |
| Session isolation between OAuth clients | none; any token drives any session_id | — | — | — | **OPEN** KNOWN WP-018 (W7) |
| Orphan explicit stop | `browser_stop` raises KeyError if the session is not in memory; only the reaper (≥60 s idle + expired lease) cleans it | — | — | — | **OPEN** KNOWN (W9) |
| Cloak capacity | count under the lock, launch outside it (race) | — | — | — | **OPEN** KNOWN WP-017 (W6) |

### Certifiable offline vs needs a real provider

- **Certified offline (VERIFIED here):**
  - the validator;
  - the request guard;
  - the redirect and quarantine logic;
  - the init script and CSP layers on local Chromium, directly and over CDP;
  - the kill switch;
  - lease persistence and reaper SQL (PG16);
  - cdpUrl/liveUrl absent from tool results;
  - fixed-code masking of tool errors under mcp 2.3.0.
- **Needs the real provider (NOT_TESTED):**
  - whether Browser Use cloud permits `Fetch.enable` at the response stage, `Fetch.fulfillRequest`, `IO.read` streams, browser-level `Target.setAutoAttach` and `add_init_script`;
  - provider egress and network (what "private" means inside Browser Use);
  - Browser Use v4 endpoint contract (`DOCS_UNVERIFIED`);
  - Cloak on the Render free plan (memory and Chromium startup);
  - CDP latency of the 64 KiB document re-serve;
  - real MCP-client smoke (WP-015 / WP-BR-7).

### What blocks re-enabling (owner decision in all cases, D05/D06)

1. **OPEN P1 residuals:**
   - speculation-rules prefetch/prerender;
   - WebRTC/WebTransport from a sandboxed srcdoc on the remote path;
   - DNS rebinding.

   All three need the WP-024 egress proxy. On Cloak (Chromium running inside Render) the speculation and rebinding residuals reach Render's own network.
2. **WP-010:** cdpUrl leaks into Render logs via SDK tracebacks (VERIFIED). Errors also need fixed codes that do not depend on the mcp version.
3. **WP-017:** payload caps, atomic Cloak capacity, `stop_all` raw text.
4. **WP-018:** per-subject session ownership. It is required once more than one OAuth client is used (ChatGPT plus Claude).
5. **Provider-specific evidence:** Browser Use cloud must support the CSP re-serve (otherwise layer 2 does not exist there).
6. **Kill-switch parser fails open (F-03, NEW):** fix before relying on it as the production gate.

---

## 4. Route / auth table (bridge/server.py:472-485, VERIFIED with TestClient)

| Route | Methods | Auth | Notes |
|---|---|---|---|
| `/healthz` | GET, HEAD | **none** | Exposes `deploy_commit` and config booleans (key configured, live authorized, DB configured, tool counts). No secret values. Intentional (WP-006). P3 info disclosure |
| `/.well-known/oauth-protected-resource` | GET, HEAD | none | static metadata |
| `/.well-known/oauth-authorization-server` | GET, HEAD | none | static metadata |
| `/oauth/register` | POST | **none** | Claude DCR. Each valid body does an **INSERT plus a DELETE in PostgreSQL with no rate limit** (F-05) |
| `/oauth/authorize` | GET, HEAD | none | Validates client (CIMD fetch to chatgpt.com/claude.ai only, or DCR) and renders a password form. No anti-framing headers (F-12) |
| `/oauth/authorize` | POST | consent password (`hmac.compare_digest`; empty password ⇒ refused) | No throttling (KNOWN WP-022/G10). DB errors give HTTP 500 (KNOWN G5/WP-007) |
| `/oauth/token` | POST | PKCE S256 + client_id + redirect binding; refresh rotation | No `Cache-Control: no-store` (F-11). DB down gives 500 (VERIFIED, KNOWN G5) |
| `/mcp` (Mount, streamable HTTP, stateless) | all | Bearer (DB-checked, resource + scope bound) via `OAuthBearerMiddleware`; DNS-rebinding host/origin allowlist | Path variants tested: `/mcp`, `/mcp/`, `/%6dcp/`, `/mcp%2F`, `/./mcp/`, `/x/../mcp/` → 401; `/MCP/`, `//mcp/` → 404. **No bypass found.** The `bearer` scheme is matched case-sensitively (F-13). The token check is a **synchronous DB connect on the event loop** (F-04) |

No webhook routes exist. No route lacks auth where it should have it.

---

## 5. Findings

Severity: P0 = blocks production or a critical exposure; P1 = high; P2 = medium; P3 = low.

| ID | Sev | NEW/KNOWN | File:line (a8fb228) | Finding | Reproduction | Fix |
|---|---|---|---|---|---|---|
| F-01 | **P1** | **NEW** | `.github/workflows/fcb-aiven-pg18-test.yml:22-45` (no `environment:`) | `FCB_PG18_TEST_URL` is consumed by a job with no environment, so it is a **repository-level secret**. Per #130 run 13 / platform.md G8 it authenticates as **`avnadmin`**, and Phase 0 found avnadmin to be the only user on piero-1. That credential reaches **production `defaultdb`**. Any workflow on any branch, including a same-repo `pull_request` (multi-market-tests runs on PRs and same-repo PRs receive secrets), can read it. This bypasses every gate of `fcb-aiven-defaultdb-restore.yml`: aiven-production reviewer, owner-only actor, confirmation phrase | Push a branch that adds `env: X: ${{ secrets.FCB_PG18_TEST_URL }}` to any job and open a same-repo PR (needs repo write plus `workflows` scope, which agents may hold). ASSUMED: the secret scope could not be read (the proxy denies `/actions/secrets`), but no environment is referenced, so it is necessarily repo-level | Move it into a protected environment (required reviewer, `main` only). Use a dedicated role with CONNECT only on `fcb_pg18_test` and REVOKE CONNECT on `defaultdb` (WP-009). Rotate the avnadmin password after the move |
| F-02 | P2 | NEW (verify) | `api-football-*.yml` (`environment: api-football-live`, no `if: github.ref == main`) | The jobs rely entirely on the environment's deployment-branch rule. For sxbet-live-readonly the docs say the owner restricted it to main; no such statement exists for `api-football-live`. If that environment is unrestricted, any branch can use `APIFOOTBALL_API_KEY` and the coordinator DSN (likely the same avnadmin `defaultdb` DSN) | NOT_TESTED (environment API blocked by proxy) | Owner: confirm or require main-only plus a reviewer on `api-football-live`. Add `if: github.ref == 'refs/heads/main'` as a second layer |
| F-03 | P2 | **NEW** | `bridge/browser_use.py:91-93` | The kill switch **fails open**: unset, empty, `disabled`, `none` or `false;` all leave the tools ENABLED. Production safety depends on an exact env value; a lost var (new service, blueprint, typo) re-enables Browser Use whenever `BROWSER_USE_API_KEY` is present | `BROWSER_TOOLS_ENABLED=disabled` → `browser_tools_enabled()==True` (VERIFIED) | Default off. Accept only an explicit `true/1/yes/on` to enable; anything else disables. Add a test and expose the raw-parse result in /healthz |
| F-04 | P2 | **NEW** | `bridge/server.py:355`; `bridge/oauth.py:235,342,378,388` (sync `oauth_store` in async handlers) | **Unauthenticated event-loop stall.** Any `Authorization: Bearer <random>` on /mcp triggers a *synchronous* psycopg connect plus query on the event loop, with no pooling. The same happens on /oauth/register and /oauth/token. A slow or full DB (Aiven max_connections=15) freezes every request, including `/healthz` | `p_block.py` with a stalling DB listener: an unauth /mcp request blocks /healthz for 3.0 s (= `PGCONNECT_TIMEOUT`; without connect_timeout, the libpq/OS default can be minutes). VERIFIED | `await asyncio.to_thread(...)` or async psycopg with a small pool, a connect_timeout in the DSN, and rate limiting on unauthenticated paths |
| F-05 | P2 | **NEW** | `bridge/oauth.py:213-252`, `oauth_store.py:120-138` | `/oauth/register` is unauthenticated, unthrottled and writes a 730-day row per call (DB growth on 1 GB Aiven; connection churn) | Repeated POST with the minimal Claude metadata gives one INSERT each (code read; not run against production by rule) | Per-IP/global throttle, a cap on live DCR rows, and a shorter TTL for never-used clients |
| F-06 | P1 | KNOWN WP-010 (W5) — now **VERIFIED** | `bridge/browser_use.py:378` (`connect_over_cdp`); SDK `mcp/server/mcpserver/server.py:455` | A Playwright CDP connect error message contains the full `wss://…cdp…` URL in the message and call log. The SDK `logger.exception` writes it to Render logs. The same path logs URLs from navigation errors | `p_cdp.py`: "contains_secret: True" | Wrap `_page()` connect and navigation, and re-raise a fixed code `from None`; log the type only |
| F-07 | P2 | KNOWN (W8/WP-BR-3) | `requirements.txt:5` `mcp[cli]>=2,<3` | Client-side error masking ("Error executing tool X") depends on mcp 2.3.0 internals. Conversely, the bridge's own fixed codes (`REDIRECT_BLOCKED`, `TOOLS_DISABLED`, `DOCUMENT_REFUSED`) **never reach the client**: they are raised as plain exceptions and masked as crashes (VERIFIED: every kill-switch refusal arrives as `UnexpectedToolError "Error executing tool …"`) | p_routes.py | Pin mcp exactly. Raise `ToolError(<fixed code>)` for the bridge's own codes |
| F-08 | P2 | KNOWN WP-017 | `browser_use.py:701-715, 761-772, 1439` | Uncapped `get_html` and full-page `screenshot`. `dom_text` max 500 000 chars exceeds the 256 KiB contract. `stop_all` returns `str(exc)`, so a psycopg error can disclose DB host/port to the MCP client | code | Byte caps with an explicit PARTIAL marker; fixed codes in stop_all |
| F-09 | P2 | KNOWN WP-017 (W6) | `browser_use.py:291-299` | Cloak capacity check-then-launch race | earlier probe in area docs | Reserve the slot under the lock |
| F-10 | P2 | KNOWN WP-018 (W7/W9) | `server.py` tools; `session_leases.py` | No per-subject session ownership; orphan sessions cannot be stopped explicitly | code | WP-018 |
| F-11 | P3 | **NEW** | `bridge/oauth.py:356-367,385,391` | The token response has no `Cache-Control: no-store` (RFC 6749 §5.1 MUST) | code | Add the header (and `Pragma: no-cache`) |
| F-12 | P3 | **NEW** | `bridge/oauth.py:314-322` | The consent page has no `X-Frame-Options`/`frame-ancestors 'none'` (clickjacking or phishing-overlay of the password form) | code | Add the headers plus a restrictive CSP |
| F-13 | P3 | **NEW** | `bridge/server.py:351` | The Bearer scheme is matched case-sensitively (`bearer x` gives 401). RFC 7235 says case-insensitive. Interop only, fail-closed | VERIFIED | `auth.split(" ",1)` with a case-insensitive scheme check |
| F-14 | P3 | **NEW** | `bridge/browser_use.py:138-160` | `Session` is a dataclass whose default `repr` includes `cdp_url` and `live_url`. Any future log or `%r` of a session leaks the capability | code | `field(repr=False)` on both |
| F-15 | P3 | **NEW** | `.github/workflows/multi-market-tests.yml`, `api-football-contract-check.yml` | No top-level `permissions:`, so the default GITHUB_TOKEN scope applies (possibly write). Actions are pinned by tag (`@v4/@v5`), not by SHA, in all workflows | code | `permissions: contents: read`; SHA-pin the actions |
| F-16 | P3 | **NEW** | `requirements.txt` | Runtime dependencies use ranges with no hashes (uvicorn, psycopg, httpx, mcp). `pytest` ships in the runtime and carries PYSEC-2026-1845, which cannot be fixed under `<9`. The Render Python version is unpinned (no runtime.txt; `.python-version` is gitignored), and the `ipaddress` semantics the SSRF guard uses vary across patch versions | pip-audit | Lock file with hashes; split dev/test requirements; pin the Python version |
| F-17 | P3 | KNOWN (W4 residue) | `bridge/security.py:52-66` | `192.88.99.0/24` (deprecated 6to4 relay anycast) passes `is_global` | p_ssrf.py | Explicit deny list for 192.88.99.0/24 |
| F-18 | P3 | **NEW** (docs) | `docs/certification/FCB_PR_WORKPACKAGES.md` WP-005 step 3, `FCB_MASTER_RECONCILIATION.md:75`, `areas/platform.md:78` | Still say "/oauth/register creates `oauth_claude_dcr_clients` at runtime". Since WP-004 the runtime performs no DDL (`oauth_store.py:123-125`). The docs are stale | code vs doc | Correct the docs |
| F-19 | P2 | KNOWN WP-007/G5 | `oauth.py:325-392` | DB errors in `/oauth/token` and `POST /oauth/authorize` give 500 with no fixed code. ASSUMED: uvicorn logs a psycopg traceback with host:port | VERIFIED 500 | WP-007 |
| F-20 | P2 | KNOWN WP-022/G10 | `oauth.py:325-334` | No throttling on the consent password, no refresh reuse detection, and the old access token stays valid after refresh | code | WP-022 |

### Checked and clean

- **Secrets:**
  - the tree has no private keys, cloud keys, GH tokens or DSNs with passwords (hex hits are SX Bet `marketHash` values);
  - **git history across all refs (794 commits): zero hits** for PEM private keys, AKIA, ghp_/github_pat_, sk-, credentialed aivencloud/neon DSNs and apisports keys;
  - `.env.example` holds empty values only, and `.gitignore` covers `.env*`, `*.pem` and `*.key`.
- **CI:**
  - no `pull_request_target` or `workflow_run`;
  - PR-triggered workflows use no secrets;
  - `workflow_dispatch` inputs reach the shell only through `env:` (`mode`, `confirm`, `ws_seconds`); `ws_seconds` is re-validated numerically, so there is no expression injection;
  - the restore workflow verifies the environment reviewer, owner actor, main ref, phrase, pinned host/port hashes and verify-full CA.
- **SQL:** all queries are parameterised. The `session_leases.list_sessions` f-string inserts only a constant. `signer/anchor.py:211` interpolates an int validated to 100..30000. Scripts use `psycopg.sql.Identifier`. No `shell=True`, `eval` or `pickle` in runtime code.
- **CloakBrowser binary download:** Ed25519-signed SHA256SUMS that cannot be bypassed (cloakbrowser 0.5.11 `download.py`).
- **Startup:**
  - a failed API-FOOTBALL or SX Bet registration is isolated and its tools are not registered (fail-closed);
  - with Cloak enabled, a missing runtime aborts startup;
  - an OAuth DB error gives /mcp 503 (fail-closed);
  - an empty `MCP_OAUTH_PASSWORD` refuses consent.
- **Log sanitisation:** bridge log lines use type names and fixed codes only. The exception is the SDK traceback path (F-06).
- **Race conditions:**
  - the lease SQL uses owner/state predicates and `FOR UPDATE SKIP LOCKED` (5/5 PG tests);
  - stop vs reaper is handled through the STOPPING state;
  - the document buffer counters are single-event-loop.

---

## 6. Summary

The kill switch blocks all 26 create and operation tools; the 8 lifecycle and observability tools are ungated by design. The switch parser fails open (F-03). The SSRF validator refuses every tested non-public class, except the 6to4 anycast range. WP-002b holds on real Chromium (25 passed, 6 strict xfail in lna_off; 21 passed in native). It stays PARTIAL with 3 OPEN P1 residuals that need WP-024.

- **New and most serious:** F-01, a repo-level avnadmin DSN that bypasses the defaultdb gates.
- **Other NEW findings:** F-04 (unauthenticated sync-DB event-loop stall, VERIFIED), F-05 (unthrottled DCR writes), F-03 (kill switch fails open).
- **Now verified:** the KNOWN WP-010 cdpUrl leak into logs.
