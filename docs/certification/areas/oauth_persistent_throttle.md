# OAuth persistent throttle (owner runbook)

Owner decisions D01–D12 (2026-11-11) plus the owner's closing decisions on the mode selector and the HMAC secret.

**Status: implemented and tested offline and on throwaway PostgreSQL 16 and 18 only. Shipped DISABLED (mode `off`). Nothing has been applied, configured or activated in production.**

These are owner actions (CLAUDE.md §2.2, §3): the merge, migration 0009, the secrets, every mode change and any unlock.

## What it is

Two layers protect the unauthenticated OAuth operations that create something new:
- the consent POST, `/oauth/authorize`;
- dynamic client registration, `/oauth/register`.

| Layer | Where | Survives a restart | Role |
|---|---|---|---|
| 1 | `bridge/oauth_throttle.py`, in memory (unchanged) | no | first filter (D09). It costs no database round trip, and it caps what reaches layer 2 |
| 2 | `bridge/oauth_persistent_throttle.py`, table `oauth_throttle_buckets` (migration 0009) | yes | the same limits, persisted (D01, D03) |

**Limits (D01, D03).** Both layers apply the same limits. There is no daily cap (D02).

| Operation | Per address | Global | What counts |
|---|---|---|---|
| Consent | 5 per 15 min | 50 per hour | failures only (an attempt is reserved before the compare and given back when the password is right) |
| Registration | 10 per hour | 100 per hour | every attempt; **observe-only** (D03) |

- **Algorithm (D08).** GCRA on the database clock (`statement_timestamp()`). Every writer of a scope locks the scope's global row first (fixed lock order). Then one conditional `INSERT … ON CONFLICT DO UPDATE` writes the per-key and global rows only when both conform.
  - A denied request writes nothing.
  - GCRA has the same sustained rate and burst as the layer-1 window.
  - After a burst of 5 consent failures, layer 2 allows the next attempt after 3 minutes (15 min / 5), while layer 1 in the same process still blocks for 15 minutes.
- **Never throttled (D12).** `/oauth/token` (code exchange and refresh tokens) and bearer-authenticated `/mcp` never call layer 2. Tests prove this with the throttle store raising and the mode invalid.

## Modes (`MCP_OAUTH_THROTTLE_MODE`, Render, owner-controlled)

| Value | Behaviour |
|---|---|
| unset / empty / `off` | **Default of this PR.** Today's behaviour: layer 1 only. Layer 2 never touches the database. Readiness does not require 0009 and reports it in `schema_deferred_versions` |
| `shadow` | Layer 2 runs and logs fixed codes (`oauth_throttle scope=… mode=shadow outcome=allow\|would_deny\|unavailable\|skipped code=…`). It never blocks. A missing secret is logged (`code=KEY_MISSING`) and flagged in healthz. Logins continue. Readiness requires 0009 |
| `enforce` | Consent: a persistent deny returns `429 slow_down`. When the check cannot run (store down or busy, missing or invalid secret), the response is the uniform `503 temporarily_unavailable`. Both happen **before** the password compare, so no 401/503 oracle exists. Registration stays observe-only (D03), but it is refused with the same 503 when the secret is missing or invalid |
| anything else | `CONFIG_INVALID`: healthz `oauth_throttle_mode=CONFIG_INVALID` and `oauth_new_authorizations_ready=false`. Consent and registration are refused with the uniform 503 until the value is corrected. `/mcp` bearer calls, existing sessions and refresh tokens keep working. **Never treated as enforce** |

The `/healthz` fields carry fixed codes only:
- `oauth_throttle_mode`;
- `oauth_throttle_registration_mode`;
- `oauth_throttle_key_state` (`OK`, `MISSING` or `INVALID`);
- `oauth_throttle_rotation_active`;
- `oauth_new_authorizations_ready`;
- `schema_deferred_versions`.

### Activation sequence (D10): every step is an owner action
1. Merge this PR (owner only: substantial authentication change). The mode stays `off`, so production behaviour is unchanged. `/healthz` stays `READY`, with `schema_deferred_versions: [9]`.
2. Create the GitHub secret `FCB_MIGRATOR_DB_URL` in `aiven-production` (fcb_migrator URI, `sslmode=verify-full`). Requires WP-009 phase 2.
3. Run `FCB Aiven defaultdb OAuth throttle 0009` with `mode=diagnose`, then with `mode=apply` and the phrase `APPLY BRIDGE 0009 OAUTH THROTTLE TO defaultdb`.
   - Before that, confirm in the Aiven console that a recent automatic backup exists.
   - The workflow also uploads a schema-only dump (no row data) and the migration rows.
