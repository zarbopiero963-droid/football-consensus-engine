"""Complete, prospective evaluation contract; historical reports cannot certify it."""
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import math
import random
from collections import defaultdict

from app.db import connect
from app.market_reference import utc
from analysis.scoring.metrics import ScorePolicy


@dataclass(frozen=True)
class Protocol:
    window_start: str
    window_end: str
    strategy: str
    min_fixtures: int
    min_days: int
    min_coverage: float
    min_log_loss_gain: float
    min_roi: float
    result_grace_days: int
    commission: float
    score_policy: ScorePolicy

    def __post_init__(self):
        start, end = utc(self.window_start), utc(self.window_end)
        if not start or not end or end <= start:
            raise ValueError('INVALID_EXPLICIT_EVALUATION_WINDOW')
        if self.strategy not in tuple('S'+str(i) for i in range(1, 10)):
            raise ValueError('INVALID_PREREGISTERED_STRATEGY')
        for value, lower in ((self.min_fixtures, 2), (self.min_days, 2), (self.result_grace_days, 0)):
            if type(value) is not int or not lower <= value <= 1000000:
                raise ValueError('INVALID_PROTOCOL_COUNT')
        if self.min_days > self.min_fixtures:
            raise ValueError('IMPOSSIBLE_MINIMUM_DAYS')
        for value in (self.min_coverage, self.min_log_loss_gain, self.min_roi, self.commission):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('INVALID_FINITE_PROTOCOL_THRESHOLD')
        if not 0 < self.min_coverage <= 1 or not 0 < self.min_log_loss_gain or not 0 < self.min_roi < 1:
            raise ValueError('POSITIVE_RELEVANT_ADVANTAGE_REQUIRED')
        if not 0 <= self.commission < 1 or not isinstance(self.score_policy, ScorePolicy):
            raise ValueError('INVALID_COST_OR_SCORE_POLICY')
        if self.score_policy.bootstrap_replicates < 1000 or self.score_policy.confidence_level < .95:
            raise ValueError('PROSPECTIVE_UNCERTAINTY_REQUIRES_1000_REPLICATES_AND_95_PERCENT')

    @classmethod
    def from_dict(cls, value):
        config = dict(value)
        scoring = dict(config.pop('score_policy'))
        scoring['bin_edges'] = tuple(scoring['bin_edges'])
        return cls(**config, score_policy=ScorePolicy(**scoring))

    def canonical(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'), allow_nan=False)

    def sha256(self):
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def final_cutoff(self):
        return utc(self.window_end) + timedelta(days=self.result_grace_days)


def validate_registration(row, protocol, known):
    if row is None:
        return 'NOT_PREREGISTERED'
    seal = utc(row.get('sealed_at_utc'))
    if (row.get('contract_version') != 'frozen-ticket-v1' or
            row.get('policy_json') != protocol.canonical() or row.get('policy_sha256') != protocol.sha256() or
            utc(row.get('window_start')) != utc(protocol.window_start) or
            utc(row.get('window_end')) != utc(protocol.window_end) or
            not seal or not seal < utc(protocol.window_start) or seal > known):
        return 'INVALID_PREREGISTRATION'
    return None


def register(protocol_id, protocol, *, connection_factory=connect):
    """Idempotent identical replay, server sealing, no retrospective registration."""
    if not isinstance(protocol_id, str) or not protocol_id.strip():
        raise ValueError('EXPLICIT_PROTOCOL_ID_REQUIRED')
    with connection_factory() as conn, conn.cursor() as cur:
        # Serialize same-id attempts before checking existing immutable evidence.
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (protocol_id,))
        cur.execute('SELECT * FROM evaluation_protocols WHERE protocol_id=%s', (protocol_id,))
        existing = cur.fetchone()
        if existing:
            if existing['policy_json'] != protocol.canonical():
                raise ValueError('PROTOCOL_ID_ALREADY_FROZEN_WITH_DIFFERENT_POLICY')
            return existing
        cur.execute('''INSERT INTO evaluation_protocols(protocol_id,window_start,window_end,policy_json)
            VALUES (%s,%s,%s,%s) RETURNING *''',
            (protocol_id, utc(protocol.window_start), utc(protocol.window_end), protocol.canonical()))
        row = cur.fetchone()
        conn.commit()
        return row


