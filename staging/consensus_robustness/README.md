# Read-only consensus robustness

Research harness for master M18 / issue40-A, with M19 / issue40-B fixtures.
It never emits strategy signals, changes eligibility or writes production data.

```bash
python -m staging.consensus_robustness.run --neon
python -m staging.consensus_robustness.run --input rows.json --known-at 2026-10-01T13:00:00Z
```

The Neon adapter opens a PostgreSQL transaction with `READ ONLY` before its
single SELECT. No migrations or reconciliation run. Reports go to stdout.
`--source-roles roles.json` supplies research classifications only; unclassified
pilot sources are excluded. The default only recognizes FootInsights as the
current model candidate anchor. A model candidate label does not prove source
independence or confer production eligibility. Pinnacle is always excluded from
the ordinary pool, even if mislabeled as a model.

Input rows carry `row_id`, `source`, `match_id`, `probabilities` (ordered H/D/A),
explicit `raw_scale` (1 or 100), `role`, `parse_status`, `match_status`,
`timezone_status`, `kickoff_utc`, `captured_at_utc`, and `sealed_at_utc`.
Cross-source matches require explicit `EXACT_PAIR_TIME_CONTEXT` or
`CANONICAL_PAIR_TIME_CONTEXT` evidence; legacy `FOUND` is not certified.
`ANCHOR_NATIVE` refers only to an anchor's own fixture, never a pilot join.
Timezone requires source proof or an explicit offset; `VERIFIED_BROWSER_TZ`
is rejected. Acquisition and sealing must precede kickoff and knowledge time.
The current pilot schema has no server sealing timestamp, so the adapter keeps
that missing unless an explicit audit value exists; run finish does not fill it.
Adding reliable pilot sealing audit is separate future schema work, not backfill.

Complete finite triplets permit genuine zero probabilities. Missing stays missing.
Input sum tolerance is fixed at 1% for published rounding; complete vectors are
explicitly normalized and raw scale/values remain in provenance. A 0–100 vector
declared as 0–1 is rejected. No probability components are inferred.

Latest eligible acquisition per source/fixture is selected before diagnostics;
repeated slots never add votes or independent samples. Conflicting simultaneous
vectors and multiple kickoff revisions block that source or fixture respectively.
Diagnostics require at least two eligible model sources. Source coverage includes
single-source groups; multisource coverage has its own denominator.

T1–T8 include linear/log shadow, entropy, TV, component sigma, LOO, exact and
slightly noisy clone attacks, supplied cluster cap, near-zero veto, epsilon grid
`[1e-6, 1e-4, .01, .05]`, and ±30% single-weight perturbations. Agreement saves
integer votes and uses exact ceiling for 2/3, calculated over cluster centroids
when a supplied cluster map is used. Ties produce no pick/gate. Probability
thresholds .50/.60 are fixed draft diagnostics, not C/L strategies. Epsilon and
weights are sensitivity inputs, never selected for performance. Supplied clusters
are hypothetical; the harness does not infer correlation or a production policy.

An explicit synthetic market vector can be passed to `diagnostics` as a separate
benchmark. The negative control mixes the model pool with that market, labels
it `NEGATIVE_CONTROL_MARKET_SHRINK` and cannot emit strategy signals. The Neon
runner leaves market benchmarks missing until a certified reference is available;
it does not reuse historical uncertified Pinnacle quotes.

All reports retain `sample_status=INSUFFICIENT_SAMPLE`: this diagnostic tool
does not qualify independence or statistical maturity, regardless of sample size.
Eligible fixtures are `SMOKE_ONLY`. A zero-overlap report is a valid research
finding, not a reason to relax matching/time/source gates.

`fixtures/cases.json` contains eleven explicitly synthetic, fixed cases for
normal/identical, clone/cap, near-zero/zero, outlier-flat, market shrink,
percent scale, scale mismatch, incomplete and tied vectors. `SHA256SUMS`
detects accidental fixture edits. No source raw, outcome or credentials are included.
