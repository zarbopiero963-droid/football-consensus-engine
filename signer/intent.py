"""Independent re-validation of a canonical order intent.

Deliberately does not import `bridge`: the signer is deployable on its own and
must not trust the caller's validation. The canonical form and hash are the
same as `bridge/sxbet/simulation.py` (a test asserts both hashes agree).
Contract: https://docs.sx.bet/developers/posting-orders,
https://docs.sx.bet/developers/odds-rounding.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

INTENT_SCHEMA = "sxbet.order_intent.v1"
ODDS_PRECISION = 10 ** 20
ODDS_EXCEPTIONS = frozenset({10 ** 17, 999 * 10 ** 17})
TIME_IN_FORCE = ("GTC", "IOC", "FOK")
CHAIN_IDS = {"mainnet": 4162, "testnet": 79479957}
INTENT_KEYS = frozenset({
    "schema", "network", "chain_id", "domain_name", "domain_version", "verifying_contract", "base_token",
    "market_hash", "maker", "is_maker_betting_outcome_one", "total_bet_size", "percentage_odds",
    "requested_percentage_odds", "salt", "expiry", "time_in_force", "idempotency_key"})

_HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class IntentInvalid(ValueError):
    pass


def canonical_json(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def intent_hash(intent: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(intent)).hexdigest()


def _uint(intent, key, code):
    v = intent[key]
    if not isinstance(v, str) or not _DECIMAL.fullmatch(v) or int(v) >= 2 ** 256:
        raise IntentInvalid(code)
    return int(v)


def validate(intent: Any, *, odds_ladder_step_size: int) -> dict[str, Any]:
    """Return a copy of a fully signable intent, or raise IntentInvalid(code)."""
    if not isinstance(intent, Mapping) or set(intent) != INTENT_KEYS:
        raise IntentInvalid("INTENT_KEYS_MISMATCH")
    i = dict(intent)
    if i["schema"] != INTENT_SCHEMA:
        raise IntentInvalid("INTENT_SCHEMA_UNSUPPORTED")
    if type(i["chain_id"]) is not int or CHAIN_IDS.get(i["network"]) != i["chain_id"]:
        raise IntentInvalid("INTENT_CHAIN_MISMATCH")
    if (i["domain_name"], i["domain_version"]) != ("OBv3 Escrow", "1"):
        raise IntentInvalid("INTENT_DOMAIN_UNEXPECTED")
    for k in ("verifying_contract", "base_token", "maker"):
        if not isinstance(i[k], str) or not _ADDRESS.fullmatch(i[k]):
            raise IntentInvalid("INTENT_ADDRESS_INVALID")
    if not isinstance(i["market_hash"], str) or not _HEX32.fullmatch(i["market_hash"]):
        raise IntentInvalid("INTENT_MARKET_HASH_INVALID")
    if not isinstance(i["is_maker_betting_outcome_one"], bool):
        raise IntentInvalid("INTENT_SIDE_INVALID")
    if _uint(i, "total_bet_size", "INTENT_STAKE_INVALID") <= 0:
        raise IntentInvalid("INTENT_STAKE_INVALID")
    odds = _uint(i, "percentage_odds", "INTENT_ODDS_INVALID")
    requested = _uint(i, "requested_percentage_odds", "INTENT_ODDS_INVALID")
    _uint(i, "salt", "INTENT_SALT_INVALID")
    step = odds_ladder_step_size * 10 ** 15
    if not 0 < odds < ODDS_PRECISION:
        raise IntentInvalid("INTENT_ODDS_OUT_OF_BOUNDS")
    if odds not in ODDS_EXCEPTIONS and odds % step:
        raise IntentInvalid("INTENT_ODDS_NOT_ON_LADDER")
    expected = requested if requested in ODDS_EXCEPTIONS else (requested // step) * step
    if expected != odds:
        raise IntentInvalid("INTENT_ODDS_NOT_ROUND_DOWN_OF_REQUESTED")
    if type(i["expiry"]) is not int or i["expiry"] <= 0:
        raise IntentInvalid("INTENT_EXPIRY_INVALID")
    if i["time_in_force"] not in TIME_IN_FORCE:
        raise IntentInvalid("INTENT_TIME_IN_FORCE_INVALID")
    if not isinstance(i["idempotency_key"], str) or not _IDEMPOTENCY.fullmatch(i["idempotency_key"]):
        raise IntentInvalid("INTENT_IDEMPOTENCY_KEY_INVALID")
    return i
