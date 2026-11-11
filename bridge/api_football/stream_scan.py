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
  max_token   64 KiB for a single token (string or number)

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
_DUP = "DUPLICATE_FIELD"
_BAD = "NOT_A_SCALAR"


def _malformed() -> ProviderDataError:
    return ProviderDataError("PROVIDER_INVALID_JSON")


class _Frame:
    __slots__ = ("kind", "expect", "key", "seen")

    def __init__(self, kind: str, expect: str):
        self.kind, self.expect, self.key, self.seen = kind, expect, None, None


class IdScanner:
    def __init__(self, fields: Mapping[str, tuple[str, ...]], *, limits: ScanLimits = DEFAULT_LIMITS,
                 clock: Callable[[], float] = time.monotonic):
        self.fields = dict(fields)
        self.limits = limits
        self.clock = clock
        self.started = clock()
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
        self.row_start = 0
        self._max_field_depth = max((len(p) for p in self.fields.values()), default=0)

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
        if self.clock() - self.started > self.limits.max_seconds:
            raise ScanBoundExceeded("MAX_SECONDS")

    # ---- tokenizer ----------------------------------------------------------------------------
    def _run(self, *, eof: bool) -> None:
        buf, pos, n = self.buf, 0, len(self.buf)
        count = 0
        while True:
            m = _TOKEN.match(buf, pos)
            if m is None or (m.end() == n and m.group(3) is not None and not eof):
                ws = _WS.match(buf, pos).end()
                if ws == n:
                    pos = ws
                    break
                if eof or buf[ws] not in _STARTS:
                    raise _malformed()
                if n - ws > self.limits.max_token:
                    raise ScanBoundExceeded("MAX_TOKEN")
                pos = ws
                break  # an incomplete token: wait for more bytes
            if m.end() - m.start() > self.limits.max_token:
                raise ScanBoundExceeded("MAX_TOKEN")
            if self.done:
                raise _malformed()  # data after the top-level value
            count += 1
            if not count % 4096:
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
                if key in f.seen:
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
            elif top == "errors" and value not in (None, "", False):
                self.errors_nonempty = True
            elif top == "paging":
                self.paging_kind = "scalar"
        elif top == "paging" and d == 2 and self.stack[1].key in ("current", "total"):
            self.paging[self.stack[1].key] = value
        elif self._in_errors(d):
            self.errors_nonempty = True
        elif top == "response" and d == 2:
            self._row_append({"id": None, "size": None})  # a scalar row has no identifier
        elif top == "response" and self.row is not None and d - 2 <= self._max_field_depth:
            self._field(tuple(f.key for f in self.stack[2:]), value)
        self._value_done()

    def _in_errors(self, d: int) -> bool:
        return d >= 2 and self.stack[0].key == "errors"

    def _field(self, rel: tuple, value: Any) -> None:
        for name, path in self.fields.items():
            if rel == path:
                if name in self.row:
                    self.row[name] = _DUP
                elif isinstance(value, tuple):
                    self.row[name] = _BAD if value[0] == "NUMBER" else None
                else:
                    self.row[name] = value

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
                self.row_start = start
                if self.row is None:
                    self._row_append({"id": None, "size": None})
            elif self.row is not None and d - 2 <= self._max_field_depth:
                rel = tuple(f.key for f in self.stack[2:])
                for name, path in self.fields.items():
                    if rel == path:
                        self.row[name] = _BAD  # an id must be a scalar

    def _end_container(self, end: int) -> None:
        d = len(self.stack)  # depth after the pop
        if d == 2 and self.stack[0].key == "response" and self.row is not None:
            row = {name: self.row.get(name) for name in self.fields}
            row["size"] = end - self.row_start
            self.row = None
            self._row_append(row)

    def _row_append(self, row: dict[str, Any]) -> None:
        if len(self.rows) >= self.limits.max_rows:
            raise ScanBoundExceeded("MAX_ROWS")
        self.rows.append(row)
