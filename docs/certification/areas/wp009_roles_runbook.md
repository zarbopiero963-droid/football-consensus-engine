# FCB-WP-009: least-privilege database roles for `defaultdb` (owner runbook)

Workflow: `.github/workflows/fcb-aiven-defaultdb-roles.yml`.
- Manual-only; `diagnose` by default.
- Runs only from `main`, only when the repository owner starts it, in the protected GitHub Environment `aiven-production` (the restore's).
- Two write modes, each with its own confirmation phrase:
  - `apply` = phase 1, phrase `APPLY WP-009 ROLES TO defaultdb`;
  - `enable-migrator` = phase 2, phrase `ENABLE WP-009 MIGRATOR ON defaultdb`.

**Status: designed and tested on throwaway PostgreSQL 16 and 18 only. Nothing has been run in production. This is not a certification.** These are owner actions (CLAUDE.md §2.2, §3): secrets, dispatches, deployment approvals, Render changes, credential rotation. The agent never receives a password.

## Architecture decision: no identity above the bridge owner ever runs owner-controlled code

### The threat
The bridge owner can attach code to its tables: triggers, forced row-level policies, defaults, CHECK constraints, rules, views. That code runs with the rights of **whoever touches the tables**. The reviews of PR #182 proved three things on PostgreSQL 16 and 18 (now RED tests):
- **A planted trigger escalates the administrator.** When the administrator inherited `fcb_migrator`, a trigger planted by `fcb_migrator` and fired by an administrator write created a role with the administrator's CREATEROLE.
- **`SET [LOCAL] ROLE fcb_migrator` is no boundary.** The invoker code runs `RESET ROLE`.
- **A read-only session is no boundary.** In `poc_rls.py`, a forced policy on `schema_migrations` calls `set_config('default_transaction_read_only','off')`. The next statement of the same `psql -f` file then ran `ALTER ROLE fcb_migrator CREATEROLE` and `ALTER ROLE fcb_signer LOGIN` as the administrator.

### The decision: reordered two-phase cutover
This is the owner's design suggestion. It removes the privileged window structurally.

| Phase | What changes | Who can act as the bridge owner during and after it |
|---|---|---|
| **1 — `apply`** | Creates `fcb_app` (LOGIN, runtime allowlist), `fcb_migrator` (**NOLOGIN, no password, owns nothing, no membership**) and `fcb_signer` (NOLOGIN). Revokes PUBLIC. **Every bridge object stays owned by the administrator (status quo).** | Only the administrator, as today. `fcb_migrator` owns nothing, so it cannot have planted code. It cannot log in. No non-superuser role can `SET ROLE` to it or inherit it (R1, checked in-transaction by `role_reachability`). |
| Owner switch | Render `DATABASE_URL` → `fcb_app`. A protected MCP E2E run proves it through an authenticated MCP call (R2). | unchanged |
| **2 — `enable-migrator`** | One transaction: refuse while any other administrator session is connected (observed 75 s); move every bridge object to `fcb_migrator`; set its default privileges; **REVOKE the administrator's membership** (it keeps only PostgreSQL's automatic ADMIN-only grant: no INHERIT, no SET); enable `fcb_migrator` LOGIN. | `fcb_migrator` only. After commit, the administrator holds **no privilege on any bridge table**. Any access by it fails with `permission denied` at the privilege check, before a row is read and before any policy, trigger, rule or default can run (D09-01, tested). |

**Why this order, compared with my previous two-phase variant (ownership moved in phase 1, migrator NOLOGIN):**
- During the window between the phases, the database here is exactly the status quo. Rolling back to the administrator DSN is therefore safe without any extra condition.
- The only owner during the window is the administrator itself, and `fcb_migrator` has nothing to plant code in.
- Ownership transfer, loss of administrator inheritance and migrator login commit **together**. No instant exists where the migrator can log in while the administrator can still reach its tables.

**Alternative considered: a NOLOGIN owner role reachable only by the migration workflow.** It does not change who executes planted code. The control is the same: no privileged identity touches owner tables. It remains an option for the future migration workflow.

### What each identity can run after phase 2

