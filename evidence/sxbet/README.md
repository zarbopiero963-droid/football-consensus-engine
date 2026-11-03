# SX Bet read-only audit: archived evidence (FCB-WP-011)

| Field | Value |
|---|---|
| Workflow | `SX Bet Full API Read-Only Audit (Live Read-Only Certification)` (`.github/workflows/sxbet-live-readonly-certification.yml`) |
| Run | 41320538088 (`workflow_dispatch`, branch `main`, conclusion `success`, 2026-11-01T15:18:20Z) |
| Code SHA | `d569e6e65f2777b36e0def2e8ea2bdcd46030b78`. `bridge/sxbet` and `signer` are unchanged from `d569e6e` to `342fed6`. |
| Artifact | `sxbet-live-readonly-report` (id 13089715340, GitHub expiry 2026-12-01T15:21:14Z) |
| Artifact ZIP SHA-256 | `5d445e5d736fe6fb4b9e3e960e1a6424972b8163e5d133c08a1dcf590245d6da` |
| Archived file | `readonly_audit_run41320538088.json`: byte-identical to `sxbet_live_readonly.json` inside the ZIP |
| File SHA-256 | `2582808205479f6023bcbd7f3a83359459b55f122f3d91b66be5458e7d07db9b` |

**Contents:**
- the suite, network (mainnet) and mode `READ_ONLY`;
- `api_key_present: true`, a boolean only;
- `write_operations_attempted: 0`;
- outcome counts: 46 PASS / 13 EMPTY_VALID / 3 NOT_TESTED / 1 PARTIAL / 0 FAIL;
- per-test result shapes, the MCP tool list, and public market hashes.

**Sanitization check before archiving:** no API key or token, no private key or seed, no wallet address. `tests/test_sxbet_evidence_archive.py` pins the hashes and re-checks these properties.

**Scope:** this is read-only evidence for SX Bet market data and account reads. It is **not** evidence for trading, signing or SIMULATION→LIVE (CLAUDE.md §2.6).

**Coordinator note:** the run used `LOCAL_PROCESS` rate coordination, as a GitHub Actions run without the database. The production runtime uses `POSTGRES_SHARED`.
