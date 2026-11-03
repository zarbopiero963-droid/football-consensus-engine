# FCB-WP-005: restoring `defaultdb` (owner runbook)

Workflow: `.github/workflows/fcb-aiven-defaultdb-restore.yml`. It is manual-only and diagnose by default.

It restores the production database `defaultdb` (Aiven `piero-1`, PostgreSQL 18) to the bridge migration series 0001–0008 (`sql/aiven/aiven_migrations_bridge_0001_0008.sql`, pinned SHA-256 `a6525b5b…`). The signer series is **never** applied.

## What the agent can and cannot do
- The agent prepared and tested the workflow on PostgreSQL 16 and 18 in CI (`tests/integration/test_defaultdb_restore_postgres.py`, `tests/test_defaultdb_restore_workflow.py`).
- The agent does **not** create the environment, does **not** create the secret, and does **not** start the run. Each of the three steps below belongs to the owner. The apply also needs an explicit owner confirmation (mandate: "L'applicazione su defaultdb richiede una conferma finale esplicita dell'owner").

## Gate availability (verified)
The repository is **public**. On every GitHub plan, including Free, environment required reviewers are available for public repositories, so the `aiven-production` gate can be used.

The job adds a second gate that does not depend on the plan: `github.triggering_actor` and `github.actor` must both be the repository owner.

Workflow logs of a public repository are public. The workflow prints only fixed categories, counts, migration names and checksums, never the URI, host, user or password.

## Owner steps
1. **Create the GitHub Environment `aiven-production`:** Settings → Environments → New environment.
   - Add **Required reviewers**: the owner.
   - Optionally restrict deployment branches to `main`.
   - The job checks this through the API and refuses to run if the environment has no required reviewer. An environment that GitHub auto-creates without protection is therefore refused.
2. **Add the secret `FCB_DEFAULTDB_URL` inside that environment only**, not as a repository secret.
   - Value: the Aiven URI of `defaultdb`, with `sslmode=require` or stronger, the pinned host and port `14274`, and the path `/defaultdb`.
   - The target check refuses any other host, port, database, query parameter or whitespace.
   - Least privilege (WP-009): a dedicated migrator role is preferable to `avnadmin`. Until WP-009 exists, the owner decides which credential to use.
3. **Run `mode=diagnose`.** It is read-only and prints:
   - the TLS and PostgreSQL 18 checks;
   - the precheck state (the allowed states are listed in the next section).

   Anything else is refused.
4. **Run `mode=apply`** with confirmation `APPLY BRIDGE 0001-0008 TO defaultdb` only after an explicit owner decision. The run:
   - uses **one transaction** containing: the target guard, the state guard (`defaultdb_restore_guard.sql`, generated from the two prechecks, evaluated under the bridge advisory lock), the bridge script, and the object-count assertion (`assert_bridge_counts.sql`);
   - commits nothing if any of them fails, including a count mismatch;
   - runs the script a second time, which must be a no-op;
   - verifies the pinned object counts and the recorded checksums;
   - requires the final state `BRIDGE_0001_0008`.

## Precheck states
| State | Meaning | Allowed |
|---|---|---|
| `EMPTY` | no object in `public` | yes |
| `RUNTIME_ONLY` | only the tables the running code creates itself (SX Bet rate coordinator, API-FOOTBALL quota coordinator, OAuth DCR), each **exactly** as the code creates it: columns, types, NOT NULL, defaults and constraints identical; indexes a subset; no rule, trigger, policy, RLS or inheritance | yes, data preserved |
| `REFUSE_RUNTIME_TABLE_DRIFT` | a runtime-named table whose definition differs (it would be skipped by `IF NOT EXISTS` and certified) | no |
| `BRIDGE_0001_0007` | exactly 0001–0007, names and checksums | yes (upgrade to 0008) |
| `BRIDGE_0001_0008` | already restored | yes (no-op) |
| `REFUSE_UNTRACKED_OBJECTS` | any other table, function or type | no |
| `REFUSE_PARTIAL_OR_DRIFT` | partial history or checksum mismatch | no |
| `REFUSE_PRE_SPLIT_SIGNER_0008` | v8 is the old signer anchor | no |
| `REFUSE_SIGNER_SERIES_PRESENT` | signer tracking found in `public` | no |

The restore also refuses when:
- the database is not `defaultdb`;
- the server is not PostgreSQL 18;
- the connection is not TLS;
- the current schema is not `public`;
- any non-system schema exists other than `public` and Aiven's `_avnadmin_monitor`.

## After a successful restore
- `/mcp` with a real token is no longer 503.
- `/healthz` stays `ok`.
- The logs no longer show `Browser reaper unavailable`.
- **Browser stays disabled**: `BROWSER_TOOLS_ENABLED=false` and `CLOAKBROWSER_ENABLED=false` remain in place until WP-002b is certified and the owner decides.

## Rollback
- The apply is a single transaction, so a failure leaves nothing behind (tested).
- After a successful restore, a rollback is an owner decision. The migrations are additive and the runtime does not migrate at boot, so a code revert never touches the database.
