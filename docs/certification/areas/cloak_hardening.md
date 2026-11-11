# CloakBrowser hardening (#189) — evidence, memory, owner canary

Base: `main` 475dae8. Branch: local only, not pushed, not deployed. Nothing in this PR was applied to
Render, the production MCP or API-FOOTBALL. `CLOAKBROWSER_ENABLED` is **not** enabled by this PR.

Status words: **VERIFIED** (reproduced here, RED before / GREEN after where it applies), **ASSUMED**
(reasoned, not reproduced), **BLOCKED** (cannot be verified here; the blocker is named).

## 0. Conflict to resolve first (owner)

The last line of #189 (owner, 2026-11-11) says `CLOAKBROWSER_LICENSE_KEY` was added on Render and
`CLOAKBROWSER_ENABLED=true` is set. The mandate for this PR says `CLOAKBROWSER_ENABLED` stays false in
production. The agent could not check Render (no Render calls allowed). If the flag really is `true`
on the current `main` (475dae8), then, whenever `BROWSER_TOOLS_ENABLED` is also on:

- Chromium is launched by cloakbrowser's stealth defaults, which carry `--no-sandbox` (VERIFIED by
  reading cloakbrowser 0.5.11 `config.py` and by the RED probe in §3);
- the browser inherits the bridge's whole environment (`DATABASE_URL`, OAuth, API-FOOTBALL, SX Bet
  keys) and runs as the bridge's own uid (VERIFIED, RED probe);
- with a licence key present, cloakbrowser 0.5.11 validates it with cloakbrowser.dev and downloads
  the **latest** Pro build, dropping any version pin for free keys (VERIFIED by reading
  `download.py`): the production binary is then whatever upstream serves that day;
- `prepare_cloak_runtime` ignores `BROWSER_TOOLS_ENABLED` and a download failure crashes startup.

Conservative action (CLAUDE.md §0): **set `CLOAKBROWSER_ENABLED=false` on Render now** (no deploy
needed beyond the env restart) and keep it false until the canary in §5 passes. After this PR is
merged, the native runtime refuses Cloak anyway (`CLOAK_ISOLATION_UNAVAILABLE`, §2 req. 2), but the
flag should still be off until the owner decides.

## 1. What the PR changes

- `bridge/browser_use.py`
  - `prepare_cloak_runtime`: does nothing unless `BROWSER_TOOLS_ENABLED` **and** `CLOAKBROWSER_ENABLED`
    are explicitly on; never raises (a failure leaves Cloak disabled with a fixed code in
    `browser_health.providers.cloak.disabled_code`); verifies the binary's SHA-256 against a pin;
    removes `CLOAKBROWSER_LICENSE_KEY` from the process environment (only the `licensed` profile hands
    it to cloakbrowser, which injects it into the browser process alone); forces
    `CLOAKBROWSER_AUTO_UPDATE=false`; scrubs the Playwright driver's environment; makes the bridge
    non-dumpable.
  - Launch: never `--no-sandbox` (`stealth_args=False` + the same fingerprint flags), `chromium_sandbox=True`,
    allowlisted environment, one combined `--disable-features` with `IsolateSandboxedIframes`,
    `--renderer-process-limit=1`, `--js-flags=--max-old-space-size=128`.
  - After every launch, `cloak_process_audit` proves from `/proc` that every renderer runs under
    seccomp-BPF (`Seccomp: 2`, `NoNewPrivs: 1`) in a nested PID namespace, that no Chromium process carries
    `--no-sandbox`, and that every Chromium process runs as a uid that is neither root nor the
    bridge's. Any failure closes the browser and latches Cloak off for the process lifetime.
  - At most **one** local session, reserved under the lock before the launch (fixes BR-048);
    `CLOAKBROWSER_MAX_LOCAL_SESSIONS` no longer has any effect.
  - Memory: a Cloak full-page screenshot over 16 Mpx is refused (`SCREENSHOT_TOO_LARGE`, never clipped);
    a watchdog kills Chromium when the container's non-reclaimable memory exceeds 448 MB
    (`CLOAKBROWSER_MEMORY_KILL_MB` can only lower it).
  - #188: `BrowserRefusal`/`UnsafeURL` carry fixed codes; `browser_error_code` maps every failure.
- `bridge/security.py`: `UnsafeURL` carries a fixed code (`URL_SCHEME_NOT_ALLOWED`, ...). Messages and
  rules unchanged.
- `bridge/server.py`: every `browser_*` tool reports failures as `ToolError(<CODE>)` (input schemas
  byte-identical, VERIFIED by diffing `tools/list`). `prepare_cloak_runtime` no longer raises.
