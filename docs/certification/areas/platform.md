# Phase A - platform reconciliation (OAuth + MCP, Database, CI)

Snapshot: `origin/main` = `342fed6` (extracted read-only with `git archive`; the local checkout sits on branch
`claude/api-football-pr-125-hardening-d9x0pz` @ 11729cb = head of merged #163). Nothing modified, pushed,
applied or deployed. Offline tests run on the snapshot: `test_bridge_auth, test_oauth_metadata,
test_mcp_offline_protocol_e2e, test_mcp_tool_registry_observability, test_migrations, test_aiven_pg18_diag,
test_bridge_security, test_bridge_config, test_repository_governance` -> **220 passed, 1 skipped** (the skip is the
DCR PostgreSQL round-trip). Ad-hoc probes (scratchpad only, DATABASE_URL pointed at a closed local port).

Detail rows: `/tmp/claude-0/phaseA/platform.csv` (43 rows). PR/branch/#130: `/tmp/claude-0/phaseA/prs.md`.

## 1. Counts (num / den)

Verified = OFFLINE_VERIFIED or stronger. Denominator excludes NOT_APPLICABLE.

| scope | rows | applicable | verified | of which real (provider/integration) | MOCK | UNTESTED | NOT_IMPL | BLOCKED | PRODUCTION_VERIFIED |
|---|---|---|---|---|---|---|---|---|---|
| OA (OAuth + MCP) | 21 | 21 | **11/21** | 0 | 4 | 3 | 2 | 1 | **0/21** |
| DB | 14 | 12 | **7/12** | 7 (5 integration on PG16, 2 real on Aiven `fcb_pg18_test`) | 0 | 3 | 1 | 1 | **0/12** |
| CI | 8 | 8 | **7/8** | 5 (manual live workflows) | 0 | 0 | 1 | 0 | n/a |
| total | 43 | 41 | **25/41** | 12 | 4 | 6 | 4 | 2 | **0** |

Nothing is PRODUCTION_VERIFIED: production `defaultdb` has 0 tables, so every `/mcp` request carrying a bearer
token returns 503. Requests without a token still get 401 (that path never reads the DB, as the probe shows).

Tool registry, measured by `_tool_registry_counts()` (the same catalogue `tools/list` serves):
browser **34** | + `APIFOOTBALL_ENABLED` -> **76** (42 apifootball) | + `SXBET_ENABLED` -> **122** (46 sxbet).
Without `DATABASE_URL`, SX Bet falls back to `LOCAL_PROCESS`. With it set, it uses `POSTGRES_SHARED`.

## 2. Evidence-backed gaps (most important first)

1. **G1 (P0) The production schema was never applied.** `app.db.migrate()` has **no caller** in the runtime. Only
   `scripts/generate_aiven_migrations.py` imports `app.migrations`. The service boots against an empty database.
2. **G2 (P1) `/healthz` only checks configuration.** It returns `ok:true` and `database_configured:true` while the DB is
   unreachable (probe), so the outage stays hidden, and the §3 "deploy healthy" check cannot detect it.
3. **G3 (P1, §5) Some runtime tables have no versioned migration.** `api_football_quota_counters/leases/attempts`
   (`bridge/api_football/quota.py:_SCHEMA`) and `oauth_claude_dcr_clients` (`bridge/oauth_store.py`) are only
   created at runtime with `CREATE TABLE IF NOT EXISTS`. `sxbet_rate_*` is in 0007, and a runtime copy is also kept.
4. **G4 (P1) Migration numbering.** `load_migrations` requires versions 1..N with no gap. 0008 is signer-only and is
   deliberately not applied to the bridge DB. As a result, a bridge migration 0009 cannot be applied to Aiven
   without also applying 0008, or without changing the numbering. This needs an owner decision.
5. **G5 (P1) Fixed error codes are missing on DB failure.** `/oauth/token` (both grants) and `POST /oauth/authorize`
   do not catch errors from `oauth_store`. Probe: HTTP 500 "Internal Server Error". `/oauth/register` returns
   503 `server_error` and `/mcp` returns 503 with a fixed body. ASSUMED, not observed: uvicorn would log the psycopg
   traceback, which includes host and port.
6. **G6 (P1) OAuth storage SQL is never run against PostgreSQL in CI.** Every OAuth/MCP test stubs
   `oauth_store`. The DCR PG test reads `DATABASE_URL`, but the CI integration job sets `TEST_DATABASE_URL` and only
   runs `tests/integration`, so the test skips everywhere. The 503 path of `/mcp` has no test. The refresh grant
   has no test.
7. **G7 (P1) Database TLS does not authenticate the server.** `app/db.py` uses `DATABASE_URL` verbatim, and nothing
   enforces `sslmode`. The PG18 workflow accepts `sslmode=require`, which encrypts the connection but does not
   verify the server certificate. No Aiven CA is pinned.
8. **G8 (P1) There are no least-privilege roles.** #130 run 13 shows the workflow sessions as `user=avnadmin`.
   The runtime DDL in G3 needs `CREATE` on schema `public` and table ownership for `CREATE INDEX`. Revoking those
   rights from the runtime role would currently break first use.
9. **G9 (P2) CI gaps.** The integration job uses `postgres:16`, but production runs PG **18.6**. No workflow runs on
   push to `main`, and nothing automates post-deploy verification (§3).
10. **G10 (P2/P3) OAuth hardening.** There is no lockout or throttling on the consent password. Refresh rotation has
    no reuse detection, and the old access token stays valid. There is no RFC 7009 revocation endpoint (404). The
    path-inserted `/.well-known/oauth-protected-resource/mcp` returns 404. The PKCE verifier length and charset are
    not checked. Expired tokens are never purged. The README still says "Neon".

## 3. Proposed work packages (only evidence-justified)

### WP-1 (P0): Recover `defaultdb` by applying 0001-0007 (owner-run psql)
- **Gap:** G1 / DB-09 / OA-20.
- **Files:** none changed. Use the existing `sql/aiven/aiven_migrations_0001_0007.sql`
  (SHA-256 `b73ed5c2...1fde`) and `sql/aiven/verify_schema.sql`.
- **RED (current state):** `verify_schema.sql` on defaultdb returns 0 tables, and `schema_migrations` does not
  exist. `/mcp` with a valid token returns 503.
- **Procedure:** the owner runs it from a trusted host with psql 18, keeping the guards of
  `fcb-aiven-pg18-test.yml`:
  1. Regenerate the bundle with `--check` and confirm the pinned SHA. Confirm the bundle contains no 0008 content.
  2. Connect with `sslmode=verify-full sslrootcert=<Aiven project CA>` (see WP-6).
  3. Prechecks: `current_database()='defaultdb'` (explicit expected value), server 18.x, `pg_stat_ssl.ssl=t`.
     The schema must be empty apart from `_avnadmin_monitor`.
     **Caveat (from code):** these code paths create tables lazily even on an empty DB:
     - `/oauth/register` creates `oauth_claude_dcr_clients`;
     - the first API-FOOTBALL call creates the `api_football_quota_*` tables;
     - the first SX Bet call creates `sxbet_rate_*`.

     If any of these exist at apply time, stop and decide explicitly. Do not relax the precheck.
  4. Run `psql -X -v ON_ERROR_STOP=1 -1 -f bundle`, re-checking DB, version and TLS inside the same transaction.
     Run it a second time to prove it is idempotent.
  5. Run `verify_schema.sql`.
- **GREEN:** 29 tables, 4 functions, 9 triggers, 54 indexes. `schema_migrations` has 7 rows whose name and SHA match
  the repo (the same figures as run 14 on `fcb_pg18_test`).
- **Real test:**
  - `/healthz` shows `deploy_commit=342fed6`.
  - `/mcp` without a token returns 401.
  - OAuth from ChatGPT and from Claude.ai, then `initialize`, then `tools/list`: the count equals
    `/healthz.mcp_tools_total`.
  - `browser_create` followed by `browser_stop` writes a lease row.
  - One API-FOOTBALL `status` call through the quota coordinator.
  - Logs are free of errors.
- **Owner authorizations:**
  - the production DB write;
  - confirmation that Render `DATABASE_URL` points to `defaultdb`;
  - a rollback plan. The DB was empty, so rollback means restoring it to empty through an Aiven backup or PITR,
    with no data loss.
- **DONE:** the GREEN figures are measured on defaultdb, the real test passes, and #130 is annotated with
  VERIFIED evidence.

### WP-2 (P1): Read-only schema readiness in `/healthz`
- **Gap:** G2 / OA-18 / OA-21.
- **Files:** `bridge/server.py`, `tests/test_bridge_auth.py`.
- **RED:** with the DB unreachable or the schema empty, `/healthz` reports nothing about readiness (today:
  `ok:true`).
- **GREEN:** add fields such as `database_reachable` and `schema_version` (the max version in
  `schema_migrations`, or null) with a bounded timeout and fixed codes, and no DSN or exception text.
  Keep HTTP 200: Render's health-check semantics are the owner's decision.
- **Real test:** after WP-1, `schema_version=7`.
- **Owner:** none, unless the status code changes.
- **DONE:** CI green, deploy verified.

### WP-3 (P1): Version the runtime-only tables (§5)
- **Gap:** G3, which is blocked by G4.
- **Owner decision first (G4).** Options:
  - (a) apply 0008 to the bridge DB as inert tables, which conflicts with signer isolation;
  - (b) split migration sequences into bridge and signer. 0008 was applied only to ephemeral CI DBs, never to
    Aiven, so this renumbering touches governance and §2.6;
  - (c) teach the runner and the bundle an explicit excluded-version allowlist.
- **Then:** add a migration with `CREATE TABLE/INDEX IF NOT EXISTS` whose definitions are byte-identical to
  `quota.py:_SCHEMA` and `oauth_store` DCR, so it is compatible with tables already created lazily. Regenerate the
  Aiven bundle and its pinned SHA.
- **RED:** a test asserting that every table referenced by bridge runtime SQL exists after `apply_migrations` on a
  fresh DB fails today (the quota and DCR tables are missing).
- **GREEN:** that test passes. A test on an already-initialized DB (0001-0007, plus runtime-created tables, plus the
  new migration) also passes, as §5 requires.
- **Real test:** first `fcb_pg18_test` via the workflow, then defaultdb, owner-run.
- **DONE:** `schema_migrations` matches on both databases.

### WP-4 (P1): Fixed-code 503 on OAuth storage failure, plus missing tests
- **Gap:** G5, G6 / OA-12, OA-13.
- **Files:** `bridge/oauth.py`, `tests/test_oauth_metadata.py`, `tests/test_bridge_auth.py`.
- **RED tests:**
  - with `consume_code`, `rotate_refresh_token` or `save_code` raising, the endpoint must return 503
    `{"error":"temporarily_unavailable"}`, with no exception text in the logs. Today they return 500;
  - with `valid_access_token` raising, `/mcp` returns 503 with the fixed body. Behaviour exists today, test missing.
- **GREEN:** wrap the calls, log fixed reason codes only.
- **Owner:** this is auth-adjacent error handling, not a substantive auth change. Flag it in the PR, and route it to
  the owner if in doubt.
- **DONE:** CI green, plus a probe after deploy.

### WP-5 (P1): Exercise the OAuth store against real PostgreSQL
- **Gap:** G6 / OA-05, OA-07, OA-08, OA-09.
- **Files:** a new `tests/integration/test_oauth_store_postgres.py`. Move the DCR round-trip there and use
  `TEST_DATABASE_URL`.
- **Tests:**
  - an authorization code is single-use, expires, and is bound to client, redirect and resource;
  - an access token fails on wrong resource, missing scope, expiry or revocation;
  - refresh rotation: the old refresh token is rejected and a new pair is issued;
  - DCR: an unknown ID or a wrong redirect is rejected.
- **GREEN:** the tests pass in the `decision-postgres` CI job. Tests only, no behaviour change.
- **Follow-up (behaviour, owner decision):** refresh reuse detection, revoking the old access token on rotation,
  and an RFC 7009 endpoint.

### WP-6 (P1): Database TLS `verify-full` with the Aiven project CA
- **Gap:** G7 / DB-12 / CI-07.
- **Owner steps:** download the project CA (a public certificate, not a secret). Provide it to Render as a secret
  file, and set `DATABASE_URL` with `sslmode=verify-full&sslrootcert=<path>`. Mirror this in `FCB_PG18_TEST_URL`.
- **Code:** an opt-in fail-closed policy in `app/db.py` that refuses a DSN without `verify-full` when the policy
  flag is on, plus tests. In the workflow, narrow the allowlist to `verify-full` with a pinned CA fingerprint.
- **RED:** a test showing a `sslmode=require` DSN is accepted today.
- **GREEN:** the DSN is rejected while the policy is on.
- **Real test:** the diagnose run shows `pg_stat_ssl.ssl=t`, and the connection fails against a wrong CA.
- **Owner:** secret management (§2.2).

### WP-7 (P1): Least-privilege DB roles
- **Gap:** G8 / DB-13.
- **Depends on:** WP-3, and on the runtime DDL being removed or guarded with `to_regclass`. Whether
  `CREATE TABLE IF NOT EXISTS` still needs schema `CREATE` when the table exists must be proven by a RED
  integration test under a non-owner role, not assumed.
- **Roles and plan:**
  - `fcb_migrator` owns objects and is used only for owner-run apply;
  - `fcb_app` (LOGIN) gets `USAGE` on public and DML only on `oauth_*`, `oauth_claude_dcr_clients`,
    `browser_session_leases`, `api_football_quota_*` and `sxbet_rate_*`. It gets no `CREATE`, no legacy tables and
    no `signer_*`;
  - `fcb_signer` lives in a separate DB or schema for 0008 (§2.6, owner-only);
  - stop using `avnadmin` for runtime and CI.
- **GREEN:** integration tests for the quota, SX Bet coordinator, leases and OAuth pass under `fcb_app`.
- **Owner:** role creation, passwords, and the `DATABASE_URL` rotation (secrets).

### WP-8 (P2): CI parity and post-deploy verification
- **Gap:** G9 / CI-01 / CI-08.
- **Changes:**
  - run the integration job on `postgres:18`, or a 16/18 matrix;
  - add a read-only, secret-free workflow on push to `main` that polls `/healthz` until `deploy_commit` equals the
    merge SHA, asserts `/mcp` returns 401 without a token, and checks the tool count.
- **Owner:** none (no cost, no secrets).

### WP-9 (P3): Docs and spec niceties
- **Gap:** G10, DB-14.
- **Changes:**
  - update README line 34 from Neon to Aiven PG 18;
  - optionally serve the path-inserted protected-resource metadata;
  - validate PKCE verifier length and charset;
  - purge expired tokens with a bounded DELETE.
- **Owner:** none, except where the change is to auth behaviour.

Not proposed, because no evidence justifies it: auto-migrate on boot (it conflicts with WP-7, and the owner decides
how to apply), removing legacy tables (destructive, §5), and reviving `cert/fcb-mcp-global-e2e-01` (#124 closed
unmerged, touches OAuth).
