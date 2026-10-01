"""Stable identities for one user-facing decision, including concurrent retries."""
import hashlib
import json
import re

KEY_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._:-]*$'


def validate_key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or not re.fullmatch(KEY_PATTERN, key):
        raise ValueError('Idempotency-Key must contain 1-128 ASCII letters, digits, dots, underscores, colons or hyphens')
    return key


def request_fingerprint(day, request):
    payload = {'date': day.isoformat(), **request.model_dump()}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def request_lock_id(key):
    # A collision merely serializes unrelated requests; the full key is the PK.
    digest = hashlib.sha256(('decision-request:' + key).encode()).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)
