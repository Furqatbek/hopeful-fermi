"""The three scoring primitives.

All 19 question types required at MVP decompose into these. The exam engine
dispatches on `ScoringSpec.primitive` and has no knowledge of any specific
question type — that is the whole design (ADR-0001 §8.3).

A fourth primitive is ~30-50 lines plus a deploy, and is expected to be rare.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import wordlimit
from .normalizers import Pipeline
from .schemas import GroupRules, ItemScore, Option, ScoringSpec, SlotScore, Verdict, WordLimit


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything a primitive needs that is not the response or the key."""

    payload: dict[str, Any]
    group: GroupRules
    spec: ScoringSpec
    pipeline: Pipeline
    tolerance: dict[str, Any]
    # Paragraph letters from the section's passage, for matching-information.
    paragraph_labels: tuple[str, ...] = ()


def _points_per_slot(spec: ScoringSpec) -> Decimal:
    return Decimal(str(spec.options.get("points_per_slot", 1)))


def _response_slots(response: Any) -> dict[str, Any]:
    if isinstance(response, dict) and "slots" in response:
        return response["slots"] or {}
    return {}


def _as_text(value: Any) -> str | None:
    """A slot's answer as text, or None when there is no text answer.

    **A container is not text.** `str(value)` on a dict yields its Python repr,
    and that repr was then normalized and compared against the accepted answers —
    so `{"text": "bicycle"}` normalized to `text bicycle`, matched nothing, and
    marked a correct answer wrong. The API layer now refuses that shape outright,
    and this is the second line: scoring must never turn a structure into a string
    that looks like something a student typed.

    None rather than an exception, because this runs over stored responses during
    a regrade as well as at submit, and one malformed row from the past should not
    abort a job covering ten thousand attempts. It reads as unanswered, which is
    honest — no text answer was received — and it keeps the repr out of the review
    screen, where it would otherwise be shown to a student as their own words.
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value)
    return text if text.strip() else None


def resolve_options(ctx: ScoringContext) -> tuple[Option, ...]:
    """Where this type's selectable options come from.

    A parameter, not a new primitive: MCQ carries its own options, matching types
    share the group's bank, T/F/NG has a fixed set, and matching-information uses
    the passage's paragraph letters.
    """
    source = ctx.spec.options.get("option_source", "payload.options")
    if source == "payload.options":
        return tuple(
            Option(id=str(o["id"]), text=str(o.get("text", "")))
            for o in (ctx.payload.get("options") or [])
        )
    if source == "group.option_bank":
        return ctx.group.option_bank
    if source == "fixed":
        return tuple(
            Option(id=str(o["id"]), text=str(o.get("text", "")))
            for o in (ctx.spec.options.get("fixed_options") or [])
        )
    if source == "section.passage_version.paragraph_labels":
        return tuple(Option(id=label) for label in ctx.paragraph_labels)
    raise ValueError(f"unknown option_source: {source!r}")


# ── choice_per_slot ──────────────────────────────────────────────────

def choice_per_slot(response: Any, key: dict[str, Any], ctx: ScoringContext) -> ItemScore:
    """N slots, each answered with an option id.

    Covers MCQ single, T/F/NG, Y/N/NG, every matching type, summary completion
    with a word bank, and map labelling.

    Note on `unique_options`: it is an AUTHORING constraint checked by the publish
    gate, not a scoring rule. A student who uses the same heading twice simply
    gets at most one of them right; penalising the duplicate would diverge from
    how the real exam marks.
    """
    options = resolve_options(ctx)
    valid_ids = {o.id.casefold() for o in options}
    per_slot = _points_per_slot(ctx.spec)
    submitted = _response_slots(response)
    scores: list[SlotScore] = []

    for slot_key, spec in (key.get("slots") or {}).items():
        raw = _as_text(submitted.get(slot_key))
        accepted = [str(a) for a in spec.get("accept", [])]
        accepted_norm = [a.strip().casefold() for a in accepted]
        explain: dict[str, Any] = {
            "primitive": "choice_per_slot",
            "option_source": ctx.spec.options.get("option_source", "payload.options"),
            "option_count": len(options),
            "accepted": accepted,
        }

        if raw is None:
            scores.append(SlotScore(slot_key, Decimal(0), per_slot, Verdict.UNANSWERED,
                                    explain=explain))
            continue

        normalized = raw.strip().casefold()
        explain["normalized_response"] = normalized

        if valid_ids and normalized not in valid_ids:
            # Not reachable through the UI, but a hand-crafted request can do it.
            explain["reason"] = "not_in_option_set"
            scores.append(SlotScore(slot_key, Decimal(0), per_slot, Verdict.INCORRECT,
                                    raw_response=raw, normalized_response=normalized,
                                    explain=explain))
            continue

        if normalized in accepted_norm:
            matched = accepted[accepted_norm.index(normalized)]
            scores.append(SlotScore(slot_key, per_slot, per_slot, Verdict.CORRECT,
                                    raw_response=raw, normalized_response=normalized,
                                    matched_alternative=matched, explain=explain))
        else:
            scores.append(SlotScore(slot_key, Decimal(0), per_slot, Verdict.INCORRECT,
                                    raw_response=raw, normalized_response=normalized,
                                    explain=explain))

    return _aggregate(ItemScore(tuple(scores)), ctx.spec)


# ── text_per_slot ────────────────────────────────────────────────────

def text_per_slot(response: Any, key: dict[str, Any], ctx: ScoringContext) -> ItemScore:
    """N slots, free text, normalized then compared to accepted alternatives.

    Covers every completion type and short answer. This is where the whole
    tolerance model lives.
    """
    per_slot = _points_per_slot(ctx.spec)
    submitted = _response_slots(response)
    apply_limit = bool(ctx.spec.options.get("apply_word_limit", True))
    scores: list[SlotScore] = []

    for slot_key, spec in (key.get("slots") or {}).items():
        raw = _as_text(submitted.get(slot_key))
        accepted = [str(a) for a in spec.get("accept", [])]

        # Tolerance resolves type -> group -> key, most specific winning.
        pipeline = ctx.pipeline
        if names := spec.get("normalizers"):
            pipeline = pipeline.replacing(tuple(names))
        if spec.get("case_sensitive"):
            pipeline = pipeline.without("casefold")

        limit = ctx.group.word_limit
        if key_limit := WordLimit.from_dict(spec.get("word_limit")):
            limit = key_limit

        explain: dict[str, Any] = {
            "primitive": "text_per_slot",
            "normalizers": list(pipeline.names),
            "accepted": accepted,
        }

        if raw is None:
            scores.append(SlotScore(slot_key, Decimal(0), per_slot, Verdict.UNANSWERED,
                                    explain=explain))
            continue

        if apply_limit and limit is not None:
            violated, wl_explain = wordlimit.violates(raw, limit)
            explain["word_limit"] = wl_explain
            if violated:
                # Marked wrong outright. Never truncated and re-compared — that
                # would award marks the real exam would not.
                explain["reason"] = "word_limit_exceeded"
                scores.append(SlotScore(slot_key, Decimal(0), per_slot, Verdict.INCORRECT,
                                        raw_response=raw, explain=explain))
                continue

        got = pipeline.run(raw)
        explain["steps"] = got.as_explain()
        explain["normalized_response"] = got.value

        matched: str | None = None
        normalized_accepted: list[str] = []
        for alternative in accepted:
            want = pipeline.run(alternative)
            normalized_accepted.append(want.value)
            if matched is None and got.value == want.value:
                matched = alternative
        explain["normalized_accepted"] = normalized_accepted

        if matched is not None:
            scores.append(SlotScore(slot_key, per_slot, per_slot, Verdict.CORRECT,
                                    raw_response=raw, normalized_response=got.value,
                                    matched_alternative=matched, explain=explain))
        else:
            scores.append(SlotScore(slot_key, Decimal(0), per_slot, Verdict.INCORRECT,
                                    raw_response=raw, normalized_response=got.value,
                                    explain=explain))

    return _aggregate(ItemScore(tuple(scores)), ctx.spec)


# ── set_selection ────────────────────────────────────────────────────

def _selection(response: Any) -> list[str]:
    """The chosen option ids, from either shape a response arrives in.

    **This read `response["selected"]` only, and the exam engine never builds
    that.** `ExamSession._score` assembles every answer as
    `{"slots": {slot_key: value}}` — it is generic by design, dispatching on the
    primitive and knowing nothing about any question type — so a real `mcq_multi`
    sitting arrived here as `{"slots": {"s1": ["B", "D"]}}`, matched no
    `selected` key, and scored **unanswered, zero**, whatever the student ticked.

    Measured: the same key and the same picks score 2/2 through
    `{"selected": [...]}` and 0/2 through `{"slots": {...}}`. The unit tests
    passed throughout because they call this primitive directly with the first
    shape, which nothing in the running product produces.

    Fixed here rather than in the assembler: teaching `_score` that some types
    take a selection would put question-type knowledge in the exam engine, which
    is exactly what the primitive design exists to prevent. A primitive
    understanding its own response is the boundary working as intended.
    """
    if not isinstance(response, dict):
        return []
    if "selected" in response:
        return [str(s) for s in (response.get("selected") or [])]
    # The assembled shape: one slot whose value is the selection. A bare string is
    # a single pick — a client with a one-choice UI should not have to wrap it.
    for value in (_response_slots(response) or {}).values():
        if isinstance(value, (list, tuple)):
            return [str(s) for s in value]
        if value is not None:
            return [str(value)]
    return []


def set_selection(response: Any, key: dict[str, Any], ctx: ScoringContext) -> ItemScore:
    """Choose K of N, unordered, partial credit. Covers `mcq_multi`.

    Over-selection scores ZERO for the whole item, as in the real exam: a student
    who ticks four boxes when asked for two has not demonstrated the skill, and
    partial credit there would reward shotgunning.
    """
    correct = [str(c) for c in (key.get("correct") or [])]
    correct_norm = {c.strip().casefold() for c in correct}
    select_count = ctx.spec.options.get("select_count", "from_key")
    expected = len(correct) if select_count == "from_key" else int(select_count)
    per_correct = Decimal(str(ctx.spec.options.get("points_per_correct", 1)))
    max_points = per_correct * expected

    selected_raw = _selection(response)
    selected_norm = {s.strip().casefold() for s in selected_raw}

    explain: dict[str, Any] = {
        "primitive": "set_selection",
        "expected_count": expected,
        "selected_count": len(selected_norm),
        "accepted": correct,
    }

    if not selected_norm:
        return ItemScore((SlotScore("selection", Decimal(0), max_points,
                                    Verdict.UNANSWERED, explain=explain),))

    if len(selected_norm) > expected and ctx.spec.options.get("over_selection") == "score_zero":
        explain["reason"] = "over_selection"
        return ItemScore((SlotScore("selection", Decimal(0), max_points, Verdict.INCORRECT,
                                    raw_response=", ".join(sorted(selected_raw)),
                                    explain=explain),))

    hits = selected_norm & correct_norm
    awarded = per_correct * len(hits)
    explain["matched"] = sorted(hits)
    verdict = (
        Verdict.CORRECT if awarded == max_points
        else Verdict.INCORRECT if awarded == 0
        else Verdict.PARTIAL
    )
    return ItemScore((SlotScore("selection", awarded, max_points, verdict,
                                raw_response=", ".join(sorted(selected_raw)),
                                explain=explain),))


# ── aggregation ──────────────────────────────────────────────────────

def _aggregate(score: ItemScore, spec: ScoringSpec) -> ItemScore:
    """`per_slot` (default) awards each slot independently; `all_or_nothing`
    collapses to full marks or none, for items the exam treats as one answer."""
    if spec.options.get("aggregate", "per_slot") != "all_or_nothing":
        return score
    total_max = score.max_points
    all_correct = all(s.verdict is Verdict.CORRECT for s in score.slots)
    return ItemScore(tuple(
        SlotScore(
            s.slot_key,
            (total_max if all_correct else Decimal(0)) if i == 0 else Decimal(0),
            total_max if i == 0 else Decimal(0),
            Verdict.CORRECT if all_correct else s.verdict,
            s.raw_response, s.normalized_response, s.matched_alternative,
            {**s.explain, "aggregate": "all_or_nothing"},
        )
        for i, s in enumerate(score.slots)
    ))


PRIMITIVES = {
    "choice_per_slot": choice_per_slot,
    "text_per_slot": text_per_slot,
    "set_selection": set_selection,
}
