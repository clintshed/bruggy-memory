"""Small time / id helpers shared across the ingestion service.

Timestamps are handled as timezone-aware ``datetime`` objects internally and
serialised to ISO-8601 strings (UTC, ``Z`` suffix) on the wire, matching the
Zep v2 API contract.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

# Stable namespace for deterministic derived ids (ingested content gets the
# same id on re-run, which is what makes idempotent ingestion work).
NAMESPACE = _uuid.UUID("7d1c3f2a-9b4e-4c1a-8f6d-2e5a9b3c7d10")


def now_utc() -> datetime:
    """Current time in UTC."""
    return datetime.now(timezone.utc)


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a tz-aware UTC datetime.

    Accepts both ``Z`` and ``+00:00`` suffixes and naive datetimes (assumed
    UTC). Returns ``None`` for ``None``/empty/invalid input rather than
    raising, so callers can degrade gracefully.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to a couple of common non-standard shapes.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime to an ISO-8601 string with a ``Z`` suffix."""
    if value is None:
        return None
    dt = value.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def uuid5(name: str, *parts: str) -> str:
    """Deterministic uuid5 for a logical document/segment.

    Determinism is important: ingesting the same page/message twice yields the
    same id, so re-runs are idempotent and never create duplicates.
    """
    seed = "|".join([name, *parts])
    return str(_uuid.uuid5(NAMESPACE, seed))
