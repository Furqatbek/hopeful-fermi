"""Value types for the scoring engine.

Plain dataclasses, not Pydantic: the scoring path runs once per slot per attempt
(40 slots x 50k attempts a year) and must stay dependency-light and fast. Pydantic
lives at the API boundary, where its JSON Schema generation earns its keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARTIAL = "partial"
    UNANSWERED = "unanswered"
    VOID = "void"


class Primitive(StrEnum):
    """The closed set. A fourth member is code plus a deploy, and should be rare."""

    CHOICE_PER_SLOT = "choice_per_slot"
    TEXT_PER_SLOT = "text_per_slot"
    SET_SELECTION = "set_selection"


# Canonical normalizer order. A definition SELECTS from this list; it does not
# reorder it. Applying `casefold` after `spelling_uk_us` would silently break
# every lexicon lookup, and no author should be able to cause that by writing
# their JSON in a different order.
NORMALIZER_ORDER: tuple[str, ...] = (
    "trim",
    "collapse_space",
    "casefold",
    "strip_punctuation",
    "hyphen_flexible",
    "strip_articles",
    "contraction_expand",
    "spelling_uk_us",
    "number_word",
    "ordinal_digit",
)


@dataclass(frozen=True, slots=True)
class Option:
    id: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class WordLimit:
    """`NO MORE THAN TWO WORDS AND/OR A NUMBER`, as an enforceable rule."""

    max_words: int
    allow_number: bool = True
    hyphen_counts_as_one: bool = True
    on_violation: str = "mark_incorrect"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> WordLimit | None:
        if not raw:
            return None
        return cls(
            max_words=int(raw["max_words"]),
            allow_number=bool(raw.get("allow_number", True)),
            hyphen_counts_as_one=bool(raw.get("hyphen_counts_as_one", True)),
            on_violation=str(raw.get("on_violation", "mark_incorrect")),
        )


@dataclass(frozen=True, slots=True)
class GroupRules:
    """What the question group contributes to scoring one of its items."""

    word_limit: WordLimit | None = None
    option_bank: tuple[Option, ...] = ()
    unique_options: bool | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> GroupRules:
        raw = raw or {}
        bank = tuple(
            Option(id=str(o["id"]), text=str(o.get("text", "")))
            for o in (raw.get("option_bank") or [])
        )
        return cls(
            word_limit=WordLimit.from_dict(raw.get("word_limit")),
            option_bank=bank,
            unique_options=raw.get("unique_options"),
        )


@dataclass(frozen=True, slots=True)
class ScoringSpec:
    primitive: Primitive
    options: dict[str, Any] = field(default_factory=dict)
    normalizers: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScoringSpec:
        requested = set(raw.get("normalizers") or ())
        unknown = requested - set(NORMALIZER_ORDER)
        if unknown:
            raise ValueError(f"unknown normalizers: {sorted(unknown)}")
        return cls(
            primitive=Primitive(raw["primitive"]),
            options=dict(raw.get("options") or {}),
            # Reordered into canonical order regardless of how it was written.
            normalizers=tuple(n for n in NORMALIZER_ORDER if n in requested),
        )


@dataclass(frozen=True, slots=True)
class QuestionTypeDef:
    key: str
    version: int
    title: str
    skills: tuple[str, ...]
    payload_schema: dict[str, Any]
    key_schema: dict[str, Any]
    response_schema: dict[str, Any]
    scoring: ScoringSpec
    validation: dict[str, Any] = field(default_factory=dict)
    authoring: dict[str, Any] = field(default_factory=dict)
    status: str = "active"

    @property
    def ref(self) -> str:
        return f"{self.key}@v{self.version}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QuestionTypeDef:
        return cls(
            key=raw["key"],
            version=int(raw["version"]),
            title=raw["title"],
            skills=tuple(raw["skills"]),
            payload_schema=raw["payload_schema"],
            key_schema=raw["key_schema"],
            response_schema=raw["response_schema"],
            scoring=ScoringSpec.from_dict(raw["scoring"]),
            validation=raw.get("validation") or {},
            authoring=raw.get("authoring") or {},
            status=raw.get("status", "active"),
        )


@dataclass(frozen=True, slots=True)
class SlotScore:
    slot_key: str
    awarded: Decimal
    max_points: Decimal
    verdict: Verdict
    raw_response: str | None = None
    normalized_response: str | None = None
    matched_alternative: str | None = None
    # Which normalizers ran, what each changed, and what was compared to what.
    # This is the answer to "why was my answer marked wrong" and the single most
    # useful support artefact the product produces.
    explain: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ItemScore:
    slots: tuple[SlotScore, ...]

    @property
    def awarded(self) -> Decimal:
        return sum((s.awarded for s in self.slots), Decimal(0))

    @property
    def max_points(self) -> Decimal:
        return sum((s.max_points for s in self.slots), Decimal(0))

    @property
    def verdict(self) -> Verdict:
        if not self.slots:
            return Verdict.VOID
        if all(s.verdict is Verdict.UNANSWERED for s in self.slots):
            return Verdict.UNANSWERED
        if self.awarded == self.max_points:
            return Verdict.CORRECT
        if self.awarded == 0:
            return Verdict.INCORRECT
        return Verdict.PARTIAL
