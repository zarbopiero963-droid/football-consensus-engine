# Observed match rejection evidence

`live_probe_20261001.json` contains full public FI HTML and Pinnacle matchup
JSON captured independently with Browser, compressed with gzip and hashed as
UTF-8 DOM serialization/text. These are not HTTP wire-byte hashes. Both source
navigations returned HTTP 200. The provider session was stopped; Neon confirmed
the lease `STOPPED` with an empty stop error at 19:07:58.918Z.

The conservative `known_at_utc` is after stop confirmation. It is not a provider
response timestamp, quote acquisition or database sealing certificate. No
moneyline market/prices were captured in this probe.

There are 206 Pinnacle objects, including 8 non-live top-level matchups. The
Argentina / Burkina Faso ordered pair and 2026-10-04 00:00 UTC kickoff agree with
the FI SportsEvent. Competition names differ: `International Friendlies` versus
`International - Friendlies`. The existing strict contract rejects this with
`LEAGUE_MISMATCH`; neither source establishes a complete canonical registry
binding in this probe. Names/time agreement alone does not establish category,
country or approval for cross-source aliases.

The live regression verifies the source hashes, source parsing and rejection.
It does not add a production quote, model probability, alias or historical row.
Run `python -m pytest tests/test_market_match_audit.py tests/test_market_reference.py`.
