# Phase A: Browser Use / CloakBrowser reconciliation (read-only)

- Code basis: `origin/main` = 342fed6. The local HEAD is 11729cb (the PR head). `git diff --stat origin/main -- bridge tests docs evidence` is empty, so the browser code is identical.
- Matrix: `/tmp/claude-0/phaseA/browser.csv` has 53 rows: BR-001..BR-034 are the MCP tools, BR-035..BR-053 are cross-cutting.
- **Tool count: 34 `browser_*` tools.** I counted 34 `@mcp.tool` in `bridge/server.py`. The pinned list in `tests/test_mcp_tool_registry_observability.py` also asserts `len == 34`, so the old "34" claim is confirmed.
- How the status level was chosen: each row gets the highest level backed by evidence someone can reproduce. `docs/browser/README.md` claims a real test through Claude MCP → production on 2026-10-31 and 2026-11-01, but no artifact, SHA or log backs it. So no row is marked above INTEGRATION_VERIFIED. `evidence/` holds only api_football material.

## Offline test results (local venv, Python 3.13)

| File | Result |
|---|---|
| tests/test_browser_actions.py | 6 passed |
| tests/test_browser_create_no_capability_leak.py | 7 passed |
| tests/test_browser_listeners.py | 8 passed |
| tests/test_browser_reaper.py | 19 passed |
| tests/test_browser_route_shutdown.py | 8 passed |
| tests/test_cloak_bridge.py | 8 passed |
| tests/test_bridge_security.py | 7 passed |
| tests/test_mcp_offline_protocol_e2e.py | 2 passed |
| tests/test_mcp_tool_registry_observability.py | 5 passed |
| tests/integration/test_browser_leases_postgres.py | 5 skipped (no TEST_DATABASE_URL) |

**Total: 70 passed, 0 failed, 5 skipped.**

The 5 integration tests do run in CI. Workflow `.github/workflows/multi-market-tests.yml`, job `decision-postgres`, runs `pytest -q tests/integration` on postgres:16. It succeeded on PR head 11729cb (2026-11-02, pull_request event).

## Counts per level

### MCP tools (34)

| Level | Count |
|---|---|
| INTEGRATION_VERIFIED | 3/34 (list_sessions, session_status, reap) |
| MOCK_VERIFIED | 12/34 (create, create_cloak, open_url, get_dom, click, fill, type, get_network_responses, stop, stop_all_idle, health, cleanup_report) |
| IMPLEMENTED_UNTESTED | 19/34 (get_html, screenshot, press, select_option, set_checked, hover, focus, scroll, wait_for_selector, wait, back, forward, reload, get_elements, list_pages, new_page, switch_page, close_page, stop_all) |
| REAL_PROVIDER / REAL_MCP_CLIENT / PRODUCTION | 0/34 (README claims only, no artifact) |

Registration and schema of all 34 tools are OFFLINE_VERIFIED by the pinned catalogue test.

### Cross-cutting (19)

| Level | Count | Rows |
|---|---|---|
| INTEGRATION_VERIFIED | 3/19 | lease persistence, reaper, crash/restart recovery |
| OFFLINE_VERIFIED | 3/19 | SSRF literal/DNS checks, error masking (via SDK), registry |
| MOCK_VERIFIED | 7/19 | provider-aware stop, redirect post-check, CDP/liveUrl non-exposure, per-session lock, Cloak capacity, timeouts, provider contract |
| IMPLEMENTED_UNTESTED | 1/19 | no cdpUrl in logs |
| NOT_IMPLEMENTED | 5/19 | DNS rebinding, subresource SSRF, CGNAT/non-global ranges, client isolation, payload caps |

## Already done (verified in code and tests)