| Path | Identity | Escalation possible? | Evidence (tests) |
|---|---|---|---|
| Runtime (Render) | `fcb_app` | No: planted code runs as `fcb_app`, and there is no session user to `RESET ROLE` to | GREEN trap tests; the real app passes OAuth + MCP |
| Rollback DSN | `fcb_migrator` (the owner) | Nothing above itself | GREEN; the real app READY |
| Administrator, by accident | administrator | No: `permission denied` before any code (SELECT, INSERT, UPDATE, DELETE, TRUNCATE, LOCK, ALTER, calling the trigger functions, SET ROLE) | `test_accidental_administrator_dsn_after_phase2_fails_closed`; the app on the admin DSN: healthz not READY, OAuth 503 |
| Workflow, administrator | catalog only | No bridge row is ever read. Rows (`schema_migrations`) are read **as `fcb_app`** (`scripts/fcb_db_roles.py evidence`); counts come from `count_bridge_catalog.sql` | `test_green_every_administrative_workflow_read_is_catalog_only`, the PoC GREEN |
| Restore workflow (on main, unchanged) | administrator | Its catalog precheck refuses (`REFUSE_RUNTIME_TABLE_DRIFT` after phase 1, `REFUSE_DEFAULT_PRIVILEGES` after phase 2) **before** its tracked part would read `schema_migrations`. Its read-only inventory skips RLS tables and gets `permission denied` | `test_green_restore_workflow_after_hardening_refuses_without_running_owner_code` |
| Phase 1 re-run after phase 2 | — | Refused: `FCB_ROLES_OWNER_GUARD_FAILED` | tested |
| Phase 2 re-run | administrator | Catalog-only check that the model is exactly HARDENED, then only `ALTER ROLE fcb_migrator PASSWORD`. Catalog DDL runs no user code. Event triggers need a superuser (tested for `fcb_migrator`, `fcb_app` and the administrator) | tested |

The only administrative row read in the whole design is the phase-1 check of `schema_migrations` against the repository (`roles_apply_guard.sql`). At that point the same transaction has just proved that the administrator owns every object and that no other non-superuser can act as the owner. So only the administrator's own code could run there.

## Membership inventory (R1, D09-01)

The inventory is asserted exactly by `verify_bridge_roles.sql`. It is identical after phase 1 and after phase 2:

```
membership fcb_app <- session admin=true inherit=false set=false grantor=bootstrap
membership fcb_migrator <- session admin=true inherit=false set=false grantor=bootstrap
membership fcb_signer <- session admin=true inherit=false set=false grantor=bootstrap
effective session -> fcb_migrator member=true inherits_privileges=false can_set_role=false
effective session -> fcb_app member=true inherits_privileges=false can_set_role=false
effective session -> fcb_signer member=true inherits_privileges=false can_set_role=false
(every effective fcb_* -> … line: member=false inherits_privileges=false can_set_role=false)
```

- **The `grantor=bootstrap` rows.** These are PostgreSQL's automatic ADMIN-only grants to the creating role (CREATEROLE design, PG16+).
  - Aiven's `createrole_self_grant=inherit,set` would add INHERIT+SET to them. Both phases switch it off with `SET LOCAL createrole_self_grant = ''`: source `session`, transaction-scoped, checked before any CREATE ROLE.
  - ADMIN lets the administrator grant the role to anyone, itself included. That is a deliberate administrative act, never the default path. `diagnose` reports it immediately (`session_reaches_migrator`, `membership_inventory`, `role_reachability`; tested).
- **`role_reachability`.** For every non-superuser role of the cluster, including predefined roles such as `pg_read_all_stats` and `pg_database_owner`, and against each bridge role:
  - nobody inherits a bridge role or can `SET ROLE` to it;
  - only the administrator may be a MEMBER, through its ADMIN-only grant.
- **Superusers.** They bypass every check. On Aiven these are internal identities; `diagnose` prints `superusers=<n>`.

## D09-02: privilege matrix for `fcb_app`

