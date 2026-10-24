"""Client-side input validation for registry-defined parameters.

Validation kinds are a CLIENT POLICY, not a provider contract: they keep the
query string safe (no injected separators, bounded lengths, typed ids) and are
marked PROVISIONAL in the registry until the official contract is acquired.
Provider-side validation remains authoritative and is returned verbatim.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_TEXT = re.compile(r"^[\w .'\-]+$", re.UNICODE)
_ROUND = re.compile(r"^[\w .'\-/()]+$", re.UNICODE)
_TIMEZONE = re.compile(r"^[A-Za-z]+(?:[/_+\-][A-Za-z0-9_+\-]+)*$")
_STATUS = re.compile(r"^[A-Z0-9]{1,4}(?:-[A-Z0-9]{1,4})*$")
_IDS = re.compile(r"^[1-9][0-9]{0,9}(?:-[1-9][0-9]{0,9})*$")

KINDS = frozenset({"id", "season", "int", "bool", "date", "text", "round", "timezone", "status", "ids", "live"})
PYTHON_TYPES = {"id": int, "season": int, "int": int, "bool": bool}


class InvalidArgument(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str            # MCP argument name
    wire: str            # provider query parameter name
    kind: str
    description: str
    min_len: int = 1
    max_len: int = 100
    low: int = 1
    high: int = 10**10
    max_items: int = 20

    @property
    def python_type(self):
        return PYTHON_TYPES.get(self.kind, str)


def _ids(param: Param, value: str) -> str:
    if not _IDS.fullmatch(value):
        raise InvalidArgument(f"{param.name} must be positive ids joined by '-'")
    items = value.split("-")
    if len(items) > param.max_items:
        raise InvalidArgument(f"{param.name} accepts at most {param.max_items} ids")
    if len(set(items)) != len(items):
        raise InvalidArgument(f"{param.name} contains duplicate ids")
    return value


def normalize(param: Param, value) -> str:
    kind = param.kind
    if kind in PYTHON_TYPES and kind != "bool":
        if type(value) is not int:
            raise InvalidArgument(f"{param.name} must be an integer")
        if not param.low <= value <= param.high:
            raise InvalidArgument(f"{param.name} must be within {param.low}..{param.high}")
        return str(value)
    if kind == "bool":
        if type(value) is not bool:
            raise InvalidArgument(f"{param.name} must be a boolean")
        return "true" if value else "false"
    if not isinstance(value, str):
        raise InvalidArgument(f"{param.name} must be a string")
    value = value.strip()
    if not param.min_len <= len(value) <= param.max_len:
        raise InvalidArgument(f"{param.name} length must be within {param.min_len}..{param.max_len}")
    if kind == "date":
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise InvalidArgument(f"{param.name} must be a valid YYYY-MM-DD date") from None
        if len(value) != 10 or parsed.isoformat() != value:
            raise InvalidArgument(f"{param.name} must be a valid YYYY-MM-DD date")
        return value
    if kind == "ids":
        return _ids(param, value)
    if kind == "live":
        return value if value == "all" else _ids(param, value)
    pattern = {"text": _TEXT, "round": _ROUND, "timezone": _TIMEZONE, "status": _STATUS}[kind]
    if not pattern.fullmatch(value):
        raise InvalidArgument(f"{param.name} contains unsupported characters or format")
    return value


def validate(params: tuple[Param, ...], arguments: dict, require_any: bool) -> dict[str, str]:
    """Return provider query parameters (wire names, string values) or raise InvalidArgument.

    None means "not supplied": it is never sent and never replaced by a default.
    Required-ness is NOT enforced client-side while the official contract is pending;
    `require_any` only refuses a call with no filter at all (quota protection).
    """
    known = {param.name: param for param in params}
    unknown = sorted(set(arguments) - set(known))
    if unknown:
        raise InvalidArgument(f"unsupported parameters: {', '.join(unknown)}")
    supplied = {name: value for name, value in arguments.items() if value is not None}
    if require_any and not supplied:
        raise InvalidArgument("at least one parameter is required (client quota policy)")
    return {known[name].wire: normalize(known[name], value) for name, value in sorted(supplied.items())}
