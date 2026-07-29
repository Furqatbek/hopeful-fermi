"""Public identifiers.

UUIDv7: time-ordered, so index locality stays good as the table grows, while the
value is still opaque. `/tests/1234` would be a scraping API; that is the whole
reason internal bigint keys never leave the process (D2 §1).
"""

from __future__ import annotations

import uuid

# Imported hard, not behind a try/except that falls back to uuid4.
#
# `uuid6` is a declared dependency, so the fallback could only fire on a broken
# install — and when it fired it would silently abandon time-ordering, which is
# the entire reason this module exists. Every id minted in that state scatters
# across the index instead of appending to it, nothing reports anything, and the
# damage is permanent because the ids are already in the database. A missing
# dependency should fail at import the same way a worker without ffmpeg does.
from uuid6 import uuid7


def new_xid() -> uuid.UUID:
    return uuid7()


def parse_xid(value: str) -> uuid.UUID:
    """Raises ValueError on a malformed id, which the API maps to 404 rather than
    422 — an unparseable id and a nonexistent one should be indistinguishable to
    someone probing for content."""
    return uuid.UUID(str(value))
