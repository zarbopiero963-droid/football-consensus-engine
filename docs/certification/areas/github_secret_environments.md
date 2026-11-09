# GitHub secrets: protected environments (G-02)

**Problem fixed in code.** Gap G-02 from the 2026-11-09 audit: `fcb-aiven-pg18-test.yml` used `FCB_PG18_TEST_URL` in a job with no environment, so the secret had to be repository-level. On Aiven `piero-1` the only database user is `avnadmin`, so that credential also reaches production `defaultdb`. Any workflow on any branch, including a same-repository pull request, could therefore receive it and bypass every gate of the `defaultdb` restore workflow.

**Evidence of misuse.** None found for this workflow: all 14 runs of `fcb-aiven-pg18-test.yml` were `workflow_dispatch` on `main` by the owner. Other workflows on other branches cannot be ruled out from here.

## What the code now enforces

Statically, in `tests/test_workflow_secret_policy.py`, on every PR:

| Rule | Detail |
|---|---|
| Manual trigger only | A workflow that references any secret other than `GITHUB_TOKEN` may only be triggered by `workflow_dispatch`. |
| No workflow-level secrets | Secrets are never referenced at workflow level. |
| Environment and branch | Every secret job declares a named `environment` and `if: github.ref == 'refs/heads/main'`. |
| Database credentials | A job that receives a database credential receives it only at step level, after two gate steps (below). |

The two gate steps, run in-job:

- `scripts/require_owner_main_dispatch.sh` requires all of these: event `workflow_dispatch`, ref `main`, actor and triggering actor equal to the repository owner.
- `scripts/require_protected_environment.sh <repo> <env>` requires both:
  - at least one required reviewer;
  - a deployment-branch policy that is either *protected branches only*, or custom with exactly the branch `main`. If there is no policy, or the policy is wider, the job is refused.

The in-job gates are **additional layers**. A workflow file can be edited on a branch, so the access control is the environment configuration below, which only the owner can set.

## Owner actions (exact)

Repository → **Settings → Environments**.

1. **Create** the environment `aiven-pg18-test`:
   - **Required reviewers:** the owner account.
   - **Deployment branches and tags:** *Selected branches and tags* → add branch rule `main`, nothing else.
2. **Move the secret** `FCB_PG18_TEST_URL`:
   - add it as an *environment secret* of `aiven-pg18-test` (same value);
   - then **delete the repository secret** `FCB_PG18_TEST_URL` (Settings → Secrets and variables → Actions → Repository secrets).
3. **Check** that `aiven-production`, `api-football-live` and `sxbet-live-readonly` each have:
   - a required reviewer (the owner);
   - *Deployment branches: Selected branches* = `main` only.

   The workflows for `aiven-production` and `api-football-live` now refuse to run otherwise. Each live API-FOOTBALL run will ask for a deployment approval.
4. **Check** that no other repository-level secret holds a database credential (Settings → Secrets → Repository secrets). The only repository-level secrets should be ones that no workflow uses, or that hold no database or provider credential.
5. **Rotate the `avnadmin` password** (Aiven Console → `piero-1` → Users → `avnadmin` → Reset password), because it was readable from any branch.
   - **When:** together with WP-009, so that values are updated once. Rotating sooner is safe but means updating everything twice.
   - **Update after rotating:**
     - Render `DATABASE_URL`;
     - `aiven-production` / `FCB_DEFAULTDB_URL`;
     - `api-football-live` / `APIFOOTBALL_COORDINATOR_DATABASE_URL`;
     - `aiven-pg18-test` / `FCB_PG18_TEST_URL`.
   - **Target state after WP-009:**
     - the runtime uses `fcb_app`;
     - migrations use `fcb_migrator`;
     - `FCB_PG18_TEST_URL` uses a role whose `CONNECT` is limited to `fcb_pg18_test` and is revoked on `defaultdb`.

**Never paste a value into an issue, PR, chat or log.**

## Verification after the owner actions

Dispatch `FCB Aiven PG18 migration test` from `main` with `mode=diagnose`.

- **Expected:**
  - the gate step prints `environment aiven-pg18-test protected (required reviewers: 1)`;
  - GitHub asks for the deployment approval;
  - the read-only prechecks pass.
- **From any other branch:** the job is skipped (`if:`) or refused by the gate. Without the branch policy, the environment would not release the secret.
