"""Consistent source/result read without rebuilding mutable consensus tables."""
from app.db import connect
from app.market_reference import utc
from analysis.scoring.report import dataset_from_rows

SNAPSHOT_SQL = '''SELECT match_id,kickoff_utc,snapshot_timestamp_utc,home_team,
    away_team,league,valid_prematch,imported_at,payload FROM model_snapshots
    WHERE snapshot_timestamp_utc<=%s AND imported_at<=%s
    ORDER BY match_id,snapshot_timestamp_utc'''
OBSERVATION_SQL = '''SELECT match_id,provider,observed_at_utc,status,
    final_home_goals,final_away_goals,ht_home_goals,ht_away_goals,corners_home,
    corners_away,ht_corners_home,ht_corners_away FROM result_observations
    WHERE observed_at_utc<=%s ORDER BY match_id,provider,observed_at_utc'''


def load_dataset(*, known_at, connection_factory=connect):
    known = utc(known_at)
    if known is None: raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cur.execute(SNAPSHOT_SQL,(known,known)); snapshots = list(cur.fetchall())
        cur.execute(OBSERVATION_SQL,(known,)); observations = list(cur.fetchall())
    return dataset_from_rows(snapshots,observations,known_at=known)
