"""Out-of-band owner approval: Ed25519 over a canonical binding, single use, short TTL.

The owner's approval device holds the Ed25519 PRIVATE key; the signer holds only
the PUBLIC key. A chat message, a tool output or a web page cannot produce a
valid approval because none of them has the owner's private approval key: the
signer, not the model, verifies the signature (governance §2.6).

v2 (this version) binds the approval to the full human-meaningful order, not
only to the intent hash. The `binding` object (BINDING_KEYS) carries:
account (API identity), maker wallet, network / chainId / verifying contract /
base token, event id, market hash, selection (outcome 1|2 + human label),
side (isMakerBettingOutcomeOne), time in force, stake, odds limit
(percentageOdds), the quote the owner saw, the expected fees (GET /user/fees-v3
snapshot), the max exposure for this order, the order expiry, the client order
id (idempotency key) and the intent hash. Any change of any field changes the
signed bytes => APPROVAL_SIGNATURE_INVALID, and the signer independently
re-derives every field it can from the intent, its config and the risk context
(APPROVAL_BINDING_MISMATCH). v1 approvals (intent hash only) are no longer
accepted: this is an intentional, non-backward-compatible change.

Canonical message (sorted-key compact JSON, ASCII):
    {"approval_nonce", "binding": {...}, "domain": "sxbet.order_approval.v2",
     "expires_at", "issued_at", "signer_id"}

WebAuthn alternative (bottom of this module + signer/webauthn.py): the owner's passkey signs
SHA-256 of {domain, decision, binding (v2, unchanged), display (human view derived from the binding),
nonce, issued_at, expires_at, signer_id}; the display must equal build_display(binding, labels).

Verification here never consumes the nonce: consumption happens atomically in
the order ledger together with the AUTHORIZED record (signer/orders.py), so a
request refused by a later check does not burn the approval.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

APPROVAL_DOMAIN = "sxbet.order_approval.v2"
BINDING_SCHEMA = "sxbet.order_binding.v1"
MAX_TTL_SECONDS = 120
CLOCK_SKEW_SECONDS = 5
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_FEE = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,18})?$")
BINDING_KEYS = frozenset({
    "schema", "account_id", "maker", "network", "chain_id", "verifying_contract", "base_token", "event_id",
    "market_hash", "selection", "is_maker_betting_outcome_one", "time_in_force", "stake_base_units",
    "percentage_odds_limit", "quoted_percentage_odds", "expected_fees", "max_exposure_base_units",
    "order_expiry", "client_order_id", "intent_hash"})


class ApprovalError(ValueError):
    pass


@dataclass(frozen=True)
class Approval:
    binding: Mapping[str, Any]
    approval_nonce: str
    issued_at: int
    expires_at: int
    signer_id: str
    signature: bytes  # 64-byte Ed25519 signature over approval_message(...)

    @property
    def intent_hash(self) -> Any:
        return self.binding.get("intent_hash") if isinstance(self.binding, Mapping) else None

    def __repr__(self) -> str:
        nonce = self.approval_nonce[:6] if isinstance(self.approval_nonce, str) else "?"
        return (f"Approval(intent_hash={self.intent_hash!r}, nonce={nonce}..., "
                f"expires_at={self.expires_at}, signer_id={self.signer_id!r})")


def build_binding(intent: Mapping[str, Any], *, account_id: str, event_id: str, selection_label: str,
                  expected_fees: Mapping[str, Any], max_exposure_base_units: int,
                  quoted_percentage_odds: int, intent_hash: str) -> dict[str, Any]:
    """What the owner's approval device shows and signs. Pure; derives the order fields from the intent."""
    return {
        "schema": BINDING_SCHEMA,
        "account_id": account_id,
        "maker": intent["maker"],
        "network": intent["network"],
        "chain_id": intent["chain_id"],
        "verifying_contract": intent["verifying_contract"],
        "base_token": intent["base_token"],
        "event_id": event_id,
        "market_hash": intent["market_hash"],
        "selection": {"outcome": 1 if intent["is_maker_betting_outcome_one"] else 2, "label": selection_label},
        "is_maker_betting_outcome_one": intent["is_maker_betting_outcome_one"],
        "time_in_force": intent["time_in_force"],
        "stake_base_units": intent["total_bet_size"],
        "percentage_odds_limit": intent["percentage_odds"],
        "quoted_percentage_odds": str(quoted_percentage_odds),
        "expected_fees": dict(expected_fees),
        "max_exposure_base_units": str(max_exposure_base_units),
        "order_expiry": intent["expiry"],
        "client_order_id": intent["idempotency_key"],
        "intent_hash": intent_hash,
    }


