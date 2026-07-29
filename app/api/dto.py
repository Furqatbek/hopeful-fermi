"""Response serialization helpers.

Handlers return plain JSON-safe values rather than letting the framework decide
how to render a datetime. Two reasons, and the second one is why it is a rule:

  * `datetime.isoformat()` gives `+00:00` while pydantic's serializer gives `Z`.
    Both are valid RFC 3339 and clients cope with either, but they are different
    strings.
  * The idempotency layer STORES a response and replays it later. If the stored
    copy is encoded by a different code path from the live one, a replay returns
    a differently-shaped body than the original — which defeats the entire point
    of storing it. Encoding once, here, makes them identical by construction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def iso(value: dt.datetime | None) -> str | None:
    """RFC 3339 with a `Z` suffix for UTC, matching the OpenAPI examples."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def jsonify(value: Any) -> Any:
    """Recursively make a response body JSON-safe and stable."""
    if isinstance(value, dt.datetime):
        return iso(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    return value
