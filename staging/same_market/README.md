# Same-market Pinnacle staging (M27 / issue43-D)

Run `python -m staging.same_market.run --input
staging/same_market/live_capture_20261001.json --known-at
2026-10-01T18:19:30Z`. The command reads an explicit saved capture and prints
JSON only. It does not access Neon, allocate browsers, change cron or write
snapshots, tickets, strategies or registry bindings.

The initial adapter supports observed soccer **FT goal totals on half-goal
lines**, including explicitly published alternate lines. Each quote keeps its
exact `FT_OU_<line>` identity, both published American/decimal prices, inverse
odds fair benchmark, overround, provider fixture ID, period, source raw hashes,
capture time and market version. Fair probabilities are market derivations,
never model probabilities or extra model votes. Team totals, corners, first-half
markets, integer/quarter lines with push/split outcomes are excluded. The feed
did not expose a BTTS market in this probe; BTTS remains unobserved, without a
1X2 or total proxy. Additional markets require their own observed evidence.

Full gzip/base64 JSON documents are bounded and hash/size verified. HTTP status,
final/requested paths, raw format, truncation and confirmed browser stop are
required. Source fixture IDs must resolve uniquely inside the same capture;
live/child/non-Regular fixtures, duplicated prices/markets, missing selections,
line disagreements and unresolved/post-kickoff time are rejected. Neither an
empty feed nor rejected markets become zero probabilities.

The real fixture contains the complete untruncated **DOM JSON text**, not wire
response bodies, from public league1842 on 1 October 2026: 19 matchup records
(44094 UTF8 bytes) and 653 market records (345961 bytes). The browser was stopped
and Neon confirms STOPPED with no stop error. `captured_at_utc=18:19:30Z` is the
conservative time after full DOM delivery and confirmed stop; it is not an
invented per-response timestamp. Replay yields **43 half-goal FT total quotes**;
119 push/split-line totals and 491 other markets/periods are excluded.

This is a source adapter probe, not a pilot slot or a server-sealed prospective
market snapshot. All quotes remain staging, `production_eligible=false`,
`prematch_certified=false`, `server_sealed=false`, `adds_source_vote=false`;
`q_open`/`q_close` stay null. Source country/category remain missing: league
labels do not supply certified registry metadata.

`select_same_market` accepts only FULL model vectors with resolved kickoff time,
explicit safe matching, and a prematch model timestamp. Exact market/period/line,
ordered pair, kickoff and competition/context must agree. Multiple matches are
ambiguous. Even a diagnostic exact match remains unsealed staging. Current native
StatsUltra vectors are PARTIAL and timezone unresolved, so these prices cannot
make them eligible. The real matched prospective/open-close DoD remains pending;
this adapter does not bypass M17 identity/time evidence or source promotion.