| Privilege | Runtime use (file:function) | Test | Decision |
|---|---|---|---|
| `schema_migrations` SELECT | `bridge/schema_readiness.py:evaluate` | readiness READY as fcb_app | keep |
| `oauth_authorization_codes` S I U | `oauth_store.save_code`, `exchange_code`/`consume_code` (UPDATE … RETURNING) | OAuth functions + real app | keep; **DELETE revoked** |
| `oauth_access_tokens` S I | `oauth_store._insert_pair`, `save_access_token`, `valid_access_token` | same | keep; **UPDATE, DELETE revoked** |
| `oauth_refresh_tokens` S I U | `refresh_and_issue`, `rotate_refresh_token` | same | keep; **DELETE revoked** |
| `oauth_claude_dcr_clients` S I D | `register_claude_dcr_client` (expiry DELETE), `valid_claude_dcr_client` | DCR + real app | keep; **UPDATE revoked** |
| `browser_session_leases` S I U | `session_leases.*` (`claim_stale` FOR UPDATE needs UPDATE) | lease lifecycle + reaper | keep; **DELETE revoked** |
| `api_football_quota_counters` S I U | `quota._reserve`/`_mark_exhausted` (ON CONFLICT DO UPDATE), `_snapshot` | coordinator test | keep; **DELETE revoked** |
| `api_football_quota_leases` S I D | `quota._reserve` (expiry DELETE), `_release`, `_snapshot` | same | keep; **UPDATE revoked** |
| `api_football_quota_attempts` S I D | `quota._reserve`, `_mark_exhausted` | same | keep; **UPDATE revoked** |
| `sxbet_rate_attempts` S I D | `ratelimit._reserve` | coordinator test | keep; **UPDATE revoked** |
| `sxbet_rate_cooldowns` S I U D | `ratelimit._reserve` (expiry DELETE), `_record` (UPSERT) | same | keep |
| `oauth_throttle_buckets` S I U D (bridge 0009) | `oauth_persistent_throttle.acquire` (UPSERT), `refund` (UPDATE), cleanup (DELETE) | `test_oauth_persistent_throttle_postgres.py`, `test_oauth_throttle_workflow_sql_postgres.py` | granted only where 0009 exists (no-op on 0001-0008); `oauth_throttle_unlock_audit`: nothing |
| Revoked DML above | none (grep of `bridge/` and `app/`) | RED: each statement denied now (the uniform grant allowed it). GREEN: the whole runtime | **implemented** |
| `TEMPORARY` on defaultdb (PUBLIC default) | none | RED: fcb_app creates a temp table. GREEN: after `REVOKE TEMPORARY … FROM PUBLIC`, the real app (OAuth, MCP) is unchanged | **owner decision**: database-wide default; Aiven tooling may rely on it |
| Large objects (`lo_create`) | none | RED: fcb_app creates one. The administrator cannot revoke EXECUTE on pg_catalog functions: PostgreSQL only warns, nothing is revoked | residual (superuser only) |
| CONNECT to other databases (PUBLIC default) | none | RED: fcb_app connects to another database, with no privilege inside | residual: WP-009 changes `defaultdb` only |
| EXECUTE on functions in `public` | none | none granted (model: `routine_grant`, global default revoke) | implemented (WP-009) |
| Privileged pg_catalog functions (`pg_read_file`, `pg_ls_dir`, `lo_import`, `COPY … PROGRAM`, signalling other backends) | none | denied | n/a |
| Advisory lock 460244120 | none | RED: fcb_app can hold it. No GRANT can forbid a lock id | **mitigated**: both phases use `pg_try_advisory_xact_lock` and refuse at once with `FCB_ROLES_LOCK_BUSY` (no hang); `diagnose` prints `bridge_lock_holders <role>`. The GitHub concurrency group already serializes restore and roles. The restore script's own lock is owner-reserved and unchanged |

## Owner procedure

### 0. Prerequisites
- [ ] The WP-009 PR is merged to `main` by the owner, with CI green on the merge SHA (PostgreSQL 16 and 18 integration jobs).
- [ ] `/healthz` shows `schema_state: READY`, and the restore workflow's last diagnose shows `BRIDGE_0001_0008`.
- [ ] `aiven-production` has a required reviewer (the owner) and deployment branches = `main` only. `mcp-e2e` has the same.

### 1. Secrets (Environment `aiven-production`)
- [ ] Create `FCB_APP_DB_PASSWORD` and `FCB_MIGRATOR_DB_PASSWORD`.
  - Generate each locally: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
  - Each must be 32–128 URL-safe characters; the two must differ from each other and from the `avnadmin` password.
  - Paste without a trailing newline, and keep both in your password manager.
- [ ] `FCB_DEFAULTDB_URL` stays the `avnadmin` URI with `sslmode=verify-full`.

### 2. Diagnose before phase 1 (read-only, catalog only)
Dispatch with `mode=diagnose`. Expected (facts verified read-only on 2026-11-09):

