"""FIX-06: timezone validation and a non-mutating consistency check of provider times.

Source. The repository holds NO captured copy of the provider's /timezone catalog. A requested
zone is validated against the IANA database shipped by the pinned PyPI package `tzdata`
(requirements.txt), never against the host's tz files, so the catalog is the same on every
host (Render included). This is IANA validation only: whether the PROVIDER accepts a given
zone is unverified. The provider stays the final authority: a zone it does not know comes
back as a declared `timezone` error, classified INVALID_PARAMETER, never retried. If the
package is missing or unreadable, only UTC is accepted (fail closed).

The bridge never converts provider times. It forwards the zone, keeps the provider's data as
received (fixture.timestamp is the provider's UTC anchor, fixture.date carries the provider's
offset) and, separately and marked DERIVED, checks that each published offset is the one the
IANA rules give for that instant in the requested zone. A UTC instant maps to exactly one
local time, so the check has no ambiguous or non-existent local times (DST included).
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from typing import Any
import zoneinfo

TIMEZONE_SOURCE = "IANA_TZDATA_PYPI_PACKAGE"
PROVIDER_ACCEPTANCE = "UNVERIFIED"
# Entries that are not real zones.
_EXCLUDED = frozenset({"Factory", "localtime", "posixrules"})
_MAX_CHECKED = 5000


def _tzdata_files():
    import importlib.resources
    return importlib.resources.files("tzdata")


@lru_cache(maxsize=1)
def tzdata_version() -> str | None:
    try:
        import tzdata
        return str(tzdata.IANA_VERSION)
    except Exception:
        return None


@lru_cache(maxsize=1)
def timezone_catalog() -> frozenset[str]:
    """Zone keys listed by the pinned tzdata package; empty (fail closed) when unavailable."""
    try:
        names = (_tzdata_files() / "zones").read_text(encoding="utf-8").split()
    except Exception:
        return frozenset()
    return frozenset(z for z in names if z not in _EXCLUDED)


@lru_cache(maxsize=1024)
def load_zone(key: str) -> tzinfo:
    """The zone's rules from the tzdata package itself (not the host). Catalog keys only."""
    if key == "UTC":
        return timezone.utc
    if key not in timezone_catalog():
        raise KeyError("zone not in catalog")
    resource = _tzdata_files() / "zoneinfo"
    for part in key.split("/"):
        resource = resource / part
    with resource.open("rb") as fh:
        return zoneinfo.ZoneInfo.from_file(fh, key=key)


def validate_timezone(value: str) -> str | None:
    """None when accepted, else a fixed error code. UTC needs no catalog."""
    if value == "UTC":
        return None
    catalog = timezone_catalog()
    if not catalog:
        return "TIMEZONE_CATALOG_UNAVAILABLE"
    if value not in catalog:
        return "TIMEZONE_NOT_IN_CATALOG"
    return None


def timezone_context(requested: str) -> dict[str, Any]:
    return {
        "requested": requested,
        "validated_against": TIMEZONE_SOURCE,
        "iana_version": tzdata_version(),
        "provider_catalog_captured": False,
        "provider_acceptance": PROVIDER_ACCEPTANCE,
        "provider_is_final_authority": True,
        "bridge_conversion": "NONE",
        "utc_anchor": "provider field fixture.timestamp (Unix seconds, UTC) when published",
    }


def _fixture_items(data: Any):
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, list):
        return
    for item in response:
        if isinstance(item, dict) and isinstance(item.get("fixture"), dict):
            yield item["fixture"]


def offset_consistency(data: Any, requested: str) -> dict[str, Any] | None:
    """DERIVED, outside the provider data: does each fixture's published `date` offset match the
    IANA offset of the requested zone at its published `timestamp`? Missing or unparseable
    fields are counted as not checkable, never filled."""
    try:
        zone = load_zone(requested)
    except Exception:
        return None
    counts = {"consistent": 0, "inconsistent": 0, "not_checkable": 0}
    mismatched: list[int] = []
    for n, fixture in enumerate(_fixture_items(data)):
        if n >= _MAX_CHECKED:
            break
        ts, raw = fixture.get("timestamp"), fixture.get("date")
        if type(ts) is not int or not isinstance(raw, str):
            counts["not_checkable"] += 1
            continue
        try:
            published = datetime.fromisoformat(raw)
        except ValueError:
            counts["not_checkable"] += 1
            continue
        if published.tzinfo is None:
            counts["not_checkable"] += 1
            continue
        expected = datetime.fromtimestamp(ts, zone)
        if published.utcoffset() == expected.utcoffset() and int(published.timestamp()) == ts:
            counts["consistent"] += 1
        else:
            counts["inconsistent"] += 1
            if type(fixture.get("id")) is int and len(mismatched) < 50:
                mismatched.append(fixture["id"])
    if not any(counts.values()):
        return None
    return {"label": "DERIVED", "requested": requested, **counts, "inconsistent_fixture_ids": mismatched,
            "rule": "published fixture.date offset == IANA offset of the requested zone at fixture.timestamp"}
