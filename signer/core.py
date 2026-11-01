"""The isolated signer. Signs an SX Bet EIP-712 Order only for an owner-approved intent.

Guarantees (governance §2.6):
- the private key is read once, by this process, from its own env var
  SXBET_SIGNER_PRIVATE_KEY; it is never returned, logged, audited or repr'd;
- a signature is produced only when (a) the intent re-validates and its
  recomputed hash equals the approval's intent_hash, and (b) the approval is a
  valid Ed25519 signature by the owner's approval key, unused (nonce), fresh
  (TTL <= 120 s) and addressed to this signer_id;
- owner-set exposure limits, default ZERO => every request is refused;
- one idempotency key = one intent: same key + same hash returns the cached
  result (no second signature), same key + different hash is refused;
- every decision is appended to the audit log;
- there is no network or submission code here: the caller gets the signature
  back and nothing is sent. LIVE_TRADING remains UNAVAILABLE.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from eth_account import Account

from . import approval as appr
from . import eip712
from .audit import AuditLog
from .intent import IntentInvalid, intent_hash, validate

KEY_ENV = "SXBET_SIGNER_PRIVATE_KEY"
MIN_EXPIRY_MARGIN_SECONDS = 12  # docs: expiry inside betting delay + 2 s is rejected; local margin on top
_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class SignerRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SignerConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignerConfig:
    """Owner-set. Exposure limits default to zero: nothing can be signed until the owner sets them."""
    signer_id: str
    network: str
    chain_id: int
    verifying_contract: str  # lowercase hex
    base_token: str          # lowercase hex
    odds_ladder_step_size: int
    max_stake_base_units: int = 0
    max_exposure_base_units: int = 0


class Signer:
    def __init__(self, config: SignerConfig, *, owner_public_key: bytes, nonce_store: appr.NonceStore,
                 audit: AuditLog, env: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time):
        if not _ADDRESS.fullmatch(config.verifying_contract) or not _ADDRESS.fullmatch(config.base_token):
            raise SignerConfigError("SIGNER_CONFIG_ADDRESS_INVALID")
        if not isinstance(config.signer_id, str) or not 1 <= len(config.signer_id) <= 64:
            raise SignerConfigError("SIGNER_CONFIG_ID_INVALID")
        raw = (os.environ if env is None else env).get(KEY_ENV, "").strip()
        if not _KEY.fullmatch(raw):
            raise SignerConfigError("SIGNER_KEY_MISSING_OR_MALFORMED")
        try:
            self.__account = Account.from_key(raw)
        except Exception:  # never let a library message carry key material
            raise SignerConfigError("SIGNER_KEY_INVALID") from None
        del raw
        try:
            self._owner_key = appr.load_public_key(owner_public_key)
        except Exception:
            raise SignerConfigError("OWNER_PUBLIC_KEY_INVALID") from None
        self.config = config
        self.address = self.__account.address
        self._nonces = nonce_store
        self._audit = audit
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, dict[str, Any]]] = {}
        # Rebuilt from the audit after a restart: keys already signed, and committed exposure.
        self._signed_before: dict[str, str] = {}
        self._exposure = 0
        for rec in audit.records():
            if rec.get("event") == "SIGN" and rec.get("decision") == "SIGNED" and rec.get("signer_id") == config.signer_id:
                self._signed_before[rec["idempotency_key"]] = rec["intent_hash"]
                self._exposure += int(rec["total_bet_size"])

    # ---- never leak the key ----
    def __repr__(self) -> str:
        return f"Signer(signer_id={self.config.signer_id!r}, address={self.address}, key=<hidden>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Signer cannot be serialised")

    def __getstate__(self):
        raise TypeError("Signer cannot be serialised")

    @property
    def committed_exposure_base_units(self) -> int:
        return self._exposure

    # ---- the only signing entry point ----
    def sign_order(self, intent: Mapping[str, Any], approval: appr.Approval) -> dict[str, Any]:
        with self._lock:
            key = intent.get("idempotency_key") if isinstance(intent, Mapping) else None
            ihash = None
            try:
                try:
                    clean = validate(intent, odds_ladder_step_size=self.config.odds_ladder_step_size)
                except IntentInvalid as exc:
                    raise SignerRefused(str(exc)) from None
                ihash = intent_hash(clean)
                key = clean["idempotency_key"]
                cached = self._cache.get(key)
                if cached is not None:
                    if cached[0] != ihash:
                        raise SignerRefused("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT")
                    self._log("DUPLICATE_RETURNED_CACHED", None, key, ihash, cached[1]["order_id"], clean)
                    return {**cached[1], "cached": True}
                if key in self._signed_before:
                    raise SignerRefused("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"
                                        if self._signed_before[key] != ihash
                                        else "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST")
                self._check_config(clean)
                now = int(self._clock())
                if clean["expiry"] < now + MIN_EXPIRY_MARGIN_SECONDS:
                    raise SignerRefused("INTENT_EXPIRED_OR_TOO_SOON")
                try:
                    appr.verify(approval, self._owner_key, expected_intent_hash=ihash,
                                signer_id=self.config.signer_id, now=now)
                except appr.ApprovalError as exc:
                    raise SignerRefused(str(exc)) from None
                if not self._nonces.consume(approval.approval_nonce):
                    raise SignerRefused("APPROVAL_NONCE_REPLAYED")
                self._check_exposure(int(clean["total_bet_size"]))
                msg = eip712.signable(clean)
                oid = eip712.order_id(msg)
                signature = self.__account.sign_message(msg).signature.to_0x_hex()
                result = {"order_id": oid, "intent_hash": ihash, "signer_address": self.address,
                          "order": eip712.order_payload(clean, signature), "submitted": False,
                          "live_trading": "UNAVAILABLE"}
                self._cache[key] = (ihash, result)
                self._exposure += int(clean["total_bet_size"])
                self._log("SIGNED", None, key, ihash, oid, clean, nonce=approval.approval_nonce)
                return {**result, "cached": False}
            except SignerRefused as exc:
                self._log("REFUSED", exc.code, key[:64] if isinstance(key, str) else None, ihash, None, None,
                          nonce=getattr(approval, "approval_nonce", None))
                raise

    def _check_config(self, i: Mapping[str, Any]) -> None:
        c = self.config
        if (i["network"], i["chain_id"]) != (c.network, c.chain_id):
            raise SignerRefused("INTENT_NETWORK_NOT_CONFIGURED")
        if i["verifying_contract"] != c.verifying_contract or i["base_token"] != c.base_token:
            raise SignerRefused("INTENT_DOMAIN_NOT_CONFIGURED")
        if i["maker"] != self.address.lower():
            raise SignerRefused("INTENT_MAKER_IS_NOT_SIGNER")

    def _check_exposure(self, stake: int) -> None:
        c = self.config
        if c.max_stake_base_units <= 0 or c.max_exposure_base_units <= 0:
            raise SignerRefused("EXPOSURE_LIMIT_ZERO")
        if stake > c.max_stake_base_units:
            raise SignerRefused("STAKE_ABOVE_LIMIT")
        if self._exposure + stake > c.max_exposure_base_units:
            raise SignerRefused("EXPOSURE_ABOVE_LIMIT")

    def _log(self, decision, reason, key, ihash, oid, clean, *, nonce=None) -> None:
        self._audit.append(event="SIGN", decision=decision, reason=reason, signer_id=self.config.signer_id,
                           idempotency_key=key, intent_hash=ihash, order_id=oid,
                           total_bet_size=None if clean is None else clean["total_bet_size"],
                           network=self.config.network,
                           approval_nonce_prefix=nonce[:6] if isinstance(nonce, str) else None)
