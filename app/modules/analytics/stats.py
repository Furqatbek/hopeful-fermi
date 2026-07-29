"""Item statistics. Pure functions over response tallies.

The column that earns this file is `discrimination` — the point-biserial
correlation between getting one item right and total score on the paper. A
NEGATIVE discrimination means strong students get the item wrong more often than
weak ones, which is almost never a hard question and almost always a bad key.
It is the single most useful automated signal we have for finding the mistake
that loses a school client.

Computed in Python rather than SQL on purpose: it is four lines of arithmetic
that are worth being able to test at the boundaries (everyone correct, nobody
correct, one response, zero variance) without a database.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class Response:
    """One student's outcome on one item, plus their total on the paper."""

    user_xid: str
    correct: bool
    total_score: float
    raw_response: str | None = None
    time_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ItemStats:
    n_responses: int
    n_correct: int
    p_value: float | None
    discrimination: float | None
    mean_time_ms: int | None
    option_distribution: dict[str, int]
    common_wrong: list[dict]
    flag_reasons: list[str]

    @property
    def flagged(self) -> bool:
        return bool(self.flag_reasons)


# Below this many responses the statistics are noise, and flagging on noise
# trains authors to ignore the flags.
MIN_RESPONSES = 20
NEAR_ZERO_P = 0.05
NEAR_ONE_P = 0.98
NEGATIVE_DISCRIMINATION = 0.0
# A wrong answer given by this share of students is a missing key alternative
# often enough to be worth an author's attention.
COMMON_WRONG_SHARE = 0.10


def analyse(responses: list[Response]) -> ItemStats:
    n = len(responses)
    if n == 0:
        return ItemStats(0, 0, None, None, None, {}, [], [])

    n_correct = sum(1 for r in responses if r.correct)
    p_value = n_correct / n

    times = [r.time_ms for r in responses if r.time_ms is not None]
    distribution = Counter(_normalise(r.raw_response) for r in responses
                           if r.raw_response is not None)
    wrong = Counter(_normalise(r.raw_response) for r in responses
                    if not r.correct and r.raw_response)

    stats = ItemStats(
        n_responses=n,
        n_correct=n_correct,
        p_value=round(p_value, 4),
        discrimination=point_biserial(responses),
        mean_time_ms=int(sum(times) / len(times)) if times else None,
        option_distribution=dict(distribution.most_common(12)),
        common_wrong=[{"value": value, "count": count,
                       "share": round(count / n, 4)}
                      for value, count in wrong.most_common(5)],
        flag_reasons=[],
    )
    return _flagged(stats, n)


def point_biserial(responses: list[Response]) -> float | None:
    """r_pb = (M1 − M0) / σ · √(p·q)

    Returns None where the number is meaningless rather than a misleading zero:
    fewer than two responses, everyone right, nobody right, or no variance in
    total score. A confident 0.0 and "cannot be computed" are different answers
    and an author acts on them differently.
    """
    n = len(responses)
    if n < 2:
        return None

    correct = [r.total_score for r in responses if r.correct]
    incorrect = [r.total_score for r in responses if not r.correct]
    if not correct or not incorrect:
        return None

    totals = [r.total_score for r in responses]
    mean = sum(totals) / n
    # Population standard deviation: this is the whole cohort that sat the item,
    # not a sample drawn from a larger one.
    variance = sum((t - mean) ** 2 for t in totals) / n
    if variance <= 0:
        return None

    p = len(correct) / n
    numerator = (sum(correct) / len(correct)) - (sum(incorrect) / len(incorrect))
    return round(numerator / math.sqrt(variance) * math.sqrt(p * (1 - p)), 4)


def _flagged(stats: ItemStats, n: int) -> ItemStats:
    reasons: list[str] = []
    if n >= MIN_RESPONSES:
        if stats.p_value is not None and stats.p_value < NEAR_ZERO_P:
            # Nearly nobody got it right. Either the key is wrong or the item is
            # unanswerable; both need a human.
            reasons.append("near_zero_p")
        if stats.p_value is not None and stats.p_value > NEAR_ONE_P:
            reasons.append("near_one_p")
        if (stats.discrimination is not None
                and stats.discrimination < NEGATIVE_DISCRIMINATION):
            reasons.append("negative_discrimination")
        if any(w["share"] >= COMMON_WRONG_SHARE for w in stats.common_wrong):
            reasons.append("common_wrong_answer")
    return replace(stats, flag_reasons=reasons)


def suggested_action(stats: ItemStats) -> str:
    if "negative_discrimination" in stats.flag_reasons:
        return "review_key"
    if "common_wrong_answer" in stats.flag_reasons:
        return "review_key"
    if "near_zero_p" in stats.flag_reasons:
        return "review_item"
    if "near_one_p" in stats.flag_reasons:
        return "retire_too_easy"
    return "none"


# ── exposure ─────────────────────────────────────────────────────────

# Sittings at which the exposure component is ~63% burned. 200 is roughly "every
# active student has seen it once" at MVP scale.
EXPOSURE_SCALE = 200.0
# Organizations at which the spread component is ~63% burned. An item sitting at
# four centres has left the building.
SPREAD_SCALE = 3.0


def burn_score(*, times_sat: int, distinct_orgs: int) -> float:
    """0..1. How compromised an item is.

    Two independent routes to burned, combined as a probabilistic OR: an item
    everybody at one centre has sat, and an item that has quietly circulated to
    four centres, are both spent — for different reasons, and neither should be
    hidden by averaging with the other.
    """
    exposure = 1 - math.exp(-max(0, times_sat) / EXPOSURE_SCALE)
    spread = 1 - math.exp(-max(0, distinct_orgs - 1) / SPREAD_SCALE)
    return round(1 - (1 - exposure) * (1 - spread), 3)


def exposure_recommendation(burn: float) -> str:
    return "retire" if burn > 0.7 else "watch" if burn > 0.3 else "fresh"


def _normalise(value: str | None) -> str:
    return (value or "").strip().casefold()
