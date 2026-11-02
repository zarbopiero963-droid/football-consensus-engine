# Browser SSRF — reproducible proof (FCB-TOTAL-RECONCILIATION-CERTIFICATION-20261102)

Code: `main` 342fed6 (`bridge/browser_use.py`, `bridge/security.py`). Chromium 153 local, **offline**.
Two local HTTP servers: "public" `127.0.0.2:8001` (stand-in for an attacker site) and "internal"
`127.0.0.1:8002` (stand-in for `169.254.169.254` / RFC1918, serving a secret marker).

What the harness runs:
- **Real code:**
  - `_navigation_guard`;
  - the pre-checks and post-checks of `open_url`;
  - the read tools: `html`, `dom_text`, `screenshot`, `elements`, `network_responses`;
  - the history tools: `back`, `forward`, `reload`;
  - the `@session_operation` decorator.
- **Stubbed:**
  - `session_leases.register/renew`, replaced with no-ops (no PostgreSQL);
  - `validate_public_http_url`, wrapped so that `127.0.0.1` counts as forbidden and `127.0.0.2` as public. This is the same rule the real validator applies to a real metadata or RFC1918 address.

Reproduced twice: once by the agent and once independently by the main session, on 2026-11-02.

## Matrix
| # | Scenario | Internal content readable through the MCP tools? | Verdict |
|---|---|---|---|
| 1 | `open_url` (public 302 → internal) | **YES**: after `UnsafeURL`, `get_html`/`get_dom`/`get_elements`/`screenshot` return the internal page | **CONFIRMED – Critical** |
| 2a | chain public→public→internal | **YES** | **CONFIRMED – Critical** |
| 2b | meta refresh → internal | no (the guard blocks the new navigation) | defended |
| 2c | JS `location=` → internal | no | defended |
| 2d | `window.open` popup → internal | no (`switch_page` re-validates) | defended |
| 3a | iframe document → internal | no (a subframe document is a navigation request, so the guard aborts it) | defended |
| 3b | `fetch()`/XHR → internal | **YES**: the content is reflected in the DOM and read with `get_html`/`get_dom`. `network_responses` exposes the internal status and headers, which works as a port-scan oracle | **CONFIRMED – Critical** |
| 3c | `<script>`/CSS/img → internal | **YES**: the internal script runs and the secret ends up in the DOM | **CONFIRMED – High** |
| 3d | WebSocket `ws://internal` | TCP connection oracle (the guard calls `continue_()` on everything that is not a navigation) | **CONFIRMED – Medium** |
| 4 | back/forward/reload into an internal history entry | no (ERR_BLOCKED_BY_CLIENT, then `page.url` re-validated) | defended |
| 5 | DNS rebinding | `security.py` resolves once and returns the string; the browser resolves again on its own (TOCTOU) | **design gap – High** |

## Root cause
- **A, redirect (items 1 and 2a).**
  - `_navigation_guard` validates only the first request. On a 3xx, Chromium follows the redirect without calling the route handler again. The guard saw only `/redirect-302`, yet the browser loaded the internal page.
  - `open_url` raises `UnsafeURL` on `final_url`, but it does not unload the page.
  - The read tools never re-validate `page.url`.
- **B, sub-resources (items 3b, 3c and 3d).** `_navigation_guard` checks only requests with `is_navigation_request()`. fetch, xhr, img, script, css and websocket requests are let through with `continue_()` unconditionally.

## Where the risk applies
- **Browser Use cloud** (default provider): Chromium is remote. "Internal" means the Browser Use cloud network.
- **CloakBrowser** (`CLOAKBROWSER_ENABLED`, off by default): Chromium runs **inside the Render container**. "Internal" means the Render metadata service, link-local addresses, localhost, and the private network reachable from the dyno.
- **Today:** `/mcp` answers 503 to any authenticated request (empty `defaultdb`), so the tools cannot be reached. **Once `defaultdb` is restored**, any authorized OAuth client can trigger the vulnerability. That includes a model steered by prompt injection from a visited page.

## Reversible stop-gap, no deploy (owner decision; not applied)
- Remove `BROWSER_USE_API_KEY` on Render, so `browser_create` fails.
- Keep `CLOAKBROWSER_ENABLED` off.
- No new session can then be created. The read tools stay registered, but there are no sessions to read.

## Code mitigation (WP-001 / WP-002)
1. On a rejected navigation, fail closed: go to `about:blank` and invalidate the session.
2. Re-validate `page.url` and every `frame.url` at the start of every read tool.
3. Handle the redirect chain in the route handler and validate every hop (WP-001).
4. Validate **every** request (any `resource_type`, WebSocket included) against the resolved IP, and block service workers (WP-002).
5. **Cloak only:** pin DNS resolution with `--host-resolver-rules` or a proxy (WP-010).
6. Add a `BROWSER_TOOLS_ENABLED` gate in `server.py`, matching `APIFOOTBALL_ENABLED` and `SXBET_ENABLED`.
