# Protected MCP E2E on the real Render deployment (`fcb-mcp-e2e`)

Workflow: `.github/workflows/fcb-mcp-e2e.yml`. Client: `scripts/mcp_e2e_render.py`.
Offline proof: `tests/test_mcp_e2e_render_offline.py`.

## Access

- **Trigger and branch:** `workflow_dispatch` only, and the job runs only from `refs/heads/main`.
- **Gates before the secret:**
  - `scripts/require_owner_main_dispatch.sh`: the actor and triggering actor must both be the repository owner;
  - `scripts/require_protected_environment.sh … mcp-e2e`: required reviewer plus a deployment-branch policy for `main` only.
- **Approval:** a run starts only after the owner approves the Environment `mcp-e2e`.
- **Secret scope:** `MCP_OAUTH_PASSWORD` exists only in that Environment and reaches only the E2E step.

## What a run does

The target is pinned to `https://football-consensus-browser-bridge.onrender.com`; the client refuses any other host. Each run, in order:

1. **Health and discovery.** Checks `/healthz`: the deployed commit must equal the `main` HEAD that dispatched the run, and Browser and Cloak must be off. Then reads the OAuth discovery metadata.
2. **`/mcp` negatives.** Each must return 401 with resource metadata:
   - no token;
   - malformed bearer;
   - unknown bearer.
3. **DCR registration.** A foreign redirect is refused; a registration with the Claude callback is accepted.
4. **Consent page headers.** Checks CSP, X-Frame-Options and no-store. The redirect is never followed.
5. **Wrong password.** Exactly one wrong password, which must return 401.
6. **Authorization codes.**
   - Code #1: a wrong PKCE verifier is refused and burns the code.
   - Code #2: the exchange succeeds, and a replay is refused.
7. **MCP.**
   - `initialize`;
   - `tools/list`, whose count must equal `/healthz`;
   - `tools/call` on exactly two allowlisted, zero-cost local tools: `sxbet_health` and `apifootball_health`. No provider request, browser tool, order, or paid call is reachable.
8. **Refresh.**
   - wrong client refused;
   - rotation succeeds;
   - replay of the old refresh token refused;
   - the refresh token is not accepted as a bearer;
   - the new access token works.

## Secret handling (VERIFIED by offline tests)

- **The password:**
  - is read from the environment and held in memory only;
  - is sent only in the consent POST body, over HTTPS, to the pinned origin;
  - is never printed.
- **Runtime secrets** (codes, PKCE verifiers, state, client id, access and refresh tokens):
  - are masked in the Actions log (`::add-mask::`) when created;
  - never appear in stdout except in those mask commands.
- **The report:**
  - holds fixed check names, outcomes and non-secret facts only;
  - is leak-checked against the raw and JSON-escaped form of every secret;
  - is replaced by a fixed `report_leak_check` failure if anything matches.
- **No artifact** is uploaded; only a sanitized step summary is written.
- **HTTP client:**
  - uses only the Python standard library, so no package is installed next to the password;
  - is run with `python -I`;
  - ignores proxy settings from the environment;
  - never follows redirects. A local two-server test proves the 302 to the client callback is parsed, and its target never contacted.

## OAuth credential lifecycle and residual risk (NOT certified)

The bridge has **no revocation endpoint** yet (RFC 7009; planned with WP-022 and migration 0009). The bridge does not store the client name, so test clients cannot be told apart from real Claude DCR clients for a targeted cleanup.

Each run therefore leaves in the production OAuth tables, as hashes only:

| Item | Count | Lifetime | Note |
|---|---:|---|---|
| DCR client row | 1 | 730 days | public PKCE client, Claude callback only |
| Authorization codes | 2 | used / expired | single use, already consumed |
| Access tokens | 2 | 1 hour | the first is not revoked by rotation |
| Refresh token (active) | 1 | 30 days | the rotated one is revoked |

- **Exposure of the plaintext:** the active refresh token and the last access token existed only in the memory of the ephemeral GitHub runner. They were masked and never written to logs, the report or artifacts. Using either one needs the token plaintext; the DB stores only SHA-256 hashes.
- **What is NOT claimed:** revocation or cleanup of test credentials, and a bound on how many credentials accumulate. Each run adds one active refresh token for 30 days.
- **Throttling side effects:**
  - one consent failure per run against the runner's address (limits: 5 per 15 min per address, 50 per hour globally);
  - two DCR registrations per run (limits: 10 per hour per address, 100 per hour globally);
  - the concurrency group prevents parallel runs.

**Planned remedy (separate, owner-gated OAuth PR):**
- an RFC 7009 revocation endpoint that the E2E calls at the end for the refresh and access tokens it holds;
- marking E2E clients, so that their rows can be removed after the run.

## What the health tool calls prove

`network_calls_made == 0` is what the two health tools report: their code makes no provider request, but the run cannot observe that independently. When live API-FOOTBALL is authorized, `apifootball_health` reads the shared quota snapshot (SELECT only, no reservation).
