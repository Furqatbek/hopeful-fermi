"""The injectable clock.

"The exam module MUST NOT call `datetime.now()`. Without an injectable clock,
'the server is the sole authority on time remaining' is untestable, and untested
time logic is how you lose a competition."

`FrozenClock` is test infrastructure, which is exactly why its own guard matters:
a naive datetime slipped into it produces `TypeError: can't compare offset-naive
and offset-aware datetimes` several frames away, inside whatever exam or
competition logic happened to do the arithmetic — and the failure reads as a bug
in that logic rather than in the fixture. The guard turns it into one sentence at
the point of the mistake, and until now it had never fired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.platform.clock import Clock, FrozenClock, SystemClock

AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class TestSystemClock:
    def test_it_is_timezone_aware_and_utc(self):
        """A naive `now()` compared against a `timestamptz` from the database is
        the bug this whole module exists to prevent."""
        now = SystemClock().now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_it_satisfies_the_protocol(self):
        def takes_a_clock(clock: Clock) -> datetime:
            return clock.now()

        assert takes_a_clock(SystemClock()).tzinfo is not None
        assert takes_a_clock(FrozenClock(AT)) == AT


class TestFrozenClock:
    def test_it_does_not_move_on_its_own(self):
        """The property the name promises. A clock that drifts under a test makes
        an expiry assertion flaky at exactly the boundary it is checking."""
        clock = FrozenClock(AT)
        assert clock.now() == clock.now() == AT

    def test_a_naive_datetime_is_refused(self):
        """Refused at construction, where the mistake is.

        Accepting it would defer the failure to a comparison several frames away,
        inside the exam logic under test, and read as a bug there.
        """
        with pytest.raises(ValueError, match="timezone-aware"):
            FrozenClock(datetime(2026, 7, 29, 12, 0))

    def test_a_non_utc_datetime_is_converted_rather_than_refused(self):
        """Tashkent is UTC+5 and this is a Tashkent product. A test that reads
        naturally in local time must not have to convert by hand — but everything
        downstream compares against UTC, so the clock does it once."""
        tashkent = timezone(timedelta(hours=5))
        clock = FrozenClock(datetime(2026, 7, 29, 17, 0, tzinfo=tashkent))
        assert clock.now() == AT
        assert clock.now().utcoffset() == timedelta(0)

    def test_advance_moves_it_and_returns_the_new_time(self):
        clock = FrozenClock(AT)
        assert clock.advance(seconds=90) == AT + timedelta(seconds=90)
        assert clock.now() == AT + timedelta(seconds=90)

    def test_advance_takes_the_units_timedelta_takes(self):
        clock = FrozenClock(AT)
        clock.advance(minutes=2, seconds=30)
        assert clock.now() == AT + timedelta(seconds=150)

    def test_set_jumps_to_an_absolute_time(self):
        """The other half of `advance`. A competition test needs "it is now two
        minutes before the start", which is an absolute instant rather than an
        offset from wherever the clock happens to be."""
        clock = FrozenClock(AT)
        target = AT + timedelta(days=3)
        assert clock.set(target) == target
        assert clock.now() == target

    def test_set_converts_to_utc_as_well(self):
        """The same normalisation as the constructor, for the same reason. Two
        entry points that disagree about timezones would make the clock's own
        output depend on which one a test happened to use."""
        clock = FrozenClock(AT)
        clock.set(datetime(2026, 7, 29, 17, 0, tzinfo=timezone(timedelta(hours=5))))
        assert clock.now() == AT
        assert clock.now().utcoffset() == timedelta(0)

    def test_set_backwards_is_allowed(self):
        """Deliberately. "What did this look like before the window opened" is a
        legitimate thing for a test to ask, and a clock that refused would push
        those tests into building a second one."""
        clock = FrozenClock(AT)
        clock.set(AT - timedelta(hours=1))
        assert clock.now() < AT
