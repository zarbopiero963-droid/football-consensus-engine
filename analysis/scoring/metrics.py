"""Explicit numerical policy and pure scores on complete unit-scale simplexes."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random


@dataclass(frozen=True)
class ScorePolicy:
    epsilon: float
    bin_edges: tuple[float, ...]
    min_bin_count: int
    bootstrap_replicates: int
    bootstrap_seed: int
    confidence_level: float

    def __post_init__(self):
        if isinstance(self.epsilon, bool) or not 0 < self.epsilon < .5:
            raise ValueError('INVALID_SCORE_EPSILON')
        edges = self.bin_edges
        if (len(edges) < 2 or edges[0] != 0 or edges[-1] != 1 or
                any(isinstance(e, bool) or not math.isfinite(e) for e in edges) or
                any(a >= b for a, b in zip(edges, edges[1:]))):
            raise ValueError('INVALID_FIXED_BIN_EDGES')
        for value, upper in ((self.min_bin_count, 1000000), (self.bootstrap_replicates, 10000)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError('INVALID_DIAGNOSTIC_COUNT')
        if type(self.bootstrap_seed) is not int:
            raise ValueError('EXPLICIT_BOOTSTRAP_SEED_REQUIRED')
        if isinstance(self.confidence_level, bool) or not 0 < self.confidence_level < 1:
            raise ValueError('INVALID_CONFIDENCE_LEVEL')

    def audit(self):
        config = asdict(self)
        return {**config, 'sha256': hashlib.sha256(json.dumps(config, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest(),
                'scope': 'EXPLICIT_DIAGNOSTIC_POLICY_NOT_PRODUCTION_PREREGISTRATION',
                'log_clip': 'COMPONENT_FLOOR_CEILING_THEN_RENORMALIZE',
                'brier_rps_probabilities': 'RAW_UNIT_SCALE',
                'bootstrap_unit': 'PAIRED_FIXTURE_IID_DIAGNOSTIC'}


def simplex(probabilities):
    p = tuple(probabilities)
    if len(p) < 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or
            not math.isfinite(x) or not 0 <= x <= 1 for x in p):
        raise ValueError('INVALID_UNIT_SCALE_VECTOR')
    if not math.isclose(sum(p), 1, rel_tol=0, abs_tol=1e-9):
        raise ValueError('INCOMPLETE_OR_NON_SIMPLEX_VECTOR')
    return p


def multiclass_scores(probabilities, outcome_index, policy):
    p = simplex(probabilities)
    if type(outcome_index) is not int or not 0 <= outcome_index < len(p):
        raise ValueError('INVALID_OUTCOME_INDEX')
    clipped = tuple(max(policy.epsilon, min(1-policy.epsilon, x)) for x in p)
    scored = tuple(x / sum(clipped) for x in clipped)
    components = [(x - int(i == outcome_index))**2 for i, x in enumerate(p)]
    return {'p_raw': list(p), 'p_scored': list(scored),
            'score_clip_epsilon': policy.epsilon, 'extreme_probability': any(x in (0, 1) for x in p),
            'outcome_index': outcome_index, 'log_loss': -math.log(scored[outcome_index]),
            'brier': sum(components), 'brier_components': components}


def scores_1x2(probabilities, outcome, policy, *, order=('1', 'X', '2')):
    if tuple(order) != ('1', 'X', '2') or outcome not in order:
        raise ValueError('EXPLICIT_1X2_ORDER_REQUIRED')
    if len(probabilities) != 3:
        raise ValueError('INVALID_1X2_VECTOR')
    result = multiclass_scores(probabilities, order.index(outcome), policy)
    p = result['p_raw']; index = result['outcome_index']
    result['rps'] = sum((sum(p[:j+1]) - int(index <= j))**2 for j in range(2)) / 2
    result['rps_order'] = list(order)
    result['rps_definition'] = 'CUMULATIVE_SQUARED_ERROR_DIVIDED_BY_K_MINUS_1'
    return result


def binary_scores(probability, outcome, policy):
    if type(outcome) is not int or outcome not in (0, 1):
        raise ValueError('BINARY_OUTCOME_REQUIRED')
    # Binary Brier is one squared error, not the two-class multiclass sum.
    result = multiclass_scores((probability, 1-probability), 0 if outcome else 1, policy)
    result['brier'] = (probability-outcome)**2
    result['brier_definition'] = 'BINARY_SINGLE_COMPONENT_SQUARED_ERROR'
    return result


def calibration(probabilities, outcomes, policy):
    if len(probabilities) != len(outcomes):
        raise ValueError('CALIBRATION_LENGTH_MISMATCH')
    for p, y in zip(probabilities, outcomes):
        simplex((p, 1-p))
        if type(y) is not int or y not in (0, 1):
            raise ValueError('BINARY_OUTCOME_REQUIRED')
    n = len(outcomes)
    if not n:
        return {'n': 0, 'ece': None, 'bins': [], 'murphy': None, 'status': 'INSUFFICIENT_SAMPLE'}
    bins = [[] for _ in policy.bin_edges[:-1]]
    for p, y in zip(probabilities, outcomes):
        i = next(i for i, (a, b) in enumerate(zip(policy.bin_edges, policy.bin_edges[1:]))
                 if a <= p < b or (p == 1 and b == 1))
        bins[i].append((p, y))
    base = sum(outcomes)/n; ece = reliability = resolution = 0.; records = []
    for a, b, rows in zip(policy.bin_edges, policy.bin_edges[1:], bins):
        count = len(rows)
        avg = sum(p for p, _ in rows)/count if count else None
        frequency = sum(y for _, y in rows)/count if count else None
        records.append({'left': a, 'right': b, 'right_closed': b == 1, 'n': count,
                        'mean_probability': avg, 'outcome_frequency': frequency,
                        'sufficient_count': count >= policy.min_bin_count})
        if count:
            weight = count/n
            ece += weight*abs(avg-frequency)
            reliability += weight*(avg-frequency)**2
            resolution += weight*(frequency-base)**2
    sufficient = all(len(rows) >= policy.min_bin_count for rows in bins if rows)
    uncertainty = base*(1-base)
    brier = sum((p-y)**2 for p,y in zip(probabilities,outcomes))/n
    murphy = {'kind': 'MURPHY_ESTIMATE', 'reliability': reliability, 'resolution': resolution,
              'uncertainty': uncertainty, 'brier_estimate': reliability-resolution+uncertainty,
              'binning_residual': brier-(reliability-resolution+uncertainty)} if sufficient else None
    return {'n': n, 'ece': ece, 'bins': records, 'murphy': murphy,
            'in_sample_base_rate_reference': base,
            'base_rate_role': 'IN_SAMPLE_BASE_RATE_REFERENCE_NOT_PREDICTIVE_BASELINE',
            'status': 'DIAGNOSTIC_ESTIMATE' if sufficient else 'INSUFFICIENT_FOR_CALIBRATION_DECOMP'}


def bootstrap_interval(values, policy):
    if any(not math.isfinite(v) for v in values):
        raise ValueError('NONFINITE_BOOTSTRAP_VALUE')
    if len(values) < 2:
        return None
    rng = random.Random(policy.bootstrap_seed)
    means = sorted(sum(rng.choice(values) for _ in values)/len(values)
                   for _ in range(policy.bootstrap_replicates))
    def quantile(q):
        index = (len(means)-1)*q; left = int(index); right = math.ceil(index)
        return means[left] + (means[right]-means[left])*(index-left)
    tail = (1-policy.confidence_level)/2
    return {'low': quantile(tail), 'high': quantile(1-tail),
            'confidence_level': policy.confidence_level, 'method': 'FIXTURE_IID_PERCENTILE_DIAGNOSTIC',
            'dependence_assumption': 'DOES_NOT_ACCOUNT_FOR_LEAGUE_OR_TEMPORAL_CLUSTERING'}
