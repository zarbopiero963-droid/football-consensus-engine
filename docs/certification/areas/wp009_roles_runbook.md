# FCB-WP-009: least-privilege database roles for `defaultdb` (owner runbook)

Workflow: `.github/workflows/fcb-aiven-defaultdb-roles.yml`. It is manual-only, `diagnose` by default, and runs only from `main`, started by the repository owner, in the protected GitHub Environment `aiven-production` (the same one the restore uses).

**Status: prepared and tested on throwaway PostgreSQL only. Nothing has been run in production.** Creating secrets, dispatching the workflow, approving the deployment, changing Render and rotating credentials are owner actions (CLAUDE.md §2.2, §3: secrets, authentication/authorization changes and production credentials are owner-gated). The agent never receives a password.

## What the workflow changes, and what it never changes

`mode=apply` runs ONE transaction, twice (the second run must change nothing):

| Step | File | Effect |
|---|---|---|
| target guard | `$GUARD` in the workflow (byte-identical to the restore's) | database `defaultdb`, PostgreSQL 18, TLS session, `search_path=public`, no SET ROLE, no unexpected schema or setting |
| state guard | `sql/aiven/roles_apply_guard.sql` | bridge advisory lock; no signer series. **First run** (no bridge role exists): every object in `public` owned by the administrative identity, `schema_migrations` = repository 0001–0008 (name + SHA-256). **Re-run** (roles exist): the model must already be `HARDENED`, checked from the catalog only. The administrator then touches no bridge table (see *Architecture*) |
| roles | `sql/aiven/upsert_bridge_roles.sql` | `SET LOCAL createrole_self_grant = ''` (Aiven's server value is `inherit,set`; see below), then creates the roles if absent; `fcb_migrator` LOGIN, `fcb_app` LOGIN (passwords from the Environment secrets, as SCRAM-SHA-256 verifiers), `fcb_signer` NOLOGIN without password; gives the administrative identity SET + INHERIT membership in `fcb_migrator` |
| prepare | `sql/aiven/prepare_bridge_roles.sql` | `public`: no PUBLIC access; `fcb_migrator` USAGE+CREATE; `fcb_app` USAGE; `fcb_migrator` default privileges grant nothing to PUBLIC |
| finalize | `sql/aiven/finalize_bridge_privileges.sql` | all 33 bridge tables, their sequences and the 4 functions become `fcb_migrator`-owned; all non-owner privileges revoked; `fcb_app` gets only the runtime allowlist |
| counts | `sql/aiven/roles_post_checks.sql` → `assert_bridge_counts.sql` (first run only) | object counts still `tables=33 functions=4 triggers=9 indexes=60 …`. A re-run checks them in read-only sessions instead |
| model | `sql/aiven/assert_bridge_roles.sql` | `sql/aiven/verify_bridge_roles.sql` must say `HARDENED` |

**Ownership and ACLs change by design.** The bridge objects move from `avnadmin` to `fcb_migrator`, and the ACLs become the allowlist. So the restore's full catalog fingerprint (`b825ab11…`, which encodes owner and ACL) changes.
- A first apply requires the database to carry exactly that fingerprint beforehand.
- `sql/aiven/fingerprint_bridge_structure.sql` is the same fingerprint without the relation/routine owner and ACL fields. It must be identical before and after.
- The second run must leave everything, owners and ACLs included, unchanged.

It never creates, drops or alters a table, column, index, constraint, row or migration, never drops a role, and never changes the administrative identity's own attributes or password. After the transaction it compares the schema evidence (`verify_bridge_schema.sql`) before and after (must be identical), then logs in as `fcb_app` and `fcb_migrator` over `verify-full` and checks, inside rolled-back transactions, what each identity can and cannot do.

## Architecture decision: no privileged identity runs owner-controlled code

**Threat.** `fcb_migrator` owns the bridge, so it can attach code to the tables: triggers, forced row-level policies, defaults, CHECK constraints, rules, views, domains, operators. That code runs with the rights of whoever touches the tables.
- Tested RED, PG16 and PG18: an invoker trigger fired by the administrator's DML ran as the administrator and created a role with its CREATEROLE. A forced policy did the same on a read-write `SELECT`.
- `SET [LOCAL] ROLE fcb_migrator` is **no boundary**: the planted code runs `RESET ROLE` and escalates anyway (tested RED).

**Decision.** Keep `fcb_migrator` as the owner, and remove every path where an identity above it executes owner-controlled code:

| Path | Identity | Why no escalation | Test |
|---|---|---|---|
| Runtime (Render) | `fcb_app` | Planted code runs as `fcb_app` (less privileged than the owner). There is no session user to `RESET ROLE` to | GREEN: trap runs as `fcb_app`, cannot create a role |
| Rollback DSN | **`fcb_migrator`** | It is the owner itself: planted code gains nothing it does not already have | GREEN: trap runs as `fcb_migrator`; real app READY, OAuth + MCP pass |
| Workflow reads (both modes) | administrator, **read-only transactions** | Planted read code (policy) cannot write, cannot create a role, and cannot switch to read-write | GREEN: forced-policy trap fires but nothing is created |
| Roles apply, first run | administrator, read-write | No bridge role exists yet (checked in-transaction). The migrator is created in the same uncommitted transaction, so nobody else's code exists | refusals tested |
| Roles apply, re-run | administrator, read-write | Model `HARDENED` is checked in-transaction **before any write**, from the catalog only. This pins every routine (body md5), trigger, type, operator, rule, policy, RLS flag and relation kind of `public`. The administrator then runs only catalog DDL: `ALTER ROLE`, GRANT/REVOKE, `ALTER … OWNER`, default privileges. Catalog DDL executes no user code; the only DDL hook, event triggers, needs a superuser (tested for `fcb_migrator`, `fcb_app` and the administrator) | GREEN: with a trigger trap and a policy trap planted, the transaction refuses and neither trap fires |
| Emergency `avnadmin` DSN | administrator | **Not a rollback.** It is a last-resort privileged procedure (step 6) because owner code would run with the administrator's rights | documented |

**Alternative considered.** Tables owned by a NOLOGIN owner role that only the migration workflow can `SET ROLE` to.
- It does not change who executes planted code: an identity able to `SET ROLE` to the owner can plant code just as the migrator can.
- The real control is the same either way: the administrator never runs read-write DML/reads on bridge tables, and the model refuses foreign code before a privileged write.
- Kept as an option if a migration workflow (0009+) is built.
- A further hardening, giving the administrator SET without INHERIT in `fcb_migrator` so an accidental administrator DSN fails with `permission denied`, needs another owner decision. It changes the documented membership inventory.

### The runtime allowlist (`fcb_app`)

| Table | Privileges | Runtime path |
|---|---|---|
| `schema_migrations` | SELECT | `bridge/schema_readiness.py` |
| `oauth_authorization_codes`, `oauth_access_tokens`, `oauth_refresh_tokens`, `oauth_claude_dcr_clients` | SELECT, INSERT, UPDATE, DELETE | `bridge/oauth_store.py` (codes, token pair, refresh rotation, DCR) |
| `browser_session_leases` | SELECT, INSERT, UPDATE, DELETE | `bridge/session_leases.py` (lease, stop, reaper) |
| `api_football_quota_counters`, `_leases`, `_attempts` | SELECT, INSERT, UPDATE, DELETE | `bridge/api_football/quota.py` |
| `sxbet_rate_attempts`, `sxbet_rate_cooldowns` | SELECT, INSERT, UPDATE, DELETE | `bridge/sxbet/ratelimit.py` |

No grant was added by WP-009: every runtime path passed through `fcb_app` with the pre-existing allowlist (`tests/integration/test_wp009_roles_runtime_postgres.py`). `finalize_bridge_privileges.sql` lists, per table, which of these privileges the code actually uses; the unused ones (for example DELETE on the token tables) could be narrowed later. That is an owner decision, not part of this change.

## 0. Preconditions

- [ ] The WP-009 PR is merged to `main` by the owner, and its CI (PostgreSQL 16 and 18 integration jobs) is green on the merge SHA.
- [ ] `/healthz` shows `schema_state: READY`. The restore workflow's last diagnose shows `BRIDGE_0001_0008`.
- [ ] `aiven-production` still has a required reviewer (the owner) and deployment branches = *Selected branches*, `main` only. The job checks this and refuses otherwise.

## 1. Environment secrets (owner)

Settings → Environments → `aiven-production` → Environment secrets. Reuse this Environment: it already holds `FCB_DEFAULTDB_URL`, `FCB_AIVEN_CA_PEM` and `FCB_AIVEN_CA_SHA256`, and it is the only Environment the job uses. No new Environment is needed.

Add two secrets:

| Secret | Value |
|---|---|
| `FCB_APP_DB_PASSWORD` | a new random password for `fcb_app` |
| `FCB_MIGRATOR_DB_PASSWORD` | a different random password for `fcb_migrator` |

- Generate each value on your own computer, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The workflow refuses anything but 32–128 characters of `A–Z a–z 0–9 _ -`, two equal values, or a value equal to the `avnadmin` password. This alphabet needs no percent-encoding in a URI.
- Paste without a trailing newline.
- Also keep both values in your password manager. GitHub secrets are write-only, and you need the `fcb_app` value for Render in step 4.
- Never paste a value into an issue, PR, chat, log or screenshot.

`FCB_DEFAULTDB_URL` stays the administrative (`avnadmin`) URI with `sslmode=verify-full`: creating roles needs `CREATEROLE`.

## 2. Dispatch `diagnose` (read-only)

Actions → *FCB Aiven defaultdb roles (WP-009, owner-gated)* → Run workflow from `main`, `mode=diagnose`, then approve the `aiven-production` deployment.

Every session of this mode runs with `default_transaction_read_only=on`. Expected job summary, matching the read-only facts verified on `defaultdb` on 2026-11-09:

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
owners relations session=34
owners routines session=4
schema_public_acl PUBLIC:USAGE pg_database_owner:CREATE pg_database_owner:USAGE
routine_non_owner_grants=4
default_acl none
ROLES_ABSENT
```

- `createrole_self_grant=inherit,set` is **expected** on Aiven (server level). Left on, it would make `avnadmin` an INHERIT+SET member of every role it creates, so it could silently act as `fcb_app` and `fcb_signer`. The apply transaction switches it off with `SET LOCAL createrole_self_grant = ''`. That applies only inside the transaction (source `session`, not a database or role setting), and the transaction checks it is off before any `CREATE ROLE`. The final model check still refuses any INHERIT/SET membership in `fcb_app` or `fcb_signer`.
- Before the first apply there are no `membership …` and no `effective …` lines, because no bridge role exists.
- **Stop and report** if any of these differs:
  - `session_has_public_owner_privileges` is false (`avnadmin` could not grant on `public`);
  - any `role fcb_*` line shows `exists=true` before the first apply. Roles created by hand on Aiven carry the automatic INHERIT+SET grant and are refused (`unexpected_app_or_signer_member`);
  - `owners …` shows anything but `session`;
  - `default_acl` is not `none`.

If the job fails, nothing was written. Report the failing step and its fixed error code (`FCB_…`).

## 3. Dispatch `apply`

Same workflow, `mode=apply`, `confirm` exactly `APPLY WP-009 ROLES TO defaultdb`, then approve the deployment.

Expected summary:

- `Roles: PASS (state before: BRIDGE_0001_0008 / ROLES_ABSENT; run 2 no-op; model after: HARDENED; schema evidence and structure fingerprint <md5> unchanged; owners/ACLs changed by design)`;
- `login fcb_app: tls=True schema_migrations_rows=8 readiness=READY create_denied=True legacy_read_denied=True`;
- `login fcb_migrator: tls=True schema_migrations_rows=8 can_create=True`. The migrator's CREATE probe is rolled back.
- In the final report:
  - `createrole_self_grant=inherit,set` (unchanged server setting);
  - the exact membership inventory. Anything else fails the apply (`membership_inventory`):
    ```
    membership fcb_app <- session admin=true inherit=false set=false grantor=bootstrap
    membership fcb_migrator <- session admin=false inherit=true set=true grantor=session
    membership fcb_migrator <- session admin=true inherit=false set=false grantor=bootstrap
    membership fcb_signer <- session admin=true inherit=false set=false grantor=bootstrap
    ```
    - The three `grantor=bootstrap` rows are PostgreSQL's automatic ADMIN-only grant to the creating role. `createrole_self_grant` was switched off, so they carry neither INHERIT nor SET.
    - The one `grantor=session` row is the explicit INHERIT+SET grant. It lets `avnadmin` hand over ownership, and it keeps rollback working;
  - the effective reach, where only the session reaches `fcb_migrator`:
    ```
    effective session -> fcb_migrator member=true inherits_privileges=true can_set_role=true
    effective session -> fcb_app member=true inherits_privileges=false can_set_role=false
    effective session -> fcb_signer member=true inherits_privileges=false can_set_role=false
    ```
    All 9 `effective fcb_* -> …` lines are `member=false inherits_privileges=false can_set_role=false`;
  - `owners relations fcb_migrator=34`, `owners routines fcb_migrator=4` (34 = 33 tables + 1 sequence);
  - `schema_public_acl fcb_app:USAGE fcb_migrator:CREATE fcb_migrator:USAGE pg_database_owner:CREATE pg_database_owner:USAGE`;
  - the 11 `grant fcb_app …` lines;
  - `routine_non_owner_grants=0`;
  - `default_acl fcb_migrator:f:all_schemas:grantees=0`;
  - `HARDENED`.

Any failure before COMMIT leaves nothing behind. A re-run is safe: it is accepted when the model is already `HARDENED`, and it re-sets both passwords from the secrets (same values, new salt).

**Production effect right after apply.** Render still connects as `avnadmin`, which keeps access through its INHERIT membership in `fcb_migrator`.
- **Until step 4 is done, `avnadmin`'s runtime DML is exposed to code `fcb_migrator` could plant.**
- The `fcb_migrator` password exists only in the Environment and in your password manager, and nothing logs in with it.
- Still, do step 4 right after step 3, and run `diagnose` (`HARDENED`) just before it.

From now on, the restore workflow's precheck reports `REFUSE_DEFAULT_PRIVILEGES` for `defaultdb`. This is expected (see *Residual risks*).

## 4. Switch Render to `fcb_app` (owner)

1. Build the **rollback DSN** and keep it in your password manager. It is the `fcb_migrator` DSN, same shape as below, with `FCB_MIGRATOR_DB_PASSWORD`. Also keep the old `avnadmin` DSN, but only for the emergency procedure of step 6.
2. Build the new DSN, same host, port and database as today:
   `postgresql://fcb_app:<FCB_APP_DB_PASSWORD>@<aiven-host>:14274/defaultdb?sslmode=verify-full`
3. TLS `verify-full` on Render (WP-008). If it is not configured yet, add it in the same change:
   - a Render **Secret File**, for example `aiven-ca.pem`, holding exactly the bytes of `FCB_AIVEN_CA_PEM` (the authenticated Aiven project CA). Check its SHA-256 against `FCB_AIVEN_CA_SHA256` before saving;
   - `PGSSLROOTCERT=/etc/secrets/aiven-ca.pem`;
   - `FCB_DATABASE_TLS_VERIFY_FULL=1` (`app/db.py` then refuses any DSN without `sslmode=verify-full` or a missing CA file).
4. Set `DATABASE_URL` to the new DSN and save. Render redeploys.
   - **Timing:** pick a quiet moment. On the free plan the switch costs one redeploy (the same short interruption as any deploy). No database change happens at this step.

## 5. Verification (owner, then the agent may repeat the read-only parts)

- [ ] Render deploy `live`. Logs show `SCHEMA_READINESS state=READY`, and no `permission denied`, no `InsufficientPrivilege`, no `DATABASE_TLS_*`, no `Browser reaper unavailable`.
- [ ] `/healthz`: `ok`, `schema_state: READY`, `schema_missing_tables: []`, tool count unchanged.
- [ ] `/mcp` without a token → **401**.
- [ ] Dispatch *FCB MCP E2E on the real Render deployment* (`fcb-mcp-e2e.yml`). It must pass: OAuth registration, authorization code, token issuance, refresh, and read-only MCP calls now go through `fcb_app`.
- [ ] Reconnect one real client (Claude or ChatGPT connector): authorization and token issuance succeed; `tools/list` works.
- [ ] Optional: dispatch the roles workflow with `mode=diagnose`. It must report `Roles model: HARDENED`.
- [ ] Record on #130: run ids, merge SHA, Render deploy id, results. Separate VERIFIED from ASSUMED.

## 6. Rollback

**Default rollback: Render `DATABASE_URL` = the `fcb_migrator` DSN** (step 4.1).
- It is runtime-compatible: it owns every bridge table.
- Tested: the real app reaches `/healthz` READY and completes DCR, consent, code exchange, refresh, MCP `initialize` and `tools/list`.
- Code it could have planted runs as itself, so there is no escalation.
- Use it when `fcb_app` misbehaves, for example an unexpected `permission denied`. Report the error so the allowlist can be fixed. Then go back to `fcb_app`.

**Emergency only: the `avnadmin` DSN.** Use it only if both `fcb_app` and `fcb_migrator` cannot log in (for example, passwords lost).
- Risk: every runtime write would then execute any code `fcb_migrator` attached to the tables with the administrator's rights, including CREATEROLE.
- Preconditions, all required:
  1. Immediately before switching, dispatch the roles workflow with `mode=diagnose`. It must say `Roles model: HARDENED`, which pins every routine, trigger, policy, rule and relation kind.
  2. Nobody holds or uses the `fcb_migrator` credential while the emergency DSN is live.
  3. Leave it as soon as one bridge identity works again: re-run `apply` with new secrets, then switch to `fcb_app`.
- The residual race is a migrator session planting code after the diagnose. It is accepted only for this emergency.
- The roles stay. They are additive.
- Do **not** drop roles or reassign ownership as part of a rollback. Undoing the model is a separate owner decision with its own reviewed SQL.

## 7. Other consumers of the administrative identity

- `api-football-live` / `APIFOOTBALL_COORDINATOR_DATABASE_URL`: the live API-FOOTBALL workflows use it only for the shared quota coordinator. After step 5 succeeds, set it to the same `fcb_app` DSN, then run one live smoke with the owner's approval. If the workflow host lacks the CA, keep the current value until WP-008 covers it.
- `aiven-production` / `FCB_DEFAULTDB_URL` and `aiven-pg18-test` / `FCB_PG18_TEST_URL` stay administrative: role creation and the PG18 test database need it.

## 8. Rotate the `avnadmin` password (after step 5 and step 7)

Order, so that nothing running uses a stale password:

1. Confirm nothing at runtime still uses `avnadmin`:
   - Render `DATABASE_URL` is `fcb_app` (step 4);
   - `APIFOOTBALL_COORDINATOR_DATABASE_URL` is `fcb_app` or not used by any scheduled run (all workflows are manual).
2. Aiven Console → `piero-1` → Users → `avnadmin` → Reset password. Existing sessions are not cut. New logins need the new password.
3. Immediately update:
   - `aiven-production` / `FCB_DEFAULTDB_URL`;
   - `aiven-pg18-test` / `FCB_PG18_TEST_URL`.

   Keep the same URI shape and `sslmode=verify-full`, and percent-encode the password if needed.
4. Verify: dispatch this workflow with `mode=diagnose` (`Roles model: HARDENED`) and the PG18 workflow with `mode=diagnose`.
5. Rebuild the emergency `avnadmin` DSN of step 4.1 with the new password. The `fcb_migrator` rollback DSN is unaffected.

**Downtime:** none for the MCP service, because the runtime no longer uses `avnadmin`. The manual workflows only need the two secrets updated before their next dispatch.

**Rotating `fcb_app` later:**
1. Update `FCB_APP_DB_PASSWORD`.
2. Dispatch `apply` (it re-sets both passwords).
3. Update Render `DATABASE_URL` right after.

Between steps 2 and 3, new Render connections fail, so do it in a quiet moment.

## Residual risks and follow-ups (not solved here)

- **Future migrations.** After hardening, the restore workflow refuses `defaultdb` (`REFUSE_DEFAULT_PRIVILEGES`, objects not owned by the session). Migration 0009+ needs a new owner-gated workflow that applies as `fcb_migrator` and grants the runtime explicitly. The restore precheck must learn the hardened state. This is a follow-up WP. Until then, no bridge migration can run on production.
- **`fcb_migrator` LOGIN has no consumer yet.** Its password exists only in the Environment. It becomes useful with the follow-up above.
- **Temporary tables.** `fcb_app` can still create temporary tables (PostgreSQL's database-level PUBLIC `TEMPORARY`). They are session-local and cannot touch the bridge. Revoking PUBLIC `TEMPORARY` on `defaultdb` is a database-wide change and is left to the owner.
- **Unused DML privileges** in the allowlist (see the table above). Narrowing them is optional.
- **`avnadmin` remains a powerful network-reachable credential.** Aiven `ip_filter` is `0.0.0.0/0` and termination protection is off (audit P1-3/P1-4). WP-009 removes it from the runtime but does not restrict the network.
- **Aiven shape.** Verified read-only on 2026-11-09:
  - `createrole_self_grant=inherit,set`;
  - `public` owned by `pg_database_owner`, and `avnadmin` a member of it;
  - database owned by `avnadmin`;
  - no role or database settings;
  - a single owner for every `public` object.

  The local evidence ran on a cluster started with the same `createrole_self_grant` and the same ownership shape. Anything else that differs in production fails closed at apply.
- **SCRAM verifiers on the server side.** The workflow never writes a verifier to stdout, stderr, the step summary, an artifact or argv (tested by running its bash). It does not even echo one as `::add-mask::`, and psql stderr is redacted.
  - On the server, the `ALTER ROLE … PASSWORD 'SCRAM-SHA-256$…'` statement text can appear, **on success as well as on failure**, in:
    - the server log (`log_statement`, `log_min_error_statement`);
    - `pg_stat_statements`;
    - `pg_stat_activity` while it runs.
  - These are visible to the administrator and Aiven, not to `fcb_app`.
  - A verifier is not a login secret: it cannot be used to log in. It does allow an offline guessing attack against the password, which 32+ random characters defeat.
- **Advisory lock.** `fcb_app` (any login) can take advisory lock `460244120`, which the restore and roles transactions wait on. This is a denial of service on those workflows, not an escalation. The workflows have a 20-minute timeout.
- **Large objects.** Any login can call `lo_create`/`lo_import` (client-side) and fill storage. There are no large objects in the bridge.
- **Other databases.** PUBLIC has CONNECT on every database of the service by default (for example `fcb_pg18_test`), so `fcb_app` can connect to them. It has no privileges inside unless granted. Revoking PUBLIC CONNECT is a database-wide change, left to the owner.
- **Temporary tables:** see above (PUBLIC `TEMPORARY`).
- **Drift is not healed automatically.** If `diagnose` reports `NOT_HARDENED:<codes>`, every apply refuses. The owner investigates with the codes; no automatic repair writes over an unexplained change.
- **Self password change.** `fcb_app` and `fcb_migrator` can change their own password (`ALTER ROLE CURRENT_USER PASSWORD`), as any PostgreSQL login can. Whoever holds the runtime DSN could lock the service out, but could not gain privileges.
- **Evidence:** local runs on PostgreSQL 16.15 and PostgreSQL 18.6 (PGDG), each with `createrole_self_grant=inherit,set` set server-wide. The tests also set it per session, so the CI PostgreSQL 16 and 18 jobs exercise the Aiven shape.

This runbook prepares WP-009. It does not certify it. WP-009 becomes PRODUCTION_VERIFIED only after steps 2–5 pass in production and are recorded on #130.