def day_interval(rows, policy):
    """Paired Rome-day block bootstrap, fixture-weighted mean in each replicate."""
    blocks = defaultdict(list)
    for day, value in rows:
        if not math.isfinite(value):
            raise ValueError('NONFINITE_BOOTSTRAP_VALUE')
        blocks[day].append(value)
    if len(blocks) < 2:
        return None
    summaries = [(sum(blocks[d]), len(blocks[d])) for d in sorted(blocks)]
    rng = random.Random(policy.bootstrap_seed)
    means = []
    for _ in range(policy.bootstrap_replicates):
        chosen = [rng.choice(summaries) for _ in summaries]
        means.append(sum(total for total, _ in chosen)/sum(n for _, n in chosen))
    means.sort()
    def quantile(q):
        pos = (len(means)-1)*q
        a, b = int(pos), math.ceil(pos)
        return means[a]+(means[b]-means[a])*(pos-a)
    tail = (1-policy.confidence_level)/2
    return {'low': quantile(tail), 'high': quantile(1-tail),
            'confidence_level': policy.confidence_level, 'replicates': policy.bootstrap_replicates,
            'seed': policy.bootstrap_seed, 'unit': 'EUROPE_ROME_KICKOFF_DAY',
            'method': 'DAY_BLOCK_PERCENTILE',
            'limitation': 'INTRA_DAY_DEPENDENCE_ONLY; INTER_DAY_DEPENDENCE_AND_SELECTION_BIAS_NOT_ELIMINATED'}


def verdict(protocol, registration, *, known_at, denominator, rows, kind):
    """Fixed terminal test. No extension, threshold changes or interim success."""
    if kind not in ('quality', 'roi'):
        raise ValueError('INVALID_VERDICT_KIND')
    known = utc(known_at)
    if known is None:
        raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    reasons = []
    failure = validate_registration(registration, protocol, known)
    if failure: reasons.append(failure)
    if known < protocol.final_cutoff(): reasons.append('WINDOW_OR_RESULT_GRACE_NOT_COMPLETE')
    if len(rows) < protocol.min_fixtures: reasons.append('MINIMUM_FIXTURES_NOT_REACHED')
    if len({day for day, _ in rows}) < protocol.min_days: reasons.append('MINIMUM_DAYS_NOT_REACHED')
    coverage = len(rows)/denominator if denominator else 0
    if coverage < protocol.min_coverage: reasons.append('MINIMUM_COVERAGE_NOT_REACHED')
    interval = day_interval(rows, protocol.score_policy)
    mean = sum(v for _, v in rows)/len(rows) if rows else None
    status = 'DATI_INSUFFICIENTI'
    if not reasons and interval:
        if kind == 'quality':
            status = ('VANTAGGIO_DIMOSTRATO_NEL_CAMPIONE' if interval['high'] <= -protocol.min_log_loss_gain
                      else 'PEGGIORE_DEL_BENCHMARK' if interval['low'] > 0 else 'VANTAGGIO_NON_DIMOSTRATO')
        else:
            status = ('VANTAGGIO_DIMOSTRATO_NEL_CAMPIONE' if interval['low'] >= protocol.min_roi
                      else 'RENDIMENTO_NEGATIVO' if interval['high'] < 0 else 'VANTAGGIO_NON_DIMOSTRATO')
    return {'status': status, 'reasons': reasons, 'n': len(rows), 'coverage': coverage,
            'distinct_days': len({day for day, _ in rows}), 'mean': mean, 'ci': interval,
            'action': 'CONTINUE_COLLECTION_WITHOUT_CLAIM' if reasons else
                      'REVIEW_EVIDENCE_NO_AUTOMATIC_PROMOTION' if status == 'VANTAGGIO_DIMOSTRATO_NEL_CAMPIONE'
                      else 'STOP_ADVANTAGE_CLAIM_NO_RETUNING_OR_WINDOW_EXTENSION'}
