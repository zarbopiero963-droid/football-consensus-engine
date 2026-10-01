"""Bounded content readiness, shared by pilots; source parsers own semantics.

No navigation retries, inference, promotion or timezone/matching decisions.
Only the final HTML is a stored raw; sample hashes describe visible text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time


def page_state(http_status, title, text):
    """Recognize strong page-level signals, never incidental football words."""
    lines = {line.strip().casefold() for line in text.splitlines() if line.strip()}
    title = title.strip().casefold()
    if ("under maintenance" in lines or title.endswith("under maintenance") or
            any(re.fullmatch(r".{0,100}\bis undergoing scheduled maintenance\.?", line)
                for line in lines)):
        return "MAINTENANCE"
    if title in {"just a moment...", "access denied", "attention required! | cloudflare"}:
        return "ACCESS_BLOCKED"
    if http_status != 200:
        return "HTTP_UNOBSERVED" if http_status is None else "HTTP_ERROR"
    if any(re.fullmatch(r"loading(?: data| matches| results)?(?:\.{3}|…)?", line)
           for line in lines):
        return "LOADING"
    return "CONTENT"


@dataclass
class PageCapture:
    raw_text: str
    html: bytes
    parsed: object
    audit: dict

    @property
    def accepted(self):
        return self.audit["state"] == "READY_PARSED"

    @property
    def text(self):
        # Existing adapters may reparse accepted text, never rejected shells.
        return self.raw_text if self.accepted else ""


def capture_page(page, http_status, parser, *, timeout_ms=20000, poll_ms=1000,
                 evidence_kind="source_rows", monotonic=time.monotonic):
    """Wait for two identical nonempty parser outputs with no Loading signal.

    A zero-row page remains EMPTY_UNCONFIRMED, not a proven layout regression.
    parser=None is a capability-only capture, never normalized match data.
    Parser exceptions terminate polling and quarantine the content, preserving
    final raw and a safe exception class. This does not retry failed navigation.
    """
    if timeout_ms < 0 or poll_ms <= 0:
        raise ValueError("invalid readiness budget")
    start = monotonic()
    samples = []
    previous = None
    parsed = []
    state = "EMPTY_UNCONFIRMED"
    parser_error = None
    capture_error = None
    text = ""
    while True:
        remaining = max(1, int(timeout_ms - (monotonic() - start) * 1000))
        try:
            text = page.locator("body").inner_text(timeout=min(2000, remaining))
            title = page.title()
        except Exception as exc:
            capture_error = type(exc).__name__
            state = "CAPTURE_ERROR"
            break
        signal = page_state(http_status, title, text)
        at = datetime.now(timezone.utc).isoformat()
        sample = {"at_utc": at, "state": signal,
                  "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        samples.append(sample)
        if signal not in {"CONTENT", "LOADING"}:
            state = signal
            break
        if parser is None and signal == "CONTENT":
            state = "CAPABILITY_ONLY"
            break
        try:
            candidate = parser(text) if parser is not None else []
            if not isinstance(candidate, (list, dict)):
                raise TypeError("readiness parser must return rows or a mapping")
            fingerprint = hashlib.sha256(json.dumps(
                candidate, sort_keys=True, default=str, allow_nan=False
            ).encode()).hexdigest()
        except Exception as exc:
            parser_error = type(exc).__name__
            state = "PARSER_ERROR"
            break
        sample["parsed_count"] = len(candidate)
        sample["parsed_sha256"] = fingerprint
        if signal == "CONTENT" and candidate:
            if previous == fingerprint:
                parsed = candidate
                state = "READY_PARSED"
                break
            previous = fingerprint
            state = "UNSTABLE_TIMEOUT"
        else:
            previous = None
            state = "LOADING_TIMEOUT" if signal == "LOADING" else "EMPTY_UNCONFIRMED"
        elapsed = (monotonic() - start) * 1000
        if elapsed >= timeout_ms:
            break
        try:
            page.wait_for_timeout(min(poll_ms, timeout_ms - elapsed))
        except Exception as exc:
            capture_error = type(exc).__name__
            state = "CAPTURE_ERROR"
            break
    try:
        html = page.content().encode("utf-8")
    except Exception as exc:
        html = b""
        capture_error = type(exc).__name__
        state = "CAPTURE_ERROR"
    if state != "READY_PARSED":
        parsed = []
    audit = {"contract": "PILOT_CONTENT_READINESS_V1", "state": state,
             "http_status": http_status, "evidence_kind": evidence_kind,
             "readiness_budget_ms": timeout_ms, "samples": samples,
             "captured_at_utc": datetime.now(timezone.utc).isoformat(),
             "parser_error_class": parser_error,
             "capture_error_class": capture_error}
    return PageCapture(text, html, parsed, audit)
