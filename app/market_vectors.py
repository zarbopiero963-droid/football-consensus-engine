"""Explicit adapter vector contract. This module does not cast votes or create tickets."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re

from app.market_reference import utc

VERSION = 'market-vector-v1'
TOLERANCE = .01
SCORE_PATTERN = r'(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)'


def _audit_value(value):
    # JSON cannot encode nonfinite numbers; retain their literal diagnostic
    # spelling instead of silently changing them to null or zero.
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _audit_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_value(v) for v in value]
    return deepcopy(value)


def _line(value):
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        line = Decimal(str(value))
        if not line.is_finite() or line <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ValueError('INVALID_LINE') from None
    return line


def _identity(market_id, period, line):
    if not isinstance(market_id, str):
        raise ValueError('UNSUPPORTED_MARKET')
    expected_period = market_id.split('_', 1)[0]
    if expected_period not in ('FT', 'HT', 'HTFT') or period != expected_period:
        raise ValueError('PERIOD_MISMATCH')
    match = re.fullmatch(r'(FT|HT)_(CORNERS_)?OU(?:_([0-9]+(?:\.[0-9]+)?))?', market_id)
    if match:
        if match[3] is None and line is None:
            raise ValueError('LINE_MISSING')
        explicit = _line(match[3]) if match[3] is not None else _line(line)
        if line is not None and _line(line) != explicit:
            raise ValueError('LINE_MISMATCH')
        text = format(explicit.normalize(), 'f')
        canonical = f'{period}_{"CORNERS_" if match[2] else ""}OU_{text}'
        return canonical, ('OVER', 'UNDER'), 'SIMPLEX', text
    if line is not None:
        raise ValueError('UNEXPECTED_LINE')
    if market_id in ('FT_1X2', 'HT_1X2'):
        return market_id, ('1', 'X', '2'), 'SIMPLEX', None
    if market_id in ('FT_BTTS', 'HT_BTTS'):
        return market_id, ('GG', 'NG'), 'SIMPLEX', None
    if market_id == 'FT_DOUBLE_CHANCE':
        return market_id, ('1X', 'X2', '12'), 'OVERLAPPING_SELECTIONS', None
    if market_id in ('FT_CORRECT_SCORE', 'HT_CORRECT_SCORE'):
        return market_id, (), 'DECLARED_DISTRIBUTION', None
    if market_id == 'HTFT_1X2':
        return market_id, tuple(f'{h}/{f}' for h in ('1', 'X', '2') for f in ('1', 'X', '2')), 'SIMPLEX', None
    raise ValueError('UNSUPPORTED_MARKET')


def _probability(value, scale):
    if value is None or value == '':
        return None
    if scale not in (1, 100) or isinstance(scale, bool):
        raise ValueError('SCALE_UNRESOLVED')
    try:
        if isinstance(value, bool):
            raise ValueError
        p = float(value) / scale
    except (ValueError, TypeError, OverflowError):
        raise ValueError('INVALID_PROBABILITY') from None
    if not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError('INVALID_PROBABILITY')
    return p


def make_vector(market_id, *, period, components=None, raw_scale=None,
                source_timestamp=None, source='', derivation='SOURCE', parse_status='OK',
                pick=None, pick_probability=None, outcome_space=None, distribution_complete=False,
                line=None, lifecycle='PILOT', matching_status='MATCH_UNRESOLVED',
                kickoff_timezone_status='TIMEZONE_UNRESOLVED', origin=None):
    """FULL means native data completeness, never verified time/matching/eligibility."""
    raw = deepcopy({} if components is None else components)
    stamp = utc(source_timestamp)
    out = {'contract_version': VERSION, 'market_id': market_id, 'period': period,
           'line': None, 'raw_line': _audit_value(line), 'source': source, 'vector_status': 'BROKEN',
           'components': [], 'normalized_vector': {}, 'raw_components': _audit_value(raw),
           'raw_scale': _audit_value(raw_scale), 'derivation': derivation, 'parse_status': parse_status,
           'source_timestamp_raw': str(source_timestamp) if source_timestamp is not None else None,
           'source_timestamp_utc': stamp.isoformat() if stamp else None,
           'lifecycle': lifecycle, 'matching_status': matching_status,
           'kickoff_timezone_status': kickoff_timezone_status,
           'production_eligible': False, 'adds_source_vote': False,
           'origin': _audit_value(origin), 'errors': [], 'audit_errors': []}
    if stamp is None:
        out['audit_errors'].append('SOURCE_TIME_UNRESOLVED')
    if not source:
        out['audit_errors'].append('SOURCE_MISSING')
    try:
        canonical, labels, semantics, canonical_line = _identity(market_id, period, line)
        out.update(market_id=canonical, components=list(labels), vector_semantics=semantics, line=canonical_line)
        if parse_status != 'OK' and not (parse_status == 'PARTIAL_PICK' and pick is not None):
            raise ValueError('PARSE_NOT_OK')
        if derivation not in ('SOURCE', 'DERIVED_FROM_1X2'):
            raise ValueError('DERIVATION_UNRESOLVED')
        if derivation == 'DERIVED_FROM_1X2' and (canonical != 'FT_DOUBLE_CHANCE'
                or not isinstance(origin, dict) or origin.get('market_id') != 'FT_1X2'
                or origin.get('source') != source):
            raise ValueError('DERIVATION_ORIGIN_MISSING')
        if not isinstance(raw, dict):
            raise ValueError('INVALID_COMPONENTS')
        if semantics == 'DECLARED_DISTRIBUTION':
            if distribution_complete and outcome_space is None:
                raise ValueError('OUTCOME_SPACE_REQUIRED')
            if outcome_space is not None:
                if not isinstance(outcome_space, (list, tuple)) or not all(isinstance(k, str) for k in outcome_space):
                    raise ValueError('INVALID_OUTCOME_SPACE')
                labels = tuple(outcome_space)
                if not labels or len(set(labels)) != len(labels) or not all(
                    isinstance(k, str) and (re.fullmatch(SCORE_PATTERN, k) or k in ('OTHER_HOME', 'OTHER_DRAW', 'OTHER_AWAY'))
                    for k in labels):
                    raise ValueError('INVALID_OUTCOME_SPACE')
                out['components'] = list(labels)
            elif raw:
                # Observed scores alone do not assert a complete outcome universe.
                labels = tuple(raw)
                if not all(isinstance(k, str) and re.fullmatch(SCORE_PATTERN, k) for k in labels):
                    raise ValueError('NONCANONICAL_SCORE')
                out['components'] = list(labels)
        if set(raw) - set(labels):
            raise ValueError('UNKNOWN_COMPONENT')
        normalized = {k: _probability(raw.get(k), raw_scale) for k in labels}
        out['normalized_vector'] = normalized
        if pick is not None:
            if raw:
                raise ValueError('PICK_VECTOR_AMBIGUITY')
            if semantics == 'DECLARED_DISTRIBUTION':
                if not isinstance(pick, str) or not re.fullmatch(SCORE_PATTERN, pick):
                    raise ValueError('NONCANONICAL_SCORE')
            elif pick not in labels:
                raise ValueError('INVALID_PICK')
            out.update(vector_status='PICK_ONLY', pick=pick,
                       pick_probability=_probability(pick_probability, raw_scale),
                       raw_pick_probability=_audit_value(pick_probability))
            return out
        if pick_probability is not None:
            raise ValueError('PICK_MISSING')
        present = [v for v in normalized.values() if v is not None]
        if not present:
            out['vector_status'] = 'ABSENT'
            return out
        total = math.fsum(present)
        expected_sum = 2 if semantics == 'OVERLAPPING_SELECTIONS' else 1
        if total > expected_sum + TOLERANCE + 1e-12:
            raise ValueError('INVALID_SUM')
        if len(present) < len(labels) or (semantics == 'DECLARED_DISTRIBUTION' and not distribution_complete):
            out['vector_status'] = 'PARTIAL'
            return out
        if abs(total - expected_sum) > TOLERANCE + 1e-12:
            raise ValueError('INVALID_SUM')
        if semantics != 'OVERLAPPING_SELECTIONS':
            out['normalized_vector'] = {k: v / total for k, v in normalized.items()}
        out['vector_status'] = 'FULL'
    except ValueError as exc:
        out['errors'].append(str(exc))
        out['vector_status'] = 'BROKEN'
    return out


def derive_double_chance(parent):
    """Explicit allowed derived product, retaining the same source and parent."""
    if parent.get('market_id') != 'FT_1X2' or parent.get('vector_status') != 'FULL':
        raise ValueError('FULL_FT_1X2_REQUIRED')
    p = parent['normalized_vector']
    return make_vector('FT_DOUBLE_CHANCE', period='FT', source=parent['source'], raw_scale=1,
                       components={'1X': p['1'] + p['X'], 'X2': p['X'] + p['2'], '12': p['1'] + p['2']},
                       source_timestamp=parent['source_timestamp_utc'], derivation='DERIVED_FROM_1X2',
                       lifecycle=parent['lifecycle'], matching_status=parent['matching_status'],
                       kickoff_timezone_status=parent['kickoff_timezone_status'],
                       origin={'market_id': 'FT_1X2', 'source': parent['source'],
                               'raw_scale': parent['raw_scale'],
                               'raw_components': deepcopy(parent['raw_components'])})
