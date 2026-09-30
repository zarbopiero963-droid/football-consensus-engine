# Issue #20 — Result-source integration plan (proposal only)

Status: **proposal / no production wiring**.

This document records the result of qualification work. Nothing in this plan
authorizes paid APIs, credential creation, bypasses, or production settlement
changes.

## Qualified / classified sources

### Footiqo — preferred technical primary candidate

Observed public league database exposes the same match IDs across:
- Scores: FT, first-half and second-half scores.
- Corners & Cards: FT, first-half and second-half corners/cards.

Homogeneous sample: 10 England League One matches were present in both tables
with matching IDs. Coverage for the sampled rows was 10/10 for:
FT score, HT score, corners FT and corners HT.

Terms: abusive-rate scraping is prohibited; copying/reproduction/redistribution
is restricted. Any collector must be low-rate, cache results, retain only the
minimum normalized observation plus audit hash, and avoid republishing raw
tables.

Proposed role: RESULT_SOURCE_PRIMARY_CANDIDATE.

### BeSoccer — technically complete, permission-gated

A 10-match Browser Use sample returned public finished match pages with ALL,
1ST and 2ND stat tabs. FT/HT/corners FT/corners HT were observed.

Legal notice states protected content may not be reproduced/distributed/
publicly communicated/transformed/modified without express authorization.

Proposed role: MANUAL_OR_PERMISSION_GATED. Do not automate persistently until
written permission/legal approval exists.

### worldfootball.net — secondary, personal-use gate

Public match pages expose FT score, HT score and corners FT. Corners HT was not
observed.

Terms allow normal/personal use but prohibit commercial/business exploitation
without express permission.

Proposed role: PERSONAL_SECONDARY / PERMISSION_REQUIRED_FOR_BUSINESS_USE.

### Smart Soccer Stats — secondary only

Public Game Statistics Explorer exposes FT, HT when available, and detailed
statistics including corners/fouls for recent/larger competitions. Missing data
is explicitly excluded rather than coerced to zero.

Coverage is intentionally incomplete; do not use it as sole settlement source.

Proposed role: QUALIFIED_SECONDARY_FT_HT; corners are optional cross-check only
when the source exposes them for the match.

### StatBunker — FT/event cross-check only

Public match pages are accessible without login and expose explicit FT results,
lineups and timestamped goal events. Free registration exists but is not needed
for the observed public data. Match-level corners were not found in the deeper
test. HT can be inferred from events, but this project does not reconstruct
missing official fields.

Proposed role: SECONDARY_FT_ONLY / EVENT_AUDIT. Do not use for corners or
canonical HT settlement.

### GotTheOdds — aggregate only

Public/free corner tables are team/league aggregates, not match-level result
observations.

Proposed role: AGGREGATE_RESEARCH_ONLY. Never feed result consensus.

### 365Scores — staging adapter only

A public web JSON feed used by the frontend was observed. A staging normalizer
extracts only fields actually observed in that feed: match id, kickoff,
competition, teams and finished FT score/result. It deliberately leaves HT and
corners null until a stable public detail payload is separately qualified.

Terms/permission remain unresolved for automated reuse, so the adapter must not
be connected to production.

Proposed role: STAGING_NEEDS_TOS_AND_DETAIL_QUALIFICATION.

### Sofascore

Automation rejected for this project because available terms restrict
scraping/aggregation/automated reuse.

Proposed role: MANUAL_ONLY.

## Proposed settlement topology

1. Primary candidate: Footiqo, only after owner approves integration and rate
   policy.
2. Independent secondary:
   - worldfootball.net only for personal/non-business use, or after permission;
   - BeSoccer only after permission;
   - Smart Soccer Stats as incomplete secondary evidence.
3. Generic result consensus remains authoritative:
   - one source => SINGLE_SOURCE;
   - two agreeing independent sources => VERIFIED;
   - disagreement => CONFLICT;
   - missing field remains MISSING, never zero.

## Matching rules

Exact/canonical home + away + kickoff UTC + competition. No permissive fuzzy
matching. Source timezone must be explicit/verified; otherwise
TIMEZONE_UNRESOLVED.

## Data retention

Store normalized observations, source match id, observed timestamp, raw SHA-256
and minimal audit metadata. Avoid storing or redistributing full third-party
pages unless permission explicitly allows it.

## Production PR gates

A production integration PR may be opened only after:
1. owner explicitly approves this plan;
2. source-specific permission/terms gate is satisfied;
3. regression tests cover missing fields, conflicts and timezone handling;
4. a real staging run proves no paid API/key is used;
5. rate limits/backoff/caching are documented;
6. no source is allowed to self-certify its own prediction without an
   independent result source.

The current 365Scores staging adapter is not a production source and must stay
unwired until these gates are satisfied.
