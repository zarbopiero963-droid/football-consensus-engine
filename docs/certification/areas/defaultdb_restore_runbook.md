# FCB-WP-005: restoring `defaultdb` (owner runbook)

Workflow: `.github/workflows/fcb-aiven-defaultdb-restore.yml`. It is manual-only and diagnose by default.

It restores the production database `defaultdb` (Aiven `piero-1`, PostgreSQL 18) to the bridge migration series 0001–0008 (`sql/aiven/aiven_migrations_bridge_0001_0008.sql`, pinned SHA-256 `a6525b5b…`). The signer series is **never** applied.

## What the agent can and cannot do
- The agent prepared and tested the workflow on PostgreSQL 16 and 18 in CI (`tests/integration/test_defaultdb_restore_postgres.py`, `tests/test_defaultdb_restore_workflow.py`).
- The agent does **not** create the environment, does **not** create the secret, and does **not** start the run. Each of the three steps below belongs to the owner. The apply also needs an explicit owner confirmation (mandate: "L'applicazione su defaultdb richiede una conferma finale esplicita dell'owner").

## Gate availability (verified)
The repository is **public**. On every GitHub plan, including Free, environment required reviewers are available for public repositories, so the `aiven-production` gate can be used.

The job adds two gates that do not depend on the plan:
- `github.triggering_actor` and `github.actor` must both be the repository owner;
- the run must start from `refs/heads/main`.

The runtime tables must be owned by the role that runs the restore. Today the application and the restore use the same Aiven role. If they ever differ (WP-009), the restore refuses until the owner aligns ownership.

Workflow logs of a public repository are public. The workflow prints only fixed categories, counts, migration names and checksums, never the URI, host, user or password.

## Owner steps

The reproducible Path A / Path B comparison and current verdict are in
[`wp005_ab_decision.md`](wp005_ab_decision.md). The selected workflow must be green on its
exact SHA before any production run.

1. **Create the GitHub Environment `aiven-production`:** Settings → Environments → New environment.
   - Add **Required reviewers**: the owner.
   - Optionally restrict deployment branches to `main`.
   - The job checks this through the API and refuses to run if the environment has no required reviewer. An environment that GitHub auto-creates without protection is therefore refused.
2. **Add the protected inputs inside that environment only**, not as repository secrets.
   - `FCB_DEFAULTDB_URL`: the Aiven URI of `defaultdb`, with exactly
     `sslmode=verify-full`, the pinned host and port `14274`, and path `/defaultdb`.
   - `FCB_AIVEN_CA_PEM`: the authentic project CA acquired from the authenticated Aiven
     console/API, never a generated or third-party certificate.
   - `FCB_AIVEN_CA_SHA256`: SHA-256 independently calculated from those exact CA bytes.
   - The target check refuses any other host, port, database, query parameter or whitespace.
   - Least privilege (WP-009): a dedicated migrator role is preferable to `avnadmin`. Until WP-009 exists, the owner decides which credential to use.
3. **Run `mode=diagnose`.** It is read-only and prints:
   - the TLS and PostgreSQL 18 checks;
   - the precheck state (the allowed states are listed in the next section).

   Anything else is refused.
4. **Run `mode=apply`** with confirmation `APPLY BRIDGE 0001-0008 TO defaultdb` only after an explicit owner decision. The run:
   - uses **one transaction** containing: the target guard, the state guard (`defaultdb_restore_guard.sql`, generated from the two prechecks, evaluated under the bridge advisory lock), the bridge script, and the object-count assertion (`assert_bridge_counts.sql`);
   - ends with the post guard (`defaultdb_restore_post_guard.sql`, generated): the end state must be `BRIDGE_0001_0008` and the md5 catalog fingerprint (`fingerprint_bridge_schema.sql`) must equal `EXPECTED_FINGERPRINT`, pinned on PostgreSQL 18. A tampered 0001–0007 object (function body, disabled trigger, extra column, privilege) therefore aborts the transaction;
   - commits nothing if any of them fails, including a count mismatch;
   - forces `search_path=public` through `PGOPTIONS` on every psql connection, which overrides database and role settings; `pg_catalog` stays implicitly first;
   - runs the script a second time, which must be a no-op;
   - verifies the pinned object counts and the recorded checksums;
   - requires the final state `BRIDGE_0001_0008`.

