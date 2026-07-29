"""Public identifiers.

UUIDv7: time-ordered, so index locality stays good as the table grows, while the
value is still opaque. `/tests/1234` would be a scraping API; that is the whole
reason internal bigint keys never leave the process (D2 §1).
"""

from __future__ import annotations

import uuid

try:
    from uuid6 import uuid7 as _uuid7
except ImportError:  # pragma: no cover - dev convenience only
    _uuid7 = None


def new_xid() -> uuid.UUID:
    return _uuid7() if _uuid7 is not None else uuid.uuid4()


def parse_xid(value: str) -> uuid.UUID:
    """Raises ValueError on a malformed id, which the API maps to 404 rather than
    422 — an unparseable id and a nonexistent one should be indistinguishable to
    someone probing for content."""
    return uuid.UUID(str(value))