4. Generate the HMAC secret (below) and set `MCP_OAUTH_THROTTLE_KEY` in Render as a secret.
5. Set `MCP_OAUTH_THROTTLE_MODE=shadow`. Check:
   - healthz shows `shadow`, `key_state OK` and `schema_state READY`;
   - the logs show `oauth_throttle … outcome=allow` on the next owner login.
6. Shadow for **at least 7 days**. Review the logs:
   - `forwarded=trusted` must dominate. It confirms that Render's proxy hop is internal; see the trusted proxies below.
   - There must be no `would_deny` for legitimate logins.
   - Note every `unavailable` or `STORE_BUSY`.
7. **Separate written owner GO** for `MCP_OAUTH_THROTTLE_MODE=enforce`.

### Rollback (D11)
- Set `MCP_OAUTH_THROTTLE_MODE=off`. This is an owner action, recorded on #130.
- Rolling back never drops the table and never deletes evidence.
- Reverting the merge commit also works. Migration 0009 is additive, and the table stays.

**Owner confirmation needed (D05 vs. the mode lever).** The mode variable can turn layer 2 off. D05 forbids an environment variable that *freely* disables the protection. This PR treats the mode as the rollout and rollback lever that D10 and D11 require.
- Any change away from `enforce` is an explicit owner action, documented here and recorded on #130.
- No workflow, deploy file or code path sets it (tested).
- Nothing technically prevents a Render user from changing it. A DB-backed latch is a possible follow-up.

## HMAC secret (D07 and the owner's decision 2)

