"""Injectable clock.

The exam module MUST NOT call `datetime.now()`. Without an injectable clock,
"the server is the sole authority on time remaining" is untestable, and untested
time logic is how you lose a competition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Production clock. Always timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock. Advance it explicitly; never drifts under you."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: float = 0, **kwargs: float) -> datetime:
        self._at += timedelta(seconds=seconds, **kwargs)
        return self._at

    def set(self, at: datetime) -> datetime:
        self._at = at.astimezone(UTC)
        return self._at
