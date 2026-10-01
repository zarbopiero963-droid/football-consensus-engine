"""Read-only operational gates. These never promote sources or mutate frozen data."""
from datetime import datetime, timezone

from app.db import connect


COUNTERS_SQL = """
WITH tickets AS (
  SELECT dt.*,rc.field_verification,rc.result_1x2,
         rc.final_home_goals,rc.final_away_goals
  FROM decision_tickets dt LEFT JOIN result_consensus rc USING(match_id)
  WHERE dt.shown_to_user
), markets AS (
  SELECT m.*,to_jsonb(rc) AS result,
    CASE WHEN market_id='HTFT_1X2' THEN ARRAY['ht_home_goals','ht_away_goals','final_home_goals','final_away_goals']
         WHEN market_id LIKE 'HT_CORNERS_OU%%' THEN ARRAY['ht_corners_home','ht_corners_away']
         WHEN market_id LIKE 'FT_CORNERS_OU%%' THEN ARRAY['corners_home','corners_away']
         WHEN left(market_id,3)='HT_' THEN ARRAY['ht_home_goals','ht_away_goals']
         WHEN left(market_id,3)='FT_' THEN ARRAY['final_home_goals','final_away_goals']
         ELSE ARRAY[]::text[] END AS required
  FROM market_decision_tickets m LEFT JOIN result_consensus rc USING(match_id)
), latest_pilot AS (
  SELECT DISTINCT ON(pilot_id,source) run_id,source,status,manifest,http_status
  FROM source_pilot_runs ORDER BY pilot_id,source,slot_no DESC
), latest_result_pilot AS (
  SELECT DISTINCT ON(pilot_id,source) http_status
  FROM result_source_pilot_runs ORDER BY pilot_id,source,slot_no DESC
), pilot_quality AS (
  SELECT r.*,count(p.source_match_key) AS rows,
    count(*) FILTER(WHERE p.parse_status='BROKEN') AS broken_rows,
    count(*) FILTER(WHERE p.source_match_key IS NOT NULL AND
      (p.kickoff_utc IS NULL OR p.timezone_status<>'VERIFIED_SOURCE_TZ')) AS unresolved_rows
  FROM latest_pilot r LEFT JOIN source_pilot_predictions p USING(run_id)
  GROUP BY r.run_id,r.source,r.status,r.manifest,r.http_status
)
SELECT
  (SELECT count(*) FROM model_snapshots) AS snapshots,
  (SELECT count(*) FROM tickets WHERE kickoff_utc<=NOW()-INTERVAL '2 hours') AS due_tickets,
  (SELECT count(*) FROM tickets WHERE kickoff_utc<=NOW()-INTERVAL '2 hours' AND
     (COALESCE(field_verification->'final_home_goals'->>'status','')<>'VERIFIED' OR
      COALESCE(field_verification->'final_away_goals'->>'status','')<>'VERIFIED' OR
      final_home_goals IS NULL OR final_away_goals IS NULL OR
      COALESCE(result_1x2,'') NOT IN ('1','X','2'))) AS pending_due_tickets,
  (SELECT count(*) FROM markets WHERE status IN ('WON','LOST','PUSH') AND
     (cardinality(required)=0 OR EXISTS (SELECT 1 FROM unnest(required) f WHERE
       COALESCE(result->'field_verification'->f->>'status','')<>'VERIFIED' OR
       result->>f IS NULL))) AS illegal_market_settlements,
  (SELECT count(*) FROM strategy_simulations s LEFT JOIN result_consensus rc USING(match_id)
     WHERE s.status IN ('WON','LOST') AND
     (COALESCE(rc.field_verification->'final_home_goals'->>'status','')<>'VERIFIED' OR
      COALESCE(rc.field_verification->'final_away_goals'->>'status','')<>'VERIFIED' OR
      rc.final_home_goals IS NULL OR rc.final_away_goals IS NULL OR
      COALESCE(rc.result_1x2,'') NOT IN ('1','X','2'))) AS illegal_strategy_settlements,
  (SELECT count(*) FROM market_decision_tickets WHERE source<>'footinsights') AS nonproduction_market_sources,
  (SELECT count(*) FROM model_snapshots WHERE
     COALESCE(payload->>'pilot_id','')<>'' OR payload->>'channel'='PILOT' OR
     COALESCE(payload->>'source','footinsights')<>'footinsights') AS pilot_or_unknown_snapshots,
  (SELECT count(*) FROM tickets WHERE
     COALESCE(snapshot_view->>'pilot_id','')<>'' OR snapshot_view->>'channel'='PILOT' OR
     COALESCE(snapshot_view->>'source','footinsights')<>'footinsights') AS pilot_or_unknown_tickets,
  (SELECT count(*) FROM model_snapshots WHERE valid_prematch AND
     (kickoff_utc IS NULL OR snapshot_timestamp_utc>=kickoff_utc OR
      payload->>'timezone_status' IN ('TIMEZONE_UNRESOLVED','DATE_UNRESOLVED','VERIFIED_BROWSER_TZ'))) AS unsafe_production_time,
  (SELECT count(*) FROM pilot_quality WHERE status<>'OK' OR broken_rows>0) AS broken_pilot_parsers,
  (SELECT count(*) FROM pilot_quality WHERE status='OK' AND rows=0) AS zero_row_pilot_parsers,
  (SELECT count(*) FROM (
     SELECT http_status FROM latest_pilot
     UNION ALL SELECT http_status FROM latest_result_pilot
   ) transports WHERE http_status IS DISTINCT FROM 200) AS failed_pilot_transports,
  (SELECT COALESCE(sum(unresolved_rows),0) FROM pilot_quality) AS unresolved_pilot_timezone_rows
"""

