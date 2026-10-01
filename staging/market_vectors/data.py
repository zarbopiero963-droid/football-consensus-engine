"""Latest captured StatsUltra run and legacy rows, enforced SELECT-only."""
from app.db import connect
from app.market_reference import utc

RUN_SQL = '''SELECT run_id,pilot_id,source,slot_no,finished_at_utc,sealed_at_utc,status,http_status,
    final_url,html_sha256,encode(html_gzip,'base64') AS html_gzip_base64
    FROM source_pilot_runs WHERE source='statsultra' AND finished_at_utc <= %s
    AND (sealed_at_utc IS NULL OR sealed_at_utc<=%s)
    ORDER BY finished_at_utc DESC,run_id DESC LIMIT 1'''
ROW_SQL = '''SELECT p.*,m.match_status,m.anchor_match_id
    FROM source_pilot_predictions p LEFT JOIN source_pilot_matches m USING(run_id,source_match_key)
    WHERE p.run_id=%s ORDER BY source_match_key'''


def load_dataset(*, known_at, connection_factory=connect):
    known = utc(known_at)
    if known is None:
        raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute(RUN_SQL, (known,known))
        runs = list(cur.fetchall())
        rows = []
        if runs:
            cur.execute(ROW_SQL, (runs[0]['run_id'],))
            rows = list(cur.fetchall())
    return {'runs': runs, 'rows': rows}
