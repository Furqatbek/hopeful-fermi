"""Leaderboard ranking. Pure — value objects in, ranked rows out.

Two decisions worth stating.

**Tiebreak rules are data, not code.** `competitions.tiebreak` is an ordered
array of comparator names evaluated left to right, so a centre can run "highest
score, then fastest" without a deploy. The names are whitelisted here; an unknown
one raises rather than being ignored, because silently falling back to a
different ordering than the one advertised is how a contest gets disputed.

**Genuine ties share a rank.** Two students with identical scores on every
comparator get the same rank, and the next rank skips (1, 2, 2, 4). Breaking a
true tie by row id would be inventing a winner, and the loser would be right to
complain.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable


class UnknownTiebreak(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Entry:
    user_xid: str
    attempt_xid: str
    raw_score: Decimal
    band: Decimal | None
    duration_ms: int
    submitted_at: dt.datetime


@dataclass(frozen=True, slots=True)
class Ranked:
    entry: Entry
    rank: int
    # Materialized so a re-render of the final board is a plain index scan and
    # does not re-evaluate comparator logic years later.
    tiebreak_key: str


# Each comparator returns a value sorted ASCENDING. Descending rules negate.
_COMPARATORS: dict[str, Callable[[Entry], Any]] = {
    "raw_score_desc": lambda e: -e.raw_score,
    "raw_score_asc": lambda e: e.raw_score,
    "duration_asc": lambda e: e.duration_ms,
    "duration_desc": lambda e: -e.duration_ms,
    "submitted_at_asc": lambda e: e.submitted_at,
    "band_desc": lambda e: -(e.band if e.band is not None else Decimal(0)),
}

DEFAULT_TIEBREAK = ("raw_score_desc", "duration_asc", "submitted_at_asc")


def rank(entries: list[Entry], tiebreak: list[str] | tuple[str, ...] | None = None
         ) -> list[Ranked]:
    rules = tuple(tiebreak or DEFAULT_TIEBREAK)
    unknown = [r for r in rules if r not in _COMPARATORS]
    if unknown:
        raise UnknownTiebreak(
            f"Unknown tiebreak rule(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(_COMPARATORS))}.")

    def key(entry: Entry) -> tuple:
        return tuple(_COMPARATORS[rule](entry) for rule in rules)

    ordered = sorted(entries, key=key)
    out: list[Ranked] = []
    previous_key: tuple | None = None
    current_rank = 0
    for position, entry in enumerate(ordered, start=1):
        entry_key = key(entry)
        if entry_key != previous_key:
            # Standard competition ranking: after two joint 2nds the next is 4th.
            current_rank = position
            previous_key = entry_key
        out.append(Ranked(entry=entry, rank=current_rank,
                          tiebreak_key=_render(entry_key)))
    return out


def _render(key: tuple) -> str:
    """A stable, sortable string. Numbers are zero-padded and offset so a negated
    score still orders correctly as text."""
    parts = []
    for value in key:
        if isinstance(value, dt.datetime):
            parts.append(value.astimezone(dt.UTC).strftime("%Y%m%d%H%M%S%f"))
        elif isinstance(value, (int, Decimal, float)):
            parts.append(f"{Decimal(value) + Decimal(10**9):018.3f}")
        else:                                                  # pragma: no cover
            parts.append(str(value))
    return "|".join(parts)


def podium_changes(before: list[Ranked], after: list[Ranked], *, places: int = 3) -> int:
    """How many of the top `places` are occupied by someone different.

    The number a platform admin actually needs when deciding whether a key fix
    may be applied to a finished contest: "twelve ranks moved" is noise if the
    podium is unchanged, and a single podium change is not.
    """
    def top(rows: list[Ranked]) -> list[str]:
        return [r.entry.user_xid for r in sorted(rows, key=lambda r: r.rank)
                if r.rank <= places]

    old, new = top(before), top(after)
    return sum(1 for i in range(max(len(old), len(new)))
               if (old[i] if i < len(old) else None) != (new[i] if i < len(new) else None))