- **cdpUrl and liveUrl are never returned.** `liveUrl` is shown only when `BROWSER_USE_EXPOSE_LIVE_URL` is affirmative (PR #133 / a1ec9ed, tested).
- **Durable PostgreSQL leases:**
  - registered before the session is returned, with compensation (the browser is stopped) if registration fails;
  - renewed before and after every operation;
  - claim with `FOR UPDATE SKIP LOCKED`, `STOP_FAILED` retried with Retry-After backoff;
  - orphans reclaimed after a restart;
  - versioned migration `sql/migrations/0003_browser_session_leases.sql`.
- **Provider-aware stop.** Cloak sessions never call the Browser Use API. Browser Use stop requires the provider to confirm a terminal state. A wedged operation lock does not hang stop.
- **SSRF validator refuses:** loopback, RFC1918, link-local/metadata 169.254.169.254, IPv4-mapped IPv6, NAT64 (via `is_reserved`), 6to4, ULA, decimal/hex/short IPv4 forms, 0.0.0.0, `::`, multicast and broadcast. I probed each one offline on Python 3.11 and 3.13.
- **fill/type never echo the value.**
- **Crash messages are masked.** mcp 2.3.0 sends only `Error executing tool <name>` to the client for unexpected exceptions (probe `/tmp/claude-0/phaseA/probe_error_passthrough.py`).

## Security weaknesses (concrete)

### W1 (P0): post-redirect content retention
`open_url`, `new_page`, `back`, `forward` and `reload` check `page.url` only after navigating. When the check fails they raise, but the page stays loaded. `browser_get_html`, `browser_get_dom` and `browser_screenshot` do not re-check `page.url`, so they then return the private content.

- Probe `/tmp/claude-0/phaseA/probe_ssrf_offline.py` PROBE1 (fakes only):
  1. `open_url('https://public.example/redirect')` lands on `http://169.254.169.254/...` and raises UnsafeURL;
  2. `html()` then returns `url=http://169.254.169.254/... html=SECRET-METADATA`.
- Not verified offline: whether Playwright 1.63 `context.route` intercepts redirect hops as navigation requests (if it does, the guard aborts them earlier). Either way the post-hoc path is unsafe as written.
- Most impact is on Cloak, which runs Chromium on Render. For Browser Use, the "private" network belongs to the provider.

### W2 (P1): subresources are not checked
`_navigation_guard` checks only `is_navigation_request()` and passes fetch/xhr/img/script through unchecked. PROBE2 confirms that a fetch to `http://127.0.0.1:10000/internal` gets `continue`.

- With Cloak, a public page can reach the bridge itself or Render-internal hosts.
- Exfiltration paths: DOM text, screenshots, and `browser_get_network_responses`, which records URL and status for xhr/fetch (a port-scan oracle). Browser CORS and Private Network Access rules limit this partially.

### W3 (P1): DNS rebinding (TOCTOU)
`validate_public_http_url` resolves the name on Render, then Chromium resolves it again on its own (for Browser Use, on the provider's network). No resolution pinning is done. Only the final-URL check runs afterwards, and it suffers from W1.

### W4 (P2): CGNAT and other non-global ranges allowed
100.64.0.0/10 is allowed, for example `100.64.0.1` and `100.100.100.200` (Alibaba metadata). I confirmed this on Python 3.11 and 3.13. 192.88.99.1 is also allowed. The fix is to require `is_global`.

### W5 (P1): cdpUrl may reach the logs (untested)
`_page()` calls `connect_over_cdp(session.cdp_url)`. When an unexpected exception happens, the mcp SDK runs `logger.exception(...)`, which writes the traceback with the cause message. Playwright connect errors usually include the endpoint in their call log. The bridge sanitizes nothing here. No test covers it, so the status stays IMPLEMENTED_UNTESTED.

### W6 (P2): Cloak capacity race
`create_cloak` counts sessions under the lock, releases it, then launches (up to 45s) before inserting. Probe `/tmp/claude-0/phaseA/probe_cloak_capacity_race.py`: with `MAX_LOCAL_SESSIONS=1`, two concurrent creates give 2 sessions. This is a memory risk on Render.

### W7 (P2): no isolation between clients
Any authenticated OAuth client can drive, stop or list any `session_id`. Transport is `stateless_http` and there is no subject binding. OAuth is single-owner today, which limits the impact.

### W8 (P2): output and error hygiene
- `browser_get_html` and full-page `browser_screenshot` are uncapped.
- `browser_stop_all` returns `str(exc)` to the client.
- Error masking depends on the SDK version (`mcp[cli]>=2,<3` is not pinned) rather than on fixed bridge codes (§2.1).

### W9 (P2): orphans cannot be stopped explicitly
After a restart, `browser_stop` on a session that is not in memory raises KeyError. Only the reaper can clean it, and only after the session is idle ≥300s and its lease has expired.

### Not a gap
The Browser Use v4 endpoints are not cited from official docs anywhere in the repo, so their status is DOCS_UNVERIFIED. They are mocked only, and I did not call the provider.

## Proposed work packages

### WP-BR-1 (P0): SSRF fail-closed after a rejected navigation
- **Gap:** W1.
- **Files:** `bridge/browser_use.py`; new `tests/test_browser_ssrf_post_navigation.py`.
- **RED:** after `open_url`, `new_page`, `back`, `forward` or `reload` lands on a private `final_url`:
  - `html`, `dom_text`, `screenshot`, `elements` and `list_pages` must not return the content;
  - today `html` returns it (PROBE1).
- **GREEN:**
  - on rejection, navigate the page to `about:blank` (or close it) and mark the session tainted;
  - every read tool re-validates `page.url` (allowing only `about:blank`) before returning;
  - tests for every navigation tool.
- **Real test:** limited local-Chromium test, offline (a localhost server redirecting to 127.0.0.1) to settle whether route sees redirect hops, plus one Cloak smoke in production after merge.
- **Owner:** none beyond a normal merge (it does not weaken any protection).
- **DONE:** RED→GREEN, CI green, deploy healthy, #130 updated.

### WP-BR-2 (P1): request-level guard and DNS pinning
- **Gap:** W2, W3 and W4.
- **Files:** `bridge/security.py`, `bridge/browser_use.py:_navigation_guard`, `tests/test_bridge_security.py`, `tests/test_browser_route_shutdown.py`.
- **RED:**
  - a non-navigation request to 127.0.0.1 gets `continue` (PROBE2);
  - 100.64.0.1 is allowed;
  - a host resolving to a public address and then a private one passes.
- **GREEN:**
  - validate every routed request (cache resolved hosts with a TTL);
  - require `is_global`;
  - for Cloak, add Chromium host-resolver pinning or an egress proxy and document the residual risk;
  - parametrized IPv6/encoded cases added to the tests.
- **Real test:** limited Cloak smoke on example.com plus one public page that makes XHRs, to check for no regression.
- **Owner:** decision only if an egress proxy adds cost.
- **DONE:** as in WP-BR-1.

### WP-BR-3 (P1): no CDP in logs or errors
- **Gap:** W5 and W8 (errors).
- **Files:** `bridge/browser_use.py:_page`, `bridge/server.py`, new test.
- **RED:** make `connect_over_cdp` raise a Playwright `Error` whose message contains `wss://x.cdp...`, then assert caplog contains neither "cdp" nor "wss".
- **GREEN:**
  - wrap CDP connect and re-raise a fixed-code error (`BROWSER_CONNECT_FAILED`) with no cause chain (`from None`);
  - map browser errors to fixed codes;
  - `stop_all` returns `type(exc).__name__` or a code;
  - pin the mcp version, or test the masking contract.
- **Real test:** none; check the Render logs after deploy.
- **Owner:** none.

### WP-BR-4 (P2): tests for the 19 untested tools
- **Gap:** BR-004/006/010..020/021..025/031.
- **Files:** new `tests/test_browser_tools_offline.py` (fakes like those in `test_browser_listeners.py`).
- **RED:** bounds and validation for each tool (values 0/51, page_index out of range, closing the last page, invalid state), and `elements` never returning input values.
- **GREEN:** all pass with no change to the code; fix only if a real bug appears.
- **Real test:** no. **Owner:** none.

### WP-BR-5 (P2): Cloak capacity atomic, and payload caps
- **Gap:** W6 and the BR-051 caps.
- **Files:** `bridge/browser_use.py`.
- **RED:**
  - the concurrent-create probe gives 2 sessions with max=1;
  - an `html` call with 2 MiB of content is returned whole.
- **GREEN:**
  - reserve a slot under the lock before launch, release it on failure;
  - `html` and `screenshot` get a byte cap with an explicit `truncated` / `PARTIAL` marker and no silent truncation.
- **Real test:** no. **Owner:** product decision on the cap values.

### WP-BR-6 (P2): session ownership by OAuth subject, explicit orphan stop
- **Gap:** W7 and W9.
- **Files:** `bridge/session_leases.py`, a new migration `sql/migrations/0009_*` adding a subject column (needs a semantic ownership review per §5), `bridge/server.py`, tests.
- **RED:** client B stops client A's session and succeeds.
- **GREEN:** scoped operations; `browser_stop` claims durable orphans.
- **Real test:** Postgres integration test plus a limited real MCP test.
- **Owner:** **required**. It is a substantial authorization change plus a schema migration.

### WP-BR-7 (P2): reproducible real evidence
- **Gap:** every tool is below REAL_*; the README claims have no artifact.
- **What:**
  - a limited, owner-authorized production smoke (create→open example.com→get_elements→stop, plus a 127.0.0.1 refusal) per provider;
  - commit a dated artifact under `evidence/browser/` with SHA, deploy id, tool results and session stop confirmation.
- **Owner:** authorization for the Browser Use cost and the run.
- **DONE:** rows upgraded to REAL_MCP_CLIENT_VERIFIED / PRODUCTION_VERIFIED with an evidence path.

## Probe scripts (offline, fakes only, no browser or network)

- `/tmp/claude-0/phaseA/probe_ssrf_offline.py`
- `/tmp/claude-0/phaseA/probe_error_passthrough.py`
- `/tmp/claude-0/phaseA/probe_cloak_capacity_race.py`
