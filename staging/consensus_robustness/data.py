"""Explicit SELECT adapter. The connection itself rejects writes."""
import json

from app.db import connect

LOAD_SQL = """
SELECT p.run_id || ':' || p.source_match_key AS row_id,p.source,
       m.anchor_match_id AS match_id,p.kickoff_utc,
       r.finished_at_utc AS captured_at_utc,
       p.payload->>'sealed_at_utc' AS sealed_at_utc,
       jsonb_build_array(p.p1,p.px,p.p2) AS probabilities,
       100 AS raw_scale,p.parse_status,p.timezone_status,
       CASE WHEN m.match_status='ANCHOR_NATIVE' THEN 'UNVERIFIED_MATCH'
            ELSE m.match_status END AS match_status
FROM source_pilot_predictions p JOIN source_pilot_runs r USING(run_id)
LEFT JOIN source_pilot_matches m USING(run_id,source_match_key)
UNION ALL
SELECT match_id || ':' || snapshot_timestamp_utc::text AS row_id,
       'footinsights' AS source,match_id,kickoff_utc,
       snapshot_timestamp_utc AS captured_at_utc,imported_at::text AS sealed_at_utc,
       jsonb_build_array(payload->'p_home_normalized',payload->'p_draw_normalized',
                        payload->'p_away_normalized') AS probabilities,
       1 AS raw_scale,'OK' AS parse_status,'EXPLICIT_OFFSET' AS timezone_status,
       'ANCHOR_NATIVE' AS match_status
FROM model_snapshots WHERE valid_prematch=TRUE
  AND payload->>'source_url' LIKE 'https://footinsights.com/predictions/%%'
  AND COALESCE(payload->>'source','footinsights')='footinsights'
ORDER BY row_id
"""


def load_rows(connection_factory=connect, *, source_roles=None):
    # This classifies the current production anchor as a model candidate only.
    # Pilot research roles need an explicit supplied mapping; no claim of
    # independence and no promotion/allowlist mutation follows from this input.
    roles = {'footinsights': 'MODEL_CANDIDATE', **(source_roles or {})}
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute(LOAD_SQL)
        rows = list(cur.fetchall())
    for row in rows:
        if isinstance(row['probabilities'], str):
            row['probabilities'] = json.loads(row['probabilities'])
        row['role'] = roles.get(row['source'], 'UNKNOWN')
    return rows
