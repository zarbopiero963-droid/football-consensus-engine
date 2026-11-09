# A — MCP core + OAuth audit (independent, read-only)

- **Audited SHA:** `a8fb2289cb06` (main, #175). I exported it with `git archive a8fb228` to `/tmp/claude-0/audit_oauth/src`. The repo working tree is at `c892e7a`, and I did not use it for any conclusion.
  - `bridge/oauth.py` and `bridge/server.py` are byte-identical between the two trees.
  - `bridge/oauth_store.py` differs: at a8fb228 it has **no runtime DDL**, because the DCR table comes from migration 0008. All `oauth_store.py` line numbers below are for a8fb228.
- **Live deploy:** `/healthz.deploy_commit = a8fb2289cb06`, `schema_state = READY`, 122 tools, `oauth_password_configured = true`, `browser_tools_enabled = false`. Observed 2026-11-09.
- **Method:**
  - Read the code.
  - Ran the offline tests.
  - Ran my own probe scripts against the **real** `oauth_store` SQL on a throwaway local PostgreSQL 16. The cluster is at `/tmp/pgaudit_oauth` on port 55477, with database `oauth_a8` migrated through `app.migrations.apply_migrations`. The probes drive the real Starlette app through `TestClient`.
  - Probe scripts: `/tmp/claude-0/audit_oauth/probe_flow.py`, `probe_dbdown.py`, `probe_get.py` and `probe_slash.py`. Output: `/tmp/claude-0/audit_oauth/probe_out_a8.json`.
  - Live probes, limited to these: GET discovery, unauthenticated POST /mcp and GET /healthz. I made no /oauth/register call, no token request and no write.

**Tests run on a8fb228:**
- `test_bridge_auth`, `test_oauth_metadata`, `test_mcp_offline_protocol_e2e`, `test_mcp_tool_registry_observability`, `test_bridge_security`, `test_bridge_config` and `test_schema_readiness` gave **82 passed, 1 skipped**. The skip is the DCR PostgreSQL round-trip, which needs `DATABASE_URL`.
- With `DATABASE_URL` pointed at the local PG16, `test_dcr_postgres_registration_round_trip_when_database_available` **passed**.
- `tests/integration/test_migration_series_postgres.py` and `test_schema_readiness_postgres.py` gave **15 passed**.

**CI gap, confirmed:**
- CI's `decision-postgres` job sets `TEST_DATABASE_URL` and runs only `tests/integration` (`.github/workflows/multi-market-tests.yml:68,76`).
- The only OAuth PostgreSQL test reads `DATABASE_URL` and lives in `tests/` (`tests/test_oauth_metadata.py:533-539`).
- As a result, **OAuth SQL never runs on PostgreSQL in CI**.

Status legend: PASS_OFFLINE means verified by a repository test or by my local probe; the evidence column says which. PASS_REAL means observed in production.

## 1. Capability table

| item | status | evidence | gap |
|---|---|---|---|
| Protected-resource metadata (RFC 9728) | PASS_REAL | live GET `/.well-known/oauth-protected-resource` returns 200, `resource=…/mcp`, scopes `browser:use offline_access`. Code: `oauth.py:44-50`. Test: `test_oauth_metadata` | Path-suffixed `/.well-known/oauth-protected-resource/mcp` returns **404** both live and locally. Clients that build the RFC 9728 path-inserted URL instead of using `WWW-Authenticate` fail |
| AS metadata (RFC 8414) | PASS_REAL | live 200: issuer, S256 only, auth method `none`, CIMD supported, `iss` parameter supported, `registration_endpoint`. Code: `oauth.py:53-67` | `/.well-known/openid-configuration` returns 404, which is acceptable. No `revocation_endpoint` |
| `WWW-Authenticate` on 401 | PASS_REAL | live unauthenticated POST /mcp/ returns 401 `Bearer resource_metadata="…/.well-known/oauth-protected-resource"`. Code: `server.py:358-364` | No `scope=` or `error=` parameters in the header |
| DCR (`/oauth/register`) | PASS_OFFLINE (local PG probe + PG test) | `oauth.py:213-252`: accepts only `redirect_uris == [claude.ai callback]`, auth method `none`, `response_types == ["code"]`. Body capped at 8 KiB. Stores only the SHA-256 of the ID (`oauth_store.py:120-138`). Probe: valid request gives 201; evil redirect gives 400 | **Unauthenticated and unthrottled.** Each call inserts a row with a 730-day TTL. Probe: 25 calls added 25 rows (F-P2-3). Response has no `client_secret`, which is correct |
| CIMD client acceptance | PASS_OFFLINE (mocked fetch) | `_is_trusted_cimd_url` (`oauth.py:70-92`) allows the exact Claude URL or `https://chatgpt.com/oauth/…/client.json`; no userinfo, port or query. The document's `client_id` must equal the URL and its `redirect_uris` must contain the redirect (`oauth.py:95-119`). `follow_redirects=False`, timeout 15 s. Tests: `test_validate_cimd_*` | No real fetch is tested. The document is re-fetched on every authorize GET and POST, with no cache. The 64 KiB cap is checked **after** the full body is read (`oauth.py:150-153`). Dot-segments in the path are not rejected (F-P3-6) |
| CIMD SSRF | PASS_OFFLINE | The host allow-list runs before any network call (`test_validate_cimd_rejects_impersonation_before_network`). The only hosts that can be fetched are `claude.ai` (exact URL) and `chatgpt.com` | None of practical significance. The host is a trusted SaaS, not an arbitrary URL |
| Authorization code + PKCE S256 | PASS_OFFLINE (real PG) | `_authorization_request_failure` (`oauth.py:290-299`): `response_type=code`, `code_challenge_method=S256`, `resource` equal to the `/mcp` URL, `browser:use` scope. Probe: `plain` gives 400, empty method gives 400, wrong resource gives 400, evil redirect gives 400. Token exchange compares the PKCE value in constant time (`oauth.py:383`). Probe: wrong verifier gives 400 | The verifier's length and charset are not validated: a 1-character verifier was **accepted (200)**. A non-ASCII verifier gives **500** (F-P3-1). An empty `code_challenge` is accepted at authorize and yields a code that can never be redeemed |
| Code single-use, expiry, binding | PASS_OFFLINE (real PG) | `consume_code` uses an atomic `UPDATE … used_at IS NULL AND expires_at>NOW() RETURNING` (`oauth_store.py:42-53`), with a 5-minute TTL. The code is stored as SHA-256; probe `code_stored_hashed=true`. Bound to `client_id` and `redirect_uri` (`oauth.py:381`). Probe: replay gives 400; a failed verifier burns the code. Test: `test_claude_dcr_code_and_pkce_remain_bound_after_consent` | `resource` is not re-checked at the token endpoint. This is acceptable because it was bound at authorize |
| Consent / password login | PASS_OFFLINE | `authorize_post` compares against `MCP_OAUTH_PASSWORD` with `hmac.compare_digest` (`oauth.py:331-334`). An empty password fails closed. Hidden fields are HTML-escaped (`oauth.py:310-313`) | **No throttling or lockout.** Probe: 60 wrong passwords in 0.19 s, all 401 (F-P1-1). No `X-Frame-Options` or CSP on the consent page, so it can be framed (F-P3-4). No CSRF token, but the password effectively acts as one |
| Access token issuance, hashing, expiry | PASS_OFFLINE (real PG) | `secrets.token_urlsafe(32)` (`oauth.py:357`), stored as SHA-256 (`oauth_store.py:56-67`); probe `access_stored_hashed=true`. TTL is 60 minutes. Probe: an expired token gets 401. Validation checks `revoked_at IS NULL`, `expires_at>NOW()`, `resource` and scope (`oauth_store.py:84-96`) | Opaque tokens, no signing keys involved. The token response lacks `Cache-Control: no-store` (probe: `null`; RFC 6749 §5.1). Access and refresh tokens are saved in two separate transactions (F-P3-5) |
| Refresh rotation | PASS_OFFLINE (real PG) | `rotate_refresh_token` uses an atomic `UPDATE … revoked_at` (`oauth_store.py:99-110`). Probe: refresh gives 200; reusing the old refresh token gives 400 | — |
| Refresh reuse detection / family revocation | **NOT_IMPLEMENTED** | Probe: after the old token is replayed, the **new refresh token still works (200)** and the **old access token still works (200)** | WP-022 (F-P2-1) |
| Refresh expiry | PASS_OFFLINE | 30 days (`oauth_store.py:70-81`); probe shows 29 days plus remainder | Each rotation grants a **fresh 30 days**, so the session never ends. No absolute lifetime |
| `offline_access` semantics | FAIL (minor) | Probe `refresh_without_offline_access=true` | A refresh token is issued even when `offline_access` is not requested |
| Revocation endpoint (RFC 7009) | **NOT_IMPLEMENTED** | `/oauth/revoke` returns 404 (probe). Not in AS metadata | WP-022. The only way to revoke is SQL on `revoked_at`, or rotating `MCP_OAUTH_PASSWORD`, which does **not** invalidate tokens already issued |
| Scope enforcement per tool | **NOT_IMPLEMENTED** | The middleware checks only `browser:use` (`server.py:355`). That single scope gates all 122 tools: browser, API-FOOTBALL and SX Bet | Arbitrary extra scopes are persisted and echoed back. Probe: `scope="browser:use admin:all"` (F-P3-2). No per-component scope |
| Subject binding / user isolation | **NOT_IMPLEMENTED** | One shared password and no subject column; probe `subject_column_present=false`. Tokens carry only `client_id` | Single owner by design. Every client (Claude, ChatGPT, any DCR client) sees every browser session (`docs/certification/areas/browser.md:94`). WP-018 |
| PostgreSQL persistence | PASS_OFFLINE (local PG16) | Tables from 0001 and 0008. All of code, token, refresh, DCR and validate were exercised by `probe_flow.py` on real SQL | Not in CI (see above). Not verified on PG18 or Aiven. Production `oauth_*` counts are 0 (PHASE0) |
| DB failure handling (WP-007) | **FAIL** | Probes with an unreachable DSN: <br>• `/oauth/token` with `authorization_code` gives **500 "Internal Server Error"** <br>• `/oauth/token` with `refresh_token` gives **500** <br>• `/oauth/authorize` POST with a valid client, when `save_code` fails, gives **500** (`probe_dbdown.py`) <br>• `/oauth/authorize` GET or POST with a DCR client gives **400 `invalid_request`**: a storage failure is masked as a client error (`oauth.py:261-262`) <br>• `/oauth/register` gives 503 `server_error` <br>• `/mcp` gives 503 `{"detail":"authorization backend unavailable"}` | WP-007 |
| DB timeout | IMPLEMENTED_UNVERIFIED (missing) | `_conn()` is `psycopg.connect(database_url())` (`oauth_store.py:17-18`): no `connect_timeout`, no pool, one new connection per `/mcp` request | A hung DB hangs every authenticated request. Aiven `max_connections=15` (PHASE0); concurrent clients can exhaust it (F-P2-4) |
| Secrets handling | PASS_OFFLINE | Password only from env. Probe log scan found no access token, code, OAuth password or DSN password in the logs. Rejections log fixed codes only (`oauth.py:135-142`; test `test_authorize_post_logs_rejection_without_password_or_state`) | ASSUMED (not observed): on the 500 paths, uvicorn logs a psycopg traceback with the DB host and port (no password) |
| Restart recovery | IMPLEMENTED_UNVERIFIED | All OAuth state is in PG, and MCP is `stateless_http=True` (`server.py:436`), so no in-memory session is lost | No test restarts the process and reuses a token. Production has had no authenticated call since the DB was initialized (PHASE0) |
| Rate limiting (OAuth/MCP) | **NOT_IMPLEMENTED** | No limiter on `/oauth/*` or `/mcp` | F-P1-1, F-P2-3 |
| Concurrency (code/refresh race) | PASS_OFFLINE (by construction, PG row lock) | A single-statement `UPDATE … RETURNING` is atomic | No concurrent test |
| MCP transport (streamable HTTP) | PASS_OFFLINE | `streamable_http_app(json_response=True, stateless_http=True)` (`server.py:433-438`). SDK `mcp` 2.3.0 in the venv | `requirements.txt:6` is `mcp[cli]>=2,<3` with no lock, so the deployed SDK version is unpinned (F-P2-5) |
| initialize / version negotiation | PASS_OFFLINE | Probe: 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05 are each echoed back. An unknown version (1999-01-01) gets the server's latest, 2025-11-25 | Not verified live |
| capabilities | PASS_OFFLINE | `tools`, `resources` and `prompts`, all with `listChanged=false` | `resources`/`prompts` are advertised but empty |
| tools/list | PASS_OFFLINE / PASS_REAL (count only) | Probe: 34 with the browser only. The offline E2E gives 42 API-FOOTBALL tools with full schema parity (`test_protocol_auth_discovery_and_calls_with_api_football_enabled`). `/healthz` live reports 122 from the same catalogue | Not observed through a real client |
| tools/call | PASS_OFFLINE | Offline E2E with API-FOOTBALL: OK, type error gives `isError`, contract rejection. Probe: `browser_list_sessions` against real PG returns an empty list | Not observed through a real client |
| Error mapping (WP-010) | PASS_OFFLINE (SDK-dependent) | SDK 2.3 `tools/base.py:205-213`: an unexpected exception gives only `"Error executing tool <name>"`, and the traceback stays server-side. Probe: `browser_open_url` with tools disabled returns exactly that. Unknown tool gives `isError` | (a) The client also loses the bridge's fixed codes (`TOOLS_DISABLED` and similar), because the browser raises plain `RuntimeError`/`ValueError` instead of returning structured codes. (b) Hiding the exception text comes from the SDK, not the bridge, and the version is unpinned. (c) Server logs keep the full exception text, which may contain Playwright URLs and selectors (WP-010) |
| Auth middleware on /mcp | PASS_REAL (401 path) / PASS_OFFLINE | `server.py:347-365` covers every path starting with `/mcp`. Probe: no token gives 401, bad token 401, expired 401, `bearer` in lowercase **401** (scheme is case-sensitive, F-P3-3). All other routes are public by design: `/healthz`, discovery, `/oauth/*` | — |
| Origin / DNS-rebinding protection | PASS_OFFLINE | `server.py:422-431`. Probe: `Origin: https://evil.example` gives 403; `https://claude.ai` gives 200 | — |
| `/mcp` without trailing slash | IMPLEMENTED_UNVERIFIED (risk) | Authenticated `POST /mcp` gives **307 to `http://…/mcp/`** locally (`probe_slash.py`). The advertised `resource` is `…/mcp` with no slash | In production the scheme of `Location` depends on uvicorn proxy-header trust, which I could not observe without a token (F-P2-6) |
| GET /mcp (SSE) and DELETE | PASS_OFFLINE | DELETE gives 405. GET opens an SSE stream that does not end (did not complete within 5 s) | Long-lived idle GET streams on a free-plan instance; harmless but unbounded |
| Timeouts | NOT_IMPLEMENTED at the MCP layer | No request timeout in the bridge. Each tool has its own timeout arguments | — |
| Real ChatGPT / Claude.ai compatibility | **NOT_TESTED** | PHASE0: no `/oauth/authorize`, `/oauth/token` or `/oauth/register` request in the Render logs since 2026-11-07 18:00, and `oauth_*` row counts are 0 | WP-015 / D13 |

## 2. Findings

- **F-P1-1: Consent password brute force (no throttling).**
  - Where: `bridge/oauth.py:331-334`.
  - Repro: 60 POSTs to `/oauth/authorize` with wrong passwords returned 60×401 in 0.19 s. There is no lockout, delay, counter or IP limit.
  - Impact: guessing the single password gives full access to all 122 tools, including SX Bet read and browser. Only the strength of the password protects the service.
  - Note: a valid CIMD or DCR client is not even required, because the password is checked **before** the client is validated (line 333 runs before 339), so each guess costs one cheap request.
- **F-P1-2 (WP-007): Raw 500 on storage failure.**
  - Where: `oauth.py:378,388` (`consume_code` / `rotate_refresh_token`), `oauth.py:342` (`save_code`) and `oauth.py:359-360` (`_token_payload`) are all uncaught.
  - Repro: `probe_flow.py` and `probe_dbdown.py` with `DATABASE_URL` set to `127.0.0.1:1`. Token endpoint (both grants) gives 500 "Internal Server Error". Authorize POST with a valid client gives 500.
  - Also: `oauth.py:261-262` maps a DCR storage error to `400 invalid_request`. A DB outage therefore looks like a client error, and Claude will not retry.
- **F-P2-1 (WP-022): Refresh reuse is not detected and the token family is not revoked.**
  - Where: `oauth_store.py:99-110`.
  - Repro: refresh with RT1 gives RT2; replaying RT1 gives 400, but RT2 still gives 200 and AT1 is still valid.
  - Impact: a stolen refresh token that the attacker uses first keeps working indefinitely.
  - Related: rotation renews 30 days every time, so there is no absolute session lifetime.
- **F-P2-2: No revocation (`/oauth/revoke` returns 404), and rotating the password does not revoke tokens already issued.**
  - Recovery after a compromise requires a manual SQL `UPDATE`.
- **F-P2-3: Unauthenticated, unthrottled DCR writes.**
  - Where: `oauth.py:213-252` and `oauth_store.py:120-138`.
  - Repro: 25 POSTs to `/oauth/register` added 25 rows, each with a 730-day TTL.
  - Impact: anyone on the internet can grow `oauth_claude_dcr_clients` without bound on a 1 GB Aiven node, and each call opens a DB connection.
- **F-P2-4: No DB connect timeout and no pool on the auth path.**
  - Where: `oauth_store.py:17-18`.
  - Every authenticated `/mcp` request opens a fresh connection with no timeout. Against `max_connections=15`, concurrent tool calls plus the coordinators can exhaust connections. A blackholed DB hangs requests instead of returning 503.
- **F-P2-5: The MCP SDK is unpinned (`mcp[cli]>=2,<3`, `requirements.txt:6`).**
  - Exception-text hiding (the core of WP-010) and protocol negotiation depend on the installed SDK minor version.
  - Render builds whatever 2.x is latest at deploy time, so the build is not reproducible.
- **F-P2-6: `resource=…/mcp` is advertised, but the app serves `/mcp/`.**
  - An authenticated POST to `/mcp` gets 307. Locally the redirect `Location` is `http://…/mcp/`.
  - If production behaves the same (unverified: it depends on `--proxy-headers`/`forwarded_allow_ips`), a client that follows the redirect and re-sends `Authorization` would go over plain HTTP before Render redirects to HTTPS.
  - Verify with the first real-client test. The fix is to serve `/mcp` directly.
- **F-P2-7: No per-tool or per-component scopes, and no subject.**
  - `browser:use` authorizes SX Bet and API-FOOTBALL, which consume quota.
  - Every client shares every browser session, and nothing is recorded about which client acted (WP-018).
- **F-P3-1: Weak PKCE verifier validation.**
  - Repro: the verifier `"a"` is accepted (200). The verifier `"é"×43` raises `UnicodeEncodeError` and gives 500 (`oauth.py:270`).
  - RFC 7636 requires 43–128 characters from `[A-Za-z0-9-._~]`.
  - An empty `code_challenge` is accepted at authorize (the code is unusable, but a row is created).
- **F-P3-2: Scope handling.**
  - Arbitrary extra scopes are stored and echoed back (`oauth.py:297`; probe: `browser:use admin:all`).
  - A refresh token is issued without `offline_access`.
- **F-P3-3: The Bearer scheme is case-sensitive (`server.py:351`).**
  - RFC 6750/7235 schemes are case-insensitive, so this is a compatibility risk.
- **F-P3-4: The consent page has no `X-Frame-Options`/CSP (framing).**
  - The token response has no `Cache-Control: no-store` (probe: header absent).
- **F-P3-5: Token issuance is not atomic.**
  - The access token and refresh token are written in two transactions (`oauth.py:359-360`), after the code has already been consumed.
  - A failure in between leaves the code burned and an orphan access token.
  - Expired codes and tokens are never purged.
- **F-P3-6: CIMD hardening.**
  - The ChatGPT path is matched by prefix/suffix only, so `/oauth/../…/client.json` is not rejected.
  - The size cap applies after the whole body is buffered.
  - There is no cache, so every authorize GET and POST makes an outbound fetch with a 15-second timeout.
  - The host is trusted, so the risk is low.
- **F-P3-7: Authorize errors are JSON 400 instead of redirecting to the client.**
  - After the redirect is validated, RFC 6749 §4.1.2.1 expects a redirect carrying the error.
  - `redirect_uri + "?"` is also wrong if a registered redirect already contains a query (`oauth.py:353`).
- **Not found:**
  - Token or password leakage in logs (probe scan clean; logging limited to fixed codes).
  - Timing leak on the password or PKCE comparison (`compare_digest`).
  - Open redirect (the redirect must appear in the CIMD document or equal the pinned Claude callback; probe: evil redirect gives 400).
  - Code replay (blocked).
  - A route missing auth (only `/mcp` is protected, by design).
  - CIMD SSRF to arbitrary hosts.

## 3. Gap list for WP-007, WP-018 and WP-022, with acceptance criteria

WP texts at a8fb228 (`docs/certification/FCB_PR_WORKPACKAGES.md:28,39,43`); all three are D08 owner merges.

**WP-007** (fixed codes, DB-unavailable handling, PG persistence tests, rollback). Status: **NOT STARTED.** No code differs from the platform.md G5/G6 findings.

| # | Acceptance criterion |
|---|---|
| 1 | With each of `save_code`, `consume_code`, `save_access_token`, `save_refresh_token`, `rotate_refresh_token` and `valid_claude_dcr_client` raising `psycopg.OperationalError`, the endpoint returns **503** `{"error":"temporarily_unavailable"}` with `Cache-Control: no-store`. This covers authorize GET/POST, token (both grants) and register. No exception text appears in the body or the logs: only `oauth_storage_error stage=<fixed>`. RED today: 500 and 400 |
| 2 | A DCR storage failure is not reported as `invalid_request` |
| 3 | `_conn()` sets a bounded `connect_timeout` (and a statement timeout). A test with a blackholed address returns 503 within N seconds |
| 4 | `/mcp` 503 path covered by a test (currently untested) |
| 5 | New `tests/integration/test_oauth_store_postgres.py` on `TEST_DATABASE_URL`, run in both the PG16 and PG18 jobs. It covers: <br>• code single-use, expiry and binding <br>• concurrent `consume_code` (exactly one winner) <br>• access-token resource, scope, expiry and revocation <br>• refresh rotation and concurrent rotation <br>• DCR round-trip (moved from `tests/test_oauth_metadata.py:533`, which never runs in CI) |
| 6 | Token issuance is atomic: code consumption plus access and refresh inserts happen in one transaction, or the issue is explicitly compensated |
| 7 | A rollback note (revert SHA; no schema change) and a post-deploy probe showing 401 on `/mcp` and discovery 200 |

**WP-018** (sessions bound to the OAuth subject). Status: **NOT STARTED.** There is no subject anywhere: the tokens, `browser_session_leases` and the middleware all lack one.

| # | Acceptance criterion |
|---|---|
| 1 | Owner decision on what the "subject" is. With a single password it can only be per-client (`client_id`) or per-grant. Document it |
| 2 | Versioned migration `0009_*` adds a subject column to the token tables and to `browser_session_leases`, with a test on an already-initialized DB (§5), with no data loss and with existing rows handled explicitly |
| 3 | The middleware puts the validated subject into the request context, and the tools read it (needs a `Context` dependency) |
| 4 | Negative tests: client B cannot `stop`, `get_html`, `list` or `status` client A's session. `browser_list_sessions` is filtered. `stop_all` and `reap` are scoped or remain owner-only |
| 5 | Real PG regression plus a limited real MCP test with two clients (Claude and ChatGPT) |

**WP-022** (revocation, refresh reuse detection, consent throttling, docs, fixed codes, rollback). Status: **NOT STARTED.**

| # | Acceptance criterion |
|---|---|
| 1 | RFC 7009 `POST /oauth/revoke`, advertised as `revocation_endpoint`. Revoking a refresh token revokes its family and its access tokens. It returns 200 for unknown tokens and does not reveal whether a token exists |
| 2 | Refresh families (a `family_id` column, via migration). Reusing a rotated token revokes the whole family. Test: replaying RT1 makes RT2 and AT2 give 401 |
| 3 | An absolute refresh lifetime that rotation cannot extend |
| 4 | Consent throttling backed by PostgreSQL, so it survives restarts and works across workers: N failures per window, per IP and globally. Fixed 429 `{"error":"slow_down"}`. Test: the 61st wrong password within the window gets 429 even with the correct password |
| 5 | Throttle `/oauth/register` and cap the row count |
| 6 | Correct the README "Neon" reference (`README.md:34`) |
| 7 | Optional, owner decision: validate the PKCE verifier and challenge format; make the Bearer scheme case-insensitive; `Cache-Control: no-store` on the token response; frame protection on the consent page |

## 4. What a real-client E2E test must prove (WP-015 / D13)

For **each** of Claude.ai and ChatGPT, on the deployed SHA recorded from `/healthz.deploy_commit`, with evidence (sanitized Render log lines plus `oauth_*` row-count deltas):

1. **Discovery path used.** The log shows GET PRM and AS metadata, which also reveals whether the path-suffixed PRM was attempted and got 404. It also shows which registration path the client used: CIMD fetch, or `/oauth/register` (DCR) for Claude.
2. **Authorization works.** The consent page loads and the owner enters the password. The 302 carries `code`, `state` and `iss`. The token returns 200, and `oauth_access_tokens`/`oauth_refresh_tokens` each grow by 1 (hash only).
3. **The MCP handshake works.**
   - `initialize`: record the negotiated `protocolVersion`.
   - `notifications/initialized`.
   - `tools/list`: the count equals `/healthz.mcp_tools_total` (122).
   - It must also record whether the client posts to `/mcp` or `/mcp/`, and whether a 307 occurred and to which scheme (F-P2-6).
4. **`tools/call` is safe and has zero cost.** For example `apifootball_health` and an SX Bet read-only no-quota tool. The call returns a structured result, and a failing call (for example a disabled browser tool) returns `isError` with no exception text.
5. **Refresh works.** Force or wait past expiry (60 minutes) and observe `grant_type=refresh_token` returning 200 with rotation (old refresh hash `revoked_at` set). The client keeps working without re-consent.
6. **Restart recovery.** After a Render restart or spin-down (free plan), the same connector works without re-authorizing: tokens persist in PG and the transport is stateless.
7. **Negatives in the same session.**
   - `/mcp` without a token gets 401.
   - An invalid token gets 401.
   - Logs contain no token, code, password or DSN.
   - Errors are fixed codes.
8. **Cross-client view (WP-018 baseline).** Document that Claude and ChatGPT currently see the same sessions and tools (one subject). This is expected until WP-018 lands.
9. **Cleanup.** The owner revokes the test tokens. Today this needs SQL, because there is no revoke endpoint.

Everything above that is marked "probe" was observed on a local PG16 against a8fb228 code, **not** in production. Production has had zero OAuth or authenticated MCP traffic since DB initialization (PHASE0), so nothing beyond discovery and the 401 path is PASS_REAL.