## Precheck states
| State | Meaning | Allowed |
|---|---|---|
| `EMPTY` | no object or type in `public`, and no extension (except `plpgsql`), large object, subscription, prepared transaction or replication slot of this database | yes |
| `RUNTIME_ONLY` | only the tables the running code creates itself (SX Bet rate coordinator, API-FOOTBALL quota coordinator, OAuth DCR), each **exactly** as the code creates it: columns, types, NOT NULL, defaults and constraints identical; indexes a subset; no rule, trigger, policy, RLS or inheritance | yes, data preserved |
| `REFUSE_RUNTIME_TABLE_DRIFT` | a runtime-named table that differs, checked in every state (tracked or not): definition, collation, UNLOGGED, reloptions, replica identity, privileges, owner other than the restoring role, RLS, rules, triggers, policies, inheritance, identity or generated columns. It would be skipped by `IF NOT EXISTS` and certified | no |
| `REFUSE_UNSAFE_SEARCH_PATH` | `pg_catalog` is not first in the search path, the current schema is not `public`, or a database/role `search_path` setting exists | no |
| `REFUSE_UNSAFE_SETTINGS` | any other setting of this database (any role), of the session role, or of every role (`ALTER ROLE ALL`), e.g. `role` | no |
| `REFUSE_UNEXPECTED_SCHEMAS` | a non-system schema other than `public`; temporary schemas are matched by the anchored regex `^pg_(toast_)?temp_[0-9]+$`, and `_avnadmin_monitor` is ignored only in Aiven's real shape (owned by the `_avnadmin_monitor` role or a superuser, not by the session role, which is neither a member of the owner nor has CREATE on it) | no |
| `REFUSE_EVENT_TRIGGERS` | an enabled event trigger exists; it would act on the restore's own DDL | no |
| `REFUSE_DEFAULT_PRIVILEGES` | `ALTER DEFAULT PRIVILEGES` entries exist; they would grant on the new tables | no |
| `BRIDGE_0001_0007` | exactly 0001–0007, names and checksums | yes (upgrade to 0008) |
| `BRIDGE_0001_0008` | already restored | yes (no-op) |
| `REFUSE_UNTRACKED_OBJECTS` | any other table, function or type | no |
| `REFUSE_PARTIAL_OR_DRIFT` | partial history or checksum mismatch | no |
| `REFUSE_PRE_SPLIT_SIGNER_0008` | v8 is the old signer anchor | no |
| `REFUSE_SIGNER_SERIES_PRESENT` | signer tracking found in `public` | no |

`REFUSE_UNTRACKED_OBJECTS` also covers operators, operator classes and families, collations, conversions, text-search objects, extensions, aggregates, window functions and procedures in `public`, plus any publication. 0001–0008 create plain functions only.

In every state, tracked ones included (audit of 2026-11-09, F2), `REFUSE_UNTRACKED_OBJECTS` also covers: a function that is not one of the four pinned bridge trigger functions (name, arguments, return type, language, SECURITY, configuration and md5 of the body); a trigger that is not one of the nine pinned bridge triggers (enabled, no arguments, columns or WHEN); a type that is not the row or array type of a relation; and any rule, policy, RLS flag or inheritance on any relation of `public`.

Inside the restore transaction (audit F2–F7): every guard and evidence query is schema-qualified; the guard, the count assertion and the post guard first re-check the session (search path exactly `public`, `CURRENT_USER = SESSION_USER`, no database/role setting, no unexpected schema) and then pin `search_path` again; the advisory lock is `pg_catalog.pg_advisory_xact_lock(460244120::bigint)`; a `BRIDGE_0001_0008` database must already carry the pinned fingerprint before the script runs; after an `EMPTY` prior state every object must carry the restore transaction's xmin.

**Documented residuals:**
- These are not fingerprinted, but they are not created or made worse by the restore: extended statistics, comments, and column STORAGE/attoptions on non-runtime tables.
- The fingerprint ordering uses the database collation. Results were identical under C and ICU en-US; glibc locales were not tested.

On the real `defaultdb`, read-only on 2026-11-03, all of these were 0 and the only extension is `plpgsql`.

The restore also refuses when:
- the database is not `defaultdb`;
- the server is not PostgreSQL 18;
- the connection is not TLS;
- the current schema is not `public`;
- any non-system schema exists other than `public` and Aiven's `_avnadmin_monitor` in its real shape;
- `CURRENT_USER` differs from `SESSION_USER`, `search_path` is not exactly `public`, or a database/role setting other than `search_path` applies to the session.

## After a successful restore
- `/mcp` with a real token is no longer 503.
- `/healthz` stays `ok`.
- The logs no longer show `Browser reaper unavailable`.
- **Browser stays disabled**: `BROWSER_TOOLS_ENABLED=false` and `CLOAKBROWSER_ENABLED=false` remain in place until WP-002b is certified and the owner decides.

