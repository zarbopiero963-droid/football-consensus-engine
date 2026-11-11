"""FIX-01 (owner option B): bounded streaming scan of provider replies above the read cap.

The body is never held in memory. Each chunk is hashed (SHA-256 of the raw bytes), checked as
strict UTF-8 and fed to a small, iterative JSON tokenizer (regular expressions over bytes, an
explicit stack, no recursion, no eval). Only what continuation needs is kept: per row of the
top-level `response` list the configured fields (e.g. fixture.id, league.id/season/round) as
small scalars and the row's raw byte length, plus `get`, `results`, `paging` and the KEYS of a
non-empty `errors`. Provider values are never kept beyond those scalars and never returned.

The whole document is validated (grammar, UTF-8, a single top-level object, no trailing data,
no duplicate top-level keys); anything else is refused with a fixed code. Finite bounds:

  max_bytes   16 MiB of body (the provider stream is closed as soon as it is passed)
  max_seconds 20 s of scanning (the request timeout of the resilience layer applies first)
  max_rows    20 000 rows (each row keeps a few small ints/strings: bounded memory)
  max_depth   32 nested containers
  max_token   64 KiB for a single VALID token (string or number; leading whitespace is not
              counted). An invalid string, literal or number is PROVIDER_INVALID_JSON as soon
              as the offending byte arrives, never a bound.
  64 distinct top-level keys (MAX_TOP_LEVEL_KEYS), else PROVIDER_REPLY_CONTRACT_INVALID; no
  other key set is kept (row fields are a fixed small set, error keys are capped at 64).

A field that is duplicated, not a scalar, a non-integer number or an unsafe/unread string is
never copied: the row gets an `invalid` flag for it, so no split or completeness can use it.

Speed (F-B05): inside a subtree the scan never reads, runs of short scalar members are consumed by
one regular expression (see _FAST_OBJECT_RUN); every bound, the grammar and the error codes are the
same as on the token-by-token path, which handles everything else.

A bound that is hit raises ScanBoundExceeded (PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN) with the
bound's name; no partial result is ever returned.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Any, Callable, Mapping

from .errors import ProviderDataError


@dataclass(frozen=True)
class ScanLimits:
    max_bytes: int = 16 * 1024 * 1024
    max_seconds: float = 20.0
    max_rows: int = 20_000
    max_depth: int = 32
    max_token: int = 65_536


DEFAULT_LIMITS = ScanLimits()


BOUND_NAMES = frozenset({"MAX_BYTES", "MAX_SECONDS", "MAX_ROWS", "MAX_DEPTH", "MAX_TOKEN"})


class ScanBoundExceeded(ProviderDataError):
    def __init__(self, bound: str):
        self.bound = bound
        super().__init__("PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN")


_STRING = rb'"([^"\\\x00-\x1f]*(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})[^"\\\x00-\x1f]*)*)"'
_NUMBER = rb"(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
_TOKEN = re.compile(rb"[ \t\n\r]*(?:([\[\]{},:])|" + _STRING + rb"|" + _NUMBER + rb"|(true|false|null))")
_WS = re.compile(rb"[ \t\n\r]*")
_STARTS = frozenset(b'"-0123456789tfn')
_INT = re.compile(rb"-?[0-9]{1,12}$")
_MAX_FIELD_TEXT = 128
_MAX_ERROR_KEYS = 64
# Documented bound: the provider envelope has 6 top-level keys; more than 64 distinct ones is
# not a provider reply (and an unbounded key set would be unbounded memory).
MAX_TOP_LEVEL_KEYS = 64
# A grouping string copied from a row: printable, no surrogates, bounded. Ids are ints only.
SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f\ud800-\udfff]{1,128}$")
_STRING_PREFIX = re.compile(rb'"[^"\\\x00-\x1f]*(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})[^"\\\x00-\x1f]*)*')
_ESCAPE_PREFIX = re.compile(rb"\\(?:u[0-9a-fA-F]{0,3})?")
_NUMBER_TAIL = re.compile(rb"[0-9.eE+-]*")
_LITERALS = (b"true", b"false", b"null")
# F-B05 fast path (a speed-up, never a relaxation): inside a subtree whose values the scan never
# reads (no row field below it, not `errors`, not `paging`, not the top level), a run of complete
# scalar members `"k":v,` (object) or values `v,` (array) is consumed by ONE regular expression
# instead of one Python step per token. It accepts exactly the same grammar as _TOKEN (strict JSON
# strings, numbers and literals) but only SHORT tokens, so no token it consumes could reach the
# token bound: anything longer, unfinished, invalid or unusual falls back to the token-by-token
# path, which applies every bound and error code unchanged. The final member of a run is consumed
# only when its closing bracket is already in the buffer (a number can never be cut short).
_FAST_STRING = rb'"[^"\\\x00-\x1f]{0,512}(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})[^"\\\x00-\x1f]{0,512}){0,16}"'
_FAST_NUMBER = rb"-?(?:0|[1-9][0-9]{0,31})(?:\.[0-9]{1,32})?(?:[eE][+-]?[0-9]{1,8})?"
_FAST_VALUE = rb"(?:" + _FAST_STRING + rb"|" + _FAST_NUMBER + rb"|true|false|null)"
_FAST_WS = rb"[ \t\n\r]*"
_FAST_MEMBER = _FAST_WS + _FAST_STRING + _FAST_WS + rb":" + _FAST_WS + _FAST_VALUE + _FAST_WS
_FAST_ITEM = _FAST_WS + _FAST_VALUE + _FAST_WS
_FAST_OBJECT_RUN = re.compile(rb"(?:" + _FAST_MEMBER + rb",)*" + _FAST_MEMBER + rb"(?:,|(?=\}))")
_FAST_ARRAY_RUN = re.compile(rb"(?:" + _FAST_ITEM + rb",)*" + _FAST_ITEM + rb"(?:,|(?=\]))")
# Longest token the fast path can consume: 17 x 512 plain bytes + 16 x 6-byte escapes + quotes.
FAST_TOKEN_MAX = 17 * 512 + 16 * 6 + 2


def _malformed() -> ProviderDataError:
    return ProviderDataError("PROVIDER_INVALID_JSON")


class _Frame:
    __slots__ = ("kind", "expect", "key", "seen", "skip")

    def __init__(self, kind: str, expect: str):
        self.kind, self.expect, self.key, self.seen = kind, expect, None, None
        self.skip = False  # True: nothing below this container is read (fast path allowed)


class IdScanner:
    def __init__(self, fields: Mapping[str, tuple[str, ...]], *, limits: ScanLimits = DEFAULT_LIMITS,
                 clock: Callable[[], float] = time.monotonic, deadline: float | None = None):
        self.fields = dict(fields)
        self.limits = limits
        self.clock = clock
        self.started = clock()
        # Absolute clock value set by the transport so a scan ends (non-retryable, MAX_SECONDS)
        # before the caller's request timeout would turn it into a retryable PROVIDER_TIMEOUT.
        self.deadline = deadline
        self.hasher = hashlib.sha256()
        self.utf8 = codecs.getincrementaldecoder("utf-8")("strict")
        self.buf = b""
        self.base = 0           # stream offset of buf[0]
        self.bytes = 0
        self.stack: list[_Frame] = []
        self.done = False
        self.top_keys: set[str] = set()
        self.get: str | None = None
        self.results: Any = None
        self.paging: dict[str, Any] = {}
        self.paging_kind: str | None = None
        self.response_seen = False
        self.errors_nonempty = False
        self.error_keys: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.row: dict[str, Any] | None = None
        self.row_invalid: set[str] = set()
        self.row_start = 0
        self._max_field_depth = max((len(p) for p in self.fields.values()), default=0)
        # The fast path only consumes tokens far below the token bound (see FAST_TOKEN_MAX).
        self._fast = limits.max_token >= FAST_TOKEN_MAX

    # ---- input --------------------------------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        if self.bytes > self.limits.max_bytes:
            raise ScanBoundExceeded("MAX_BYTES")
        self._check_time()
        self.hasher.update(chunk)
        try:
            self.utf8.decode(chunk)
        except UnicodeDecodeError:
            raise _malformed() from None
        self.buf = self.buf + chunk if self.buf else chunk
        self._run(eof=False)

    def finish(self) -> dict[str, Any]:
        try:
            self.utf8.decode(b"", final=True)
        except UnicodeDecodeError:
            raise _malformed() from None
        self._run(eof=True)
        if not self.done:
            raise _malformed()  # truncated stream
        if not self.response_seen:
            raise ProviderDataError("PROVIDER_RESPONSE_FIELD_MISSING")
        return {"mode": "STREAMED_SCAN", "bytes": self.bytes, "body_sha256": self.hasher.hexdigest(),
                "get": self.get, "results": self.results,
                "paging": dict(self.paging) if self.paging_kind == "object" else None,
                "paging_present": self.paging_kind is not None,
                "errors_nonempty": self.errors_nonempty, "error_keys": sorted(set(self.error_keys)),
                "rows": self.rows}

    def _check_time(self) -> None:
        now = self.clock()
        if now - self.started > self.limits.max_seconds or (self.deadline is not None and now > self.deadline):
            raise ScanBoundExceeded("MAX_SECONDS")

    # ---- tokenizer ----------------------------------------------------------------------------
    def _run(self, *, eof: bool) -> None:
        buf, pos, n = self.buf, 0, len(self.buf)
        count = 0
        next_check = 4096
        stack = self.stack
        while True:
            if stack:
                f = stack[-1]
                if f.skip and (f.expect in ("KEY_OR_END", "KEY") if f.kind == "O"
                               else f.expect in ("VALUE_OR_END", "VALUE")):
                    run = (_FAST_OBJECT_RUN if f.kind == "O" else _FAST_ARRAY_RUN).match(buf, pos)
                    if run is not None:
                        end = run.end()
                        # A run ends after a comma (a key/value must follow) or before the bracket.
                        if buf[end - 1] == 0x2C:
                            f.expect = "KEY" if f.kind == "O" else "VALUE"
                        else:
                            f.expect = "COMMA_OR_END"
                        count += 1 + ((end - pos) >> 4)  # keeps the in-loop time check per bytes read
                        pos = end
                        if count >= next_check:
                            next_check = count + 4096
                            self._check_time()
                        continue
            m = _TOKEN.match(buf, pos)
            if m is None or (m.group(3) is not None and not eof
                             and _NUMBER_TAIL.match(buf, m.end()).end() == n):
                # No complete token yet (a number may still grow: "1." / "1e" / "-" at the end).
                ws = _WS.match(buf, pos).end()
                if ws == n:
                    pos = ws
                    break
                if eof or buf[ws] not in _STARTS:
                    raise _malformed()
                self._incomplete_or_invalid(buf, ws, n)
                if n - ws > self.limits.max_token:
                    raise ScanBoundExceeded("MAX_TOKEN")  # a VALID but still unfinished token
                pos = ws
                break  # an incomplete token: wait for more bytes
            if m.end() - m.start(m.lastindex) > self.limits.max_token:  # leading whitespace not counted
                raise ScanBoundExceeded("MAX_TOKEN")
            if self.done:
                raise _malformed()  # data after the top-level value
            count += 1
            if count >= next_check:
                next_check = count + 4096
                self._check_time()
            if m.group(1) is not None:
                self._punct(m.group(1), self.base + m.end(1) - 1, self.base + m.end(1))
            elif m.group(2) is not None:
                self._string(m.group(2))
            elif m.group(3) is not None:
                raw = m.group(3)
                self._scalar(int(raw) if _INT.fullmatch(raw) else ("NUMBER", raw))
            else:
                lit = m.group(4)
                self._scalar(True if lit == b"true" else False if lit == b"false" else None)
            pos = m.end()
        self.base += pos
        self.buf = buf[pos:]

    @staticmethod
    def _incomplete_or_invalid(buf: bytes, ws: int, n: int) -> None:
        """An unfinished token must be a prefix of a valid one; otherwise PROVIDER_INVALID_JSON
        at once (an invalid string is never waited on until the token bound)."""
        c = buf[ws]
        if c == 0x22:  # '"'
            rest = buf[_STRING_PREFIX.match(buf, ws).end():n]
            if rest and not _ESCAPE_PREFIX.fullmatch(rest):
                raise _malformed()
        elif c in b"tfn":
            if not any(lit.startswith(buf[ws:n]) for lit in _LITERALS):
                raise _malformed()
        elif _NUMBER_TAIL.match(buf, ws).end() != n:
            raise _malformed()

    # ---- grammar ------------------------------------------------------------------------------
    def _expecting_value(self) -> bool:
        if not self.stack:
            return not self.done
        f = self.stack[-1]
        return f.expect in ("VALUE", "VALUE_OR_END")

    def _value_done(self) -> None:
        if not self.stack:
            self.done = True
        else:
            self.stack[-1].expect = "COMMA_OR_END"

    def _enter_value(self) -> None:
        f = self.stack[-1] if self.stack else None
        if f is not None and f.kind == "A":
            f.key = 0 if f.key is None else f.key + 1

    def _punct(self, c: bytes, start: int, end: int) -> None:
        if c in (b"{", b"["):
            if not self._expecting_value():
                raise _malformed()
            self._enter_value()
            self._begin_container(c, start)
            if len(self.stack) >= self.limits.max_depth:
                raise ScanBoundExceeded("MAX_DEPTH")
            frame = _Frame("O" if c == b"{" else "A", "KEY_OR_END" if c == b"{" else "VALUE_OR_END")
            if not self.stack:
                frame.seen = set()
            elif self._fast:
                frame.skip = self._skippable()
            self.stack.append(frame)
            return
        if not self.stack:
            raise _malformed()
        f = self.stack[-1]
        if c in (b"}", b"]"):
            if (c == b"}") != (f.kind == "O") or f.expect not in ("KEY_OR_END", "VALUE_OR_END", "COMMA_OR_END"):
                raise _malformed()
            self.stack.pop()
            self._end_container(end)
            self._value_done()
        elif c == b",":
            if f.expect != "COMMA_OR_END":
                raise _malformed()
            f.expect = "KEY" if f.kind == "O" else "VALUE"
        elif c == b":":
            if f.kind != "O" or f.expect != "COLON":
                raise _malformed()
            f.expect = "VALUE"

    def _string(self, raw: bytes) -> None:
        f = self.stack[-1] if self.stack else None
        if f is not None and f.kind == "O" and f.expect in ("KEY_OR_END", "KEY"):
            key = self._decode(raw)
            f.key, f.expect = key, "COLON"
            if f.seen is not None:  # top-level object: duplicate keys are refused
                if key in f.seen or len(f.seen) >= MAX_TOP_LEVEL_KEYS:
                    raise ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID")
                f.seen.add(key)
            if self._in_errors(len(self.stack)) and len(self.error_keys) < _MAX_ERROR_KEYS:
                self.errors_nonempty = True
                self.error_keys.append(key)
            return
        if len(raw) > _MAX_FIELD_TEXT and not self._wants_text():
            self._scalar(("STRING", None))
            return
        self._scalar(self._decode(raw))

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8") if b"\\" not in raw else json.loads(b'"' + raw + b'"')
        except (UnicodeDecodeError, ValueError):
            raise _malformed() from None

    def _wants_text(self) -> bool:
        return len(self.stack) == 1 and self.stack[0].key == "get"

    def _scalar(self, value: Any) -> None:
        if not self._expecting_value():
            raise _malformed()
        self._enter_value()
        d = len(self.stack)
        if d == 0:
            raise ProviderDataError("PROVIDER_JSON_NOT_OBJECT")
        top = self.stack[0].key
        if d == 1:
            if top == "response":
                raise ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID")
            if top == "results":
                self.results = value if type(value) is int else None
            elif top == "get" and isinstance(value, str):
                self.get = value[:_MAX_FIELD_TEXT]
            elif top == "errors" and value is not None:  # in memory: errors not in (None, [], {})
                self.errors_nonempty = True
            elif top == "paging":
                self.paging_kind = "scalar"
        elif top == "paging" and d == 2 and self.stack[1].key in ("current", "total"):
            self.paging[self.stack[1].key] = value
        elif self._in_errors(d):
            self.errors_nonempty = True
        elif top == "response" and d == 2:
            self._row_append({"id": None, "size": None, "invalid": []})  # a scalar row has no identifier
        elif top == "response" and self.row is not None and d - 2 <= self._max_field_depth:
            self._field(tuple(f.key for f in self.stack[2:]), value)
        self._value_done()

    def _skippable(self) -> bool:
        """For a container about to be pushed: True only when no value below it is ever read.
        Never at the top level, never under `errors` or `paging`, never the `response` list, and
        under `response` only when no row field path continues below this container."""
        parent = self.stack[-1]
        if parent.skip:
            return True
        if len(self.stack) < 2:
            return self.stack[0].key not in ("errors", "paging", "response")
        if self.stack[0].key != "response":
            return self.stack[0].key not in ("errors", "paging")
        path = tuple(f.key for f in self.stack[2:])  # this container's path inside the row
        return not any(len(p) > len(path) and p[:len(path)] == path for p in self.fields.values())

    def _in_errors(self, d: int) -> bool:
        return d >= 2 and self.stack[0].key == "errors"

    def _field(self, rel: tuple, value: Any) -> None:
        for name, path in self.fields.items():
            if rel == path:
                if name in self.row or name in self.row_invalid:
                    self._invalidate(name)  # a duplicate key: neither value is trusted
                elif isinstance(value, tuple) or (isinstance(value, str) and (
                        name == "id" or not SAFE_TEXT.fullmatch(value))):
                    self._invalidate(name)  # a non-integer number, an unread or unsafe string
                else:
                    self.row[name] = value

    def _invalidate(self, name: str) -> None:
        """A flag, never a value: the field is unusable for ids and for splits."""
        self.row.pop(name, None)
        self.row_invalid.add(name)

    def _begin_container(self, c: bytes, start: int) -> None:
        d = len(self.stack)
        if d == 0:
            if c != b"{":
                raise ProviderDataError("PROVIDER_JSON_NOT_OBJECT")
            return
        top = self.stack[0].key
        if d == 1:
            if top == "response":
                if c != b"[":
                    raise ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID")
                self.response_seen = True
            elif top == "paging":
                self.paging_kind = "object" if c == b"{" else "array"
            return
        if self._in_errors(d):
            self.errors_nonempty = True
            return
        if top == "response":
            if d == 2:
                self.row = {} if c == b"{" else None
                self.row_invalid = set()
                self.row_start = start
                if self.row is None:
                    self._row_append({"id": None, "size": None, "invalid": []})
            elif self.row is not None and d - 2 <= self._max_field_depth:
                rel = tuple(f.key for f in self.stack[2:])
                for name, path in self.fields.items():
                    if rel == path:
                        self._invalidate(name)  # a field must be a scalar

    def _end_container(self, end: int) -> None:
        d = len(self.stack)  # depth after the pop
        if d == 2 and self.stack[0].key == "response" and self.row is not None:
            row = {name: self.row.get(name) for name in self.fields}
            row["size"] = end - self.row_start
            row["invalid"] = sorted(self.row_invalid)
            self.row = None
            self._row_append(row)

    def _row_append(self, row: dict[str, Any]) -> None:
        if len(self.rows) >= self.limits.max_rows:
            raise ScanBoundExceeded("MAX_ROWS")
        self.rows.append(row)
