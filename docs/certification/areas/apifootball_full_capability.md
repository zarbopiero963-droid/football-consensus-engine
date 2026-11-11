# API-FOOTBALL — full capability (FIX-01..FIX-09)

- Mandate: FCB-OWNER-AF-FULL-CAPABILITY-ONE-PR-20261111 (PR #190). One branch, one deliverable, one commit
  per fix, plus the review follow-up commits (P2, P3s, FIX-01 option B).
- Base: `origin/main` `475dae8`. Gap ids (G01..G13, D06) refer to the reconciliation matrix of 2026-11-10.
- Evidence in this document is **offline only**: unit tests, `httpx.MockTransport` behind the real transport,
  resilience client and MCP tools, synthetic payloads, and a throwaway local PostgreSQL 18 for the quota
  coordinator. **No call was made to API-FOOTBALL, Render or the production MCP.**
- "PASS" below means **PASS offline** (code + RED/GREEN tests + mutations). No fix has a real-provider PASS;
  the real campaign is proposed (not run) in `apifootball_full_capability_budget.md`.

## 1. Per-fix verdict

| Fix | Offline verdict | Real-provider status | Why not more |
|---|---|---|---|
| FIX-01 replies > 256 KiB | **PASS offline**, completeness conditional | NOT_PROVEN_REAL | Completeness is claimed only when provable (§4); detailed `ids` slices are never complete; replies above 16 MiB / 20 000 rows / other scan bounds are refused with `PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN` |
| FIX-02 `live` id list | **PASS offline** | NOT_PROVEN_REAL | Maximum number of ids DOCS_UNVERIFIED |
| FIX-03 /fixtures combinations | **PASS offline** | NOT_PROVEN_REAL | Most combinations are DOCS_UNVERIFIED (§2) |
| FIX-04 page > 100 | **PASS offline** | NOT_PROVEN_REAL | Provider answer on a page past the end DOCS_UNVERIFIED (D06) |
| FIX-05 odds manifest | **PASS offline** | NOT_PROVEN_REAL | — |
| FIX-06 timezones | **PASS offline** (IANA validation) | **Provider acceptance UNVERIFIED** | Validated against IANA (pinned `tzdata`), never against the provider's `/timezone` catalog, which is not captured |
| FIX-07 timestamps and cache | **PASS offline** | **Date header never observed live** | `provider_timestamp_utc` comes from the HTTP `Date` header, which no captured reply shows |
| FIX-08 local quota codes | **PASS offline** on real PostgreSQL 18 | NOT_PROVEN on Neon/Aiven | No reason to spend quota to hit a local limit on purpose |
| FIX-09 suspended live odds | **PASS offline** | **C08 NOT proven** | Nulls stay null; no real `/odds/live` payload is captured; C08 needs separate real proof |
| P3 (PR #185) reserved keys, hold vs success(), single-flight | **PASS offline** | — | — |

## 2. Coverage matrix

| Fix | Code | Tests |
|---|---|---|
| FIX-01 | `continuation.py`, `stream_scan.py` (new), `transport.py` (`ScannedReply`, scan above the cap), `resilience.py` (`scan` in the outcome), `tools.py` (`PARTIAL_ID_MANIFEST`, `navigation`), `scripts/mcp_e2e_render.py` (`check_id_manifest`) | `test_apifootball_continuation.py` (32), `test_apifootball_stream_scan.py` (35) |
| FIX-02 | `contracts.py` (`_live_filter`) | `test_apifootball_fixtures_live_filter.py` (24) |
| FIX-03 | `contracts.py` (`fixtures_combination`), `tools.py` (`combination`) | `test_apifootball_fixtures_combinations.py` (54) |
| FIX-04 | `contracts.py` (`MAX_PAGE`), `transport.py`, `tools.py` (`pagination_state`) | `test_apifootball_paging_beyond_100.py` (58) |
| FIX-05 | `odds_manifest.py`, `tools.py` | `test_apifootball_odds_manifest_navigation.py` (12), `test_odds_manifest_and_parameter_matrix.py` |
| FIX-06 | `timezones.py`, `contracts.py`, `tools.py`, `requirements.txt` (`tzdata==2025.2`) | `test_apifootball_timezones.py` (56) |
| FIX-07 | `transport.py` (`provider_http_date`), `cache.py`, `resilience.py`, `tools.py` | `test_apifootball_timestamps_cache.py` (13) |
| FIX-08 | `quota.py`, `tools.py`, E2E runner | `test_apifootball_local_quota_codes.py` (11), `tests/integration/test_apifootball_quota_codes_postgres.py` (8) |
| FIX-09 | `live_odds_state.py`, `tools.py`, E2E runner, `tests/fixtures/apifootball_live_odds/shapes.json` | `test_apifootball_live_odds_states.py` (38) |
| P3s | `errors.py`, `circuit.py`, `resilience.py`, `tools.py` | `test_apifootball_review_p3.py` (11) |

Regression: `test_apifootball_full_capability_regression.py` drives all 40 provider tools end to end; the
contract tests against the official matrix are unchanged and green.

Spot mutations, each applied alone and reverted:
- first round 17/17 killed (validation, timezone, manifests, quota, circuit, shared flight, runner);
- review round 19/20 killed: every scan bound (bytes, time, rows, depth, token, including off-by-one), truncated
  stream, UTF-8, duplicate keys and id fields, hash coverage, scan of the already-buffered prefix, the P2
  guard (detail slices claimed provable; measured-size check removed), invalid-id range, results mismatch,
  combination labels, from/to mapping, open zero odd. The one survivor removes a redundant "data after the
  top-level value" check: the grammar already refuses any token after the document (tested by
  `test_malformed_payloads_are_refused`), so it is an equivalent mutant.

## 3. Continuation design (FIX-01)

**Plain, re-validated provider parameters, never a token.** A follow-up is an ordinary tool call:
validated, authorized and quota-charged. It needs no secret, no server state and no new env var, and it
works across restarts and redeploys (tested with a fresh process). No HMAC or other opaque token exists.

Two modes, one manifest (`PARTIAL_ID_MANIFEST`, never data, never prices):

- `IN_MEMORY`: a reply above the 256 KiB MCP cap and at most 512 KiB (the read cap, unchanged) is parsed;
  the manifest hashes the canonical JSON of `response`.
- `STREAMED_SCAN` (owner option B): above the read cap, a listing path with a documented row id is streamed
  through `stream_scan.IdScanner`. The body is never buffered whole. It keeps only the per-row ids, the
  grouping fields and the raw row byte sizes, plus `get`, `results`, `paging` and the keys of a non-empty
  `errors`. The manifest carries the SHA-256 of the raw body. A path without a documented row id
  (statistics, events, …) is still refused at the read cap.

**Scan bounds** (`stream_scan.ScanLimits`): 16 MiB of body, 20 s of scanning, 20 000 rows, depth 32,
64 KiB per token. Hitting any of them gives `PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN` with the bound's name,
`navigation.complete_retrieval_available: false`, a narrowing hint, and no partial result. The resilience
request timeout (default 8 s) applies first. Measured scan speed is about 3 MB/s of CPU, in 64 KiB steps
between network reads, so 16 MiB is about 6 s.

**Validation:** strict UTF-8; a single top-level object; `response` must be a list; no duplicate top-level
keys; no data after the document; a strict JSON grammar (an iterative regex tokenizer with an explicit
stack, no recursion, no eval). A malformed or truncated stream gives `PROVIDER_INVALID_JSON`, and a
contract violation gives its own fixed code.

**Splitting and completeness** (review P2):

| Request | Follow-ups | Complete? |
|---|---|---|
| fixtures `date` (no league) | `date + league + season` per league (DOCS_UNVERIFIED combination) | yes, if every group is provably under the read cap |
| fixtures `league + season` (no round) | `+ round` per round | idem |
| fixtures `ids` | the two halves | idem (the same detailed rows) |
| fixtures otherwise (or one group only) | detailed `ids` slices of 2 | **never** (`IDS_DETAIL_REPLY_SIZE_UNPROVEN`) |
| fixtures `id` | — | not splittable |
| leagues, teams, venues, coachs, players, profiles, injuries, transfers, odds/live odds (streamed) | the original filters + one documented id parameter, one call per id | yes, if provable |

"Provable" means a follow-up returns a SUBSET of rows already seen, and its size is under the read cap:
- in memory: a subset of a reply that itself fitted under the cap;
- streamed: the measured raw row bytes plus a 16 KiB envelope margin.

The stated condition is that the same row comes back with the same content. When a follow-up uses a
DOCS_UNVERIFIED combination, provider acceptance is also listed as a condition. Ids outside 1..2·10⁹,
non-integers and duplicates are counted as `rows_without_id`, and completeness is then refused (review
P3-1). Each call carries `combination_status` (review P3-2). Order is `NOT_ASSERTED`: clients compare id
sets, counts and `row_ids_sorted_sha256`, so a provider reorder is not a false "data changed". Follow-up
and next-page arguments use the MCP names `from_date`/`to_date` (review P3-6).

### Declared behaviour changes (pre-existing tests changed on purpose)

| Test | Change | Fix |
|---|---|---|
| `test_mcp_e2e_apifootball_suite.py` | `STOP_LOCAL_QUOTA_EXHAUSTED` → two stops; N00 negative zone | FIX-08, FIX-06 |
| `test_apifootball_pr07_certification.py` | the joining caller of a shared flight reports 0 attempts | P3 |
| `test_apifootball_typed_mcp.py` | `TIMEZONE_NOT_IN_CATALOG`; oversized mock → `PARTIAL_ID_MANIFEST` | FIX-06, FIX-01 |
| `test_apifootball_full_catalog.py`, `test_apifootball_real_reply_pagination.py` | out-of-bound page is 100001 | FIX-04 |
| `test_apifootball_resilience.py` | empty page past the end valid; past the read cap a listing path is scanned (garbage → `PROVIDER_INVALID_JSON`) | FIX-04, FIX-01 |
| `test_apifootball_error_codes.py` | oversized players page → `PARTIAL_ID_MANIFEST` | FIX-01 |
| `test_odds_manifest_and_parameter_matrix.py` | no-price scan skips bridge-clock fields; 0.6–2.2 MB single strings refused by the scan token bound | FIX-07, FIX-01 |
| `test_mcp_e2e_apifootball_error_codes.py` | live facts gain `open_priced`, `live_state` | FIX-09 |
| `test_apifootball_continuation.py` | rewritten: provable completeness, sets not order, MCP names | FIX-01 review |
| `test_apifootball_timezones.py` | `validated_against` = `IANA_TZDATA_PYPI_PACKAGE` | FIX-06 review |

## 4. /fixtures combination table (FIX-03)

Source: `docs/api_football/official_parameter_matrix_v3.9.3.json` only. It lists the 15 parameters as
independent optional filters and records **no combination rule**. Nothing becomes DOCUMENTED_VALID just
because the docs do not forbid it. `timezone` may join any accepted set.

| Combination | Status | Basis / code |
|---|---|---|
| `id` | DOCUMENTED_VALID | single documented parameter; real PASS id=1557464 (runs on 37e74fd) |
| `ids` (≤ 20) | DOCUMENTED_VALID | single documented parameter, "Maximum of 20" |
| `live` = `all` or `id-id` (≥ 2 ids) | DOCUMENTED_VALID | documented enum; real `live=all` (13 fixtures) |
| `live` = one league id | DOCS_UNVERIFIED | documented form is a list ("several leagues ids") |
| `date` | DOCUMENTED_VALID | single documented parameter |
| `league` + `season` | DOCUMENTED_VALID | `season`: "The season of the league" |
| `league` + `season` + `from` + `to` | DOCUMENTED_VALID | same, plus real PASS on 37e74fd |
| `team` + `season`, `venue` + `season` | DOCS_UNVERIFIED | no combination rule captured |
| `league`/`team`/`venue` + `last`/`next`; `last` or `next` alone | DOCS_UNVERIFIED | idem |
| `league`+`season` + `round`/`status`/`team`/`venue` | DOCS_UNVERIFIED | idem |
| `team`/`venue` (+`season`) + `from` + `to` | DOCS_UNVERIFIED | idem |
| `date` + `league`/`season`/`team`/`status`/… | DOCS_UNVERIFIED | idem |
| `live` + `status`/`league`/…; `id` + `ids` | DOCS_UNVERIFIED | idem |
| no scoping filter (`{}`, `league`, `team`, `venue`, `season`, `status`, `round`, `timezone`, `from`+`to`, `team`+`from`, `league`+`team`) | MCP_REJECTED | `FIXTURE_FILTER_REQUIRED` |
| `last` + `next` | MCP_REJECTED | `FIXTURE_LAST_NEXT_EXCLUSIVE` |
| `from` after `to` | MCP_REJECTED | `INVALID_DATE_RANGE` |
| `last` or `next` > 99 | MCP_REJECTED | `PAGE_OR_BATCH_TOO_LARGE` |

## 5. Timezone source (FIX-06)

- **What is validated:** a zone is checked against the IANA database of the pinned PyPI package
  `tzdata==2025.2` (IANA 2025b), not the host's tz files, so the result is the same on every host. The
  reply states `validated_against: IANA_TZDATA_PYPI_PACKAGE`, the IANA version and
  `provider_acceptance: UNVERIFIED`.
- **What is not validated:** the provider's own `/timezone` catalog is not captured, so the bridge never
  claims provider-catalog validation.
- **Fail closed:** a missing package leaves only UTC accepted.
- **Provider authority:** the provider decides. A zone it rejects comes back as a declared `timezone` error.
- **No conversion:** the bridge never converts times. `timezone_check` (DERIVED) checks the published
  offsets against IANA at each UTC instant, so DST changes are handled exactly.

## 6. Times (FIX-07)

- `provider_timestamp_utc` comes from the provider's HTTP `Date` header, in strict IMF-fixdate form. It
  is null when the header is absent or malformed.
- That header is the **generation time of the HTTP reply**. It may be set by an edge or CDN server, and it
  says nothing about data freshness.
- **It has never been observed in a real API-FOOTBALL reply.**
- Data times such as `update` and `fixture.timestamp` stay inside the provider data.
- The bridge-clock times are labelled in `time_sources`: `retrieved_at_utc` (acquisition, kept on a
  cache HIT), `responded_at_utc`, `age_seconds` and `cache_age_seconds`.

## 7. Quota (FIX-08)

- **Unchanged:** limits and defaults (`APIFOOTBALL_MAX_RPM=60`, `APIFOOTBALL_MAX_DAILY_REQUESTS=7000`,
  `max_concurrency` 1 as configured; code defaults 8/80/2).
- **`LOCAL_RATE_LIMITED`:** carries `retry_after_seconds`, computed from the rolling 60 s window or from
  the provider-zero marker.
- **`LOCAL_DAILY_QUOTA_EXHAUSTED`:** carries `reset_at_utc`, the next UTC midnight.

## 8. Residual risks

| Id | Risk | Mitigation |
|---|---|---|
| R1 | Replies above the scan bounds (16 MiB, 20 000 rows, …) are not retrievable completely | Fixed code + narrowing hint; bounds are owner-adjustable constants |
| R2 | `fixtures?ids=` detail size not evidenced | Detail slices of 2, never claimed complete; oversized `ids` replies are halved |
| R3 | Per-id splits cost one call per id (e.g. 1 200 leagues) | Explicit call list; nothing fetched automatically |
| R4 | DOCS_UNVERIFIED combinations may be refused upstream | Labelled per call; listed as a completeness condition; provider errors surface with keys |
| R5 | IANA (tzdata) and the provider catalog may differ | Provider is final authority; `provider_acceptance: UNVERIFIED` |
| R6 | `Date` header never observed live; edge time, not freshness | Null when absent; documented semantics |
| R7 | No real `/odds/live` payload; selection fields from repo fixtures | C08 NOT proven; conservative DERIVED states |
| R8 | Page past the end DOCS_UNVERIFIED (D06) | Accepted only without rows |
| R9 | Official docs returned 403 on 2026-11-10 | Re-capture when reachable |
| R10 | A scan of up to 16 MiB costs about 6 s of CPU on the event loop, in steps of about 20 ms | Bounded by the scan time limit and the request timeout |

Real certification: proposal (not executed) in `apifootball_full_capability_budget.md`.
