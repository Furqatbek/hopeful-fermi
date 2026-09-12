"""Attempt scoring.

The invariant the whole design rests on:

    score = f(responses, key_versions, band_map_version, engine_version)

A pure function over frozen inputs. That is what makes regrade a recomputation
rather than a mutation, and it is why event sourcing is not needed (ADR-0001 §6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.modules.qtypes.registry import Scorer, ScoreRequest
from app.modules.qtypes.schemas import GroupRules, ItemScore, SlotScore, Verdict


@dataclass(frozen=True, slots=True)
class ItemInput:
    """One scoreable item, resolved from the attempt's frozen test version."""

    question_xid: str
    question_version_xid: str
    type_key: str
    type_version: int
    payload: dict[str, Any]
    slot_keys: tuple[str, ...]
    skill: str
    group: GroupRules = GroupRules()
    paragraph_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KeyVersion:
    """An answer key AT A SPECIFIC VERSION. A score run records exactly which
    key version produced it, which is what makes a mark reproducible years on."""

    xid: str
    key: dict[str, Any]
    tolerance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttemptInput:
    attempt_xid: str
    user_xid: str
    items: tuple[ItemInput, ...]
    responses: dict[str, Any]          # question_version_xid -> response
    competition_xid: str | None = None
    mode: str = "exam"


@dataclass(frozen=True, slots=True)
class BandMap:
    xid: str
    max_raw: int
    rows: tuple[tuple[int, int, Decimal], ...]   # (raw_min, raw_max, band)

    def band_for(self, raw: Decimal) -> Decimal | None:
        whole = int(raw.to_integral_value(rounding=ROUND_HALF_UP))
        for lo, hi, band in self.rows:
            if lo <= whole <= hi:
                return band
        return None


@dataclass(frozen=True, slots=True)
class ScoreRun:
    """Immutable. A regrade INSERTS a new run and supersedes the old one, so the
    history of what a student was told is preserved rather than overwritten."""

    attempt_xid: str
    reason: str
    engine_version: str
    band_map_xid: str | None
    key_versions: dict[str, str]        # question_version_xid -> key_version_xid
    raw_score: Decimal
    max_raw: Decimal
    band: Decimal | None
    per_section: dict[str, Any]
    item_scores: tuple[tuple[str, ItemScore], ...]   # (question_version_xid, score)


def _band(band_map: BandMap | None, raw: Decimal) -> float | None:
    """A section band, or None when the table does not reach that mark.

    The old expression was `float(band_map.band_for(...)) if band_map else None`,
    which guards against a MISSING band map and not against a band map that
    returns None — so `float(None)` raised `TypeError` **inside scoring**, and the
    student's submission failed rather than their band being absent.

    Reaching it needs only an ordinary authoring mistake. A band map is content:
    a centre-admin fills in a table of raw ranges, and this function is called
    per SECTION with the section's raw against the whole-test table — only for
    the section that IS the whole paper, see `score_attempt`. A table starting
    at 10, or one whose `max_raw` no longer matches a paper that has since
    gained a question, is enough.

    A raw score with no band is recoverable — fix the table, regrade, and the
    engine is a pure function so the mark is reproducible. An exception during
    scoring is not.
    """
    if band_map is None:
        return None
    band = band_map.band_for(raw)
    return float(band) if band is not None else None


def score_attempt(
    attempt: AttemptInput,
    keys: dict[str, KeyVersion],
    scorer: Scorer,
    band_map: BandMap | None,
    *,
    reason: str = "initial",
) -> ScoreRun:
    """Score one attempt. No I/O, no clock, no database — so the regrade path can
    be tested exhaustively and a historical score can be reproduced on demand."""
    raw = Decimal(0)
    maximum = Decimal(0)
    per_skill: dict[str, list[Decimal]] = {}
    per_skill_max: dict[str, Decimal] = {}
    item_scores: list[tuple[str, ItemScore]] = []
    used_keys: dict[str, str] = {}

    for item in attempt.items:
        key_version = keys.get(item.question_version_xid)
        if key_version is None:
            # An item with no key scores zero and is marked VOID rather than
            # incorrect: the student did nothing wrong, the test did.
            #
            # The comment above said that and the code did not. It counted the
            # marks into `maximum` and emitted no ItemScore, so the item was
            # absent from review entirely — the numbering skipped, and nothing
            # said why. A student comparing their paper to their review found a
            # question missing and no explanation anywhere in the product.
            #
            # `Verdict.VOID` was already in the contract and already handled by
            # the student's marking display; it was declared, rendered, and
            # never produced by anything. One slot score per slot, awarded zero
            # against the same maximum, so the raw and the band do not move.
            maximum += Decimal(len(item.slot_keys))
            # The void item is still part of its section: it counts against the
            # section's maximum exactly as it counts against the paper's, so the
            # "is this section the whole paper" test below still holds.
            per_skill.setdefault(item.skill, [])
            per_skill_max[item.skill] = (per_skill_max.get(item.skill, Decimal(0))
                                         + Decimal(len(item.slot_keys)))
            item_scores.append((item.question_version_xid, ItemScore(tuple(
                SlotScore(slot_key=slot, awarded=Decimal(0), max_points=Decimal(1),
                          verdict=Verdict.VOID,
                          explain={"reason": "no_answer_key"})
                for slot in item.slot_keys))))
            continue

        score = scorer.score_item(ScoreRequest(
            type_key=item.type_key,
            type_version=item.type_version,
            payload=item.payload,
            key=key_version.key,
            response=attempt.responses.get(item.question_version_xid),
            group=item.group,
            tolerance=key_version.tolerance,
            paragraph_labels=item.paragraph_labels,
        ))
        raw += score.awarded
        maximum += score.max_points
        per_skill.setdefault(item.skill, []).append(score.awarded)
        per_skill_max[item.skill] = per_skill_max.get(item.skill, Decimal(0)) + score.max_points
        item_scores.append((item.question_version_xid, score))
        used_keys[item.question_version_xid] = key_version.xid

    band = band_map.band_for(raw) if band_map else None
    # One band map per test version, calibrated on the WHOLE paper's `max_raw`.
    # This applied it to each section's partial raw, so on a Reading + Listening
    # paper a student with 30/40 in each section was shown the band for 30 of
    # 80 in both — a wrong band, not a rough one, and the student result screen
    # rendered it beside the correct headline band. A section is banded only
    # when it IS the paper the table was built for. Until a per-skill map exists
    # (`BandMap.skill` is a label today, not a second table) a multi-skill paper
    # shows no section band; the headline band above it is unaffected.
    per_section = {
        skill: {"raw": float(sum(marks, Decimal(0))),
                "band": (_band(band_map, sum(marks, Decimal(0)))
                         if per_skill_max[skill] == maximum else None)}
        for skill, marks in per_skill.items()
    }

    return ScoreRun(
        attempt_xid=attempt.attempt_xid,
        reason=reason,
        engine_version=scorer.engine_version,
        band_map_xid=band_map.xid if band_map else None,
        key_versions=used_keys,
        raw_score=raw,
        max_raw=maximum,
        band=band,
        per_section=per_section,
        item_scores=tuple(item_scores),
    )
