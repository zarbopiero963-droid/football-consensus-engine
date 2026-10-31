# Contract registry changelog

## 2026-10-31 (first call after process start)
- Owner real test after PR #126: call 1 `QUOTA_COORDINATOR_UNAVAILABLE`, call 2 HTTP 200. Render logs: first API-FOOTBALL call of process [59] (started 21:50:08, no restart); ~3.85 s total; one fresh DB connection measured at ~0.95 s.
- Root cause: the first PostgresQuota operation per process runs schema init (connect + DDL) and the reservation (connect + statements) inside the same 3 s lease timeout. Reproduced on real PostgreSQL with the measured latency.
- Fix: one-time `prepare()` runs before the timed reservation with its own 10 s shielded bound; reservation timeout stays 3 s. Fixed secret-free diagnostics: `QUOTA_COORDINATOR_UNAVAILABLE stage=<prepare|reserve> reason=<TIMEOUT|ERROR> error_type=<class> elapsed_ms=<n>`.

## 2026-10-31 (quota coordinator orphan lease)
- Root cause of `QUOTA_COORDINATOR_UNAVAILABLE` followed by repeated `LOCAL_CONCURRENCY_BUSY`: the 3 s coordinator timeout abandoned a worker thread that still committed its lease; with concurrency 1 the slot stayed held for the 180 s lease expiry. Reproduced on real PostgreSQL.
- Fix: the reservation is shielded; on timeout or cancellation a compensating release returns the orphan lease. Budget counters stay consumed (conservative); limits, timeout value and error codes unchanged.

## 2026-10-25
- Added canonical machine-readable endpoint and parameter registry.
- Added deterministic SHA-256 snapshot support and offline semantic detector.
- Preserved pending status for unreconciled official documentation.
- No provider calls, secrets, cron, or live-gate changes.
