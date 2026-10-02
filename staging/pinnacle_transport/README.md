# Pinnacle transport evidence

`live_probe_20261001.json` retains gzip/base64 of two actual browser JSON **DOM
text** documents from the already-observed public guest feed, with hash/size,
HTTP status, final URL and conservative observation window. It is a source
capability/transport probe, not a prospective market snapshot. The DOM text hash
is not presented as a wire-response hash. It does not enter snapshots or tickets.

League 2117 returned 296 matchup records (913766 bytes) and 668 market records
(372113 bytes), including published moneyline/total/spread/team_total markets.
The matchup document exceeds the previous 500000-character DOM limit. A second
fresh browser navigation confirmed HTTP 200 and application/json network type;
both managed sessions were explicitly stopped. The automated regression replays
full saved text/hash, in addition to size/UTF8/schema/auth/cleanup negative tests.

Collector fallback uses the actual Playwright response body, not DOM excerpts:
5 MiB/document, 20 MiB raw batch, 12 IDs/session, two concurrent batches, fixed
Pinnacle host/path and authenticated internal endpoints. The decoded transport
envelope is additionally bounded at 45 MiB to allow JSON string escaping. A
confirmed browser stop, full body byte count and SHA-256 must agree before the
collector accepts either response. Challenges, redirects, truncation, status
errors or unknown documents remain explicit failures. Empty JSON lists are valid
missing coverage, not outages.

Direct failure and successful fallback are separate audited events. Partial
direct responses are never blended with later browser bodies. The collector
uses its post-delivery acquisition timestamp as a conservative prematch cutoff,
preserves browser response time in the transport audit, and retains existing
strict exact pair/kickoff/context matching. Quote availability is not canonical
competition binding, source independence, production promotion or certified
open/close coverage. Those real data gates remain pending.

`moneyline_capture_20261002.json` is a new independent HTTP 200 DOM capture of
league 1842 (19 matchup rows, 653 market rows), with complete compressed text,
byte counts, SHA-256 and confirmed browser STOPPED lease. Its timestamp is
conservatively after delivery and lease verification. It is diagnostic evidence,
not a production market seal, wire response or pilot slot.

The production moneyline parser in `app/pinnacle_moneyline.py` requires explicit
soccer/league identity, a regular pending top-level non-live fixture, unique home
and away participants, an open non-alternate period-0 `s;0;m` market, exactly
three unique finite numeric American prices and offset-aware future kickoff and
cutoff. Duplicate fixture/market IDs reject the ambiguous quote, including a
damaged duplicate alongside a valid market. Prices are ordered H/D/A explicitly;
the existing `overround` convention remains the sum of implied probabilities.

The collector seals `moneyline_audit.json` in its manifest and exposes identical
reason counts in quality reports, runtime state and stdout. Other market/period
counts describe excluded coverage, not transport outages. Malformed records
cannot silently replace prices or price a production snapshot. Replay accepts
17 unchanged FT1X2 quotes; adding a duplicate home price rejects that fixture
alone, preserving the other 16. No historical capture or ticket is rewritten.