- `deploy/cloak-uid-split/` (PREPARED, NOT APPLIED): Dockerfile and `cloak-launch`.
- `scripts/cloak_uid_split_lab.sh`: reproduces the image layout on a host for the uid-split tests.
- CI job `cloak-hardening-chromium` (multi-market-tests.yml): full Chrome + Cloak 146, sandbox on,
  non-root runner, plus the uid-split lab. ASSUMED until it runs on GitHub (not pushed).

## 2. Per-requirement evidence

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Real, active sandbox | **VERIFIED (lab)** / BLOCKED on Render | Cloak 146 and full Chrome 1243, non-root, sandbox on: `chrome://sandbox` "adequately sandboxed" (namespace layer 1, PID+net namespaces, seccomp-BPF + TSYNC); `/proc` audit: renderers `Seccomp: 2`, `NoNewPrivs: 1`, nested `NSpid`. RED control: the same binary with `--no-sandbox` fails the audit (`CLOAK_SANDBOX_UNAVAILABLE`). Offline audit table (8 negative cases) + 5 audit mutants killed. Render: unknown whether its containers allow unprivileged user namespaces → canary §5 step C4. |
| 2 | No access to bridge files, credentials, secrets | **VERIFIED (lab, uid split)** / BLOCKED on Render (Docker switch) | Browser/driver environment allowlisted (secret markers absent from the real browser `environ`); licence never in env for the keyless profile. uid split (`tests/test_cloak_uid_split_e2e.py`, Cloak 146 and full Chrome): as uid `cloak`, `cat /proc/<bridge>/environ` fails, a `0600` bridge secret file is unreadable, the code is not writable; all Chromium processes run as `cloak`. Same uid (native Render runtime) is **refused** (`CLOAK_ISOLATION_UNAVAILABLE`, real-binary test): env scrubbing alone cannot stop a same-uid compromised browser from rewriting the bridge's code. |
| 3 | SSRF on navigation, redirects, iframes, WebSockets | **VERIFIED** with named residuals | Full Chrome with production Cloak options: capability suite `lna_off` 51 passed / 6 xfailed; SSRF suites `native` 92 passed / 15 skipped. Cloak 146: `native` 90 passed + 2 binary-specific failures, `lna_off` 49 passed + 3 binary-specific (see §3.2), plus a binary-independent WebSocket test (nothing reaches internal on either binary). uid split: redirect to an internal host → `NAVIGATION_REDIRECT_BLOCKED`, iframe/img to internal never fetched. **Residuals unchanged (WP-002b): DNS rebinding (strict xfail on the local path too) and speculation-rules on non-Cloak binaries need an egress proxy.** |
| 4 | Fail-closed kill switch | **VERIFIED** | `prepare` does nothing with either flag unset/false/typo (18 parametrised cases, RED on HEAD: it called `ensure_binary` with `BROWSER_TOOLS_ENABLED` off); a prepare failure no longer escapes (RED on HEAD: `OSError` escaped the lifespan); `/healthz` serves with a broken Cloak runtime; any launch error latches Cloak off with a fixed code. Mutants: "prepare ignores BROWSER_TOOLS_ENABLED", "prepare re-raises", "launch not latched" all killed. |
| 5 | At most one local session | **VERIFIED** | 4 concurrent creates → 1 launch, 3 × `CLOAK_CAPACITY_REACHED` (RED on HEAD: 4 launches with `MAX_LOCAL_SESSIONS=1`); env values 1/4/999/junk all give 1; a stopping session with a live browser holds the slot. Mutant "capacity ignores pending" killed. |
| 6 | Memory/process limits on Render Free (512 MB) | **PARTIAL** — measured, see §4; Render accounting ASSUMED | Light and typical pages fit (peaks 293 MB and 433 MB of 512); a tall full-page screenshot measured 1.8 GB → now refused; a heavy page drove the container to the limit → watchdog kills Chromium (peak 476 MB, bridge alive). Nothing bought. |
| 7 | Lifecycle: cleanup, restart, crash, rollback | **VERIFIED (lab)** / rollback ASSUMED | uid split: stop leaves 0 Chromium processes and removes the launcher's profile; `kill -9` of the browser → fixed code, slot held until stop, then a new session works; watchdog kill → session disconnected, bridge alive. Restart clears the latch (state is per process). Rollback (§5.R) is a procedure, not executed on Render. |
| 8 | Compatibility (OAuth, API-FOOTBALL, SX Bet, Aiven, Browser Use) | **VERIFIED offline** / live NOT_TESTED | Full unit suite green (§3.1); browser tool schemas byte-identical; OAuth middleware untouched; no DB/migration change; Browser Use SSRF suites green on both profiles. No live call made (mandate). |
| 9 | #188 fixed codes, both providers | **VERIFIED offline** / live NOT_TESTED | `tests/test_browser_error_codes.py`: 43 tests, **42 RED on HEAD** (the client got the bare "Error executing tool browser_open_url"), all GREEN: ws/wss/file/ftp/javascript/chrome → `URL_SCHEME_NOT_ALLOWED`; private/metadata → `URL_NON_PUBLIC_DESTINATION`; credentials, host, unresolvable, kill switch, Cloak disabled/not ready, unknown session, invalid argument, redirect/document/content refusals; an unexpected exception → `BROWSER_INTERNAL_ERROR` without its text. Every `browser_*` tool is wrapped (checked). |

