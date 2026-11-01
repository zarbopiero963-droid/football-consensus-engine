"""The isolated signer. Signs an SX Bet EIP-712 Order only for an owner-approved intent.

Guarantees (governance §2.6):
- the private key is read once, by this process, from its own env var
  SXBET_SIGNER_PRIVATE_KEY; it is never returned, logged, audited or repr'd;
- a signature is produced only when (a) the intent re-validates and its
  recomputed hash equals the approval binding's intent_hash, every binding
  field agrees with the intent / config / risk context, and (b) the approval is
  a valid Ed25519 signature by the owner's approval key, unused (nonce), fresh
  (TTL <= 120 s) and addressed to this signer_id;
- risk limits come only from the owner-signed config (signer/risk.py); the
  all-zero default refuses every request;
- transactional authorization: the approval is verified WITHOUT consuming it,
  every risk check runs, and only then one fsync'd ledger line atomically
  consumes the nonce, records AUTHORIZED and reserves the stake (single lock).
  A refused request never burns the approval; a used one can never sign a
  second order (threads, restarts). The SIGNED line is persisted before the
  signature is released;
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
from . import risk as rk
from .audit import AuditLog
from .intent import IntentInvalid, intent_hash, validate
from .orders import AUTHORIZED, REJECTED, SIGNED, ExposureView, OrderLedger, OrderStateError

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
    """Owner-set identity and domain. Limits are NOT here: they come only from the owner-signed risk config."""
    signer_id: str
    network: str
    chain_id: int
    verifying_contract: str  # lowercase hex
    base_token: str          # lowercase hex
    odds_ladder_step_size: int
    account_id: str = ""     # SX Bet API identity the approvals must name; empty => every request refused


class Signer:
    def __init__(self, config: SignerConfig, *, owner_public_key: bytes, ledger: OrderLedger, audit: AuditLog,
                 risk_config: rk.LoadedRiskConfig | None = None, env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.time):
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
        if not isinstance(ledger, OrderLedger) or ledger.signer_id != config.signer_id \
                or ledger.signer_address != self.address.lower():
            raise SignerConfigError("LEDGER_FOREIGN")  # a ledger of another signer never feeds this one
        self._ledger = ledger
        self._audit = audit
        self._clock = clock
        self._risk = risk_config if risk_config is not None else rk.load_from_env(env, self._owner_key)
        if not isinstance(self._risk, rk.LoadedRiskConfig):
            raise SignerConfigError("RISK_CONFIG_INVALID")
        self._session_started_at = int(clock())
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, dict[str, Any]]] = {}

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
        return self._ledger.exposure().total

    def exposure(self) -> ExposureView:
        return self._ledger.exposure()

    @property
    def risk_config_source(self) -> str:
        return self._risk.source

    def _produce_signature(self, msg) -> str:
        return self.__account.sign_message(msg).signature.to_0x_hex()

    # ---- the only signing entry point ----
    def sign_order(self, intent: Mapping[str, Any], approval: appr.Approval,
                   context: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
                known = self._ledger.get(key)
                if known is not None:
                    raise SignerRefused("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"
                                        if known.intent_hash != ihash
                                        else "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST")
                self._check_config(clean)
                now = int(self._clock())
                if clean["expiry"] < now + MIN_EXPIRY_MARGIN_SECONDS:
                    raise SignerRefused("INTENT_EXPIRED_OR_TOO_SOON")
                # 1. verify the approval WITHOUT consuming it
                try:
                    appr.verify(approval, self._owner_key, expected_intent_hash=ihash,
                                signer_id=self.config.signer_id, now=now)
                except appr.ApprovalError as exc:
                    raise SignerRefused(str(exc)) from None
                field = appr.binding_mismatch(approval.binding, clean)
                if field is not None:
                    raise SignerRefused("APPROVAL_BINDING_MISMATCH_" + field.upper())
                if not self.config.account_id or approval.binding["account_id"] != self.config.account_id:
                    raise SignerRefused("APPROVAL_BINDING_MISMATCH_ACCOUNT_ID")
                if self._ledger.nonce_consumed(approval.approval_nonce):
                    raise SignerRefused("APPROVAL_NONCE_REPLAYED")
                msg = eip712.signable(clean)
                oid = eip712.order_id(msg)

                # 2. risk checks + 3. atomic consume/AUTHORIZED/reserve, all inside the ledger lock
                def precheck(view: ExposureView) -> None:
                    try:
                        rk.evaluate(self._risk.limits, intent=clean, binding=approval.binding, context=context,
                                    exposure=view, now=now, session_started_at=self._session_started_at)
                    except rk.RiskRefused as exc:
                        raise SignerRefused(exc.code) from None

                try:
                    self._ledger.authorize(client_order_id=key, intent_hash=ihash, order_id=oid,
                                           market_hash=clean["market_hash"], event_id=approval.binding["event_id"],
                                           stake=int(clean["total_bet_size"]), expiry=clean["expiry"],
                                           approval_nonce=approval.approval_nonce, precheck=precheck)
                except OrderStateError as exc:
                    raise SignerRefused(exc.code) from None
                self._log("AUTHORIZED", None, key, ihash, oid, clean, nonce=approval.approval_nonce)

                # 4. sign; persist SIGNED before the signature leaves this function
                try:
                    signature = self._produce_signature(msg)
                except Exception:
                    # In-process failure: the signature never left, so the reservation can be released.
                    self._ledger.transition(key, REJECTED, reason="SIGNING_FAILED")
                    raise SignerRefused("SIGNING_FAILED") from None
                self._ledger.transition(key, SIGNED, reason="SIGNED")
                result = {"order_id": oid, "intent_hash": ihash, "signer_address": self.address,
                          "order": eip712.order_payload(clean, signature), "submitted": False,
                          "live_trading": "UNAVAILABLE"}
                self._cache[key] = (ihash, result)
                self._log("SIGNED", None, key, ihash, oid, clean, nonce=approval.approval_nonce)
                return {**result, "cached": False}
            except SignerRefused as exc:
                self._log("REFUSED", exc.code, key[:64] if isinstance(key, str) else None, ihash, None, None,
                          nonce=getattr(approval, "approval_nonce", None))
                raise

    def abandon_unsigned(self, client_order_id: str) -> str:
        """Release an order left AUTHORIZED by a crash between nonce consumption and signing.

        Safe because the SIGNED line is always persisted before a signature is released: an
        AUTHORIZED order whose signature is not in this process's cache was never handed out.
        The nonce stays consumed for ever (it is part of the AUTHORIZE line).
        """
        with self._lock:
            o = self._ledger.get(client_order_id)
            if o is None or o.state != AUTHORIZED or client_order_id in self._cache:
                raise SignerRefused("NOT_AN_ABANDONED_AUTHORIZATION")
            self._ledger.transition(client_order_id, REJECTED, reason="SIGNING_INTERRUPTED")
            self._log("ABANDONED_UNSIGNED", "SIGNING_INTERRUPTED", client_order_id, o.intent_hash, o.order_id, None)
            return REJECTED

    def _check_config(self, i: Mapping[str, Any]) -> None:
        c = self.config
        if (i["network"], i["chain_id"]) != (c.network, c.chain_id):
            raise SignerRefused("INTENT_NETWORK_NOT_CONFIGURED")
        if i["verifying_contract"] != c.verifying_contract or i["base_token"] != c.base_token:
            raise SignerRefused("INTENT_DOMAIN_NOT_CONFIGURED")
        if i["maker"] != self.address.lower():
            raise SignerRefused("INTENT_MAKER_IS_NOT_SIGNER")

    def _log(self, decision, reason, key, ihash, oid, clean, *, nonce=None) -> None:
        self._audit.append(event="SIGN", decision=decision, reason=reason, signer_id=self.config.signer_id,
                           idempotency_key=key, intent_hash=ihash, order_id=oid,
                           total_bet_size=None if clean is None else clean["total_bet_size"],
                           network=self.config.network,
                           approval_nonce_prefix=nonce[:6] if isinstance(nonce, str) else None)