## Rollback
- The apply is a single transaction, so a failure leaves nothing behind (tested).
- After a successful restore, a rollback is an owner decision. The migrations are additive and the runtime does not migrate at boot, so a code revert never touches the database.

## WP-005 owner checklist (diagnose → apply → verify)
Values below were read from Aiven read-only on 2026-11-03. Re-check them before the run.

- [ ] Before requesting an apply decision, complete the Path A / Path B comparison and publish the evidence-backed GO/NO-GO in [`FCB_PR_WORKPACKAGES.md`](../FCB_PR_WORKPACKAGES.md).
- [ ] Confirm that the owner approved the definitive workflow selected from that comparison.

### 1. Backups (before any write)
- [ ] Aiven Console → `piero-1` → **Backups**:
  - the latest automatic backup is recent. Schedule: daily at 21:11 UTC. Observed: full `20261101-224828F` and incremental `20261102-211103I`;
  - the plan (`developer-1`) keeps backups for its retention window. Note the restore point you would use.
- [ ] Optional safety net. `defaultdb` is empty today, so there is no data to lose; this matters only if data appears first. Fork the service from the latest backup and keep the fork until verification passes. **The fork is a new paid resource: owner decision.**
- [ ] Recommended: enable **termination protection** on `piero-1`. It is `false` today.

### 2. PostgreSQL permissions
- [ ] The URI in `FCB_DEFAULTDB_URL` uses the same role the application uses. Today that is `avnadmin`, the only user. The restore refuses runtime tables owned by another role.
- [ ] That role can `CREATE` in schema `public` of `defaultdb`, and no `ALTER DEFAULT PRIVILEGES` entries exist. On 2026-11-03 there were 0.
- [ ] After WP-009, re-check with the dedicated migrator role. Until then, `avnadmin` is the owner's decision.

### 3. GitHub gates
- [ ] Create the environment `aiven-production` with **Required reviewers: owner**. Optionally restrict deployment branches to `main`.
- [ ] Add protected `FCB_DEFAULTDB_URL` with exactly `sslmode=verify-full`, pinned host,
  port `14274`, and path `/defaultdb`.
- [ ] Add authentic `FCB_AIVEN_CA_PEM` and independently calculated
  `FCB_AIVEN_CA_SHA256`; the workflow must parse the certificate and match the checksum.

### 4. Diagnose (read-only)
- [ ] Run the workflow with `mode=diagnose`, from `main`, as the owner.
- [ ] Expected:
  - target check PASS, PostgreSQL 18, TLS;
  - precheck state `EMPTY`, or `RUNTIME_ONLY` if the application created its own tables meanwhile.
- [ ] Any `REFUSE_*` state means **stop**. Do not apply; report the state on #130.

### 5. Apply (single transaction)
- [ ] Run `mode=apply` with confirmation `APPLY BRIDGE 0001-0008 TO defaultdb`.
- [ ] Expected summary:
  - `Restore: PASS`;
  - state before `EMPTY` or `RUNTIME_ONLY`, after `BRIDGE_0001_0008`;
  - second run a no-op;
  - counts `tables=33 functions=4 triggers=9 indexes=60 constraints_information_schema=325 constraints_pg_constraint=65 schema_migrations_rows=8`;
  - 8 checksums identical to `sql/migrations/bridge`.
- [ ] If the job fails at any step, nothing was committed: the whole restore is one transaction. Report the step and the fixed code.

### 6. Rollback (only if a committed restore must be undone)
- The migrations are additive and the runtime never migrates at boot, so a **code revert never touches the database**.
- Undoing a committed restore is an **owner decision**. Two options:
  - restore or fork from the backup noted in step 1;
  - if the database held nothing else, drop the objects created by 0001–0008.

  The agent never drops production objects without an explicit owner mandate.

### 7. Post-restore verification (the agent can run it; read-only)
- [ ] `/healthz`: `ok`, `schema_state: READY`, `schema_missing_tables: []`, `mcp_tools_total: 122`.
- [ ] `/healthz`: `browser_tools_enabled: false` and `cloak_enabled: false`, **still disabled**.
- [ ] `/mcp` without a token returns **401**.
- [ ] OAuth with a real client (Claude/ChatGPT connector):
  - registration and authorization succeed;
  - `initialize` and `tools/list` return 122 tools, no longer 503.
- [ ] One read-only call per component:
  - `apifootball_health` (no provider request);
  - `sxbet_health`;
  - `browser_health`, which must report disabled.
- [ ] Render logs:
  - `SCHEMA_READINESS state=READY`;
  - no `UndefinedTable`;
  - no `Browser reaper unavailable`.
- [ ] Only then mark WP-005 **PRODUCTION_VERIFIED** on #130, with run id, SHA, counts and the client used.
