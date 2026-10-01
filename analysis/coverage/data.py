"""Explicit SELECT adapter, no reconciliation, schema or registry writes."""
from app.db import connect
from app.market_reference import utc

PREDICTION_SQL = '''WITH revisions AS (
    SELECT match_id,count(DISTINCT (kickoff_utc,league,home_team,away_team)) AS fixture_versions
    FROM model_snapshots WHERE snapshot_timestamp_utc<=%s AND imported_at<=%s GROUP BY match_id
), anchor AS (
    SELECT DISTINCT ON (s.match_id) s.*,r.fixture_versions
    FROM model_snapshots s JOIN revisions r USING(match_id)
    WHERE s.snapshot_timestamp_utc<=%s AND s.imported_at<=%s
    ORDER BY s.match_id,s.snapshot_timestamp_utc DESC
), pilot AS (
    SELECT DISTINCT ON (p.source,p.source_match_key) p.*,r.finished_at_utc,m.match_status
    FROM source_pilot_predictions p JOIN source_pilot_runs r USING(run_id)
    LEFT JOIN source_pilot_matches m USING(run_id,source_match_key)
    WHERE r.finished_at_utc<=%s ORDER BY p.source,p.source_match_key,r.finished_at_utc DESC,p.run_id DESC
)
SELECT match_id AS source_match_key,match_id,COALESCE(payload->>'source','footinsights') AS source,
    league,payload->>'country' AS country,payload->>'category' AS category,
    kickoff_utc,snapshot_timestamp_utc AS captured_at_utc,imported_at AS sealed_at_utc,
    COALESCE(payload->>'timezone_status','TIMEZONE_UNRESOLVED') AS timezone_status,
    'ANCHOR_NATIVE' AS matching_status,valid_prematch,fixture_versions,
    jsonb_build_array(payload->'p_home_normalized',payload->'p_draw_normalized',payload->'p_away_normalized') AS probabilities,
    1 AS raw_scale,'OK' AS parse_status,
    (COALESCE(payload->>'source','footinsights')='footinsights'
     AND payload->>'source_url' LIKE 'https://footinsights.com/predictions/%%'
     AND COALESCE(payload->>'pilot_id','')='' AND COALESCE(payload->>'channel','')<>'PILOT') AS is_anchor
FROM anchor
UNION ALL
SELECT source_match_key,NULL AS match_id,source,league,payload->>'country',payload->>'category',
    kickoff_utc,finished_at_utc,NULL AS sealed_at_utc,timezone_status,match_status,
    FALSE AS valid_prematch,1 AS fixture_versions,jsonb_build_array(p1,px,p2),100,parse_status,FALSE AS is_anchor
FROM pilot'''
OBSERVATION_SQL = '''SELECT * FROM result_observations WHERE observed_at_utc<=%s
    ORDER BY match_id,provider,observed_at_utc'''
TICKET_SQL = '''SELECT match_id,league,source,market_id,status,kickoff_utc,updated_at,
    payload->>'country' AS country,payload->>'category' AS category
    FROM market_decision_tickets WHERE created_at<=%s'''


def load_dataset(*, known_at, connection_factory=connect):
    known = utc(known_at)
    if known is None: raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cur.execute(PREDICTION_SQL, (known,)*5); predictions = list(cur.fetchall())
        cur.execute(OBSERVATION_SQL, (known,)); observations = list(cur.fetchall())
        cur.execute(TICKET_SQL, (known,)); tickets = list(cur.fetchall())
    return {'predictions':predictions,'observations':observations,'market_tickets':tickets}
