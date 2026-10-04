"""Durable ownership and leases. Never stores a CDP URL or provider credential."""
from app.db import connect


def register(session_id, owner_id, seconds=300):
    with connect() as conn:
        conn.execute('''INSERT INTO browser_session_leases(session_id,owner_id,lease_until,state)
                        VALUES (%s,%s,NOW()+%s*INTERVAL '1 second','ACTIVE')''',
                     (session_id, owner_id, seconds))


def renew(session_id, owner_id, seconds=300):
    with connect() as conn:
        row = conn.execute('''UPDATE browser_session_leases
                              SET last_activity_at=NOW(),lease_until=NOW()+%s*INTERVAL '1 second',updated_at=NOW()
                              WHERE session_id=%s AND owner_id=%s AND state='ACTIVE'
                              RETURNING session_id''', (seconds, session_id, owner_id)).fetchone()
        if row is None:
            raise RuntimeError('Browser lease is no longer active or owned by this worker')


def claim_stop(session_id, owner_id):
    with connect() as conn:
        row = conn.execute('''UPDATE browser_session_leases SET state='STOPPING',
                              lease_until=NOW()+INTERVAL '120 seconds',updated_at=NOW()
                              WHERE session_id=%s AND owner_id=%s AND state IN ('ACTIVE','STOP_FAILED')
                              RETURNING session_id''', (session_id, owner_id)).fetchone()
        return bool(row)


def claim_stale(owner_id, idle_seconds=300, exclude=()):
    with connect() as conn:
        rows = conn.execute('''WITH candidates AS (
                     SELECT session_id FROM browser_session_leases
                     WHERE state IN ('ACTIVE','STOP_FAILED','STOPPING')
                       AND lease_until<=NOW()
                       AND last_activity_at<=NOW()-%s*INTERVAL '1 second'
                       AND NOT (session_id=ANY(%s::text[]))
                     ORDER BY lease_until FOR UPDATE SKIP LOCKED LIMIT 20
                 ) UPDATE browser_session_leases s SET owner_id=%s,state='STOPPING',
                     lease_until=NOW()+INTERVAL '120 seconds',updated_at=NOW()
                 FROM candidates c WHERE s.session_id=c.session_id RETURNING s.session_id''',
                            (idle_seconds, list(exclude), owner_id)).fetchall()
        return [r['session_id'] for r in rows]


def renew_stop(session_id, owner_id):
    with connect() as conn:
        row = conn.execute('''UPDATE browser_session_leases SET lease_until=NOW()+INTERVAL '120 seconds',updated_at=NOW()
                              WHERE session_id=%s AND owner_id=%s AND state='STOPPING'
                              RETURNING session_id''', (session_id, owner_id)).fetchone()
        if row is None:
            raise RuntimeError('Browser stop is no longer owned by this reaper')


def finish(session_id, owner_id, success, error='', retry_seconds=60):
    with connect() as conn:
        row = conn.execute('''UPDATE browser_session_leases SET state=%s,stop_error=%s,
                              lease_until=NOW()+%s*INTERVAL '1 second',updated_at=NOW()
                              WHERE session_id=%s AND owner_id=%s AND state='STOPPING'
                              RETURNING session_id''',
                           ('STOPPED' if success else 'STOP_FAILED', error, retry_seconds, session_id, owner_id)).fetchone()
        if row is None:
            raise RuntimeError('Browser stop ownership changed before completion')


def get_session(session_id):
    with connect() as conn:
        row = conn.execute('''SELECT session_id,state,created_at,last_activity_at,lease_until,
                                     updated_at,stop_error
                              FROM browser_session_leases
                              WHERE session_id=%s''', (session_id,)).fetchone()
        return dict(row) if row is not None else None


def list_sessions(include_stopped=False, limit=200):
    if type(include_stopped) is not bool:
        raise ValueError('include_stopped must be boolean')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    where = '' if include_stopped else "WHERE state <> 'STOPPED'"
    with connect() as conn:
        rows = conn.execute(f'''SELECT session_id,state,created_at,last_activity_at,lease_until,
                                      updated_at,stop_error
                               FROM browser_session_leases
                               {where}
                               ORDER BY created_at DESC
                               LIMIT %s''', (limit,)).fetchall()
        return [dict(row) for row in rows]


def state_summary():
    with connect() as conn:
        rows = conn.execute('''SELECT state,COUNT(*) AS count
                               FROM browser_session_leases
                               GROUP BY state''').fetchall()
        return {row['state']: int(row['count']) for row in rows}


def healthcheck():
    with connect() as conn:
        row = conn.execute('SELECT 1 AS ok').fetchone()
        return bool(row and row['ok'] == 1)