- **Value.** `MCP_OAUTH_THROTTLE_KEY` = `<key id>:<secret>`.
  - The key id matches `[a-z0-9]{1,8}`, for example `k202611`.
  - The secret is at least 32 characters from a CSPRNG, for example `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **Dedicated.** It is distinct from `MCP_OAUTH_PASSWORD`, every other OAuth value and every PostgreSQL credential.
- **Where it lives.** It is set only in Render's protected environment, when the owner authorises the phase. It never appears in the repository, tests, logs or docs. Tests generate a random key at runtime.
- **What is stored.** Stored keys are `<key id>:<HMAC-SHA256 hex>` of the normalised address. The plain address, the secret and the key hash are never logged. A CHECK constraint refuses anything else.
- **Missing or invalid secret.**
  - `enforce`: consent and registration are refused, uniform 503.
  - `shadow`: reported in healthz and the log (`KEY_MISSING`, `KEY_INVALID`); logins are not interrupted.
  - `off`: not used.

### Rotation (never silently disables protection)
1. Set `MCP_OAUTH_THROTTLE_KEY_PREVIOUS` to the current value and `MCP_OAUTH_THROTTLE_KEY` to a new value with a **new key id**. Deploy.
2. During the overlap, the old key's buckets are still **checked**, never written, and new events go to the new key. A source limited under the old key stays limited. healthz shows `oauth_throttle_rotation_active=true`.
3. After at least **1 hour**, the longest window, remove `MCP_OAUTH_THROTTLE_KEY_PREVIOUS`. The old rows decay and are collected.

A malformed previous key, or one with the same id or secret, makes the whole keyring `INVALID`. That is fail-safe in `enforce`, never a silent bypass.

## Addresses, proxies and shared egress

- **IPv4.** One key per address. An IPv4-mapped IPv6 address counts as IPv4.
- **IPv6.** Grouped by **/64**. A neighbouring /64 is a different key.
- **Unparseable values.** They share one bucket, so junk values cannot mint keys.
- **Trusted proxies.** `CF-Connecting-IP`, and then the first `X-Forwarded-For` hop, are honoured **only** when the immediate peer is internal: RFC 1918, 100.64/10, loopback or ULA.
  - A public peer reached the app directly, so its forwarding headers are client-chosen and its own address is used.
  - That Render's proxy hop is internal is `DOCS_UNVERIFIED`. The shadow log field `forwarded=trusted|ignored` verifies it before enforce.
  - Layer 1 is unchanged.
- **User-Agent.** There is no User-Agent or client-name exemption.
- **Shared egress (Claude.ai / ChatGPT servers).** Their users share one per-address key, which is unchanged from layer 1. Only consent **failures** count, and a correct password is refunded. Legitimate clients are therefore blocked only by repeated wrong passwords from the same egress.

## Anti-DoS

- **Layer 1 first.** At most about 50 consent and 100 registration events per hour and process reach the database, however many keys flood.
- **Connections.**
  - At most `DB_CONCURRENCY = 2` throttle DB calls run at once per process. Excess calls are refused at once (`STORE_BUSY`) without opening a connection.
  - They run on the existing 4-worker OAuth store pool, so at least 2 workers stay free for `/oauth/token` and bearer checks. This is tested with a 200-key flood, and with 120 keys on real PostgreSQL.
  - In total, at most 2 of Aiven's 15 connections.
- **Timeouts.** `connect_timeout` 2 s, `statement_timeout` 1.5 s, `lock_timeout` 0.5 s. A saturated or locked database fails fast; this is tested with the global row held.
- **Cardinality and cleanup.**
  - Rows are created only for conforming requests, so per hour at most the global limits are added. Denied keys leave no row; this is tested with a 400-key flood.
  - Decayed rows are deleted after each check, at most 64 per check (`LIMIT … FOR UPDATE SKIP LOCKED`). The cleanup never deletes the global rows and never waits on a locked row.
- **Residual risk (global buckets).** A distributed attacker can exhaust the global consent bucket (50 failures per hour). New authorizations are then refused (429) for up to an hour, also for the owner.
  - Existing sessions, `/mcp` and refresh tokens are unaffected.
  - Recovery: wait, or use the emergency unlock (`target=global`).
  - This is the designed trade-off of a global backstop (D01).

## Emergency unlock (D05)

Workflow `FCB Aiven defaultdb OAuth throttle emergency unlock`.
- **Gates.** Owner dispatch from `main`, the protected environment `aiven-production` (required reviewer) and the phrase `UNLOCK OAUTH THROTTLE ON defaultdb`.
- **Scope.** One scope (`consent` or `registration`), and one target:
  - `global`: that scope's global bucket;
  - `key`: one exact key hash. Compute it locally with `python3 scripts/oauth_throttle_key_hash.py <address>`. The secret is read without echo and never leaves your machine.
- **Audit.** Every removed bucket is copied first to `oauth_throttle_unlock_audit`, with the run id and actor. Every run also writes a summary row. The runtime has no privilege on that table.
- **Other lever (not an unlock).** No environment variable unlocks a bucket. `MCP_OAUTH_THROTTLE_MODE` (`off`/`shadow`), or removing it, does switch enforcement off entirely for anyone with access to the Render environment: see "Owner confirmation needed (D05 vs. the mode lever)" above.
- **Target guard.** The unlock runs the same in-transaction guard as the 0009 workflow: `current_database()` must be `defaultdb`, the server PostgreSQL 18, the session over SSL (`pg_stat_ssl`) and the identity `fcb_migrator`; otherwise nothing is executed. The target check also rejects a URI `sslmode` other than `verify-full` (in libpq it would override `PGSSLMODE=verify-full`), so the Aiven console default `?sslmode=require` is refused.

## OAuth password policy (D06)

- **New passwords and rotations.** `MCP_OAUTH_PASSWORD` must be at least **20 random characters**, for example `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
- **Startup warning.** At startup the bridge logs `OAUTH_PASSWORD_POLICY meets_policy=false` when the configured password is shorter. It is non-blocking: no forced reset. Only the boolean is logged, never the password or its length.
- **Not in `/healthz`.** The policy result is deliberately not exposed there: `/healthz` is public, and a weak-password flag would help an attacker. If you want it in healthz, confirm it explicitly.

## Database objects and privileges (migration 0009)

- `oauth_throttle_buckets`:
  - columns `scope`, `key_hash`, `tat`, `updated_at`;
  - CHECK constraints on scope and key format;
  - index on `tat`;
  - `fcb_app` gets S I U D (acquire = UPSERT, refund = UPDATE, cleanup = DELETE).
- `oauth_throttle_unlock_audit`: `fcb_app` gets nothing.
- **Grant files.** `sql/aiven/grant_bridge_runtime.sql` and `finalize_bridge_privileges.sql` carry the same grant. It runs only where 0009 exists, so on a 0001–0008 database it is a no-op. The 0009 workflow grants and asserts the exact end state as `fcb_migrator`.
- **Residual: WP-009 workflow after 0009.** The WP-009 roles workflow and its diagnose model are pinned to 0001–0008. After 0009 they report or refuse the new state. Extending them is a follow-up.
