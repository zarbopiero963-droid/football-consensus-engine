# Pilot completion audit — 2 October 2026 UTC

This audit records acquisition completion separately from qualification and
promotion. Counts are repeated rows across slots, not independent fixtures.
No snapshots, pilot slots, probabilities or tickets were rewritten.

## Calendar / decisions (#87)

Neon independently confirms batch `decision-20261002T230338Z-9fdda975a2`,
requested date 3 October Europe/Rome, with 22 persisted tickets.
Three prediction URLs contain 4 October but their captured SportsEvent.startDate
has explicit offsets on 3 October. Do not derive the date from a URL suffix.

| Fixture | Snapshot UTC | Rome | Independent evidence |
|---|---|---|---|
| Boca–Unión | 3 Oct 00:30 | 02:30 | Telefootball secondary; official confirmation missing |
| Almería–Burgos | 3 Oct 14:15 | 16:15 | Official UD Almería ticket announcement |
| Vision–Gold Stars | 3 Oct 15:00 | 17:00 | Football Finder secondary; official confirmation missing |

The generic Sofascore pair URL returns a 22 March reverse fixture and was
rejected as confirmation. Calendar checks are not FT result verification.

Official Almería evidence:
https://www.udalmeriasad.com/en/news/a-la-venta-las-entradas-para-el-partido-almeria-burgos

## Prediction pilot #1

Control COMPLETE; 30 Sep 14:45:49.276006Z → 2 Oct 14:45:49.276006Z.
Each source has 16/16 slots and 8 legacy unsealed runs.

| Source | Rows / complete HDA | Server-sealed before kickoff | Classification |
|---|---:|---:|---|
| ProSoccer | 309 / 309 | 0 | NEEDS_MORE_TESTS: timezone unresolved; browser timezone insufficient |
| StatsUltra | 112 / 112 | 0 | NEEDS_MORE_TESTS: timezone unresolved |
| Elofoot | 160 / 160 | 50 | PARTIAL: 110 unresolved dates; matching not certified |

## Prediction pilot #29

Window 30 Sep 22:06:02.776033Z → 2 Oct 22:06:02.776033Z.
Each of 10 sources has 16/16 slots. The expired RUNNING control was advanced to
COMPLETE with the runner's `ends_at_utc <= NOW()` predicate; readback confirmed
2 Oct 23:15:40.073Z. This changes lifecycle control only, not qualification.

| Source | Rows | Complete HDA | Server-sealed before kickoff | Classification |
|---|---:|---:|---:|---|
| xgtoday | 1048 | 1048 | 0 | NEEDS_MORE_TESTS timezone |
| kickform | 96 | 96 | 0 | NEEDS_MORE_TESTS timezone |
| thedatabetics | 435 | 0 | 0 | PARTIAL_PICK |
| kickoff_ai | 128 | 128 | 0 | NEEDS_MORE_TESTS timezone |
| vitisport | 302 | 0 | 0 | PARTIAL_PICK |
| clubelo | 428 | 428 | 300 | PARTIAL: uncertified matching and transport failure |
| opta_analyst | 403 | 403 | 203 | PARTIAL: uncertified matching; 5 legacy HTTP404/OK anomalies |
| scoutingstats_ai | 16 | 16 | 0 | NEEDS_MORE_TESTS timezone |
| aipredict_io | 14 | 14 | 0 | NEEDS_MORE_TESTS timezone |
| totalcorner | 581 | 0 | 0 | CORNERS_ONLY |

Five legacy runs per source lack server seals and stay missing. No existing
match record certifies EXACT_PAIR_TIME_CONTEXT/CANONICAL_PAIR_TIME_CONTEXT.
FOUND_NAME_ONLY and legacy FOUND do not provide that proof. No new source is
eligible for a production HDA vote on this evidence.

## Result pilot #20 / settlement