```
## Restore precheck state: BRIDGE_0001_0008
## Roles model: ROLES_ABSENT
session_superuser=false
session_createrole=true
database_owner=session
public_schema_owner=pg_database_owner
session_has_public_owner_privileges=true
createrole_self_grant=inherit,set
role fcb_app exists=false
role fcb_migrator exists=false
role fcb_signer exists=false
superusers=<n>
bridge_lock_holders none
owners relations session=34
owners routines session=4
schema_public_acl PUBLIC:USAGE pg_database_owner:CREATE pg_database_owner:USAGE
routine_non_owner_grants=4
default_acl none
ROLES_ABSENT
```

**Stop and report** if:
- any `role fcb_*` exists;
- any `reach …` line appears;
- an owner is not `session`;
- `default_acl` is not `none`.

### 3. Phase 1: `mode=apply`, phrase `APPLY WP-009 ROLES TO defaultdb`
The summary must show:
- `Phase 1: PASS (… model after: HARDENED_PHASE1; … objects still owned by the administrator)`;
- `login fcb_app: tls=True … readiness=READY create_denied=True legacy_read_denied=True`;
- `login fcb_migrator: refused (phase 1: NOLOGIN, no password)`;
- `owners relations session=34`, the three membership lines above, and 11 `grant fcb_app …` lines matching the matrix.

**Production effect: none.** Render still uses `avnadmin` on avnadmin-owned objects (status quo).
**Rollback: none needed.** Nothing about the runtime changed.

### 4. Switch Render to `fcb_app`
1. Keep the current `avnadmin` DSN in your password manager. It is the rollback **until phase 2**.
2. Set `DATABASE_URL = postgresql://fcb_app:<FCB_APP_DB_PASSWORD>@<host>:14274/defaultdb?sslmode=verify-full`.
   - With WP-008 TLS, if it is not already configured: a Secret File with the authenticated CA (check its SHA-256 against `FCB_AIVEN_CA_SHA256`), `PGSSLROOTCERT=/etc/secrets/<file>`, and `FCB_DATABASE_TLS_VERIFY_FULL=1`.
3. Wait for the deploy to go `live`, then check:
   - [ ] `/healthz` `deploy_commit` equals the current `main` SHA;
   - [ ] `schema_state: READY`, `schema_missing_tables: []`;
   - [ ] `/mcp` without a token returns 401;
   - [ ] Render logs: no `permission denied`, no `oauth_storage_error`, no `Browser reaper unavailable`.
4. Dispatch **FCB MCP E2E** with `expected_db_identity=fcb_app`.
   - The run name becomes `FCB MCP E2E (expected database identity: fcb_app)`.
   - It must pass, including the check `database_identity_is_expected`. That check comes from an authenticated MCP call to the allowlisted read-only `apifootball_health`, which returns only the role names `current_user`/`session_user`.
   - Note the **run id**.

**Rollback before phase 2:** set `DATABASE_URL` back to the `avnadmin` DSN. This is safe because the administrator still owns everything and nobody can act as `fcb_migrator`.

### 5. Phase 2: `mode=enable-migrator`, phrase `ENABLE WP-009 MIGRATOR ON defaultdb`, `e2e_run_id=<run id>`
The workflow refuses unless the E2E run:
- succeeded;
- ran `fcb-mcp-e2e.yml` on `main`;
- was dispatched by the owner;
- is on the **same commit**;
- is less than 6 hours old;
- asserted `fcb_app`.

The transaction then refuses while **any other `avnadmin` session** is connected to `defaultdb` during 75 s (`FCB_ROLES_ADMIN_SESSION_PRESENT`). If it refuses, find the session in the Aiven console and close it, or wait, then retry. The summary must show:
- `Phase 2: PASS (model before: HARDENED_PHASE1; model after: HARDENED; …)`;
- `login fcb_migrator: tls=True …`;
- `admin: bridge access denied=True inherits_migrator=False can_set_migrator=False`;
- `sessions on defaultdb now: fcb_app=…`, with no `session`/`other` except the workflow's own.

**After phase 2:**
- [ ] Dispatch `diagnose`: `Roles model: HARDENED`, `owners relations fcb_migrator=34`.
- [ ] Dispatch the E2E again with `expected_db_identity=fcb_app`: PASS.
- [ ] Render logs clean, `/healthz` READY.

