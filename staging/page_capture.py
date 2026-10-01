"""Bounded content readiness, shared by pilots; source parsers own semantics.

No navigation retries, inference, promotion or timezone/matching decisions.
Only the final HTML is a stored raw; sample hashes describe visible text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import base64
import gzip
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


RETRY_DELAYS_SECONDS = (3, 10)
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


def retry_after_seconds(value, now):
    """Parse only the public Retry-After field; malformed values stay unknown."""
    if not value:
        return None
    try:
        if re.fullmatch(r"\d+", value.strip()):
            seconds = int(value)
        else:
            at = parsedate_to_datetime(value)
            if at.tzinfo is None:
                return None
            seconds = max(0, (at - now).total_seconds())
        # Do not convert unbounded attacker-controlled integers to a sleep/date.
        return seconds if seconds <= 50 * 365 * 86400 else None
    except (ValueError, TypeError, OverflowError):
        return None


def navigate_capture(page, url, parser, *, evidence_kind="source_rows",
                     budget_ms=210000, monotonic=time.monotonic,
                     capture=capture_page):
    """Retry known public document GETs, never browser creation or ingestion.

    At most three navigations in the same browser with 3/10s minimum backoff;
    valid Retry-After is honored, or the retry is deferred beyond this budget.
    Failed retry attempts retain compressed raw and true acquisition timestamps.
    HTTP/Loading timeouts may retry; maintenance/access/parser/empty/unstable do
    not. Final capture time is never the time of the first navigation.
    """
    from playwright.sync_api import TimeoutError as NavigationTimeout
    budget_ms = min(budget_ms, 210000)
    start = monotonic()
    attempts = []
    if budget_ms <= 0:
        return PageCapture("", b"", [], {"state": "BUDGET_EXHAUSTED", "http_status": None,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "navigation_contract": "PUBLIC_GET_RETRY_V1", "navigation_attempts": [],
            "navigation_budget_ms": budget_ms, "evidence_kind": evidence_kind})
    for index in range(len(RETRY_DELAYS_SECONDS) + 1):
        response = None
        error = None
        try:
            response = page.goto(url, wait_until="domcontentloaded",
                                 timeout=max(1, min(45000, int(budget_ms - (monotonic() - start) * 1000))))
        except (NavigationTimeout, TimeoutError) as exc:
            error = type(exc).__name__
        result = capture(page, response.status if response else None,
                         parser if response else None, evidence_kind=evidence_kind,
                         timeout_ms=max(0, min(20000, int(budget_ms - (monotonic() - start) * 1000))))
        attempt = {"attempt": index + 1, "http_status": result.audit["http_status"],
                   "state": result.audit["state"], "navigation_error_class": error,
                   "captured_at_utc": result.audit["captured_at_utc"],
                   "html_sha256": hashlib.sha256(result.html).hexdigest(),
                   "html_bytes": len(result.html), "raw_storage": "FINAL_HTML"}
        attempts.append(attempt)
        retryable = (error is not None or result.audit["state"] == "LOADING_TIMEOUT" or
                     (result.audit["state"] == "HTTP_ERROR" and result.audit["http_status"] in RETRYABLE_HTTP))
        if result.audit["state"] in {"MAINTENANCE", "ACCESS_BLOCKED", "PARSER_ERROR"}:
            retryable = False
        if not retryable or index == len(RETRY_DELAYS_SECONDS):
            break
        delay = RETRY_DELAYS_SECONDS[index]
        if result.audit["http_status"] == 429:
            delay = max(delay, 30)
        retry_after = None
        if response is not None:
            # No headers/cookies are copied into the audit.
            try:
                header = response.header_value("retry-after")
                retry_after = retry_after_seconds(header, datetime.now(timezone.utc))
                if header and retry_after is None:
                    attempt["retry_deferred"] = "INVALID_RETRY_AFTER"
                    break
            except Exception as exc:
                attempt["retry_deferred"] = "RETRY_AFTER_UNOBSERVED"
                attempt["header_error_class"] = type(exc).__name__
                break
        if retry_after is not None:
            delay = max(delay, retry_after)
            attempt["retry_after_seconds"] = retry_after
        remaining = budget_ms - (monotonic() - start) * 1000
        if delay * 1000 >= remaining:
            attempt["retry_deferred"] = "BUDGET_OR_RETRY_AFTER"
            break
        # Persist full failed attempt raw for replay; avoid unbounded audit growth.
        if len(result.html) > 5 * 1024 * 1024:
            attempt["retry_deferred"] = "RAW_SIZE_LIMIT"
            break
        attempt["raw_storage"] = "ATTEMPT_GZIP_BASE64"
        attempt["html_gzip_base64"] = base64.b64encode(gzip.compress(result.html, mtime=0)).decode()
        attempt["backoff_seconds"] = delay
        page.wait_for_timeout(delay * 1000)
    result.audit["navigation_contract"] = "PUBLIC_GET_RETRY_V1"
    result.audit["navigation_attempts"] = attempts
    result.audit["navigation_budget_ms"] = budget_ms
    return result


class BrowserStopError(RuntimeError):
    pass


def confirmed_browser_stop(browser_api, base_url, session_id):
    """Do not ingest a source run while its provider browser is still running."""
    status, stopped = browser_api("PATCH", f"{base_url}/{session_id}", {"action": "stop"})
    if status in (404, 410):
        return {"status": "stopped", "provider_missing": True}
    if status not in (200, 201, 204):
        raise BrowserStopError("provider stop HTTP failure")
    status, session = browser_api("GET", f"{base_url}/{session_id}")
    if status in (404, 410):
        return {"status": "stopped", "provider_missing": True}
    if status != 200 or session.get("status") != "stopped":
        raise BrowserStopError("provider stop unconfirmed")
    return session
