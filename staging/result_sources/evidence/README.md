# Result pilot context evidence — 2026-10-01

Operational follow-up to master #35, #37-E and #20. Public pages were read using
two independent Browser Use sessions, without login or custom challenge bypass.
Both documents returned HTTP 200 on their published URLs. Both browser leases
were confirmed STOPPED in Neon, without stop errors.

- `worldfootball_20261001.json` contains gzip/base64 **unmodified DOM fragments**
  from the first capture: competition header plus the complete gameplan. Its
  fragment hash/size are separate from the original full-document hash/size.
  This is not a full HTML capture or screenshot. Replay sees 380 cards, accepts
  50 finished cards and rejects 330 scheduled cards. Native UTC and IDs are
  publisher values, not reconstructed timestamps or cross-source registry bindings.
- `smart_20261001.json` contains first-capture visible text and its hash, with
  the separate original full-document hash/size. This is not raw HTML. It
  parses 20 results and does not supply a source kickoff/timezone. No browser
  timezone is treated as source evidence.
- `verification_20261001.json` records the independent second-session hashes,
  parser/matching counts against 169 live read-only model anchors, and the
  unchanged 19-observation pilot baseline. Second full HTML and visible text were
  inspected transiently; their full bytes and screenshots are not committed.
  This summary is not a production run or a prospective observation seal.

The fresh worldfootball page accepts 50 finished results and rejects 330
scheduled cards, with zero anchor overlap. Smart has eight literal-competition
diagnostic matches, nine competition mismatches and three absent ordered pairs.
The previous pair/date matcher accepted 17 Smart rows; competition aliases such
as Friendlies/International Friendlies remain unqualified. Exact names/date do
not establish canonical country/category or source timezone, and none of these
rows is promoted or participates in production consensus.

Regression mutations and PostgreSQL fixtures use explicit **controlled** anchors,
HTTP responses and negative cards. They test rejected clocks, live/HT/AET,
ambiguous IDs, wrong competition, timezone, kickoff and future end time. They are
not evidence of those failures occurring in a production publisher response.
PostgreSQL integration tests verify raw preservation, idempotency, immutable old
runs and zero production ticket/snapshot writes; they do not replace real browser
checks. No old slot, clock, raw, observation, strategy or ticket is rewritten.