**Rollback after phase 2:** set `DATABASE_URL` to the **`fcb_migrator` DSN**. It is runtime-compatible (it owns the bridge, tested with the real app) and gains nothing from its own code. Report the `fcb_app` error, then go back to `fcb_app`.

The `avnadmin` DSN is **no longer a rollback**: it fails closed (healthz not READY, OAuth 503). Making it work again would need an explicit, recorded owner action: granting the administrator privileges or membership. That reopens exactly the escalation this design removes. Only consider it with a fresh `diagnose` reporting HARDENED, in an emergency where both bridge passwords are lost; re-run phase 2 with new secrets instead whenever possible.

### 6. Other consumers of `avnadmin`
- `api-football-live` / `APIFOOTBALL_COORDINATOR_DATABASE_URL`: switch it to the `fcb_app` DSN after step 5 (quota tables only). The owner approves one live smoke.
- `FCB_DEFAULTDB_URL` and `FCB_PG18_TEST_URL` stay administrative.

### 7. Rotate the `avnadmin` password (owner GO only, after steps 5 and 6)
1. Confirm nothing at runtime uses `avnadmin`: Render is on `fcb_app`, and the coordinator secret is on `fcb_app` or unused.
2. Aiven Console → `piero-1` → Users → `avnadmin` → Reset password.
3. Immediately update `aiven-production`/`FCB_DEFAULTDB_URL` and `aiven-pg18-test`/`FCB_PG18_TEST_URL`.
4. Dispatch `diagnose` (HARDENED) and the PG18 workflow diagnose.

The MCP service has no downtime, because the runtime does not use `avnadmin`.

**Rotating `fcb_app`:**
1. Update the secret.
2. Run phase 1 `apply`. After phase 2 this is refused, so instead use `enable-migrator`, which re-sets only the migrator password; an `fcb_app` rotation after phase 2 needs a follow-up mode (residual).
3. Update Render right after.

## Residual risks (not solved here)
- **HARDENED is verified only when the workflow runs.** Between runs, `fcb_migrator`, as the owner, can plant code or grant privileges. Who would execute it:
  - `fcb_app`: no escalation;
  - `fcb_migrator`: itself;
  - the administrator: `permission denied`, *unless* the owner explicitly grants the administrator table privileges or EXECUTE. Unless that happens, the administrator's DSN is never used against the bridge. `diagnose` reports both (`unexpected_relation_grant`, `routine_set`).
  - The model also refuses CHECK/DEFAULT/index expressions calling any function outside the bridge's own pg_catalog allowlist (`expression_functions`, e.g. `set_config`).
- **Unqualified function calls by an administrator.** With `public` in `search_path`, an unqualified call can resolve to a `fcb_migrator` function whose argument types match better (CVE-2018-1058 class, tested RED). The workflow's SQL is fully qualified (static test). Manual administrator sessions must use `SET search_path = pg_catalog`.
- **Future migrations.** The restore workflow refuses after phase 1. Migration 0009+ needs a new owner-gated workflow that runs as `fcb_migrator`.
- **`fcb_app` password rotation after phase 2** needs a follow-up mode.
- **Advisory-lock denial of service** by any login (mitigated above, not removable). TEMPORARY, large objects and CONNECT: see the matrix.
- **SCRAM verifiers on the server side.** They never reach stdout, stderr, the summary, artifacts or argv (tested). On the server, the `ALTER ROLE … PASSWORD 'SCRAM-SHA-256$…'` text can appear, on success and on failure, in the log (`log_statement`, `log_min_error_statement`), in `pg_stat_statements` and in `pg_stat_activity`. Only the administrator and Aiven can see those. A verifier is not a login secret.
- **Self password change.** Any login can change its own password: a lock-out, not an escalation.
- **Phase-2 session check limits.** It sees sessions of the administrator role on `defaultdb` only. Aiven's internal superusers are outside the model (`superusers=<n>` is reported).
- **Network.** `avnadmin` is still reachable from any IP (Aiven `ip_filter` 0.0.0.0/0), and termination protection is off.
- **Evidence.** Local runs on PostgreSQL 16.15 and 18.6 (PGDG), both with `createrole_self_grant=inherit,set`. CI runs PostgreSQL 16 and 18 with the same per-session setting. Production Aiven is not exercised.

This runbook prepares WP-009. It does not certify it. WP-009 becomes PRODUCTION_VERIFIED only after steps 2–5 pass in production and are recorded on #130.
