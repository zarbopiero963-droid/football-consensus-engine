# Competition coverage (M28 / issue42-F)

`python -m analysis.coverage.run --neon --known-at <explicit-offset-time>`
prints a read-only report. `--input dataset.json` accepts a saved export with
`predictions`, `observations` and `market_tickets`. The SQL adapter enforces a
repeatable-read, read-only transaction and calls no reconciliation, migration,
registry, source-promotion or ticket-writing code.

Competition groups use exact normalized league labels with country/category
when supplied. Missing metadata stays missing; no canonical competition ID or
cross-source alias binding is invented. Sources sharing a textual label are
described together, not asserted to share a canonical competition. Result
coverage is linked only through existing FootInsights anchor match IDs. Pilot
name-only joins cannot borrow anchor competition/result coverage.

Prediction denominators are **latest observed source rows**, not snapshot or
slot counts and not asserted canonical fixture counts. SQL deduplicates by
source/source key (and anchor match ID) at the acquisition cutoff. Each source
has separate complete-vector, timezone-resolved and safe-match counts/rates.
1X2 scale is explicit, invalid/partial vectors are not FULL, and safe matching
requires a recognized strict match status, documented source timezone, valid
prematch acquisition and sealing, and unambiguous fixture revisions. The source
timezone is not inferred from a UTC database column. Legacy pilot server seals
remain null; a JSON client timestamp cannot substitute for a server column.

Result provider coverage is observation presence, not qualification or
independence proof. `pilot:` providers are separate diagnostics and cannot
verify production results. The report reuses the existing field consensus on
latest final observations per provider before the cutoff; real zeros survive,
missing/invalid fields and conflicts cannot count as VERIFIED. It never writes
that reconstructed diagnostic consensus back to Neon.

Weekly counts use distinct fixtures whose kickoff lies in the trailing seven
days, including the cutoff. Verified FT counts require both goal fields.
Settled counts require a stored WON/LOST/PUSH production market ticket with
the fields required by that market verified and numeric. FT cannot certify HT
or corners. Multiple tickets/markets on one fixture count once in the combined
fixture denominator; per-market counts are also explicit. Illegal settled
tickets are counted as a blocker, never as successful coverage.

The knowledge cutoff selects stored acquisition/observation times and guards
anchor import time; result/pilot tables lack server knowledge timestamps.
Mutable ticket updates after the cutoff are excluded because their old status
is unavailable. This is a diagnostic of the current stored evidence with a
time cutoff, not a certified historical as-of replay.

There is no approved quantitative coverage/promotion policy yet. This report
does not create one: groups with observed predictions are MONITORING_ONLY;
ticket-only groups are INSUFFICIENT; empty input returns empty groups and
explicit zero denominators. `production_eligible` is always false, even with
synthetic perfect coverage. Future PRODUCTION_ELIGIBLE classification requires
approved policy, source promotion, canonical bindings and clean real evidence.
Pilot server seals introduced in migration 0005 apply only to new inserts. Both
run and prediction must have a server seal; existing legacy NULLs stay missing.
Future rows still need actual source time/matching and prematch proof before
they can improve the safe-match diagnostic; no promotion follows from a seal.
