# Elofoot / Opta / ClubElo structured-time regression evidence

`live_fragments_20261001.json` retains gzip/base64 **DOM fragments**, extracted
without modifying the observed cards/tables. It is not a wire-response archive.
Each document records original DOM SHA-256/size and independent fragment hash/size.
Only public prediction cards or the fixture table are retained; unrelated page
scripts and chrome are omitted. Fragment extraction does not confer a server seal.

The `pilot_saved` documents replay these immutable Neon run raws:

- Elofoot: `source-pilot-48h-issue1-v1-elofoot-s10-c489cac5`.
- Opta: `source-pilot-48h-issue29-v1-opta_analyst-s07-bb8c2737`.
- ClubElo: `source-pilot-48h-issue29-v1-clubelo-s07-02ea864e`.

The original full HTML and seal remain in Neon. The diagnostic cutoff 18:26Z
permits deterministic regression replay, not retrospective certification or
backfilling of historical pilot columns. Elofoot's original hash includes its
`PAGE:main` wrapper. Opta's Austria Wien–Internazionale card is LIVE at 83 minutes,
score 1–0, yet publishes a full 75.5/21.2/3.4 triple: lifecycle must exclude it.
ClubElo exposes 50 fixtures although its former text parser acquired only 16.

The `independent_pass2` documents are from clean browser session
`519a4f26-43f2-41a3-8715-cf155ae0649f`; its confirmed stop is
2026-10-01T21:04:21.260Z. All three official URLs returned HTTP 200. A separate
session `eedcc9b4-2156-4c9a-a046-a51348f7a5a5` confirmed the same 10/28/50 future
vectors/timestamps. Both lease stops were verified in Neon without errors.
The master #35 comment 5940568144 records the full audit and acquisition limits.

Runtime adapters use only source-local metadata and published 1/X/2 components.
Missing/conflicting clocks, missing context, in-play/final status, broken vectors,
conflicting source IDs and kickoff at/before ingestion are rejected. Identical
responsive cards are deduplicated. ClubElo reads native decimal columns, not the
rounded mobile duplicates or unrelated title/promotion probabilities. Strict
matching hints remain `FOUND_NAME_ONLY`; source promotion is a separate decision.

Unit regressions use fixed recorded cutoffs. Negative variants are synthetic.
PostgreSQL tests replay the DOM with **explicitly shifted synthetic dates**, in
isolated schemas with controlled CDP, to test sealing and persistence on any
calendar date. They are real database tests, not live-browser tests. No runtime
pilot clock/slot/history is recreated by running these regressions.