### Chromium 156 / cloakbrowser 0.7.x

**BLOCKED** (no licence key here, by design). Read from the cloakbrowser 0.7.2 wheel (PyPI):
`ensure_binary` claims a free key when none is configured, a free key is always served the latest
build (version pins are dropped), and the stealth defaults still carry `--no-sandbox` (neutralised here
by `stealth_args=False`). 0.5.11 also downloads the latest Pro build when a key is set. So "Chromium
156" cannot be pinned upstream: the PR adds a `licensed` profile that accepts a binary only when its
SHA-256 equals `CLOAKBROWSER_BINARY_SHA256` set by the owner; a mismatch logs the observed hash and
fails closed. `requirements.txt` stays on `cloakbrowser==0.5.11` (PR #190 touches it for tzdata only).
Not verified on 156: Playwright runtime bindings (`route_web_socket`), sandbox, WebSocket guard, the
binary's own calls to cloakbrowser.dev (browser-process traffic is not seen by the CDP request
guard). Under the uid split the licensed binary would also need to be root-owned under `/opt/cloak`
(the launcher refuses anything else), i.e. fetched at image build time with the key: an owner
decision about build-time secrets. Canary §5.L lists the checks.

## 3. Test evidence

### 3.1 Suites (local, 2026-11-11)

| Run | Result |
|---|---|
| Full unit suite, HEAD 475dae8 (baseline) | 1887 passed, 480 skipped, 6 xfailed |
| Full unit suite, branch | 1998 passed, 490 skipped, 6 xfailed |
| `test_cloak_launch_hardening.py` (offline, root) | 68 passed, 3 skipped (non-root with a real binary: 71 passed) |
| same, real binary, non-root uid (`-k real`), Cloak 146 / full Chrome | 3 passed / 3 passed |
| `test_browser_error_codes.py` | 43 passed (HEAD: 42 failed, 1 passed) |
| uid-split lab (`scripts/cloak_uid_split_lab.sh`), Cloak 146 / full Chrome | 4 passed / 4 passed |
| Capability suite `lna_off`, full Chrome with Cloak options | 51 passed, 6 xfailed |
| SSRF suites `native` (redirect, subresources, capability, OAuth consent), full Chrome | 92 passed, 15 skipped |
| Capability suite `lna_off`, Cloak 146 | 49 passed, 5 xfailed, 3 binary-specific failures (§3.2) |
| SSRF suites `native`, Cloak 146 | 90 passed, 15 skipped, 2 binary-specific failures (§3.2) |
| Spot mutations (18 mutants of kill switch, sandbox/uid audit, capacity, launch options, pin, licence, error codes) | 18/18 killed |

RED/GREEN probe of the Cloak findings (an uncommitted agent probe run on both trees):

```
HEAD   {'prepare_with_tools_off_called_ensure_binary': True, 'prepare_failure_escapes': 'OSError',
        'concurrent_launches_with_max_1': 4, 'no_sandbox_in_args': True, 'chromium_sandbox': None,
        'browser_env': 'inherits the bridge environment'}
BRANCH {'prepare_with_tools_off_called_ensure_binary': False, 'prepare_failure_escapes': False,
        'concurrent_launches_with_max_1': 1, 'no_sandbox_in_args': False, 'chromium_sandbox': True,
        'browser_env': 'allowlisted'}
```

### 3.2 Cloak 146 binary-specific outcomes (not bridge regressions)

