# Source availability and qualified fallback

Applies to the current prediction pilots #1/#29 and result pilot #20. The
shared policy does not claim to parse arbitrary sites. Structural changes need
source-specific fixtures and independent real checks before declaring recovery.

Public document GET retry (PR #76): at most three attempts, minimum delays
3/10 seconds; HTTP 429 waits at least 30 seconds. Valid Retry-After takes priority;
when outside the acquisition budget, the request is deferred. Explicit
maintenance, access walls and parser errors terminate navigation retries. The
same browser is reused. Browser creation, writes and unknown delivery are never
blindly repeated. Full failed retry raw stays in the database manifest; stdout
contains hashes and safe metadata only.

Durable circuit (`SOURCE_CIRCUIT_V1`): each role/pilot/source has a separate key
in existing `runtime_state`. PostgreSQL clock, advisory lock and a 15-minute
lease admit one worker. Ordinary exhausted failures need two cycles; maintenance,
rate/access denial, parser error or unconfirmed cleanup suspend immediately.
Cooldowns increase 3/6/12 hours, capped at 12 hours; valid longer Retry-After is
honored. Access/parser/401/403/404 failures wait at least 12 hours. After expiry,
only one half-open probe runs. Accepted parsed data resets the circuit. An empty
unconfirmed page or an already sealed slot cannot certify recovery.

Browser IDs are registered during acquisition. If a process dies, the next owner
must confirm stopping that browser before allocating another. Opaque session IDs
are operational identifiers; CDP URLs, credentials and lease tokens never enter
manifests or logs. An expired/superseded owner cannot seal evidence. BUSY workers
do not seal a competing owner's slot. Suspended slots are auditable CIRCUIT_OPEN
with HTTP NULL and no invented raw or observations. Earlier slots remain immutable.
Result observations and the associated raw parent run commit in one transaction.

`QUALIFIED_RESULT_FALLBACK_V1` routes fields using only accepted, fresh, exact
matched evidence captured in the current result-pilot slot. Existing pilot
adapters qualify Smart Soccer Stats for FT/HT and worldfootball for FT. Corners
remain missing. Chosen provider/hash/capture time remain explicit; zero remains
zero, conflicting values block the field, and one source remains SINGLE_SOURCE.
Replaying the same provider creates neither another independent vote nor a new
production observation. This report is pilot-only and cannot settle tickets.

Production has no separately promoted alternative to the existing FIFA adapter:
the report therefore says NO_QUALIFIED_FALLBACK. Prediction models and Pinnacle
prices are not interchangeable fallback data. Adding an actual production
alternative still requires the repository qualification/promotion gates and
owner review; source-supplied flags cannot self-promote a pilot provider.

Health counters `suspended_pilot_sources` and `pending_pilot_browser_stops` expose
operational degradation. Database connectivity or elapsed cooldown does not mean
the source is recovered. Maintenance data stays absent; no backfill, timeline
reset, timezone inference or fabricated completion is performed.

Regression coverage includes graduated retry and failed raw replay, concurrency
on real PostgreSQL, durable restart/half-open state, stale-owner rejection,
cleanup-before-allocation, no-browser suspension, immutable prior evidence,
atomic rollback, source/field/channel qualification and missing/conflict cases.
Controlled browser fixtures are distinguished from live external checks. CI and
live Browser/Neon/Render evidence are recorded in the PR and master issue #35.
