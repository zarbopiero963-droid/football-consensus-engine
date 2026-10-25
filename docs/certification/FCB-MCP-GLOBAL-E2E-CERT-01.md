# FCB-MCP-GLOBAL-E2E-CERT-01 — GLOBAL NO-GO / CERTIFIED GATE

Baseline release: `ee4cabd1cde5a15256651f0a4db6af814cc1bf15` (#123).

## Mission
A single PR to gather actual verifiable evidence for all exposed API-FOOTBALL
MCP tools, all Browser Use tools, OAuth, Render deployment, Neon quota coordinator,
ChatGPT connector, and operational recovery. **Implemented ≠ certified.**

## Non-negotiable acceptance
1. Enumerate all actual tool names from the deployed authenticated MCP tools/list.
   Match exact expected counts against discovered inventory; never assume 42+34.
2. All official provider routes/parameters reconciled against dated official
   documentation. Source section, request, response and deviations documented.
3. Provider LIVE samples with two timestamped snapshots; quota-budgeted, no
   polling bypass. Actual missing or empty data is clearly classified.
4. API/Browser tool-by-tool valid and invalid inputs, auth denials, bounds,
   error mapping and concurrency tests with recorded sanitized evidence.
5. OAuth negative/refresh/expiry/revoke flows, Render exact SHA readback,
   Neon transaction/quota races, restart/recovery, and authenticated ChatGPT E2E.
6. Independent reviewer approval tied to exact SHA, resolved threads and CI green.
7. Machine readable verdict: `CERTIFIED` ONLY with evidence SHA256, URI,
   exact commit and all mandatory domains PASS. Else `NO_GO`.
8. No production secrets copied, no limit removal, no unapproved merge.

## Execution
The first commit intentionally defines a **fail-closed evidence schema and
baseline gate only**; it does NOT assert real E2E completion. Browser tool count
is unknown until discovery; even passing offline tests only proves the gate
logic. Follow-up commits within this same PR must attach all evidence and
implement outstanding tests, then undergo independent review.

## Publication
Evidence must be reproducible using CI with SHA256 and log fallback when
GitHub artifact storage is full. Keep sampled LIVE scope distinct from a
universal promise about future football data.