| Test | On Cloak 146 | Why it is not a weakening |
|---|---|---|
| `test_positive_controls[local]` | `new WebSocket()` throws `TypeError` instead of a CSP violation | The socket is still refused; Playwright's Runtime bindings are broken on this binary (prior finding). `test_cloak_websockets_never_reach_internal_whatever_the_binary_does` passes on both binaries. |
| `test_websocket_guard_allows_public_and_closes_internal` | `Page.evaluate: TypeError ... _globalBindingName is not a function` | Same cause; nothing reaches the internal host. Public WebSockets therefore do **not** work on Cloak 146. |
| `test_speculation_rules_residual_is_still_open` (strict xfail) | XPASS | The binary issues no speculation-rules request; the residual stays open on other binaries. |
| `test_control_webtransport_and_webrtc_fire_on_the_local_path_without_the_guards` | WebRTC STUN does not fire even with the guards off (WebTransport does) | The RED control is vacuous for WebRTC on this binary; the GREEN test passes and the P1 RED control with Cloak options passes on full Chrome. |

The CI job deselects exactly these four on the Cloak binary only; they run on full Chrome.

## 4. Memory (bridge + Cloak, one session)

Method: `scripts/cloak_memory_lab/` (lab only: `run_measure.sh <repo> <binary> light|typical|heavy 512`) in a cgroup-v1 memory group limited to 512 MB, the
whole bridge imported with API-FOOTBALL and SX Bet registered (122 MCP tools), Chromium under the
uid split. Session = create, open page, read DOM, type, click, screenshot, `get_html`, second tab,
`get_elements`, stop; three rounds each. Pages served locally (no external site): **light** (20-row
page), **typical** (3 000-row odds table + 2 000 scripted nodes), **heavy** (20 000 rows, 40 000
nodes, 4 M-element JS array). Figures are cgroup `max_usage_in_bytes` (MB).

| Phase | MB |
|---|---|
| Bridge alone, all components loaded | 65 |
| + Cloak runtime prepared | 69 |
| + Cloak session created, idle (`about:blank`) | 250–254 |
| Light page session, peak | 293 |
| Typical page session, viewport screenshot, peak | 433 |
| Typical page, full-page screenshot (before the cap) | 1 814–1 837 (4 GB group); `Target crashed` in 512 MB |
| Typical page, full-page screenshot (after) | refused `SCREENSHOT_TOO_LARGE`, peak 432 |
| Heavy page, before the watchdog | reached 512 (renderer crash, bridge alive) |
| Heavy page, watchdog 448 MB / 0.25 s | peaks 464 / 471 / 508 |
| Heavy page, watchdog 448 MB / 0.1 s (shipped) | peak 476.5, Chromium killed 3/3, bridge alive |
| After stop | 75–82, 0 Chromium processes |

Per process (PSS, typical session): bridge Python 85–135 MB, Playwright node driver 82–145 MB;
Chromium processes run as another uid and are not PSS-readable from the bridge.

Conclusion: light and typical pages fit 512 MB with 80–220 MB headroom; heavy pages do not, and are
contained only by the watchdog (a hard poll, not a guarantee). How Render counts memory and whether it
restarts the service at 512 MB is **ASSUMED** (not measured on Render). No upgrade was bought; if the
owner wants arbitrary sites on Cloak, the options are a paid instance or keeping Cloak for light pages.

## 5. Owner canary on Render (before `CLOAKBROWSER_ENABLED=true`)

Pre-requisites: PR merged and deployed on the current native runtime with `CLOAKBROWSER_ENABLED=false`;
deploy healthy (§3 of CLAUDE.md checks). Write down the current Render **Build Command**, **Start
Command**, runtime and the previous deploy id: they are the rollback.

**A. Native runtime after merge (flag off)**
1. `/healthz`: `ok`, `deploy_commit` = merge SHA, `cloak_enabled=false`, tool counts unchanged.
2. `/mcp` without token → 401.
3. `browser_open_url` on a Browser Use session with `wss://echo.websocket.org` → error text ends with
   `URL_SCHEME_NOT_ALLOWED`; `http://169.254.169.254/` → `URL_NON_PUBLIC_DESTINATION`.
4. `apifootball_health`, `sxbet_health`, an OAuth login: unchanged.

**B. (optional) Native runtime refuses Cloak** — set `CLOAKBROWSER_ENABLED=true`, restart:
1. logs: `CloakBrowser runtime prepared: profile=pinned-146` (binary SHA-256 verified) or
   `CLOAK_DISABLED code=...` (then stop: report the code);
2. `browser_create_cloak` → `CLOAK_ISOLATION_UNAVAILABLE` (same uid refused), `browser_health`
   shows `disabled_code`; set the flag back to `false`.

