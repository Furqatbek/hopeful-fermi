"""The normalizer pipeline.

Applied in the canonical order declared by `NORMALIZER_ORDER`, filtered to the
set a question type selects. A definition selects; it does not reorder. See
`schemas.NORMALIZER_ORDER` for why.

Every stage records what it changed, so `SlotScore.explain` can show a student
the exact transformation chain that led to their mark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .lexicon import Lexicon
from .schemas import NORMALIZER_ORDER

_EDGE_PUNCT = "\"'“”‘’.,;:!?()[]{}…"
_ARTICLES = frozenset({"a", "an", "the"})
_ORDINAL_SUFFIX = re.compile(r"^(\d+)(st|nd|rd|th)$")
_ORDINAL_WORDS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12", "twentieth": "20", "thirtieth": "30",
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class Normalized:
    value: str
    steps: tuple[Step, ...]

    def as_explain(self) -> list[dict[str, str]]:
        return [{"normalizer": s.name, "before": s.before, "after": s.after} for s in self.steps]


# ── individual normalizers ───────────────────────────────────────────

def _trim(v: str, lex: Lexicon) -> str:
    return v.strip()


def _collapse_space(v: str, lex: Lexicon) -> str:
    return " ".join(v.split())


def _casefold(v: str, lex: Lexicon) -> str:
    return v.casefold()


def _strip_punctuation(v: str, lex: Lexicon) -> str:
    # Edges only. Internal hyphens and apostrophes are owned by hyphen_flexible
    # and contraction_expand, which run later and need them intact.
    return " ".join(t.strip(_EDGE_PUNCT) for t in v.split() if t.strip(_EDGE_PUNCT))


def _hyphen_flexible(v: str, lex: Lexicon) -> str:
    # `well-known` == `well known`. Runs after the word-limit check, so making
    # one token into two cannot turn a legal answer into an over-limit one.
    return " ".join(v.replace("-", " ").replace("–", " ").split())


def _strip_articles(v: str, lex: Lexicon) -> str:
    kept = [t for t in v.split() if t not in _ARTICLES]
    # An answer that is nothing BUT an article stays as written rather than
    # collapsing to the empty string, which would read as unanswered.
    return " ".join(kept) if kept else v


def _contraction_expand(v: str, lex: Lexicon) -> str:
    return " ".join(lex.expand_contraction(t) for t in v.split())


def _spelling_uk_us(v: str, lex: Lexicon) -> str:
    tokens = lex.apply_phrases("spelling_variant", v.split())
    return " ".join(lex.canonical_token("spelling_variant", t) for t in tokens)


def _ordinal_digit(v: str, lex: Lexicon) -> str:
    out = []
    for t in v.split():
        if (m := _ORDINAL_SUFFIX.match(t)):
            out.append(m.group(1))
        else:
            out.append(_ORDINAL_WORDS.get(t, t))
    return " ".join(out)


def _number_word(v: str, lex: Lexicon) -> str:
    """Canonicalize spelled numbers to digits: `fourteen` -> `14`.

    Handles single tokens, `twenty five` / `twenty-five` compounds, and scale
    words (`three hundred`). Anything it cannot parse is left untouched — a
    normalizer that guesses is worse than one that declines.
    """
    tokens = lex.apply_phrases("number_word", v.split())
    tokens = [x for t in tokens for x in (t.split("-") if "-" in t else [t])]
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        consumed, value = _parse_number(tokens, i, lex)
        if consumed:
            out.append(str(value))
            i += consumed
        else:
            out.append(lex.canonical_token("unit_form", tokens[i]))
            i += 1
    return " ".join(out)


def _digit_of(token: str, lex: Lexicon) -> int | None:
    canon = lex.canonical_token("number_word", token)
    if canon.isdigit():
        return int(canon)
    return None


def _parse_number(tokens: list[str], i: int, lex: Lexicon) -> tuple[int, int]:
    """Returns (tokens_consumed, value). (0, 0) means "not a number here"."""
    first = _digit_of(tokens[i], lex)
    if first is None:
        return 0, 0

    total = first
    consumed = 1

    # `three hundred`, `two thousand`
    if i + 1 < len(tokens) and (scale := _SCALES.get(tokens[i + 1])):
        total = first * scale
        consumed = 2
        # `three hundred and fifty`
        j = i + 2
        if j < len(tokens) and tokens[j] == "and":
            j += 1
        if j < len(tokens):
            rest = _digit_of(tokens[j], lex)
            if rest is not None and rest < scale:
                nested, value = _parse_number(tokens, j, lex)
                if nested:
                    total += value
                    consumed = j - i + nested
        return consumed, total

    # `twenty five` -> 25. Only tens (20..90) take a unit follower.
    if 20 <= first <= 90 and first % 10 == 0 and i + 1 < len(tokens):
        unit = _digit_of(tokens[i + 1], lex)
        if unit is not None and 1 <= unit <= 9:
            return 2, first + unit

    return consumed, total


_IMPLEMENTATIONS: dict[str, Callable[[str, Lexicon], str]] = {
    "trim": _trim,
    "collapse_space": _collapse_space,
    "casefold": _casefold,
    "strip_punctuation": _strip_punctuation,
    "hyphen_flexible": _hyphen_flexible,
    "strip_articles": _strip_articles,
    "contraction_expand": _contraction_expand,
    "spelling_uk_us": _spelling_uk_us,
    "number_word": _number_word,
    "ordinal_digit": _ordinal_digit,
}

assert set(_IMPLEMENTATIONS) == set(NORMALIZER_ORDER), (
    "every declared normalizer needs an implementation, and vice versa"
)


class Pipeline:
    """A resolved, ordered normalizer chain bound to one lexicon."""

    __slots__ = ("_names", "_lex")

    def __init__(self, names: tuple[str, ...], lexicon: Lexicon) -> None:
        # Canonical order, whatever order the caller passed.
        self._names = tuple(n for n in NORMALIZER_ORDER if n in set(names))
        self._lex = lexicon

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def without(self, *skip: str) -> Pipeline:
        """Per-key overrides, e.g. `case_sensitive: true` drops `casefold`."""
        return Pipeline(tuple(n for n in self._names if n not in skip), self._lex)

    def replacing(self, names: tuple[str, ...]) -> Pipeline:
        """Per-key normalizer override from `answer_key_versions.tolerance`."""
        return Pipeline(names, self._lex)

    def run(self, value: str) -> Normalized:
        steps: list[Step] = []
        current = value
        for name in self._names:
            after = _IMPLEMENTATIONS[name](current, self._lex)
            if after != current:
                steps.append(Step(name=name, before=current, after=after))
                current = after
        return Normalized(value=current, steps=tuple(steps))