RUNTIME_SQL = "SELECT key,updated_at FROM runtime_state WHERE key IN ('collector','settlement')"


def evaluate(counters, workers, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('health timestamp requires an explicit timezone')
    violations = [name for name in (
        'illegal_market_settlements', 'illegal_strategy_settlements',
        'nonproduction_market_sources', 'pilot_or_unknown_snapshots',
        'pilot_or_unknown_tickets', 'unsafe_production_time') if counters[name] > 0]
    warnings = [name for name in ('broken_pilot_parsers', 'zero_row_pilot_parsers',
                                  'failed_pilot_transports',
                                  'unresolved_pilot_timezone_rows') if counters[name] > 0]
    due = counters['due_tickets']
    ratio = counters['pending_due_tickets'] / due if due else None
    if ratio is not None and ratio > .5:
        warnings.append('high_pending_ratio')
    if not counters['snapshots']:
        warnings.append('no_production_snapshots')
    age_limits = {'collector': 6 * 3600, 'settlement': 3600}
    worker_ages = {}
    for key, limit in age_limits.items():
        stamp = workers.get(key)
        if stamp is None:
            warnings.append(key + '_unobserved')
            worker_ages[key] = None
        else:
            age = (now - stamp).total_seconds()
            worker_ages[key] = age
            if age < 0 or age > limit:
                warnings.append(key + '_stale_or_future')
    status = 'FAIL' if violations else ('DEGRADED' if warnings else 'PASS')
    return {'status': status, 'ready': status == 'PASS', 'at_utc': now.isoformat(),
            'counts': counters, 'pending_ratio': ratio, 'violations': violations,
            'warnings': warnings, 'worker_age_seconds': worker_ages,
            'policy': {'pending_after_kickoff_hours': 2, 'pending_ratio_warning_above': .5,
                       'production_sources': ['footinsights'], 'worker_max_age_seconds': age_limits}}


def runtime_health():
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cur.execute("SET LOCAL statement_timeout='10s'")
        cur.execute(COUNTERS_SQL)
        counters = {k: int(v) for k, v in cur.fetchone().items()}
        cur.execute(RUNTIME_SQL)
        workers = {r['key']: r['updated_at'] for r in cur.fetchall()}
    return evaluate(counters, workers)