**C. Docker uid split** (production change; owner only). DOCS_UNVERIFIED: whether Render lets an
existing native service switch to Docker in place. If not, the canary needs a separate free service
(free-plan hours are shared: owner decision).
1. Runtime Docker, Dockerfile path `deploy/cloak-uid-split/Dockerfile`, context = repo root; keep
   `CLOAKBROWSER_ENABLED=false`. Build log must show `sha256sum: ... OK` for the Cloak binary.
2. `/healthz` and A.1–A.4 again (the image changes the runtime for every component).
3. Set `BROWSER_TOOLS_ENABLED=true`, `CLOAKBROWSER_ENABLED=true`; do not set
   `CLOAKBROWSER_RUNTIME_PROFILE` (default `pinned-146`). Restart.
4. `browser_health.providers.cloak`: `ready=true`, `disabled_code=null`, `profile=pinned-146`.
   Then `browser_create_cloak`:
   - success → `browser_health.providers.cloak.last_sandbox_audit` must show `separate_uid: true`,
     `renderers >= 1`;
   - `CLOAK_SANDBOX_UNAVAILABLE` → Render refuses user namespaces: STOP, flag off. The only fallback
     is a root-owned SUID `chrome-sandbox` helper in the image (owner decision; never `--no-sandbox`);
   - `CLOAK_ISOLATION_UNAVAILABLE` → the launcher or the ambient capabilities are not in effect: STOP.
5. Typical session: `browser_open_url https://example.com/`, `browser_get_dom`, type and click on
   `https://httpbin.org/forms/post` (do not submit), `browser_screenshot full_page=false`, then
   `browser_stop`.
6. Refusals: `wss://...` → `URL_SCHEME_NOT_ALLOWED`; `http://169.254.169.254/latest/meta-data/` →
   `URL_NON_PUBLIC_DESTINATION`; `https://httpbin.org/redirect-to?url=http://169.254.169.254/` →
   `NAVIGATION_REDIRECT_BLOCKED`; a second `browser_create_cloak` while one is open →
   `CLOAK_CAPACITY_REACHED`.
7. Memory: Render metrics during the session stay under 448 MB; logs contain no `CLOAK_MEMORY_LIMIT`
   and no OOM/restart event. `browser_cleanup_report`: `local_sessions=0` after stop.
8. Compatibility again: OAuth login, `apifootball_health`, `sxbet_health`, a Browser Use session.
9. Record everything in #130 (VERIFIED/ASSUMED) before leaving the flag on.

**L. Licensed profile / Chromium 156** (only after C passes; requires a decision on supplying the key
at image build time, see §2). Set `CLOAKBROWSER_RUNTIME_PROFILE=licensed` and
`CLOAKBROWSER_BINARY_SHA256` to 64 zeros once: the bridge downloads the licensed build and refuses it,
logging `CLOAK_BINARY_UNVERIFIED observed_sha256=<hash>`. Pin that hash only after checking: launch
works through `browser_create_cloak`; `route_web_socket` works (a public `wss://` test page opens only
if the owner decides to allow public WebSockets, otherwise `URL_SCHEME_NOT_ALLOWED`/CSP refusal);
`last_sandbox_audit` as in C.4; the SSRF steps of C.6; memory as in C.7. Any failure: back to
`pinned-146`.

**R. Rollback**
- Instant: `CLOAKBROWSER_ENABLED=false` (or `BROWSER_TOOLS_ENABLED=false`) and restart: Cloak is
  never prepared or launched; nothing else changes.
- Docker switch: set the runtime back to the native Python runtime with the Build/Start commands
  recorded before C, or redeploy the previous deploy id.
- Code: revert the merge commit (no migration, no persisted state: the latch and the session slot
  live in process memory; durable leases of `cloak-*` sessions are cleaned by the reaper as before).

## 6. Residuals and limits

- DNS rebinding and the WP-002b remote-path residuals are unchanged; they need an egress proxy.
- Public WebSockets stay refused on Cloak (DOCUMENT_CSP; broken bindings on Cloak 146).
- The memory watchdog polls every 0.1 s: a faster allocation spike can still reach the limit.
- `PR_SET_DUMPABLE=0` applies to the whole bridge process once Cloak is prepared (no core dumps).
- In the image, the bridge and its Playwright driver hold CAP_SETUID/SETGID/KILL (ambient) so the
  launcher can start Chromium as `cloak` and the watchdog can kill it; Chromium itself holds none.
- On a licence denial, cloakbrowser's status file lives in the bridge's home; under the uid split
  Chromium cannot write it, so a denial surfaces as `CLOAK_LAUNCH_FAILED` without detail.
- Not covered: Render's real user-namespace policy, Render's memory accounting, the Docker build on
  Render (no registry access here), the CI job (not pushed).
