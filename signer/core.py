"""The isolated signer. Signs an SX Bet EIP-712 Order only for an order the OWNER explicitly commanded.

Guarantees (governance §2.6, owner decisions #187):
- the private key is read once, by this process, from its own env var SXBET_SIGNER_PRIVATE_KEY; it is
  never returned, logged, audited or repr'd;
- a signature is produced only when (a) the intent re-validates; (b) an `OwnerCommandAuthorization`
  carries the literal command text the owner wrote in the trading chat, a NEW single-use command id, a
  matching text hash and a received_at inside the short owner-signed window (signer/command.py);
  (c) the command parses with the signer's own deterministic grammar and EVERY field (event, market,
  line, selection, stake + currency, odds / limit price, order type, time in force, post-only) equals the
  order (COMMAND_MISMATCH_<FIELD>, COMMAND_INCOMPLETE_<FIELD>); (d) every limit of the owner-signed risk
  config v2 passes (signer/risk.py); (e) the kill switch is not engaged (signer/killswitch.py);
- the per-order Ed25519 `Approval` of the previous version is gone (intentional change, §2.6 as rewritten):
  owner-signed acts remain for the risk config, the LIVE_TRADING flag, the ledger-head acknowledgement and
  the kill-switch release, never for a single order;
- residual risk accepted by the owner (§2.6): without an out-of-band factor the signer cannot tell an
  owner's command from one injected into the chat or produced by the model. The limits bound the damage;
- transactional authorization: the command is verified WITHOUT consuming it, every risk check runs inside
  the ledger lock, and only then one fsync'd AUTHORIZE line atomically consumes the command id AND its
  canonical text hash, records AUTHORIZED and reserves the stake. A refused request never burns the
  command; a consumed one can never authorise a second order (threads, restarts: the ledger is replayed);
- one idempotency key = one intent: same key + same hash returns the cached result (no second signature),
  same key + different hash is refused;
- snapshot: the command record and the risk context are copied into plain values BEFORE anything is
  verified, and only the copies are used afterwards;
- a released signature keeps its stake reserved even if the order is later REJECTED, until its expiry
  has passed (signer/orders.py);
- every decision is appended to the audit log (command id and text HASH only, never the text);
- there is no network or submission code here. LIVE_TRADING remains UNAVAILABLE.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_account import Account

from . import command as cmd
from . import eip712
from . import risk as rk
from .audit import AuditLog
from .intent import IntentInvalid, intent_hash, validate
from .market_data import MarketDataProvider
from .orders import AUTHORIZED, REJECTED, SIGNED, ExposureView, LedgerError, OrderLedger, OrderStateError

KEY_ENV = "SXBET_SIGNER_PRIVATE_KEY"
CLOCK_JUMP_TOLERANCE_SECONDS = 120
MIN_EXPIRY_MARGIN_SECONDS = 12  # docs: expiry inside betting delay + 2 s is rejected; local margin on top
_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class SignerRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SignerConfigError(RuntimeError):
    pass


_MAX_SNAPSHOT_DEPTH = 16


class _NotPlain(Exception):
    pass


def _plain(obj: Any, depth: int = 0) -> Any:
    """Deep, one-pass copy into plain JSON-like values; anything else raises _NotPlain."""
    if depth > _MAX_SNAPSHOT_DEPTH:
        raise _NotPlain()
    if obj is None or type(obj) in (str, int, bool, float):
        return obj
    if isinstance(obj, Mapping):
        out = {}
        for k, v in list(obj.items()):
            if type(k) is not str:
                raise _NotPlain()
            out[k] = _plain(v, depth + 1)
        return out
    if type(obj) in (list, tuple):
        return [_plain(v, depth + 1) for v in obj]
    raise _NotPlain()


def load_owner_public_key(raw: bytes) -> Ed25519PublicKey:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
        raise SignerConfigError("OWNER_PUBLIC_KEY_INVALID")
    return Ed25519PublicKey.from_public_bytes(bytes(raw))


@dataclass(frozen=True)
class SignerConfig:
    """Owner-set identity and domain. Limits are NOT here: they come only from the owner-signed risk config."""
    signer_id: str
    network: str
    chain_id: int
    verifying_contract: str  # lowercase hex
    base_token: str          # lowercase hex
    odds_ladder_step_size: int
    account_id: str = ""     # SX Bet API identity of this signer; empty => every request refused
    # Review P1-2: the token the owner's stake is converted with is owner configuration bound to `base_token`,
    # never a caller-supplied value. Empty / None => every order refused (SIGNER_TOKEN_CONFIG_MISSING).
    base_token_symbol: str = ""
    base_token_decimals: int | None = None
    # Review P1-1: the signer's OWN market reads (signer/market_data.py), configured here, never per call.
    # None => every order refused (SIGNER_MARKET_READS_UNAVAILABLE). No network provider ships in this PR.
    market_data: Any = None


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
            self._owner_key = load_owner_public_key(owner_public_key)
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
        self._risk = risk_config if risk_config is not None else rk.load_from_env(env, self._owner_key,
                                                                                    network=config.network)
        if not isinstance(self._risk, rk.LoadedRiskConfig):
            raise SignerConfigError("RISK_CONFIG_INVALID")
        if self._risk.network != config.network:
            raise SignerConfigError("RISK_CONFIG_NETWORK_MISMATCH")  # testnet limits never feed mainnet
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, dict[str, Any]]] = {}
        # Host clock sanity (review P3): the wall clock is a trust root (windows, expiries, UTC-day counters).
        # With the real clock a forward / backward jump against the monotonic clock beyond the tolerance stops
        # signing (SIGNER_CLOCK_JUMP_DETECTED) until restart; injected test clocks are not checked.
        self._real_clock = clock is time.time
        self._wall0, self._mono0 = clock(), time.monotonic()

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
    def _clock_sane(self) -> bool:
        if not self._real_clock:
            return True
        drift = (self._clock() - self._wall0) - (time.monotonic() - self._mono0)
        return abs(drift) <= CLOCK_JUMP_TOLERANCE_SECONDS

    def _reads(self, clean: Mapping[str, Any], now: int) -> Any:
        """The signer's OWN reads (review P1-1): never the caller's. Snapshotted into plain values."""
        provider = self.config.market_data
        if provider is None or not isinstance(provider, MarketDataProvider):
            raise SignerRefused("SIGNER_MARKET_READS_UNAVAILABLE")
        try:
            raw = provider.read(market_hash=clean["market_hash"], outcome_one=clean["is_maker_betting_outcome_one"],
                                percentage_odds=int(clean["percentage_odds"]), now=now)
        except Exception:
            raise SignerRefused("SIGNER_MARKET_READS_FAILED") from None
        if raw is None:
            return None
        try:
            return _plain(raw)
        except Exception:
            raise SignerRefused("RISK_CONTEXT_MALFORMED") from None

    def sign_order(self, intent: Mapping[str, Any], authorization: cmd.OwnerCommandAuthorization, *,
                   post_only: bool = False) -> dict[str, Any]:
        """There is no context parameter (review P1-1, intentional change): market data, event markets, events
        with the same teams, quote, balance, fees and exchange metadata come only from the signer's own
        provider (`SignerConfig.market_data`)."""
        with self._lock:
            key = intent.get("idempotency_key") if isinstance(intent, Mapping) else None
            ihash = None
            auth = None
            try:
                # Snapshot FIRST (before anything else reads caller-owned objects); only the copies are used.
                try:
                    auth = cmd.snapshot(authorization)
                except cmd.CommandRefused as exc:
                    raise SignerRefused(exc.code) from None
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
                if self._ledger.kill_switch_engaged:
                    raise SignerRefused("KILL_SWITCH_ENGAGED")
                self._check_config(clean)
                if type(post_only) is not bool:
                    raise SignerRefused("POST_ONLY_INVALID")
                now = int(self._clock())
                if clean["expiry"] < now + MIN_EXPIRY_MARGIN_SECONDS:
                    raise SignerRefused("INTENT_EXPIRED_OR_TOO_SOON")
                limits = self._risk.limits
                if not limits.configured():
                    raise SignerRefused("EXPOSURE_LIMIT_ZERO")  # safe default: zero live exposure
                if not self.config.account_id:
                    raise SignerRefused("ACCOUNT_ID_NOT_CONFIGURED")
                c = self.config
                if not (isinstance(c.base_token_symbol, str) and c.base_token_symbol
                        and type(c.base_token_decimals) is int and 0 <= c.base_token_decimals <= 36):
                    raise SignerRefused("SIGNER_TOKEN_CONFIG_MISSING")
                if not self._clock_sane():
                    raise SignerRefused("SIGNER_CLOCK_JUMP_DETECTED")

                # 1. the owner command: record, single use, grammar, field-by-field -- nothing consumed yet
                try:
                    cmd.verify_record(auth, now=now, max_age_seconds=limits.command_max_age_seconds)
                except cmd.CommandRefused as exc:
                    raise SignerRefused(exc.code) from None
                if self._ledger.command_consumed(auth.command_id):
                    raise SignerRefused("COMMAND_ID_ALREADY_CONSUMED")
                context = self._reads(clean, now)
                verified, reference = self._verify_command(auth, clean, context, post_only, limits)
                # Replay fingerprint of the PARSED, resolved operation + its reference token (review P2-2).
                fields = cmd.ledger_fields(auth, cmd.place_fingerprint(verified, reference))
                if self._ledger.command_fingerprint_consumed(fields["command_fingerprint_sha256"]):
                    raise SignerRefused("COMMAND_REPLAYED")
                msg = eip712.signable(clean)
                oid = eip712.order_id(msg)

                # 2. risk checks + 3. atomic consume/AUTHORIZED/reserve, all inside the ledger lock
                def precheck(view: ExposureView) -> None:
                    try:
                        rk.evaluate(limits, intent=clean, event_id=verified["event_id"], context=context,
                                    exposure=view, now=now)
                    except rk.RiskRefused as exc:
                        raise SignerRefused(exc.code) from None

                try:
                    self._ledger.authorize(client_order_id=key, intent_hash=ihash, order_id=oid,
                                           market_hash=clean["market_hash"], event_id=verified["event_id"],
                                           stake=int(clean["total_bet_size"]), expiry=clean["expiry"],
                                           command=fields, post_only=verified["post_only"], precheck=precheck)
                except (OrderStateError, LedgerError) as exc:
                    raise SignerRefused(exc.code) from None
                self._log("AUTHORIZED", None, key, ihash, oid, clean, auth=auth)
                # Review P2-1: an engagement between AUTHORIZE and here stops the signature from ever being made
                # (the order is in the kill switch's intents as AUTHORIZED and is released as never signed).
                if self._ledger.kill_switch_engaged:
                    try:
                        self._ledger.transition(key, REJECTED, reason="KILL_SWITCH_ENGAGED")
                    except LedgerError:
                        pass  # stays AUTHORIZED (reserved, never signed): abandon_unsigned after restart
                    raise SignerRefused("KILL_SWITCH_ENGAGED")

                # 4. sign; persist SIGNED before the signature leaves this function
                try:
                    signature = self._produce_signature(msg)
                except Exception:
                    # In-process failure: the signature never left, so the reservation can be released.
                    try:
                        self._ledger.transition(key, REJECTED, reason="SIGNING_FAILED")
                    except LedgerError:
                        pass  # stays AUTHORIZED (reserved); abandon_unsigned after restart
                    raise SignerRefused("SIGNING_FAILED") from None
                try:
                    self._ledger.transition(key, SIGNED, reason="SIGNED")
                except LedgerError as exc:
                    # SIGNED not durably recorded: the signature is discarded and never returned.
                    del signature
                    raise SignerRefused(exc.code) from None
                result = {"order_id": oid, "intent_hash": ihash, "signer_address": self.address,
                          "order": eip712.order_payload(clean, signature), "submitted": False,
                          "live_trading": "UNAVAILABLE", "order_type": verified["order_type"],
                          "post_only": verified["post_only"], "owner_command_id": auth.command_id,
                          "owner_command_sha256": auth.command_text_sha256}
                self._cache[key] = (ihash, result)
                self._log("SIGNED", None, key, ihash, oid, clean, auth=auth)
                return {**result, "cached": False}
            except SignerRefused as exc:
                self._log("REFUSED", exc.code, key[:64] if isinstance(key, str) else None, ihash, None, None,
                          auth=auth)
                raise

    def _verify_command(self, auth: cmd.OwnerCommandAuthorization, clean: Mapping[str, Any], context: Any,
                        post_only: bool, limits: rk.RiskLimits) -> tuple[dict[str, Any], str | None]:
        """Parse the literal command and compare every field with the order and the signer's OWN reads; the
        market resolved from those reads (one event, one market) must be the market being signed."""
        try:
            parsed = cmd.parse(auth.command_text)
        except cmd.CommandRefused as exc:
            raise SignerRefused(exc.code) from None
        if not isinstance(context, Mapping):
            raise SignerRefused("RISK_CONTEXT_MISSING")
        market = context.get("market")
        if not isinstance(market, Mapping):
            raise SignerRefused("MARKET_DATA_MISSING")
        if not isinstance(market.get("marketHash"), str) or market["marketHash"].lower() != clean["market_hash"]:
            raise SignerRefused("MARKET_MISMATCH")
        xl = context.get("exchange_limits")
        if isinstance(xl, Mapping) and "base_token_decimals" in xl \
                and xl["base_token_decimals"] != self.config.base_token_decimals:
            raise SignerRefused("TOKEN_DECIMALS_DIFFER_FROM_EXCHANGE")  # cross-check only: config is the source
        try:
            verified = cmd.verify_place(parsed, intent=clean, market=market,
                                        event_markets=context.get("event_markets"),
                                        same_team_events=context.get("same_team_events"), post_only=post_only,
                                        currency=self.config.base_token_symbol,
                                        token_decimals=self.config.base_token_decimals,
                                        odds_ladder_step_size=self.config.odds_ladder_step_size,
                                        market_time_in_force=limits.market_time_in_force,
                                        limit_time_in_force=limits.limit_time_in_force)
        except cmd.CommandRefused as exc:
            raise SignerRefused(exc.code) from None
        if verified["market_hash"] != clean["market_hash"]:
            raise SignerRefused("COMMAND_MISMATCH_MARKET")  # defence in depth: resolved hash == signed hash
        return verified, parsed.reference

    def abandon_unsigned(self, client_order_id: str) -> str:
        """Release an order left AUTHORIZED by a crash between command consumption and signing.

        Safe because the SIGNED line is always persisted before a signature is released: an
        AUTHORIZED order whose signature is not in this process's cache was never handed out.
        The command id stays consumed for ever (it is part of the AUTHORIZE line).
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

    def _log(self, decision, reason, key, ihash, oid, clean, *, auth=None) -> None:
        cid = auth.command_id if isinstance(auth, cmd.OwnerCommandAuthorization) and \
            isinstance(auth.command_id, str) else None
        csha = auth.command_text_sha256 if cid is not None and isinstance(auth.command_text_sha256, str) else None
        self._audit.append(event="SIGN", decision=decision, reason=reason, signer_id=self.config.signer_id,
                           idempotency_key=key, intent_hash=ihash, order_id=oid,
                           total_bet_size=None if clean is None else clean["total_bet_size"],
                           network=self.config.network,
                           owner_command_id=cid[:128] if cid else None,
                           owner_command_sha256=csha[:71] if csha else None)
