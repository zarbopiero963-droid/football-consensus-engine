# WP-005 / WP-008 / WP-009 — PostgreSQL 18 Path A/B decision

Mandate: `FCB-WP005-AB-RESTORE-FULL-EXECUTION-20261107`. Baseline:
`285bdd5a5e3e85061eebddced8fd27c0ea265384`.

## Reproduced production failure

On 2026-11-07 the live Render deploy was still the baseline SHA. Current logs showed
`SCHEMA_READINESS state=EMPTY`, `oauth_refresh_tokens` missing, OAuth token HTTP 500,
and the browser lease reaper failing once per minute. This is the RED production state.

## Reproducible harness

The isolated harness uses throwaway PostgreSQL 18 databases and roles with no production
credentials. Path A applies the bridge under the initial administrator and then transfers
ownership. Path B prepares roles first and applies the same bridge under
`fcb_migrator`. Both must end at the same fingerprint, remain idempotent, permit only the
runtime table allowlist through `fcb_app`, and deny schema creation, legacy reads and all
bridge access to `fcb_signer`.

Existing WP-005 tests cover partial/drifted schemas, altered functions/triggers,
unexpected default privileges, search-path attacks, state change between diagnose and
apply, advisory locking, SQL failure before commit and count/fingerprint rollback.

The PostgreSQL 18 TLS job uses controlled test certificates for valid `verify-full`, wrong
hostname, wrong CA, expired certificate and wrong credentials. `sslmode=require` is a RED
control proving encryption alone does not authenticate the server. Test certificates are
never described as the Aiven CA.

## Objective comparison

| Criterion | Path A — schema first | Path B — roles first |
|---|---|---|
| Restore latency | Existing protected administrator path is usable | Role creation and credential distribution must complete first |
| Initial ownership | Administrator, then tested transfer | `fcb_migrator` from creation |
| Final ownership | `fcb_migrator` | `fcb_migrator` |
| Final runtime privilege | Explicit runtime allowlist; no schema `CREATE` | Same |
| Signer isolation | No signer DDL or bridge grant | Same |
| Credential change before recovery | None | Required |
| Failure between stages | Schema restored; hardening remains explicitly pending | MCP remains down until all coordinated identity changes succeed |
| Rollback | Bridge and privilege changes are transactional | Same, with more pre-recovery coordination |
| TLS | `verify-full` is enforced before apply | Same |
| Cost | None | None |

## Decision

**Candidate recommendation: Path A with D02 enforced before apply and D03 immediately
after recovery.** This is not a security waiver. The restore refuses anything other than
`verify-full`, requires authenticated Aiven CA bytes and a separately protected checksum,
and retains every target, Environment, migration, transaction and signer-exclusion gate.
The tested ownership transition reaches the same final D03 model without blocking P0
recovery on new credentials. The administrative identity remains available until the new
roles are verified.

The verdict becomes GO only when exact-head PostgreSQL 18, TLS adversarial, full CI and
independent review are green. Until then: `CANDIDATE_GO / NO_PRODUCTION_WRITE`.

## Rollback

- Before commit, any error, disconnect, state change, count mismatch or fingerprint
  mismatch rolls back the bridge transaction.
- After commit, a failed smoke test is `RESTORED_NOT_VERIFIED`; preserve evidence and do
  not delete data.
- Privilege hardening is transactional. Keep the original administrator until
  `fcb_migrator` and `fcb_app` are verified.
