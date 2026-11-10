# API-FOOTBALL full capability — real certification budget (PROPOSAL, NOT EXECUTED)

Status: **proposal only**. Nothing here has been run. Every campaign needs the owner's explicit approval
and runs only after this branch is merged and its deploy is verified healthy (CLAUDE.md §3).

## Frame

- Workflow: the existing `.github/workflows/fcb-mcp-e2e.yml` driving `scripts/mcp_e2e_render.py` against the
  Render MCP (OAuth, no direct provider call from the runner).
- Accounting: unchanged. Every provider call is charged its worst case, 3 HTTP attempts
  (`APIFOOTBALL_WORST_CASE_PER_CALL`); a cache HIT or a refused input is charged 0. Per-campaign ceiling
  45 attempts (`APIFOOTBALL_BUDGET_CEILING`, owner-approved): at most 15 provider calls per campaign.
- Spacing 9 s between calls (≤ 7 per minute, far below the local 60/min and the provider 300/min).
- The cases below need a new runner plan (`apifootball_full_capability`). Adding it is a separate change,
  not part of this PR; until then they can be run one by one from an authorized MCP client with the same
  accounting.
- Placeholders: L = 39, Y = 2026, X = 1557464 (finished, already PASS), U = an upcoming fixture of L (discovered as in
  case C01 of the completion plan), T1/V = home team / venue of X, Z = a live fixture from A01,
  L1-L2 = two distinct league ids of the live fixtures of A01.

## Campaign A — validation, timezones, paging, timestamps (FIX-02, 03, 04, 06, 07): max 13 calls, 39 attempts

| Case | Call | Proves | Pass rule |
|---|---|---|---|
| A01 | `fixtures(live="all")` | live listing (also feeds A02, B08) | DATA_PRESENT or EMPTY_VALID (PARTIAL) |
| A02 | `fixtures(live="L1-L2")` | FIX-02 id list | every fixture's league in {L1, L2}; NOT_APPLICABLE (0 calls) if < 2 live leagues |
| A03 | `fixtures(team=T1, season=Y)` | FIX-03 team+season | HTTP 200, no `errors`, every row has team T1 |
| A04 | `fixtures(venue=V, season=Y)` | FIX-03 venue+season | idem, venue V |
| A05 | `fixtures(league=L, next=5)` | FIX-03 league+next | ≤ 5 rows, all league L, all not started |
| A06 | `fixtures(league=L, last=5, status="FT")` | FIX-03 league+last+status | ≤ 5 rows, all FT |
| A07 | `fixtures(date=D, league=L, season=Y, timezone="Europe/Rome")` | FIX-06 | `timezone_check.inconsistent == 0`, `fixture.timezone == "Europe/Rome"` |
| A08 | `fixture_rounds(league=L, season=Y, dates=true, timezone="America/New_York")` | FIX-06 other zone | HTTP 200, no `errors` |
| A09 | `player_profiles(page=101)` | FIX-04 | `pagination.current == 101`, `pagination_state` coherent |
| A10 | `player_profiles(page=<total+1>)` | FIX-04 / D06 out of range | empty reply, `PAGE_OUT_OF_RANGE`; a provider `errors.page` is recorded, not a FAIL of the bridge |
| A11 | `odds_mapping(page=2)` | FIX-04 second page | `current == 2` |
| A12 | `fixtures(id=X)` then the same call again | FIX-07 | 1st: MISS, `provider_timestamp_source` set or null (never bridge time); 2nd: HIT, 0 attempts, same `retrieved_at_utc` |
| A13 | `fixtures(id=X, timezone="Mars/Olympus_Mons")` | FIX-06 negative | `INVALID_INPUT` `TIMEZONE_NOT_IN_CATALOG`, 0 attempts (not charged) |

## Campaign B — manifests and live odds (FIX-01, 05, 09): max 13 calls, 39 attempts

| Case | Call | Proves | Pass rule |
|---|---|---|---|
| B01 | `fixtures(date=D)` (no league) | FIX-01 | `PARTIAL_ID_MANIFEST` with `complete_retrieval_available` true, **or** `PROVIDER_RESPONSE_TOO_LARGE` + `navigation.NOT_COMPLETE` (documents R1). Either is recorded; neither is PASS of completeness |
| B02-B04 | first 3 `follow_up.calls` of B01 (only if B01 is a manifest) | FIX-01 continuation | each reply holds exactly `expect_ids` in order; a mismatch is recorded as DATA_CHANGED, not hidden |
| B05 | `leagues()` | FIX-01 / R1 | expected read-cap refusal with navigation; a manifest is also acceptable |
| B06 | `odds(league=L, season=Y)` | FIX-05 page 1 (3 pages known) | full reply, or `PARTIAL_ODDS_MANIFEST` without `odd`, `phase PRE_MATCH` |
| B07 | `odds(fixture=U)` | FIX-05 per fixture | full reply or manifest split by bookmaker; if manifest, B07b = its first follow-up (counts must match `expect`) |
| B08 | `live_odds(fixture=Z)` | FIX-09 / C08 | PASS only with `live_odds_state.state == ACTIVE`; ALL_SUSPENDED / ALL_NULL_ODDS / NO_OPEN_PRICED_SELECTION / EMPTY = PARTIAL; NOT_TESTED (0 calls) if no live fixture |
| B09 | `live_odds(league=<league of Z>)` | FIX-05 live manifest | full reply or `phase LIVE` manifest, LIVE bet ids only |
| B10 | first follow-up of B09 if it is a manifest | FIX-05 live continuation | counts equal `expect` |

## FIX-08 and the P3s

No real call: exhausting a local limit on purpose would spend real quota for nothing the PostgreSQL
integration tests do not already prove. Recorded as `NOT_TESTED_REAL (proven on real PostgreSQL offline)`.

## Totals

- At most 26 provider calls, **78 HTTP attempts** worst case, in 2 campaigns of ≤ 45 each, on the same UTC day.
- About 1.0 % of the 7 500/day plan and 1.1 % of the local 7 000/day limit.

## Stop conditions (all existing S1-S5 rules apply, plus)

- `STOP_LOCAL_RATE_LIMITED` or `STOP_LOCAL_DAILY_QUOTA_EXHAUSTED` → stop the campaign, remaining cases NOT_TESTED.
- Provider 401/403/429/5xx, open circuit, declared `requests`/`rateLimit`/`token` → stop.
- Provider-reported remaining day or minute = 0 → stop.
- Budget reached → stop (never raised in the run).
- Any FAIL that is an MCP defect (envelope, manifest not explicit, `odd` in a manifest, pre-match/live id mix)
  → stop and open a fix; no retry of the campaign before the fix is merged.
- Two consecutive `PROVIDER_RESPONSE_TOO_LARGE` → stop campaign B (R1 is then documented, not re-proven).
- No live match at run time → B08-B10 NOT_TESTED with 0 calls; C08 stays NOT proven.
