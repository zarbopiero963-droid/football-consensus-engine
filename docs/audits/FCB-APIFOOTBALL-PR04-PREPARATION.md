# FCB-APIFOOTBALL-INTEGRATION-CERT-01 — PR-04 staging / NO OPEN PR

**Status:** WORKTREE/BRANCH PREPARATION ONLY · 2026-10-25 · €0 ·
**NO MERGE / NO DEPLOY / NO PROVIDER CALL / NO MATCHPILOT WRITE**

## Dependency / one-PR-at-a-time gate

The predecessor [#107](https://github.com/zarbopiero963-droid/football-consensus-engine/pull/107)
remains OPEN at SHA `b4b282ae7da342c417a634f1f8e382e340a9908e`.
This prep branch is deliberately based on that SHA for eventual integration.
It is **NOT** an opened PR and must not be treated as accepted release work.
Do not open PR-04 until #107 has passed its independent review
(or an explicit owner-specific review waiver), merged with owner approval,
and the Render post-merge smoke tests are successful. Reconcile this branch
against the new `main` before opening PR-04; do not assume the SHA remains
current. Any #107 subsequent revisions invalidate the branch baseline.

## Goal

Measure actual substitution potential of FutPythonTrader (FPT) by
API-FOOTBALL for MatchPilot **without changing MatchPilot**. TotalCorner
(TC) remains SUSPENDED, not a proven fallback. Preserve historical FPT
snapshots and the immutable archive issue #40.

Review-only MatchPilot source inventory:
- [#249 — owner-report FPT unavailable](https://github.com/zarbopiero963-droid/MatchPilot/issues/249)
- [#248 — API-FOOTBALL v3.9.3 documentary audit](https://github.com/zarbopiero963-droid/MatchPilot/issues/248)
- [#12 — FPT contract](https://github.com/zarbopiero963-droid/MatchPilot/issues/12)
- [#20 — TotalCorner contract](https://github.com/zarbopiero963-droid/MatchPilot/issues/20)
- [#29 — live board](https://github.com/zarbopiero963-droid/MatchPilot/issues/29)
- [#34 — Strategy Lab and indicators](https://github.com/zarbopiero963-droid/MatchPilot/issues/34)
- [#40 — immutable historical archive](https://github.com/zarbopiero963-droid/MatchPilot/issues/40)
- [#97 — roadmap](https://github.com/zarbopiero963-droid/MatchPilot/issues/97)
- [#102 — Trading Copilot](https://github.com/zarbopiero963-droid/MatchPilot/issues/102)

Actual MatchPilot default branch source paths reviewed:
- `src/providers/futpython/schema.mjs`: source `fieldFamily`,
  `fieldTiming`, null/zero normalization and lineage rules.
- `docs/futpython-certification-2026-10-05.md`: snapshot certificate
  identifies **325 raw unique field names and 325 registry fields**, but
  does not expose a complete machine-readable 325-name registry in the
  report itself. This is historic proof **only for its dated baseline**.
- `migrations/014-fpt-normalized-layer.sql`: filter registry and PIT
  context; no replay without original observed time/version.
- `migrations/015-fpt-query-facts.sql`: FPT typed odds and xG fields.
  Historical FPT xG examples are **not** proof API-FOOTBALL supports
  equivalent xG.
- `src/providers/totalcorner/historical.mjs`: dangerous attacks may be
  present as a TC source-family candidate, **not** a live verified feed.

## FR-01 — Complete field inventory (PENDING REAL REGISTRY EXPORT)

The `bridge/api_football/gap_matrix.py` staging implementation enforces
**exactly 325 unique FPT field entries** with explicit source family and
timing class. There is deliberately **no fabricated 325-field list**
in the repository and no synthetic list may be certified as MatchPilot
data. A verifiable export will be needed from a source-owned registry file
or read-only DB snapshot, with SHA-256 and source commit recorded.

The staged tests use 325 **synthetic** names to test correctness, not
actual provider field names. `build_gap_matrix` refuses 324/326 rows,
duplicates, malformed metadata or unsupported timing classes.
Real FR-01 status: **BLOCKED_ON_COMPLETE_325_FIELD_SOURCE_EXPORT**.

## FR-02 — Field-by-field provider substitution

For all 325 verified fields eventually loaded:
`FPT field/family/timing → API endpoint candidate → provider source
field/units/HT-2T semantics → null/zero and identity mapping →
league/season coverage → source capture & PIT → license →
DIRECT_REPLACEMENT | REPLACEMENT_WITH_TRANSFORMATION |
PARTIAL_REPLACEMENT | TOTALCORNER_REQUIRED | UNAVAILABLE | UNKNOWN`.

Default for every offline candidate is **UNKNOWN**. A lexical name
match, a published endpoint, mock data or a `/leagues` coverage flag
must **never** automatically promote the verdict. `/predictions`
advice or expected-goals model output must not be relabeled historical
shot-based xG or xGoT. Optional status `TOTALCORNER_REQUIRED`
requires evidence and does not mean TC is currently enabled.

Timing:
- FPT `PREMATCH_IDENTITY`: some identity may be known pre-kickoff,
  provider ID joins and timing still need proof.
- FPT `PREMATCH_MARKET_UNTIMED`: unknown quote capture timestamp,
  **no historical PIT certification**.
- FPT `POSTMATCH_OUTCOME`: not a safe pre-kickoff model feature without
  prior observed snapshots.
- API-FOOTBALL `retrieved_at_utc` is NOT automatically
  `provider_generated_at`; cannot reconstruct past observations.

## FR-03 — Product impact

Separate matrices must connect every material FPT field to:
- #29 live clock, live statistics, odds, freshness, data quality.
- #34 indicator/Strategy Lab feature inputs, formulas, testing.
- #102 assistant explanation vs hard trading decision: never use
  incomplete data as a source for generated signals.
- #40 historical PIT/frozen datasets: never overwrite; future-only
  observations must use `observed_at` and consistent versioning.

## FR-04 — All 37 Strategy Lab indicators

The owner requests 37 input-to-output formulas reconciled against #34.
No formula should be inferred from an indicator label.
For each: `ID, formula/version, primitive inputs, unit, time horizon,
freshness, validity for HT/2T/live, AF/TC candidate, degradation,
test vector, verdict`. If the repository lacks a formula, classify
`REQUIREMENTS_UNSPECIFIED`, not PASS.

## FR-05 — Scenario outcomes (none approved)

- R0 — API-FOOTBALL only: UNKNOWN; gaps expected in xG/xGoT, dangerous
  attacks, live price movements and missing past PIT snapshots.
- R1 — API-FOOTBALL + TotalCorner: TC **suspended**, no renewal or calls.
- R2 — API-FOOTBALL + immutable legacy FPT historical: historical-only.
- R3 — API-FOOTBALL + TC + historical FPT: TC remains suspended.
- R4 — no new provider promotion: safe current baseline until evidence.

No scenario is yet a certified **FULL REPLACEMENT**.

## FR-06 — First-month experimental gate (no calls currently)

First obtain specific owner authorization to acquire a provider key
and run real tests. Verify the actual plan/quota on `/status`, provider
header meanings, provider terms (storage/redistribution/betting/ML),
limits and rate semantics, actual price and IP protection. Start with
small samples of major and minor football leagues, both finished and
live fixtures, then compare shot/corner/market fields per period and
observed timestamps. Store raw body/hashes ONLY where license permits.
Stop on quota uncertainty, 429, license restrictions or browser health
regression. All mock-derived fields are `UNKNOWN`, never `REAL_VERIFIED`.

## FR-07 — Migration strategy (proposal only)

No MatchPilot collector, DB, frontend, historical archive, ledger,
prediction or trading-strategy change is permitted by this preparatory
branch. If future evidence supports migration, propose a separate
provider-agnostic adapter with schema-versioned raw data, immutable
origins, unit conversions, temporal validation and dual-read comparison
with explicit stop/rollback gates. Do not reactivate TotalCorner.

## FR-08 — Verdict / owner decisions

- `PR04_PREPARATION_ONLY`: YES.
- `FR01_COMPLETE_325_ACTUAL_FIELD_NAMES`: NO.
- `FR02_325_FIELD_REAL_MAPPING_CERTIFIED`: NO.
- `FR04_37_INDICATORS_CERTIFIED`: NO.
- `REAL_API_TEST_NOT_AUTHORIZED`: YES.
- `MATCHPILOT_REPLACEMENT_NOT_YET_CERTIFIED`: YES.
- `PR04_OPEN`: NO.
- `PR107_CLOSED`: NO (at the time of prep).

Owner action required before beginning official PR-04: close #107 by
approved review or explicit waiver, authorize its merge (automatically
deploys Render), confirm post-merge health, and reopen the PR-04 base
comparison. No new spending or infrastructure is requested by this prep.
