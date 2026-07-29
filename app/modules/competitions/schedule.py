"""The competition state machine. Pure: (state, times, now) → next state.

Separated from the tick that drives it so the transitions can be tested at a
hundred points in time in a millisecond, rather than by waiting.

    scheduled → registration → lobby → live → grading → final
                     ↑ the only state a student may register in
                                ↑ prefetch window opens (T-120s)
                                       ↑ key release; the clock starts
                                              ↑ everyone's deadline has passed
                                                      ↑ results are published

`cancelled` is terminal and reachable from anywhere by an admin.

Transitions are driven by wall-clock thresholds with one exception: `grading →
final` also requires every started entry to be scored, because publishing a
leaderboard that is still missing rows is worse than publishing it a minute late.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

TERMINAL = ("final", "cancelled")

# How long to wait for stragglers before publishing without them. An attempt that
# is not scored 10 minutes after the contest ended is not coming: either the
# sweeper will auto-submit it or it failed, and both are visible in monitoring.
GRADING_PATIENCE = dt.timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Timing:
    status: str
    lobby_opens_at: dt.datetime
    starts_at: dt.datetime
    ends_at: dt.datetime
    registration_closes_at: dt.datetime | None = None


def next_status(timing: Timing, now: dt.datetime, *,
                entries_pending: int = 0) -> str | None:
    """The next state, or None if the contest is where it should be.

    Returns ONE step at a time. A tick that has been down for an hour walks the
    machine forward one state per pass rather than jumping to the end, so every
    transition's side effects still run in order.
    """
    if timing.status in TERMINAL:
        return None

    if timing.status == "scheduled":
        return "registration"

    if timing.status == "registration":
        if now >= timing.lobby_opens_at:
            return "lobby"
        return None

    if timing.status == "lobby":
        if now >= timing.starts_at:
            return "live"
        return None

    if timing.status == "live":
        if now >= timing.ends_at:
            return "grading"
        return None

    if timing.status == "grading":
        if entries_pending == 0:
            return "final"
        if now >= timing.ends_at + GRADING_PATIENCE:
            # Publish without the stragglers. They are added by a later tick if
            # they arrive; a board that never appears is the worse failure.
            return "final"
        return None

    return None                                                # pragma: no cover


def registration_open(timing: Timing, now: dt.datetime) -> bool:
    if timing.status not in ("scheduled", "registration"):
        return False
    if timing.registration_closes_at and now >= timing.registration_closes_at:
        return False
    return now < timing.starts_at
