from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def to_rome(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(ROME)


def to_rome_iso(value: datetime) -> str:
    return to_rome(value).isoformat()


def normalize_source_datetime(value: str, source_tz: str) -> datetime:
    """Parse a source-local ISO-like datetime and normalize to UTC.

    If the string already contains an offset, that offset wins. If it is naive,
    source_tz is mandatory and is interpreted as an IANA timezone name.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        if not source_tz:
            raise ValueError("source_tz is required for a naive source datetime")
        dt = dt.replace(tzinfo=ZoneInfo(source_tz))
    return dt.astimezone(timezone.utc)