def approval_message(*, binding: Mapping[str, Any], approval_nonce: str, issued_at: int, expires_at: int,
                     signer_id: str) -> bytes:
    return json.dumps({"domain": APPROVAL_DOMAIN, "binding": dict(binding), "approval_nonce": approval_nonce,
                       "issued_at": issued_at, "expires_at": expires_at, "signer_id": signer_id},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
        raise ApprovalError("OWNER_PUBLIC_KEY_INVALID")
    return Ed25519PublicKey.from_public_bytes(bytes(raw))


def fees_well_formed(fees: Any) -> bool:
    """GET /user/fees-v3 `data`: decimal-fraction strings or null (null stays null, never 0)."""
    if not isinstance(fees, Mapping) or not fees:
        return False
    return all(isinstance(k, str) and (v is None or (isinstance(v, str) and _FEE.fullmatch(v)))
               for k, v in fees.items())


def _binding_well_formed(b: Any) -> bool:
    if not isinstance(b, Mapping) or set(b) != BINDING_KEYS or b["schema"] != BINDING_SCHEMA:
        return False
    sel = b["selection"]
    return (all(isinstance(b[k], str) and b[k] for k in ("account_id", "event_id", "client_order_id"))
            and isinstance(b["intent_hash"], str) and _HASH.fullmatch(b["intent_hash"]) is not None
            and isinstance(sel, Mapping) and set(sel) == {"outcome", "label"} and sel["outcome"] in (1, 2)
            and type(sel["outcome"]) is int and isinstance(sel["label"], str) and bool(sel["label"])
            and isinstance(b["is_maker_betting_outcome_one"], bool)
            and (sel["outcome"] == 1) == b["is_maker_betting_outcome_one"]
            and all(isinstance(b[k], str) and _DECIMAL.fullmatch(b[k]) for k in
                    ("stake_base_units", "percentage_odds_limit", "quoted_percentage_odds", "max_exposure_base_units"))
            and type(b["order_expiry"]) is int and type(b["chain_id"]) is int
            and fees_well_formed(b["expected_fees"]))


def verify(approval: Any, public_key: Ed25519PublicKey, *, expected_intent_hash: str, signer_id: str,
           now: int) -> None:
    """Raise ApprovalError(code) unless the approval is authentic, bound and fresh. Never consumes the nonce."""
    if not isinstance(approval, Approval):
        raise ApprovalError("APPROVAL_MISSING")
    if not _binding_well_formed(approval.binding):
        raise ApprovalError("APPROVAL_MALFORMED")
    if not isinstance(approval.approval_nonce, str) or not _NONCE.fullmatch(approval.approval_nonce):
        raise ApprovalError("APPROVAL_MALFORMED")
    if type(approval.issued_at) is not int or type(approval.expires_at) is not int:
        raise ApprovalError("APPROVAL_MALFORMED")
    if not isinstance(approval.signature, (bytes, bytearray)) or len(approval.signature) != 64:
        raise ApprovalError("APPROVAL_MALFORMED")
    if approval.signer_id != signer_id:
        raise ApprovalError("APPROVAL_WRONG_SIGNER")
    if approval.intent_hash != expected_intent_hash:
        raise ApprovalError("APPROVAL_INTENT_MISMATCH")
    if approval.expires_at - approval.issued_at > MAX_TTL_SECONDS or approval.expires_at <= approval.issued_at:
        raise ApprovalError("APPROVAL_TTL_TOO_LONG")
    if approval.issued_at > now + CLOCK_SKEW_SECONDS:
        raise ApprovalError("APPROVAL_NOT_YET_VALID")
    if now >= approval.expires_at:
        raise ApprovalError("APPROVAL_EXPIRED")
    msg = approval_message(binding=approval.binding, approval_nonce=approval.approval_nonce,
                           issued_at=approval.issued_at, expires_at=approval.expires_at, signer_id=approval.signer_id)
    try:
        public_key.verify(bytes(approval.signature), msg)
    except InvalidSignature:
        raise ApprovalError("APPROVAL_SIGNATURE_INVALID") from None


def binding_mismatch(binding: Mapping[str, Any], intent: Mapping[str, Any]) -> str | None:
    """Name of the first binding field that disagrees with the (re-validated) intent, else None."""
    expected = {
        "maker": intent["maker"], "network": intent["network"], "chain_id": intent["chain_id"],
        "verifying_contract": intent["verifying_contract"], "base_token": intent["base_token"],
        "market_hash": intent["market_hash"],
        "is_maker_betting_outcome_one": intent["is_maker_betting_outcome_one"],
        "time_in_force": intent["time_in_force"], "stake_base_units": intent["total_bet_size"],
        "percentage_odds_limit": intent["percentage_odds"], "order_expiry": intent["expiry"],
        "client_order_id": intent["idempotency_key"],
    }
    for k, v in expected.items():
        if binding[k] != v:
            return k
    return None


# ---------------------------------------------------------------- WebAuthn (passkey) approvals
# Alternative to the Ed25519 path above, for an owner device that holds a WebAuthn passkey (P-256, usually
# hardware-backed on Android). The authenticator signs SHA-256 of a canonical envelope that contains the v2
# binding (unchanged) PLUS a human display derived from it, so what the owner reads is what gets signed.
WEBAUTHN_ORDER_DOMAIN = "sxbet.order_approval.webauthn.v1"
WEBAUTHN_CANCEL_DOMAIN = "sxbet.cancel_approval.webauthn.v1"
DECISION_APPROVE, DECISION_REJECT = "APPROVE", "REJECT"
DISPLAY_SCHEMA = "sxbet.order_display.v1"
DATA_SOURCES = frozenset({"SIMULATED", "EXCHANGE_READ"})
ODDS_ONE = 10 ** 20
_LABEL = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
# Control, format (bidi overrides, zero-width, BOM), surrogate, private-use, unassigned and line/paragraph
# separator characters could make a label render differently from what is signed: refused.
_LABEL_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _label_ok(v: Any) -> bool:
    import unicodedata
    return isinstance(v, str) and _LABEL.fullmatch(v) is not None and \
        not any(unicodedata.category(ch) in _LABEL_BAD_CATEGORIES for ch in v)
DISPLAY_KEYS = frozenset({
    "schema", "operation_id", "data_source", "network", "chain_id", "verifying_contract", "base_token", "maker",
    "account_id", "event_id", "event_label", "market_hash", "market_label", "selection", "time_in_force",
    "stake_base_units", "stake_usdc", "max_potential_loss_base_units", "max_potential_loss_usdc",
    "percentage_odds_limit", "decimal_odds_limit", "implied_probability_limit_pct", "quoted_percentage_odds",
    "decimal_odds_quoted", "implied_probability_quoted_pct", "slippage_limit_bps",
    "potential_payout_before_fees_base_units", "potential_payout_before_fees_usdc", "expected_fees",
    "max_exposure_base_units", "order_expiry", "order_expiry_utc", "client_order_id", "intent_hash",
    "derived_fields"})
DERIVED_DISPLAY_FIELDS = ("decimal_odds_limit", "implied_probability_limit_pct", "decimal_odds_quoted",
                          "implied_probability_quoted_pct", "slippage_limit_bps",
                          "potential_payout_before_fees_base_units", "potential_payout_before_fees_usdc",
                          "max_potential_loss_base_units", "stake_usdc", "max_potential_loss_usdc",
                          "order_expiry_utc")


def binding_well_formed(b: Any) -> bool:
    return _binding_well_formed(b)


def _fixed(value: int, decimals: int) -> str:
    return f"{value // 10 ** decimals}.{value % 10 ** decimals:0{decimals}d}"


def _decimal_odds(p: int) -> str:
    return _fixed(ODDS_ONE * 10 ** 4 // p, 4)       # 1 / implied probability, truncated to 4 decimals


def _implied_pct(p: int) -> str:
    return _fixed(p // 10 ** 14, 4)                  # p / 1e20 * 100, truncated to 4 decimals


def _utc(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_display(binding: Mapping[str, Any], *, operation_id: str, event_label: str, market_label: str,
                  data_source: str) -> dict[str, Any]:
    """Human-readable view of a v2 binding. Every number is DERIVED deterministically from the binding.

    SX Bet has no BUY / SELL: an order backs one outcome. is_maker_betting_outcome_one=True backs outcome one
    (the market's outcomeOneName), False backs outcome two; "laying" outcome one = backing outcome two. The
    stake is the most the order can lose; percentageOdds is the HIGHEST implied probability (worst price)
    the order can ever be matched at, so the slippage limit is enforced by the signed order itself.
    Fees are shown as the fees-v3 snapshot only: how they apply to a payout is DOCS_UNVERIFIED.
    """
    if not _binding_well_formed(binding):
        raise ApprovalError("APPROVAL_MALFORMED")
    for v in (operation_id, event_label, market_label):
        if not _label_ok(v):
            raise ApprovalError("APPROVAL_DISPLAY_LABEL_INVALID")
    if data_source not in DATA_SOURCES:
        raise ApprovalError("APPROVAL_DISPLAY_LABEL_INVALID")
    stake = int(binding["stake_base_units"])
    limit = int(binding["percentage_odds_limit"])
    quoted = int(binding["quoted_percentage_odds"])
    if not 0 < limit < ODDS_ONE or not 0 < quoted < ODDS_ONE:
        raise ApprovalError("APPROVAL_MALFORMED")
    outcome = binding["selection"]["outcome"]
    payout = stake * ODDS_ONE // limit
    return {
        "schema": DISPLAY_SCHEMA,
        "operation_id": operation_id,
        "data_source": data_source,
        "network": binding["network"], "chain_id": binding["chain_id"],
        "verifying_contract": binding["verifying_contract"], "base_token": binding["base_token"],
        "maker": binding["maker"], "account_id": binding["account_id"],
        "event_id": binding["event_id"], "event_label": event_label,
        "market_hash": binding["market_hash"], "market_label": market_label,
        "selection": {"outcome": outcome, "label": binding["selection"]["label"],
                      "semantics": "BACK_OUTCOME_ONE" if outcome == 1 else "BACK_OUTCOME_TWO",
                      "is_maker_betting_outcome_one": binding["is_maker_betting_outcome_one"]},
        "time_in_force": binding["time_in_force"],
        "stake_base_units": binding["stake_base_units"], "stake_usdc": _fixed(stake, 6),
        "max_potential_loss_base_units": str(stake), "max_potential_loss_usdc": _fixed(stake, 6),
        "percentage_odds_limit": binding["percentage_odds_limit"],
        "decimal_odds_limit": _decimal_odds(limit), "implied_probability_limit_pct": _implied_pct(limit),
        "quoted_percentage_odds": binding["quoted_percentage_odds"],
        "decimal_odds_quoted": _decimal_odds(quoted), "implied_probability_quoted_pct": _implied_pct(quoted),
        "slippage_limit_bps": (limit - quoted) * 10_000 // quoted,
        "potential_payout_before_fees_base_units": str(payout),
        "potential_payout_before_fees_usdc": _fixed(payout, 6),
        "expected_fees": dict(binding["expected_fees"]),
        "max_exposure_base_units": binding["max_exposure_base_units"],
        "order_expiry": binding["order_expiry"], "order_expiry_utc": _utc(binding["order_expiry"]),
        "client_order_id": binding["client_order_id"], "intent_hash": binding["intent_hash"],
        "derived_fields": list(DERIVED_DISPLAY_FIELDS),
    }


def display_mismatch(display: Any, binding: Mapping[str, Any]) -> str | None:
    """None when `display` is exactly build_display(binding, <its own labels>), else the first bad field."""
    if not isinstance(display, Mapping) or set(display) != DISPLAY_KEYS:
        return "DISPLAY"
    try:
        expected = build_display(binding, operation_id=display["operation_id"], event_label=display["event_label"],
                                 market_label=display["market_label"], data_source=display["data_source"])
    except ApprovalError:
        return "DISPLAY"
    for k in sorted(DISPLAY_KEYS):
        if display[k] != expected[k] or type(display[k]) is not type(expected[k]):
            return k
    return None


def webauthn_challenge(domain: str, *, decision: str, binding: Mapping[str, Any], display: Mapping[str, Any],
                       approval_nonce: str, issued_at: int, expires_at: int, signer_id: str) -> bytes:
    """32-byte WebAuthn challenge = SHA-256(canonical envelope). Any changed field => another challenge."""
    import hashlib
    if domain not in (WEBAUTHN_ORDER_DOMAIN, WEBAUTHN_CANCEL_DOMAIN) or \
            decision not in (DECISION_APPROVE, DECISION_REJECT):
        raise ApprovalError("APPROVAL_MALFORMED")
    msg = json.dumps({"domain": domain, "decision": decision, "binding": dict(binding), "display": dict(display),
                      "approval_nonce": approval_nonce, "issued_at": issued_at, "expires_at": expires_at,
                      "signer_id": signer_id},
                     sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(msg).digest()


@dataclass(frozen=True)
class WebAuthnApproval:
    binding: Mapping[str, Any]
    display: Mapping[str, Any]
    approval_nonce: str
    issued_at: int
    expires_at: int
    signer_id: str
    credential_id: bytes
    authenticator_data: bytes
    client_data_json: bytes
    signature: bytes  # DER ECDSA P-256 over authenticator_data || SHA-256(client_data_json)

    @property
    def intent_hash(self) -> Any:
        return self.binding.get("intent_hash") if isinstance(self.binding, Mapping) else None

    def __repr__(self) -> str:
        nonce = self.approval_nonce[:6] if isinstance(self.approval_nonce, str) else "?"
        return (f"WebAuthnApproval(intent_hash={self.intent_hash!r}, nonce={nonce}..., "
                f"expires_at={self.expires_at}, signer_id={self.signer_id!r})")


def check_webauthn_envelope(approval: Any, *, expected_intent_hash: str, signer_id: str, now: int) -> None:
    """Same envelope rules as the Ed25519 path (TTL <= 120 s, skew 5 s, signer id, intent hash) + display."""
    if not isinstance(approval, WebAuthnApproval):
        raise ApprovalError("APPROVAL_MISSING")
    if not _binding_well_formed(approval.binding):
        raise ApprovalError("APPROVAL_MALFORMED")
    if not isinstance(approval.approval_nonce, str) or not _NONCE.fullmatch(approval.approval_nonce):
        raise ApprovalError("APPROVAL_MALFORMED")
    if type(approval.issued_at) is not int or type(approval.expires_at) is not int:
        raise ApprovalError("APPROVAL_MALFORMED")
    for raw in (approval.credential_id, approval.authenticator_data, approval.client_data_json, approval.signature):
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise ApprovalError("APPROVAL_MALFORMED")
    field = display_mismatch(approval.display, approval.binding)
    if field is not None:
        raise ApprovalError("APPROVAL_DISPLAY_MISMATCH_" + field.upper())
    if approval.signer_id != signer_id:
        raise ApprovalError("APPROVAL_WRONG_SIGNER")
    if approval.intent_hash != expected_intent_hash:
        raise ApprovalError("APPROVAL_INTENT_MISMATCH")
    if approval.expires_at - approval.issued_at > MAX_TTL_SECONDS or approval.expires_at <= approval.issued_at:
        raise ApprovalError("APPROVAL_TTL_TOO_LONG")
    if approval.issued_at > now + CLOCK_SKEW_SECONDS:
        raise ApprovalError("APPROVAL_NOT_YET_VALID")
    if now >= approval.expires_at:
        raise ApprovalError("APPROVAL_EXPIRED")
