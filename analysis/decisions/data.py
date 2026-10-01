"""Consistent read of frozen tickets, exact provenance, and cutoff observations."""
from app.db import connect
from app.market_reference import utc

RESULT_SQL = '''SELECT match_id,provider,observed_at_utc,kickoff_utc,status,
    final_home_goals,final_away_goals FROM result_observations
    WHERE observed_at_utc<=%s ORDER BY match_id,provider,observed_at_utc'''

TICKET_SQL = '''SELECT t.*, b.requested_at_utc, b.created_at AS batch_created_at,
    to_jsonb(s) AS original_snapshot
    FROM decision_tickets t JOIN decision_batches b USING(batch_id)
    LEFT JOIN model_snapshots s ON s.match_id=t.match_id
        AND s.snapshot_timestamp_utc=t.snapshot_timestamp_utc
    WHERE b.requested_at_utc >= %s AND b.requested_at_utc < %s
        AND b.requested_at_utc <= %s AND t.created_at <= %s AND b.created_at <= %s
    ORDER BY t.match_id,b.requested_at_utc,t.batch_id'''


def load_dataset(*, known_at, protocol, protocol_id=None, connection_factory=connect):
    known = utc(known_at)
    if known is None: raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    cutoff = min(known, protocol.final_cutoff())
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cur.execute(TICKET_SQL,(utc(protocol.window_start),utc(protocol.window_end),cutoff,cutoff,cutoff))
        tickets = list(cur.fetchall())
        cur.execute(RESULT_SQL,(cutoff,)); observations = list(cur.fetchall())
        registration = None
        if protocol_id:
            cur.execute('SELECT * FROM evaluation_protocols WHERE protocol_id=%s AND sealed_at_utc<=%s',
                        (protocol_id,known))
            registration = cur.fetchone()
    return {'tickets': tickets, 'observations': observations}, registration
