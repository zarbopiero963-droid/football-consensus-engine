# Read-only scoring / M24, PR43-A

Pure numerical implementation is available. M24's real settled comparison DoD
remains pending until safe sealed predictions, certified benchmark prices and
field-level VERIFIED outcomes coexist. No production scoring policy, strategy,
promotion, simulation or ticket is created.

```bash
python -m analysis.scoring.run --neon --known-at 2026-10-01T16:11:13.602+02:00 \
  --methods footinsights pinnacle_open pinnacle_close \
  --policy analysis/scoring/diagnostic_policy.json
```

`--input` accepts exported `{forecasts, observations}` instead of a DB connection.
Policy is mandatory and hashed into the report; the bundled file is an explicit
diagnostic configuration, **not** an approved production preregistration. Fixed
bin edges, clipping, minimum occupied-bin count, bootstrap replicates/seed and
confidence level are visible. Scores or bin settings never retune a strategy.

## Numerical definitions

Score inputs must be complete finite unit-scale simplexes; scale 100 is rejected.
The FI storage adapter reuses M25's explicit market-vector normalization (including
numeric CSV strings and its fixed rounding tolerance), retaining exact stored
components and the adapter contract version. It does not repair missing sides.
Real model zeros remain in `p_raw` and in the sample regardless of outcome.
Log scoring alone floors/caps all components at epsilon/1-epsilon then
renormalizes, recording `p_scored`. Brier and RPS use the unchanged raw vector.
Multiclass Brier is the **sum** of component squared errors. Binary Brier is
the single-component squared error. Binary metrics are pure functions only;
this first DB report evaluates FT_1X2 and does not imply multi-market settlement.

RPS uses explicit order `[1,X,2]` and cumulative squared errors divided by K-1.
No market-family scores are mixed. CLV is not RPS and remains NOT_COMPUTED until
there is a separate frozen selection/price evaluation contract.

Calibration is one-versus-rest per outcome with fixed outcome-blind bins. ECE
retains all observations, including small bins. Murphy is `MURPHY_ESTIMATE`,
with the binning residual shown; insufficient occupied-bin counts suppress the
decomposition. Same-sample frequencies are `IN_SAMPLE_BASE_RATE_REFERENCE`,
never an out-of-sample predictive baseline.

## Common sample and quality

All requested methods use exactly the same fixture IDs and denominator. A missing
requested method yields an empty comparison; it is never silently dropped.
Coverage before the intersection is reported separately. Per-method means and
paired differences use fixture bootstrap percentile intervals with a common
explicit seed. Singleton/empty samples have null CI. Intervals are IID fixture
diagnostics and do not account for temporal/league clustering; they cannot certify
source independence, stability, promotion or future predictive performance.

Forecast checks run before reading outcomes: FULL/parse OK, complete simplex,
unit scale, documented timezone, safe native/context matching, provenance,
capture <= server seal < kickoff, and capture/seal known at cutoff. Latest valid
prematch is selected; conflicting equal-time rows are ambiguous. Fixture
revisions are excluded. The FI adapter additionally verifies the exact published
`SportsEvent.startDate` raw offset against kickoff, so legacy UTC columns do not
retroactively become timezone evidence. Pilot data never joins by team name.

Pin open/close reuse the certified contract in `app.market_reference`, including
strictly prematch sealing, exact context, raw hash and observed series. Legacy
prices remain uncertified and absent from proper-score comparisons. Close remains
the latest **observed** pre-kickoff proxy; no claim of official bookmaker close.

Results are reconstructed purely from latest provider observations known at
cutoff, final-status only. Existing field consensus is reused; both FT goals
must be independently VERIFIED, nonnegative integers, and observed after kickoff.
Pilot providers, single-source fields, conflicts and future corrections cannot
score. No mutable result table is rebuilt. The SQL adapter enforces one
REPEATABLE READ, READ ONLY transaction and contains SELECTs only.

## Real evidence

`neon_smoke_20261001.json` records an actual Neon export and its input hash with
explicit cutoff/policy. An insufficient report is evidence of missing usable
data, not a performance verdict. Tests use synthetic outcomes only in isolated
PostgreSQL schemas; no fabricated outcome or prediction is inserted into Neon.
# Report dei ticket mostrati

Questo modulo confronta le **fonti**, selezionando l'ultimo snapshot valido
pre-match per metodo. Per valutare esattamente i pronostici mostrati usare
[`analysis.decisions`](../decisions/README.md): probabilità e quote congelate
nel ticket, calibrazione della scelta, protocollo prospettico e verdetti
distinti fra dati insufficienti e vantaggio non dimostrato.
