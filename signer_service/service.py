"""Signer service logic (framework-free; signer_service/app.py is the HTTP shell).

Request lifecycle (persisted in signer_service/store.py, append-only):
    REQUESTED -> PENDING_APPROVAL -> APPROVED -> SIGNED -> HANDED_TO_GATEWAY      (orders)
    REQUESTED -> PENDING_APPROVAL -> APPROVED -> HANDED_TO_GATEWAY                (cancels: nothing is signed)
    ... -> REJECTED (owner rejected, signer/risk refused, restart) | EXPIRED (TTL <= 120 s, server-side)

Who may do what:
- the MCP (HMAC key, scopes request:create + request:read) files an order / cancel REQUEST and reads its
  status. The request carries only the market, outcome, stake, odds limit, time in force and TTL: the
  maker, domain, salt, client order id, approval nonce, labels, fees and quote are set HERE, from the
  signer config and the (simulated) market data provider, never from the MCP;
- only a registered owner passkey (WebAuthn assertion over SHA-256 of the exact binding + display)
  moves a request out of PENDING_APPROVAL. No HMAC scope can approve, sign, enrol or change limits;
- nothing about an order is shown to the phone before authentication. The page first gets an opaque
  UNLOCK challenge (random, request-bound, single-use, 60 s, no credential id: discoverable passkeys);
  a valid passkey assertion over it yields a session token (random 32 bytes, only its SHA-256 kept in
  memory, bound to the request and to the unlocking credential, 60 s, usable for ONE details read and
  ONE decision). The decision itself still needs a second assertion over the exact order challenge, from
  the same credential. Every pre-auth failure is a generic code (APPROVAL_LOCKED / UNLOCK_REFUSED) that
  does not reveal whether the request exists;
- signing happens in signer.core.Signer with an EPHEMERAL key generated in memory at start; the risk
  limits come only from the owner-signed risk config (default all zero => every order REJECTED);
- the hand-off to the gateway always fails closed: the LIVE_TRADING flag path is hard-wired to None and
  no transport exists, so SubmissionGateway refuses to construct; the refusal code is recorded and the
  signature is dropped (it is never persisted nor returned).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from eth_account import Account

from signer import approval as appr
from signer import cancellation as cx
from signer import gateway as gw
from signer import risk as rk
from signer import webauthn as wa
from signer.audit import AuditLog
from signer.core import KEY_ENV, Signer, SignerConfig, SignerRefused
from signer.intent import CHAIN_IDS, INTENT_SCHEMA, IntentInvalid, ODDS_EXCEPTIONS, intent_hash, validate
from signer.orders import OrderLedger

from . import store as st
from .auth import HmacKey, HmacVerifier, NonceCache
from .market import MarketDataProvider, MarketDataUnavailable

MAX_REQUEST_TTL_SECONDS = 120
UNLOCK_CHALLENGE_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 60
MAX_PENDING_UNLOCKS = 256
UNLOCK_DOMAIN = b"sxbet.approval_unlock.v1"
REFUSAL_AUDIT_PER_WINDOW = 10
REFUSAL_AUDIT_WINDOW_SECONDS = 60
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
ENROLLMENT_CODE_TTL_SECONDS = 600
REGISTRATION_CHALLENGE_TTL_SECONDS = 120
ALLOWED_NETWORKS = frozenset({"testnet"})  # this PR: never mainnet
CANCEL_DISPLAY_SCHEMA = "sxbet.cancel_display.v1"
_CLIENT_REQ = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_REQUEST_ID = re.compile(r"^req_[0-9a-f]{32}$")
_HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^[1-9][0-9]{0,30}$")
_EVENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ORDER_KEYS = frozenset({"client_request_id", "network", "market_hash", "outcome", "stake_base_units",
                        "percentage_odds_limit", "time_in_force", "order_ttl_seconds"})
CANCEL_KEYS = frozenset({"client_request_id", "network", "scope", "order_ids", "market_hash", "event_id"})


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


class ServiceConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: str
    owner_public_key: bytes = field(repr=False)      # Ed25519 (risk config + live flag verification)
    hmac_keys: tuple[HmacKey, ...] = field(repr=False)
    signer_id: str = "signer-service-dev"
    account_id: str = ""
    network: str = "testnet"
    request_ttl_seconds: int = 90
    rp_id: str = "localhost"
    origins: tuple[str, ...] = ("http://localhost:8787",)
    risk_config_path: str | None = None
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")  # Host header allowlist (DNS rebinding)


class RefusalAuditLimiter:
    """At most N audit rows per event kind per window; the rest is counted and summarised once per window
    (AUDIT_SUPPRESSED {suppressed_event, count}), so unauthenticated traffic cannot grow the audit freely."""

    def __init__(self, store, clock):
        self._store, self._clock = store, clock
        self._win: dict[str, list[int]] = {}  # event -> [window_start, written, suppressed]

    def audit(self, event: str, **kw: Any) -> None:
        now = int(self._clock())
        w = self._win.get(event)
        if w is None or now - w[0] >= REFUSAL_AUDIT_WINDOW_SECONDS:
            if w is not None and w[2]:
                self._store.audit("AUDIT_SUPPRESSED", detail={"suppressed_event": event, "count": w[2]})
            w = self._win[event] = [now, 0, 0]
        if w[1] < REFUSAL_AUDIT_PER_WINDOW:
            w[1] += 1
            self._store.audit(event, **kw)
        else:
            w[2] += 1


def _canon_sha(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(st.canonical(obj).encode("ascii")).hexdigest()


def _round_down(requested: int, step_size: int) -> int:
    if requested in ODDS_EXCEPTIONS:
        return requested
    step = step_size * 10 ** 15
    return (requested // step) * step


def issue_enrollment_code(store: st.ServiceStore, clock: Callable[[], float]) -> str:
    """One-time code, valid 10 min, stored as SHA-256 only. The CLI writes it to a 0600 file; never logged."""
    code = secrets.token_urlsafe(18)
    now = int(clock())
    store.add_enrollment_code(_canon_sha(code), now, now + ENROLLMENT_CODE_TTL_SECONDS)
    store.audit("ENROLLMENT_CODE_ISSUED")
    return code


class SignerService:
    def __init__(self, config: ServiceConfig, market: MarketDataProvider, *,
                 clock: Callable[[], float], env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        # Fail closed on anything that could turn this prototype into a real trading path.
        if KEY_ENV in env:
            raise ServiceConfigError("SIGNER_SERVICE_REFUSES_KEY_FROM_ENV")
        if gw.LIVE_FLAG_ENV in env:
            raise ServiceConfigError("SIGNER_SERVICE_REFUSES_LIVE_TRADING_FLAG")
        if config.network not in ALLOWED_NETWORKS:
            raise ServiceConfigError("SIGNER_SERVICE_NETWORK_NOT_ALLOWED")
        if not 10 <= config.request_ttl_seconds <= MAX_REQUEST_TTL_SECONDS:
            raise ServiceConfigError("SIGNER_SERVICE_REQUEST_TTL_INVALID")
        if not config.hmac_keys:
            raise ServiceConfigError("AUTH_CONFIG_MISSING")
        if not config.allowed_hosts or not all(isinstance(h, str) and _HOST.fullmatch(h) for h in config.allowed_hosts):
            raise ServiceConfigError("SIGNER_SERVICE_ALLOWED_HOSTS_INVALID")
        if getattr(market, "data_source", None) not in appr.DATA_SOURCES:
            raise ServiceConfigError("MARKET_PROVIDER_INVALID")
        meta = market.metadata()
        if (meta.get("network"), meta.get("chain_id")) != (config.network, CHAIN_IDS[config.network]):
            raise ServiceConfigError("MARKET_METADATA_NETWORK_MISMATCH")
        try:
            owner_key = appr.load_public_key(config.owner_public_key)
        except Exception:
            raise ServiceConfigError("OWNER_PUBLIC_KEY_INVALID") from None
        self.config = config
        self._market = market
        self._clock = clock
        self._owner_key = owner_key
        self._lock = threading.Lock()
        os.makedirs(os.path.join(config.state_dir, "ledgers"), exist_ok=True)
        self.store = st.ServiceStore(os.path.join(config.state_dir, "service.sqlite3"), clock)
        self._nonces = NonceCache(os.path.join(config.state_dir, "hmac_nonces.sqlite3"), clock)
        self.auth = HmacVerifier({k.key_id: k for k in config.hmac_keys}, self._nonces, clock)
        self.policy = wa.WebAuthnPolicy(config.rp_id, frozenset(config.origins))
        self.webauthn = wa.WebAuthnApprovalVerifier(self.policy, st.CredentialView(self.store))
        self._meta = meta
        self._reg_challenges: dict[str, tuple[bytes, str, int]] = {}
        self._unlocks: dict[str, tuple[bytes, str, int]] = {}       # unlock_id -> (challenge, request_id, exp)
        self._sessions: dict[str, dict[str, Any]] = {}              # sha256(token) -> session
        self.refusals = RefusalAuditLimiter(self.store, clock)

        # EPHEMERAL throwaway order-signing key: generated here, in memory only, never read from env or disk.
        ephemeral = secrets.token_hex(32)
        self.signer_address = Account.from_key(ephemeral).address.lower()
        self._ledger = OrderLedger.create(
            os.path.join(config.state_dir, "ledgers", f"ledger-{self.signer_address}.jsonl"),
            signer_id=config.signer_id, signer_address=self.signer_address, clock=clock)
        self._signer_audit = AuditLog(os.path.join(config.state_dir, "signer-audit.jsonl"))
        risk = rk.load_risk_config(config.risk_config_path, owner_key)
        self.risk_config_source = risk.source
        self.signer = Signer(
            SignerConfig(signer_id=config.signer_id, network=config.network, chain_id=meta["chain_id"],
                         verifying_contract=meta["verifying_contract"], base_token=meta["base_token"],
                         odds_ladder_step_size=meta["odds_ladder_step_size"], account_id=config.account_id),
            owner_public_key=config.owner_public_key, ledger=self._ledger, audit=self._signer_audit,
            risk_config=risk, env={KEY_ENV: ephemeral}, clock=clock, webauthn=self.webauthn)
        del ephemeral
        self.store.audit("SERVICE_STARTED", detail={"data_source": market.data_source})
        self._recover()

    def close(self) -> None:
        self._ledger.close()
        self._nonces.close()
        self.store.close()

    # ---------------------------------------------------------------- restart recovery (fail closed)
    def _recover(self) -> None:
        now = int(self._clock())
        for rid in self.store.non_terminal():
            r = self.store.get(rid)
            if r["state"] == st.PENDING and now >= r["expires_at"]:
                self.store.transition(rid, st.EXPIRED, reason="REQUEST_EXPIRED")
            else:
                # The ephemeral key changed and any in-flight signature was memory-only: nothing to resume.
                self.store.transition(rid, st.REJECTED, reason="SERVICE_RESTARTED")

    # ---------------------------------------------------------------- MCP: create / read
    def create_order_request(self, key: HmacKey, payload: Any) -> tuple[int, dict[str, Any]]:
        p = self._validate_order(payload)
        return self._create(key, "ORDER", p, self._build_order_record)

    def create_cancel_request(self, key: HmacKey, payload: Any) -> tuple[int, dict[str, Any]]:
        p = self._validate_cancel(payload)
        return self._create(key, "CANCEL", p, self._build_cancel_record)

    def _create(self, key, kind, p, builder) -> tuple[int, dict[str, Any]]:
        sha = _canon_sha({"kind": kind, "payload": p})
        with self._lock:
            now = int(self._clock())
            request_id = "req_" + secrets.token_hex(16)
            try:
                record, initial = builder(p, request_id, now)
            except (MarketDataUnavailable, ServiceError, appr.ApprovalError, IntentInvalid) as exc:
                record, initial = {"refused": True}, [(st.REQUESTED, None), (st.REJECTED, getattr(exc, "code", str(exc)))]
            try:
                rid, created = self.store.create(
                    request_id=request_id, kind=kind, key_id=key.key_id, client_request_id=p["client_request_id"],
                    payload_sha256=sha, created_at=now, expires_at=now + self.config.request_ttl_seconds,
                    record=record, initial=initial)
            except st.StoreError as exc:
                raise ServiceError(exc.code, 409 if exc.code == "DUPLICATE_REQUEST_CONFLICT" else 500) from None
        return (201 if created else 200), self.status(key, rid)

    def status(self, key: HmacKey, request_id: Any) -> dict[str, Any]:
        r = self._load(request_id)
        if r["key_id"] != key.key_id:
            raise ServiceError("REQUEST_NOT_FOUND", 404)
        r = self._expire_if_due(r)
        rec = r["record"]
        out = {"request_id": r["request_id"], "kind": r["kind"], "state": r["state"],
               "client_request_id": r["client_request_id"], "expires_at": r["expires_at"],
               "history": [{"state": h["state"], "reason": h["reason"], "at": h["at"]} for h in r["history"]],
               "approval_path": f"/approve/{r['request_id']}", "live_trading": "UNAVAILABLE", "submitted": False,
               "data_source": self._market.data_source}
        if "display" in rec:
            out["display"] = rec["display"]
        for h in r["history"]:
            if h["state"] == st.SIGNED:
                out["order_id"] = h["detail"].get("order_id")
            if h["state"] == st.HANDED:
                out["gateway"] = h["detail"].get("gateway")
        return out

    # ---------------------------------------------------------------- owner device: page data + decision
    # ---------------------------------------------------------------- owner device: unlock (pre-auth)
    def unlock_options(self, request_id: Any) -> dict[str, Any]:
        """Opaque challenge for ANY well-formed id: existence, state and order fields are not disclosed."""
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ServiceError("NOT_FOUND", 404)
        now = int(self._clock())
        with self._lock:
            self._unlocks = {k: v for k, v in self._unlocks.items() if v[2] > now}
            if len(self._unlocks) >= MAX_PENDING_UNLOCKS:
                raise ServiceError("UNLOCK_BUSY", 429)
            uid = secrets.token_hex(16)
            challenge = hashlib.sha256(UNLOCK_DOMAIN + b"\n" + request_id.encode("ascii") + b"\n"
                                       + secrets.token_bytes(32)).digest()
            self._unlocks[uid] = (challenge, request_id, now + UNLOCK_CHALLENGE_TTL_SECONDS)
        # allowCredentials omitted on purpose: passkeys are discoverable (residentKey required), so no
        # credential id ever leaves the server before authentication.
        return {"request_id": request_id, "unlock_id": uid, "challenge": wa.b64url_encode(challenge),
                "rp_id": self.policy.rp_id, "user_verification": "required", "timeout": 60000}

    def unlock(self, request_id: Any, body: Any) -> tuple[str, int]:
        """Verify the unlock assertion; return (session token, expires_at). Failures: UNLOCK_REFUSED only."""
        refused = ServiceError("UNLOCK_REFUSED", 401)
        keys = {"unlock_id", "credential_id", "authenticator_data", "client_data_json", "signature"}
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id) \
                or not isinstance(body, Mapping) or set(body) != keys or not isinstance(body["unlock_id"], str):
            raise refused
        with self._lock:
            now = int(self._clock())
            entry = self._unlocks.pop(body["unlock_id"], None)  # single use, whatever happens next
            code = None
            if entry is None or entry[2] <= now or entry[1] != request_id:
                code = "UNLOCK_CHALLENGE_INVALID"
            parts = None
            if code is None:
                try:
                    parts = {k: wa.b64url_decode(body[k]) for k in keys - {"unlock_id"}}
                    count = self.webauthn.verify_challenge(challenge=entry[0], **parts)
                    self.webauthn.commit(parts["credential_id"], count)
                except appr.ApprovalError as exc:
                    code = str(exc)
            if code is None and self.store.get(request_id) is None:
                code = "REQUEST_NOT_FOUND"
            if code is not None:
                self.refusals.audit("UNLOCK_REFUSED", request_id=None, reason=code)
                raise refused
            token = secrets.token_urlsafe(32)
            expires = now + SESSION_TTL_SECONDS
            self._sessions = {k: v for k, v in self._sessions.items() if v["expires_at"] > now}
            self._sessions[hashlib.sha256(token.encode("ascii")).hexdigest()] = {
                "request_id": request_id, "credential_id": parts["credential_id"], "expires_at": expires,
                "details_served": False}
            self.store.audit("UNLOCK_GRANTED", request_id=request_id,
                             detail={"credential_prefix": wa.b64url_encode(parts["credential_id"])[:8]})
            return token, expires

    def _session(self, request_id: Any, token: Any, *, consume: bool) -> dict[str, Any]:
        """The session for (request, token) or APPROVAL_LOCKED. Details: once. Decision: consumes it."""
        locked = ServiceError("APPROVAL_LOCKED", 401)
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise locked
        h = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
        s = self._sessions.get(h)
        if s is None or not secrets.compare_digest(s["request_id"].encode("ascii"),
                                                         str(request_id).encode("utf-8", "replace")) \
                or int(self._clock()) >= s["expires_at"]:
            raise locked
        if consume:
            del self._sessions[h]
        else:
            if s["details_served"]:
                raise locked
            s["details_served"] = True
        return s

    def approval_view(self, request_id: Any, token: Any = None) -> dict[str, Any]:
        with self._lock:
            self._session(request_id, token, consume=False)
        r = self._expire_if_due(self._load(request_id))
        view = {"request_id": r["request_id"], "kind": r["kind"], "state": r["state"], "expires_at": r["expires_at"],
                "data_source": self._market.data_source, "live_trading": "UNAVAILABLE"}
        if r["state"] != st.PENDING:
            return view
        rec = r["record"]
        a = rec["approval"]
        domain = appr.WEBAUTHN_ORDER_DOMAIN if r["kind"] == "ORDER" else appr.WEBAUTHN_CANCEL_DOMAIN
        chal = {d: wa.b64url_encode(appr.webauthn_challenge(
            domain, decision=d, binding=rec["binding"], display=rec["display"], approval_nonce=a["nonce"],
            issued_at=a["issued_at"], expires_at=a["expires_at"], signer_id=a["signer_id"]))
            for d in (appr.DECISION_APPROVE, appr.DECISION_REJECT)}
        view.update({"display": rec["display"], "binding": rec["binding"], "issued_at": a["issued_at"],
                     "signer_id": a["signer_id"], "challenge_approve": chal[appr.DECISION_APPROVE],
                     "challenge_reject": chal[appr.DECISION_REJECT], "rp_id": self.policy.rp_id})
        return view

    def submit_assertion(self, request_id: Any, body: Any, token: Any = None) -> dict[str, Any]:
        with self._lock:
            session = self._session(request_id, token, consume=True)  # single use, even if refused below
        if not isinstance(body, Mapping) or set(body) != {"decision", "credential_id", "authenticator_data",
                                                          "client_data_json", "signature"}:
            raise ServiceError("ASSERTION_MALFORMED")
        decision = body["decision"]
        if decision not in (appr.DECISION_APPROVE, appr.DECISION_REJECT):
            raise ServiceError("ASSERTION_MALFORMED")
        try:
            parts = {k: wa.b64url_decode(body[k]) for k in ("credential_id", "authenticator_data", "client_data_json",
                                                            "signature")}
        except appr.ApprovalError:
            raise ServiceError("ASSERTION_MALFORMED") from None
        if not secrets.compare_digest(parts["credential_id"], session["credential_id"]):
            raise ServiceError("APPROVAL_CREDENTIAL_MISMATCH", 403)
        with self._lock:
            r = self._expire_if_due(self._load(request_id))
            rid = r["request_id"]
            if r["state"] == st.EXPIRED:
                raise ServiceError("REQUEST_EXPIRED", 410)
            if r["state"] != st.PENDING:
                raise ServiceError("REQUEST_NOT_PENDING", 409)
            rec = r["record"]
            a = rec["approval"]
            now = int(self._clock())
            prefix = wa.b64url_encode(parts["credential_id"])[:8]
            try:
                if r["kind"] == "ORDER":
                    approval = appr.WebAuthnApproval(rec["binding"], rec["display"], a["nonce"], a["issued_at"],
                                                     a["expires_at"], a["signer_id"], **parts)
                    count = self.webauthn.verify_order_approval(
                        approval, expected_intent_hash=rec["binding"]["intent_hash"], signer_id=a["signer_id"],
                        now=now, decision=decision)
                else:
                    approval = None
                    chal = appr.webauthn_challenge(
                        appr.WEBAUTHN_CANCEL_DOMAIN, decision=decision, binding=rec["binding"],
                        display=rec["display"], approval_nonce=a["nonce"], issued_at=a["issued_at"],
                        expires_at=a["expires_at"], signer_id=a["signer_id"])
                    count = self.webauthn.verify_challenge(challenge=chal, **parts)
            except appr.ApprovalError as exc:
                self.refusals.audit("APPROVAL_ATTEMPT_REFUSED", request_id=rid, reason=str(exc),
                                 detail={"decision": decision, "credential_prefix": prefix})
                raise ServiceError(str(exc), 403) from None

            if decision == appr.DECISION_REJECT:
                try:
                    self.webauthn.commit(parts["credential_id"], count)
                except appr.ApprovalError as exc:
                    raise ServiceError(str(exc), 403) from None
                self._move(rid, st.REJECTED, "OWNER_REJECTED", {"decision": decision, "credential_prefix": prefix},
                           expect=st.PENDING)
                return self._decision_result(rid)

            self._move(rid, st.APPROVED, None, {"decision": decision, "credential_prefix": prefix}, expect=st.PENDING)
            if r["kind"] == "CANCEL":
                try:
                    self.webauthn.commit(parts["credential_id"], count)
                except appr.ApprovalError as exc:
                    self._move(rid, st.REJECTED, str(exc), {})
                    return self._decision_result(rid)
                self._move(rid, st.HANDED, "GATEWAY_REFUSED", {"gateway": self._hand_to_gateway()})
                return self._decision_result(rid)

            intent = rec["intent"]
            try:
                ctx = self._market.risk_context(intent["market_hash"], 1 if intent["is_maker_betting_outcome_one"] else 2)
                signed = self.signer.sign_order(intent, approval, ctx)  # commits the passkey counter itself
            except SignerRefused as exc:
                self._move(rid, st.REJECTED, exc.code, {"code": exc.code})
                return self._decision_result(rid)
            except MarketDataUnavailable as exc:
                self._move(rid, st.REJECTED, exc.code, {"code": exc.code})
                return self._decision_result(rid)
            self._move(rid, st.SIGNED, None, {"order_id": signed["order_id"], "intent_hash": signed["intent_hash"],
                                              "client_order_id": intent["idempotency_key"]})
            del signed  # the signature never leaves this function: no transport exists
            self._move(rid, st.HANDED, "GATEWAY_REFUSED", {"gateway": self._hand_to_gateway()})
            return self._decision_result(rid)

    def _decision_result(self, rid: str) -> dict[str, Any]:
        r = self.store.get(rid)
        last = r["history"][-1]
        out = {"request_id": rid, "state": r["state"], "reason": last["reason"], "live_trading": "UNAVAILABLE",
               "submitted": False}
        if r["state"] == st.HANDED:
            out["gateway"] = last["detail"].get("gateway")
        return out

    def _hand_to_gateway(self) -> str:
        """Always refused: no LIVE_TRADING flag is ever loaded (path None) and no transport exists."""
        now = int(self._clock())
        flag = gw.load_live_trading_flag(None, self._owner_key, signer_id=self.config.signer_id,
                                         network=self.config.network, now=now)
        try:
            gw.SubmissionGateway(flag, owner_public_key=self._owner_key, transport=None, ledger=self._ledger,
                                 audit=self._signer_audit, odds_ladder_step_size=self._meta["odds_ladder_step_size"],
                                 network=self.config.network, clock=self._clock)
        except gw.GatewayDisabled as exc:
            return str(exc)
        raise RuntimeError("GATEWAY_UNEXPECTEDLY_ENABLED")  # unreachable by construction; never send anything

    # ---------------------------------------------------------------- passkey enrolment (owner, on the host)
    def issue_enrollment_code(self) -> str:
        return issue_enrollment_code(self.store, self._clock)

    def enroll_options(self, body: Any) -> dict[str, Any]:
        code = body.get("enrollment_code") if isinstance(body, Mapping) else None
        if not isinstance(code, str) or not self.store.enrollment_code_valid(_canon_sha(code), int(self._clock())):
            self.refusals.audit("ENROLLMENT_REFUSED", reason="ENROLLMENT_CODE_INVALID")
            raise ServiceError("ENROLLMENT_CODE_INVALID", 403)
        now = int(self._clock())
        with self._lock:
            self._reg_challenges = {k: v for k, v in self._reg_challenges.items() if v[2] > now}
            cid = secrets.token_hex(16)
            challenge = secrets.token_bytes(32)
            self._reg_challenges[cid] = (challenge, _canon_sha(code), now + REGISTRATION_CHALLENGE_TTL_SECONDS)
        return {"challenge_id": cid, "challenge": wa.b64url_encode(challenge), "rp": {"id": self.policy.rp_id,
                "name": "FCB signer (DEV)"}, "user": {"id": wa.b64url_encode(b"fcb-owner"), "name": "owner",
                "displayName": "Owner"}, "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
                "authenticatorSelection": {"userVerification": "required", "residentKey": "required",
                                           "requireResidentKey": True},
                "attestation": "none", "timeout": 60000}

    def enroll_finish(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, Mapping) or set(body) != {"challenge_id", "attestation_object", "client_data_json"}:
            raise ServiceError("ENROLLMENT_MALFORMED")
        with self._lock:
            entry = self._reg_challenges.pop(body["challenge_id"], None) if isinstance(body["challenge_id"], str) else None
            now = int(self._clock())
            if entry is None or entry[2] <= now:
                raise ServiceError("ENROLLMENT_CHALLENGE_UNKNOWN", 403)
            challenge, code_hash, _ = entry
            try:
                cred = wa.verify_registration(attestation_object=wa.b64url_decode(body["attestation_object"]),
                                              client_data_json=wa.b64url_decode(body["client_data_json"]),
                                              expected_challenge=challenge, policy=self.policy)
            except appr.ApprovalError as exc:
                self.refusals.audit("ENROLLMENT_REFUSED", reason=str(exc))
                raise ServiceError(str(exc), 403) from None
            if not self.store.enrollment_code_valid(code_hash, now) or not self.store.consume_enrollment_code(code_hash):
                raise ServiceError("ENROLLMENT_CODE_INVALID", 403)
            try:
                self.store.add_credential(cred)
            except st.StoreError as exc:
                raise ServiceError(exc.code, 409) from None
            prefix = wa.b64url_encode(cred.credential_id)[:8]
            self.store.audit("CREDENTIAL_ENROLLED", detail={"credential_prefix": prefix})
            return {"enrolled": True, "credential_prefix": prefix}

    # ---------------------------------------------------------------- helpers
    def _load(self, request_id: Any) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ServiceError("REQUEST_NOT_FOUND", 404)
        r = self.store.get(request_id)
        if r is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404)
        return r

    def _expire_if_due(self, r: dict[str, Any]) -> dict[str, Any]:
        if r["state"] == st.PENDING and int(self._clock()) >= r["expires_at"]:
            try:
                self.store.transition(r["request_id"], st.EXPIRED, reason="REQUEST_EXPIRED", expect=st.PENDING)
            except st.StoreError:
                pass  # someone else moved it first; re-read below
            return self.store.get(r["request_id"])
        return r

    def sweep_expired(self) -> int:
        n = 0
        for rid in self.store.non_terminal():
            if self._expire_if_due(self.store.get(rid))["state"] == st.EXPIRED:
                n += 1
        return n

    def _move(self, rid, state, reason, detail, expect=None) -> None:
        try:
            self.store.transition(rid, state, reason=reason, detail=detail, expect=expect)
        except st.StoreError as exc:
            raise ServiceError("REQUEST_NOT_PENDING" if exc.code == "STATE_CHANGED" else exc.code, 409) from None

    # ---------------------------------------------------------------- validation and record building
    def _common(self, payload: Any, keys: frozenset[str], required: frozenset[str]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or not required <= set(payload) <= keys:
            raise ServiceError("REQUEST_KEYS_INVALID", 422)
        if not isinstance(payload["client_request_id"], str) or not _CLIENT_REQ.fullmatch(payload["client_request_id"]):
            raise ServiceError("CLIENT_REQUEST_ID_INVALID", 422)
        if payload["network"] != self.config.network:
            raise ServiceError("NETWORK_NOT_ALLOWED", 422)
        return dict(payload)

    def _validate_order(self, payload: Any) -> dict[str, Any]:
        p = self._common(payload, ORDER_KEYS, ORDER_KEYS)
        if not isinstance(p["market_hash"], str) or not _HEX32.fullmatch(p["market_hash"]):
            raise ServiceError("MARKET_HASH_INVALID", 422)
        if p["outcome"] not in (1, 2) or type(p["outcome"]) is not int:
            raise ServiceError("OUTCOME_INVALID", 422)
        for k in ("stake_base_units", "percentage_odds_limit"):
            if not isinstance(p[k], str) or not _DECIMAL.fullmatch(p[k]):
                raise ServiceError(k.upper() + "_INVALID", 422)
        if not 0 < int(p["percentage_odds_limit"]) < appr.ODDS_ONE:
            raise ServiceError("PERCENTAGE_ODDS_LIMIT_INVALID", 422)
        if p["time_in_force"] not in ("GTC", "IOC", "FOK"):
            raise ServiceError("TIME_IN_FORCE_INVALID", 422)
        if type(p["order_ttl_seconds"]) is not int or not 60 <= p["order_ttl_seconds"] <= 3600:
            raise ServiceError("ORDER_TTL_INVALID", 422)
        return p

    def _validate_cancel(self, payload: Any) -> dict[str, Any]:
        p = self._common(payload, CANCEL_KEYS, frozenset({"client_request_id", "network", "scope"}))
        scope = p["scope"]
        if scope not in cx.SCOPES:
            raise ServiceError("CANCEL_SCOPE_INVALID", 422)
        allowed = {cx.SCOPE_ORDER_IDS: {"order_ids"}, cx.SCOPE_MARKET: {"market_hash"},
                   cx.SCOPE_EVENT: {"event_id"}, cx.SCOPE_ALL: set()}[scope]
        if set(p) - {"client_request_id", "network", "scope"} != allowed:
            raise ServiceError("CANCEL_TARGET_INVALID", 422)
        if scope == cx.SCOPE_ORDER_IDS:
            ids = p["order_ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids) \
                    or not all(isinstance(i, str) and _HEX32.fullmatch(i) for i in ids):
                raise ServiceError("CANCEL_TARGET_INVALID", 422)
            p["order_ids"] = sorted(ids)
        if scope == cx.SCOPE_MARKET and (not isinstance(p["market_hash"], str) or not _HEX32.fullmatch(p["market_hash"])):
            raise ServiceError("CANCEL_TARGET_INVALID", 422)
        if scope == cx.SCOPE_EVENT and (not isinstance(p["event_id"], str) or not _EVENT.fullmatch(p["event_id"])):
            raise ServiceError("CANCEL_TARGET_INVALID", 422)
        return p

    def _envelope(self, now: int) -> dict[str, Any]:
        return {"nonce": "apn_" + secrets.token_urlsafe(24), "issued_at": now,
                "expires_at": now + self.config.request_ttl_seconds, "signer_id": self.config.signer_id}

    def _build_order_record(self, p: Mapping[str, Any], request_id: str, now: int):
        m = self._market.market(p["market_hash"])
        if m.get("status") != "ACTIVE":
            raise ServiceError("MARKET_NOT_ACTIVE")
        outcome = p["outcome"]
        quote = self._market.quote(p["market_hash"], outcome)
        quoted = int(quote["best_percentage_odds"])
        meta = self._meta
        requested = int(p["percentage_odds_limit"])
        odds = _round_down(requested, meta["odds_ladder_step_size"])
        if odds <= 0:
            raise ServiceError("PERCENTAGE_ODDS_LIMIT_INVALID")
        intent = {
            "schema": INTENT_SCHEMA, "network": self.config.network, "chain_id": meta["chain_id"],
            "domain_name": "OBv3 Escrow", "domain_version": "1", "verifying_contract": meta["verifying_contract"],
            "base_token": meta["base_token"], "market_hash": p["market_hash"], "maker": self.signer_address,
            "is_maker_betting_outcome_one": outcome == 1, "total_bet_size": p["stake_base_units"],
            "percentage_odds": str(odds), "requested_percentage_odds": str(requested),
            "salt": str(secrets.randbits(256)), "expiry": now + p["order_ttl_seconds"],
            "time_in_force": p["time_in_force"], "idempotency_key": "fcb" + request_id[4:28]}
        intent = validate(intent, odds_ladder_step_size=meta["odds_ladder_step_size"])
        label = m.get("outcomeOneName" if outcome == 1 else "outcomeTwoName")
        binding = appr.build_binding(intent, account_id=self.config.account_id or "UNCONFIGURED",
                                     event_id=str(m.get("sportXeventId")), selection_label=str(label),
                                     expected_fees=self._market.fees(),
                                     max_exposure_base_units=int(p["stake_base_units"]),
                                     quoted_percentage_odds=quoted, intent_hash=intent_hash(intent))
        if not appr.binding_well_formed(binding):
            raise ServiceError("BINDING_NOT_BUILDABLE")
        display = appr.build_display(binding, operation_id=request_id, event_label=str(m.get("eventLabel")),
                                     market_label=str(m.get("marketLabel")), data_source=self._market.data_source)
        record = {"intent": intent, "binding": binding, "display": display, "approval": self._envelope(now)}
        return record, [(st.REQUESTED, None), (st.PENDING, None)]

    def _build_cancel_record(self, p: Mapping[str, Any], request_id: str, now: int):
        binding = cx.build_cancel_binding(account_id=self.config.account_id or "UNCONFIGURED",
                                          maker=self.signer_address, network=self.config.network, scope=p["scope"],
                                          order_ids=p.get("order_ids", ()), market_hash=p.get("market_hash"),
                                          event_id=p.get("event_id"))
        effect = {cx.SCOPE_ORDER_IDS: "CANCEL_LISTED_ORDERS", cx.SCOPE_MARKET: "CANCEL_ALL_ORDERS_IN_MARKET",
                  cx.SCOPE_EVENT: "CANCEL_ALL_ORDERS_IN_EVENT", cx.SCOPE_ALL: "CANCEL_EVERY_OPEN_ORDER"}[p["scope"]]
        display = {"schema": CANCEL_DISPLAY_SCHEMA, "operation_id": request_id,
                   "data_source": self._market.data_source, "effect": effect,
                   **{k: v for k, v in binding.items() if k != "schema"}}
        record = {"binding": binding, "display": display, "approval": self._envelope(now)}
        return record, [(st.REQUESTED, None), (st.PENDING, None)]