Control COMPLETE; 30 Sep 16:42:30.983721Z → 2 Oct 16:42:30.983721Z.

| Source | Slots | Last-slot finding | Classification |
|---|---:|---|---|
| Footiqo | 16/16 | Capability-only, 0 parsed observations | NEEDS_MORE_TESTS |
| Smart | 16/16 | 20 parsed, 9 diagnostic matches, 0 new persisted | SECONDARY_STAGING |
| worldfootball | 15/16 | 50 finished parsed, 0 overlap; 330 nonfinal rejected | SECONDARY_STAGING |
| StatBunker | 14/16 | CIRCUIT_OPEN | BLOCKED_ACCESS |

Missing slots are not backfilled. Result consensus contains 58 SINGLE_SOURCE
and 4 UNVERIFIED records, zero with BOTH FT goal fields VERIFIED. No automatic
official ticket settlement is enabled by this audit.

### Footiqo next qualification step

Phase 0: the existing collector reads a capability page, not match results.
A new real browser read of the public UEFA Nations League overview returned
HTTP200 and 25 rows with IDs, teams, competition, season, FT goals and FTR.
The first football-data navigation timed out even though later DOM inspection
found its page; this is not recorded as a successful HTTP capture.

`footiqo.py` parses only the observed overview schema. It checks FTR against
both goal fields, preserves true zeros, rejects missing/negative/fractional
scores, blocks conflicting duplicate IDs, and tolerates markup in cells.
The table's local two-digit-year `matchDate` has no demonstrated timezone.
UTC remains null, status FINISHED_REPORTED is diagnostic only, and
production_eligible remains false. HT/corners remain null. This parser has no
network, DB writes, allowlist or settlement wiring.

Evidence metadata/hash is in `footiqo_20261002.json`; third-party raw table is
not committed. Remaining gates: independent proof of source timezone/date,
repeatability across competitions, HT/corners detail capture, actual strict
anchor overlap, rate/retention permission policy and owner integration review.

## Pinnacle / future frozen odds

Latest collector 2 Oct 21:09:12Z has zero eligible joins: 58 NO_EXACT_PAIR,
39 EXACT_PAIR_REJECTED; first failed gates are 38 LEAGUE_MISMATCH and one
KICKOFF_MISMATCH. This is an observed identity gap, not absent odds transport.

Observed competition pairs include:

| Anchor | Pinnacle | Binding risk |
|---|---|---|
| Segunda División | Spain - Segunda Division | Anchor country/category missing; name alone not canonical proof |
| International Friendlies | International - Friendlies | National-team/category/context proof required |
| Liga Profesional Argentina | Argentina - Liga Pro | Explicit source ID and competition context required |
| UEFA Nations League | UEFA - Nations League A/C/D | Anchor lacks division; never collapse all divisions blindly |
| CONCACAF Nations League | CONCACAF - Nations League B | Division proof missing in anchor |

Prepare explicit versioned source-ID bindings only after competition, country,
category and division evidence exists. Never strip country prefixes globally,
relax matching, or fill old ticket odds from new captures. Once certified,
apply only to future capture/decision paths and freeze actual observed odds.

## Consensus / master #35

Research gates remain in place: server seal and acquisition before kickoff,
explicit timezone, complete vector, certified pair/time/context, one latest
source vote, supplied source role and separate market benchmark. Pilot
completion does not demonstrate independence or statistical maturity.
No production promotion, weight tuning, new strategy or threshold adjustment.

Validation: 138 existing targeted tests passed (consensus robustness, Pinnacle
moneyline, result validation/context/kickoff); 25 market-reference tests passed;
four new Footiqo qualification tests passed. Real HTML parse accepted 25 rows,
rejected one footer/identity row, found no duplicate conflict, and left all
timezones unresolved. Full CI is a separate PR gate.

Issues stay open for owner closure. The immediate blocker is qualified evidence,
not permission to ignore missing data. No source is proposed for promotion yet.
