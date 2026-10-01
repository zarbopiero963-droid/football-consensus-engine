"""Operational source circuits in runtime_state, separate from immutable evidence."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid


CONTRACT = 'SOURCE_CIRCUIT_V1'
LEASE_SECONDS = 15 * 60
COOLDOWNS = (3 * 3600, 6 * 3600, 12 * 3600)


def source_key(role, pilot_id, source):
    if role not in {'prediction', 'result'}:
        raise ValueError('unknown source role')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}', v) for v in (pilot_id, source)):
        raise ValueError('invalid source identity')
    return f'source_circuit:v1:{role}:{pilot_id}:{source}'


def initial_state():
    return {'contract': CONTRACT, 'mode': 'CLOSED', 'failures': 0, 'open_count': 0,
            'retry_at_utc': None, 'lease_token': None, 'lease_until_utc': None,
            'stop_pending_id': None, 'active_browser_id': None, 'events': []}


def timestamp(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('source state timestamp requires timezone')
    return dt


def public_state(state):
    return {k: state.get(k) for k in ('contract', 'mode', 'failures', 'open_count',
                                     'retry_at_utc', 'lease_until_utc', 'reason', 'stop_pending_id')}


def event(state, now, action, reason=''):
    state['events'] = (state.get('events', []) + [{'at_utc': now.isoformat(),
        'action': action, 'reason': reason, 'mode': state['mode']}])[-32:]


def admit_state(previous, now, token):
    state = {**previous}
    if state.get('contract') != CONTRACT or state.get('mode') not in {'CLOSED', 'OPEN', 'HALF_OPEN'}:
        raise ValueError('invalid source circuit state')
    until = timestamp(state.get('lease_until_utc'))
    if until and until > now:
        return state, 'BUSY'
    retry_at = timestamp(state.get('retry_at_utc'))
    if state['mode'] == 'OPEN' and retry_at and retry_at > now:
        return state, 'SUSPENDED'
    if state['mode'] != 'CLOSED' or state.get('lease_token'):
        state['mode'] = 'HALF_OPEN'
    if state.get('active_browser_id'):
        state['stop_pending_id'] = state['active_browser_id']
    state['lease_token'] = token
    state['lease_until_utc'] = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
    event(state, now, 'ADMITTED')
    return state, 'ADMITTED'


def finish_state(previous, now, fault):
    state = {**previous, 'lease_token': None, 'lease_until_utc': None}
    state['reason'] = fault['reason']
    if fault.get('stop_pending_id'):
        state['stop_pending_id'] = fault['stop_pending_id']
    if fault.get('unchanged'):
        if state['mode'] == 'HALF_OPEN':
            state['mode'] = 'OPEN'
    elif not fault['failed']:
        state.update(mode='CLOSED', failures=0, open_count=0, retry_at_utc=None,
                     active_browser_id=None, stop_pending_id=None)
    else:
        state['failures'] += 1
        if fault.get('immediate') or previous['mode'] == 'HALF_OPEN' or state['failures'] >= 2:
            state['open_count'] += 1
            seconds = COOLDOWNS[min(state['open_count'] - 1, len(COOLDOWNS) - 1)]
            if fault['reason'] in {'PARSER_ERROR', 'ACCESS_BLOCKED', 'HTTP_401', 'HTTP_403', 'HTTP_404'}:
                seconds = COOLDOWNS[-1]
            seconds = max(seconds, fault.get('retry_after_seconds') or 0)
            state.update(mode='OPEN', retry_at_utc=(now + timedelta(seconds=seconds)).isoformat())
    event(state, now, 'UNCHANGED' if fault.get('unchanged') else
          ('FAILED' if fault['failed'] else 'RECOVERED'), fault['reason'])
    return state


def capture_fault(outcome):
    if outcome.get('status') == 'ALREADY_DONE':
        return {'reason': 'ALREADY_DONE', 'failed': False, 'unchanged': True}
    capture = outcome['page_capture']
    state = capture['state']
    # A working tomorrow/base page keeps the partial source available. Extra
    # failed fields remain missing and retain their own capture audit.
    captures = [capture, *outcome.get('extra_page_captures', {}).values()]
    if any(c['state'] == 'READY_PARSED' for c in captures):
        return {'reason': 'READY_PARSED', 'failed': False}
    if state in {'CAPABILITY_ONLY', 'EMPTY_UNCONFIRMED'}:
        return {'reason': state, 'failed': False, 'unchanged': state == 'EMPTY_UNCONFIRMED'}
    status = capture.get('http_status')
    reason = f'HTTP_{status}' if state == 'HTTP_ERROR' else state
    attempts = capture.get('navigation_attempts', [])
    retry_after = max((a.get('retry_after_seconds', 0) for a in attempts), default=0)
    return {'reason': reason, 'failed': True, 'retry_after_seconds': retry_after,
            'immediate': state in {'MAINTENANCE', 'ACCESS_BLOCKED', 'PARSER_ERROR'} or
                         status in {401, 403, 404, 429}}


class SourceCircuit:
    def __init__(self, connect, key):
        self.connect, self.key = connect, key
        self.lock = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') & ((1 << 63) - 1)

    def _load(self, cur):
        cur.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock,))
        cur.execute('''INSERT INTO runtime_state(key,value) VALUES (%s,%s::jsonb)
                       ON CONFLICT(key) DO NOTHING''', (self.key, json.dumps(initial_state())))
        cur.execute('SELECT value,clock_timestamp() AS at FROM runtime_state WHERE key=%s FOR UPDATE', (self.key,))
        return cur.fetchone()

    def _save(self, cur, value):
        cur.execute('UPDATE runtime_state SET value=%s::jsonb,updated_at=clock_timestamp() WHERE key=%s',
                    (json.dumps(value), self.key))

    def claim(self):
        token = uuid.uuid4().hex
        with self.connect() as conn, conn.cursor() as cur:
            row = self._load(cur)
            state, decision = admit_state(row['value'], row['at'], token)
            if decision == 'ADMITTED':
                self._save(cur, state)
            conn.commit()
        return {'key': self.key, 'token': token if decision == 'ADMITTED' else None,
                'decision': decision, 'state': state}

    def finish(self, claim, fault):
        with self.connect() as conn, conn.cursor() as cur:
            row = self._load(cur)
            self._owned(row, claim)
            state = finish_state(row['value'], row['at'], fault)
            self._save(cur, state)
            conn.commit()
        return public_state(state)

    def clear_stopped_browser(self, claim):
        with self.connect() as conn, conn.cursor() as cur:
            row = self._load(cur)
            self._owned(row, claim)
            row['value']['stop_pending_id'] = None
            row['value']['active_browser_id'] = None
            self._save(cur, row['value'])
            conn.commit()

    @staticmethod
    def _owned(row, claim):
        if (row['value'].get('lease_token') != claim['token'] or
                timestamp(row['value'].get('lease_until_utc')) <= row['at']):
            raise RuntimeError('stale source circuit owner')

    def register_browser(self, claim, session_id):
        with self.connect() as conn, conn.cursor() as cur:
            row = self._load(cur)
            self._owned(row, claim)
            row['value']['active_browser_id'] = session_id
            self._save(cur, row['value'])
            conn.commit()


def assert_owner(cur, claim):
    if claim is None:
        raise RuntimeError('source acquisition requires a circuit owner')
    cur.execute('SELECT value,clock_timestamp() AS at FROM runtime_state WHERE key=%s FOR UPDATE', (claim['key'],))
    row = cur.fetchone()
    if (row is None or row['value'].get('lease_token') != claim['token'] or
            not row['value'].get('lease_until_utc') or
            timestamp(row['value'].get('lease_until_utc')) <= row['at']):
        raise RuntimeError('expired or superseded source acquisition')


def record_skipped(connect, role, pilot_id, source, url, slot, decision, state, error_class=None):
    # BUSY is deliberately not sealed: the active owner may finish this slot.
    if decision == 'BUSY':
        return {'source': source, 'slot_no': slot, 'status': 'CIRCUIT_BUSY', 'rows': 0,
                'source_resilience': public_state(state)}
    table = 'source_pilot_runs' if role == 'prediction' else 'result_source_pilot_runs'
    status = 'CIRCUIT_OPEN' if decision == 'SUSPENDED' else 'ERROR'
    run_id = f'{pilot_id}-{source}-s{slot:02d}-{uuid.uuid4().hex[:8]}'
    manifest = {'source_resilience': public_state(state), 'error_class': error_class,
                'rows_parsed': 0, 'page_capture': {'state': status, 'http_status': None},
                'normalized_observations': {'parsed': 0, 'matched': 0, 'persisted': 0}}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f'''INSERT INTO {table}(run_id,pilot_id,source,slot_no,started_at_utc,
                     finished_at_utc,status,final_url,manifest)
                     VALUES (%s,%s,%s,%s,clock_timestamp(),clock_timestamp(),%s,%s,%s::jsonb)
                     ON CONFLICT(pilot_id,source,slot_no) DO NOTHING''',
                     (run_id, pilot_id, source, slot, status, url, json.dumps(manifest)))
        conn.commit()
    return {'source': source, 'slot_no': slot, 'status': status, 'rows': 0,
            'normalized_observations': manifest['normalized_observations'],
            'source_resilience': public_state(state), 'error_class': error_class}


def run_source(connect, role, pilot_id, source, url, slot, fetch, stop):
    circuit = SourceCircuit(connect, source_key(role, pilot_id, source))
    claim = circuit.claim()
    if claim['decision'] != 'ADMITTED':
        return record_skipped(connect, role, pilot_id, source, url, slot, claim['decision'], claim['state'])
    try:
        pending = claim['state'].get('stop_pending_id')
        if pending:
            stop(pending)  # Do not allocate another browser until this stop is confirmed.
            circuit.clear_stopped_browser(claim)
        table = 'source_pilot_runs' if role == 'prediction' else 'result_source_pilot_runs'
        with connect() as conn, conn.cursor() as cur:
            cur.execute(f'SELECT run_id FROM {table} WHERE pilot_id=%s AND source=%s AND slot_no=%s',
                        (pilot_id, source, slot))
            existing = cur.fetchone()
        if existing:
            outcome = {'source': source, 'slot_no': slot, 'status': 'ALREADY_DONE', 'run_id': existing['run_id']}
        else:
            outcome = fetch(claim)
    except Exception as exc:
        session_id = getattr(exc, 'session_id', None)
        fault = {'reason': 'STOP_UNCONFIRMED' if session_id else 'COLLECT_ERROR',
                 'failed': True, 'immediate': True, 'stop_pending_id': session_id}
        state = circuit.finish(claim, fault)
        return record_skipped(connect, role, pilot_id, source, url, slot, 'ERROR', state, type(exc).__name__)
    state = circuit.finish(claim, capture_fault(outcome))
    outcome['source_resilience'] = state
    return outcome
