"""Kill switch (governance §2.6, owner decision D7 / #187 decision 3). Risk-REDUCING only; never sends here.

Engaging:
- by the owner or an operator (`engage`): engaging only reduces risk, so it needs no owner signature;
- AUTOMATICALLY (`evaluate_automatic`) under deterministic, certified conditions only:
    DAILY_LOSS_LIMIT_BREACHED   realised loss of the UTC day (ledger settlements) > 0 and >= the owner-signed
                                `max_daily_loss_base_units`;
    EXPOSURE_ABOVE_LIMIT        ledger exposure > 0 and > the owner-signed `max_total_exposure_base_units`
                                (e.g. late fills discovered by reconciliation);
    RECONCILIATION_MISMATCH     the exchange shows orders of our maker the ledger does not know
                                (`foreign_order_ids`) or an exchange observation contradicts the ledger
                                (`evidence_conflicts`, the monitor's EVIDENCE_CONFLICT alerts);
    LEDGER_ANCHOR_FORK          the ledger reports a fork / fencing / head anomaly, or is poisoned.
  Same inputs => same decision; nothing is inferred.

Effects (all recorded in the append-only ledger, so they survive a restart, and in the audit log):
1. KILL_SWITCH ENGAGE line: from then on `Signer.sign_order` refuses (`KILL_SWITCH_ENGAGED`) and the gateway
   refuses every submission (`KILL_SWITCH_ENGAGED`); reconciliation reads continue;
2. KILL_SWITCH CANCEL_INTENT line: the ids of every order of ours that may be on the book (SUBMITTED, OPEN,
   PARTIALLY_FILLED, UNKNOWN, a REJECTED with a live hold). These are cancel INTENTS only: this module has no
   transport, signs nothing and creates no exposure. The cancels are SENT by the sender PR (owner-reserved),
   on this exact list and only to reduce risk;
3. `verify_cancellations`: after reconciliation (gateway / monitor reads applied to the ledger) records in a
   KILL_SWITCH CANCEL_VERIFIED line, per intended order, whether it is now off the book (CONFIRMED: CANCELLED,
   FILLED, EXPIRED, SETTLED, or a REJECTED without hold) or STILL_OPEN. A cancel reply is never trusted.

If the ledger cannot be written (anomaly / poisoned), the kill switch is engaged in memory and audited; the
signer already refuses everything on a poisoned ledger, and a restart on a forked ledger is refused until
the owner decides (signer/anchor.py).

Release: ONLY by an owner-authenticated act outside the MCP: a `KillSwitchRelease` signed with the owner's
Ed25519 key (domain `sxbet.kill_switch_release.v1`) over {ledger_id, engage_seq, release_id, issued_at,
expires_at}, TTL <= 300 s, verified with the owner public key from the signer's own configuration. It is
bound to the ENGAGE line it releases (a later engagement needs a new release) and its id is single use.
An automatic condition that still holds re-engages at the next evaluation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .audit import AuditLog
from .orders import (CANCELLED, EXPIRED, FILLED, OPEN, PARTIALLY_FILLED, REJECTED, SETTLED, SUBMITTED, UNKNOWN,
                     LedgerError, OrderLedger, OrderStateError, utc_day_start)

RELEASE_DOMAIN = "sxbet.kill_switch_release.v1"
MAX_RELEASE_TTL_SECONDS = 300
CLOCK_SKEW_SECONDS = 5
DAILY_LOSS_LIMIT_BREACHED = "DAILY_LOSS_LIMIT_BREACHED"
EXPOSURE_ABOVE_LIMIT = "EXPOSURE_ABOVE_LIMIT"
RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
LEDGER_ANCHOR_FORK = "LEDGER_ANCHOR_FORK"
AUTOMATIC_TRIGGERS = (DAILY_LOSS_LIMIT_BREACHED, EXPOSURE_ABOVE_LIMIT, RECONCILIATION_MISMATCH, LEDGER_ANCHOR_FORK)
LEDGER_ANOMALIES = frozenset({"LEDGER_ANCHOR_HASH_MISMATCH", "LEDGER_ANCHOR_AHEAD_OF_LOCAL", "LEDGER_FENCED",
                              "LEDGER_HEAD_MISMATCH", "LEDGER_ANCHOR_CONFLICT", "LEDGER_POISONED",
                              "LEDGER_ANCHOR_WRITE_FAILED"})
_CANCELLABLE = frozenset({SUBMITTED, OPEN, PARTIALLY_FILLED, UNKNOWN})
_OFF_BOOK = frozenset({CANCELLED, FILLED, EXPIRED, SETTLED})
_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TRIGGER = re.compile(r"^[A-Z0-9_]{1,64}$")


class KillSwitchError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReconciliationFindings:
    """Deterministic inputs from reconciliation (gateway / monitor / cancellation reports)."""
    foreign_order_ids: tuple[str, ...] = ()
    evidence_conflicts: int = 0

    @classmethod
    def from_monitor_report(cls, report: Mapping[str, Any]) -> "ReconciliationFindings":
        alerts = report.get("alerts", []) if isinstance(report, Mapping) else []
        return cls(evidence_conflicts=sum(1 for a in alerts if getattr(a, "code", None) == "EVIDENCE_CONFLICT"))


@dataclass(frozen=True)
class KillSwitchRelease:
    ledger_id: str
    engage_seq: int
    release_id: str
    issued_at: int
    expires_at: int
    signature: bytes  # 64-byte Ed25519 over release_message(...)


def release_message(*, ledger_id: str, engage_seq: int, release_id: str, issued_at: int, expires_at: int) -> bytes:
    return json.dumps({"domain": RELEASE_DOMAIN, "ledger_id": ledger_id, "engage_seq": engage_seq,
                       "release_id": release_id, "issued_at": issued_at, "expires_at": expires_at},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def load_release_file(path: str) -> KillSwitchRelease:
    """An owner-signed release file: {ledger_id, engage_seq, release_id, issued_at, expires_at, signature(hex)}."""
    try:
        with open(path, encoding="ascii") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or set(doc) != {"ledger_id", "engage_seq", "release_id", "issued_at",
                                                      "expires_at", "signature"}:
            raise ValueError
        return KillSwitchRelease(doc["ledger_id"], doc["engage_seq"], doc["release_id"], doc["issued_at"],
                                 doc["expires_at"], bytes.fromhex(doc["signature"]))
    except (OSError, ValueError, TypeError):
        raise KillSwitchError("KILL_SWITCH_RELEASE_MALFORMED") from None


class KillSwitch:
    def __init__(self, ledger: OrderLedger, audit: AuditLog, *, owner_public_key: Ed25519PublicKey, network: str,
                 clock: Callable[[], float] = time.time):
        if not isinstance(ledger, OrderLedger):
            raise KillSwitchError("KILL_SWITCH_LEDGER_INVALID")
        if not isinstance(owner_public_key, Ed25519PublicKey):
            raise KillSwitchError("OWNER_PUBLIC_KEY_INVALID")
        self._ledger = ledger
        self._audit = audit
        self._owner_key = owner_public_key
        self._network = network
        self._clock = clock
        self._volatile: str | None = None  # engaged while the ledger could not record it

    # ---- state ----
    @property
    def engaged(self) -> bool:
        return self._volatile is not None or self._ledger.kill_switch_engaged

    def state(self) -> dict[str, Any]:
        st = self._ledger.kill_switch_state()
        st["volatile_trigger"] = self._volatile
        st["engaged"] = self.engaged
        return st

    def _log(self, decision: str, *, reason: str | None = None, order_id: str | None = None,
             bucket: str | None = None) -> None:
        self._audit.append(event="KILL_SWITCH", decision=decision, reason=reason, signer_id=self._ledger.signer_id,
                           network=self._network, order_id=order_id, bucket=bucket)

    # ---- engage ----
    def cancel_intents(self) -> list[str]:
        """Order ids of ours that may still be on the book: the only ones a cancel can affect (risk-reducing)."""
        now = int(self._clock())
        return sorted(o.order_id for o in self._ledger.orders()
                      if o.state in _CANCELLABLE or (o.state == REJECTED and o.rejected_hold_active(now)))

    def engage(self, trigger: str, *, source: str = "MANUAL", triggers: Sequence[str] = ()) -> dict[str, Any]:
        if not isinstance(trigger, str) or not _TRIGGER.fullmatch(trigger) or source not in ("MANUAL", "AUTOMATIC"):
            raise KillSwitchError("KILL_SWITCH_TRIGGER_INVALID")
        all_triggers = sorted({trigger, *triggers})
        intents = self.cancel_intents()
        try:
            self._ledger.record_kill_switch("ENGAGE", trigger=trigger, source=source, triggers=all_triggers)
            self._ledger.record_kill_switch("CANCEL_INTENT", order_ids=intents)
            persisted = True
        except (LedgerError, OrderStateError) as exc:
            self._volatile = trigger
            self._log("KILL_SWITCH_ENGAGED_LEDGER_UNWRITABLE", reason=getattr(exc, "code", "LEDGER_ERROR"))
            persisted = False
        self._log("KILL_SWITCH_ENGAGED", reason=trigger, bucket=source)
        for oid in intents:
            self._log("KILL_SWITCH_CANCEL_INTENT", reason=trigger, order_id=oid, bucket="NOT_SENT_NO_SENDER")
        return {"engaged": True, "trigger": trigger, "triggers": all_triggers, "source": source,
                "persisted": persisted, "cancel_intents": intents, "cancels_sent": False,
                "note": "CANCEL_INTENTS_RECORDED_SENDING_REQUIRES_THE_SENDER_PR"}

    def evaluate_automatic(self, limits: Any, *, findings: ReconciliationFindings | None = None,
                           ledger_anomaly: str | None = None) -> dict[str, Any] | None:
        """Deterministic conditions; engages (once per engagement) when any holds. Returns the engage report."""
        now = int(self._clock())
        hits: list[str] = []
        if ledger_anomaly in LEDGER_ANOMALIES or self._ledger.poisoned:
            hits.append(LEDGER_ANCHOR_FORK)
        loss = max(0, -self._ledger.realized_pnl_since(utc_day_start(now)))
        max_loss = getattr(limits, "max_daily_loss_base_units", 0)
        if loss > 0 and loss >= max_loss:
            hits.append(DAILY_LOSS_LIMIT_BREACHED)
        total = self._ledger.exposure().total
        if total > 0 and total > getattr(limits, "max_total_exposure_base_units", 0):
            hits.append(EXPOSURE_ABOVE_LIMIT)
        f = findings or ReconciliationFindings()
        if f.foreign_order_ids or f.evidence_conflicts > 0:
            hits.append(RECONCILIATION_MISMATCH)
        if not hits or self.engaged:
            return None
        return self.engage(hits[0], source="AUTOMATIC", triggers=hits)

    # ---- verify what the (future) cancels actually achieved ----
    def verify_cancellations(self) -> dict[str, str]:
        """Read back, from the ledger AFTER reconciliation, which intended cancels are off the book."""
        st = self._ledger.kill_switch_state()
        if not st["engaged"]:
            raise KillSwitchError("KILL_SWITCH_NOT_ENGAGED")
        now = int(self._clock())
        results: dict[str, str] = {}
        for oid in st["intent_ids"]:
            o = self._ledger.by_order_id(oid)
            off = o is not None and (o.state in _OFF_BOOK or (o.state == REJECTED and not o.rejected_hold_active(now)))
            results[oid] = "CONFIRMED" if off else "STILL_OPEN"
        self._ledger.record_kill_switch("CANCEL_VERIFIED", results=results)
        for oid, r in sorted(results.items()):
            self._log("KILL_SWITCH_CANCEL_" + r, order_id=oid, bucket=r)
        return results

    # ---- release: owner-signed, outside the MCP ----
    def release(self, rel: Any) -> dict[str, Any]:
        st = self._ledger.kill_switch_state()
        if not st["engaged"]:
            raise KillSwitchError("KILL_SWITCH_NOT_ENGAGED")
        if not isinstance(rel, KillSwitchRelease):
            self._log("KILL_SWITCH_RELEASE_REFUSED", reason="KILL_SWITCH_RELEASE_MISSING")
            raise KillSwitchError("KILL_SWITCH_RELEASE_MISSING")
        try:
            self._verify_release(rel, st, int(self._clock()))
            self._ledger.record_kill_switch("RELEASE", release_id=rel.release_id,
                                            release_sha256="sha256:" + _sha(rel), engage_seq=rel.engage_seq)
        except KillSwitchError as exc:
            self._log("KILL_SWITCH_RELEASE_REFUSED", reason=exc.code)
            raise
        except OrderStateError as exc:
            self._log("KILL_SWITCH_RELEASE_REFUSED", reason=exc.code)
            raise KillSwitchError(exc.code) from None
        self._volatile = None
        self._log("KILL_SWITCH_RELEASED", reason="OWNER_SIGNED_RELEASE")
        return {"engaged": False, "released_engage_seq": rel.engage_seq}

    def _verify_release(self, rel: KillSwitchRelease, st: Mapping[str, Any], now: int) -> None:
        if not (isinstance(rel.ledger_id, str) and type(rel.engage_seq) is int and isinstance(rel.release_id, str)
                and _ID.fullmatch(rel.release_id) and type(rel.issued_at) is int and type(rel.expires_at) is int
                and isinstance(rel.signature, (bytes, bytearray)) and len(rel.signature) == 64):
            raise KillSwitchError("KILL_SWITCH_RELEASE_MALFORMED")
        if rel.expires_at <= rel.issued_at or rel.expires_at - rel.issued_at > MAX_RELEASE_TTL_SECONDS:
            raise KillSwitchError("KILL_SWITCH_RELEASE_TTL_TOO_LONG")
        if rel.issued_at > now + CLOCK_SKEW_SECONDS:
            raise KillSwitchError("KILL_SWITCH_RELEASE_NOT_YET_VALID")
        if now >= rel.expires_at:
            raise KillSwitchError("KILL_SWITCH_RELEASE_EXPIRED")
        msg = release_message(ledger_id=rel.ledger_id, engage_seq=rel.engage_seq, release_id=rel.release_id,
                              issued_at=rel.issued_at, expires_at=rel.expires_at)
        try:
            self._owner_key.verify(bytes(rel.signature), msg)
        except (InvalidSignature, TypeError, ValueError):
            raise KillSwitchError("KILL_SWITCH_RELEASE_SIGNATURE_INVALID") from None
        if rel.ledger_id != self._ledger.ledger_id or rel.engage_seq != st["engage_seq"]:
            raise KillSwitchError("KILL_SWITCH_RELEASE_MISMATCH")
        if self._ledger.nonce_consumed(rel.release_id):
            raise KillSwitchError("KILL_SWITCH_RELEASE_REPLAYED")


def _sha(rel: KillSwitchRelease) -> str:
    return hashlib.sha256(release_message(ledger_id=rel.ledger_id, engage_seq=rel.engage_seq,
                                          release_id=rel.release_id, issued_at=rel.issued_at,
                                          expires_at=rel.expires_at) + bytes(rel.signature)).hexdigest()


__all__ = ["AUTOMATIC_TRIGGERS", "KillSwitch", "KillSwitchError", "KillSwitchRelease", "ReconciliationFindings",
           "load_release_file", "release_message"]
