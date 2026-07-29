"""The question-type registry and the scorer that consumes it.

This module is the only place in the system that knows question types exist as a
concept. The exam engine calls `Scorer.score_item()` and never names a type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.platform.errors import RegistryError

from .lexicon import Lexicon, StaticLexiconSource
from .normalizers import Pipeline
from .primitives import PRIMITIVES, ScoringContext
from .schemas import GroupRules, ItemScore, QuestionTypeDef

ENGINE_VERSION = "1.0.0"
"""Recorded on every score run. Without it, fixing a normalizer bug would make
old and new scores silently incomparable."""


class Registry:
    """Question type definitions, keyed by (key, version).

    Content binds to an exact `(key, version)`, so changing a definition can never
    retroactively reinterpret a question that was already sat.
    """

    __slots__ = ("_defs",)

    def __init__(self, defs: Iterable[QuestionTypeDef] = ()) -> None:
        self._defs: dict[tuple[str, int], QuestionTypeDef] = {}
        for d in defs:
            self.register(d)

    def register(self, definition: QuestionTypeDef) -> None:
        ref = (definition.key, definition.version)
        if ref in self._defs:
            raise RegistryError(
                f"{definition.ref} already registered; bump the version instead",
                code_ref=definition.ref,
            )
        if definition.scoring.primitive.value not in PRIMITIVES:
            raise RegistryError(f"{definition.ref}: unknown primitive")
        self._defs[ref] = definition

    def get(self, key: str, version: int) -> QuestionTypeDef:
        try:
            return self._defs[(key, version)]
        except KeyError:
            raise RegistryError(f"question type {key}@v{version} is not registered") from None

    def all(self) -> tuple[QuestionTypeDef, ...]:
        return tuple(self._defs.values())

    def for_skill(self, skill: str) -> tuple[QuestionTypeDef, ...]:
        return tuple(d for d in self._defs.values()
                     if skill in d.skills and d.status == "active")

    def __len__(self) -> int:
        return len(self._defs)

    @classmethod
    def from_directory(cls, path: Path) -> Registry:
        """Load `registry/question_types/*.json`.

        Adding a type in production is an INSERT through the admin API; this
        loader is the dev and boot-time path onto the same table.
        """
        defs = []
        for file in sorted(Path(path).glob("*.json")):
            raw = json.loads(file.read_text(encoding="utf-8"))
            raw.pop("_comment", None)
            try:
                defs.append(QuestionTypeDef.from_dict(raw))
            except (KeyError, ValueError) as exc:
                raise RegistryError(f"{file.name}: {exc}") from exc
        return cls(defs)


def load_lexicon(path: Path) -> Lexicon:
    rows: list[dict] = []
    for file in sorted(Path(path).glob("*.json")):
        rows.extend(json.loads(file.read_text(encoding="utf-8")))
    return Lexicon(StaticLexiconSource(rows).entries())


@dataclass(frozen=True, slots=True)
class ScoreRequest:
    """One item to score. Deliberately flat and DB-free so the scoring engine can
    be tested exhaustively without a database."""

    type_key: str
    type_version: int
    payload: dict[str, Any]
    key: dict[str, Any]
    response: Any
    group: GroupRules = GroupRules()
    tolerance: dict[str, Any] | None = None
    paragraph_labels: tuple[str, ...] = ()


class Scorer:
    """Registry-driven scoring. Contains no question-type-specific branches."""

    __slots__ = ("_registry", "_lexicon", "engine_version")

    def __init__(self, registry: Registry, lexicon: Lexicon,
                 engine_version: str = ENGINE_VERSION) -> None:
        self._registry = registry
        self._lexicon = lexicon
        self.engine_version = engine_version

    def score_item(self, req: ScoreRequest) -> ItemScore:
        definition = self._registry.get(req.type_key, req.type_version)
        spec = definition.scoring

        # Type-level normalizers, overridable at the key level via tolerance.
        names = spec.normalizers
        tolerance = req.tolerance or {}
        if override := tolerance.get("normalizers"):
            names = tuple(override)
        pipeline = Pipeline(names, self._lexicon)

        group = req.group
        if limit := tolerance.get("word_limit"):
            from .schemas import WordLimit
            group = GroupRules(
                word_limit=WordLimit.from_dict(limit),
                option_bank=group.option_bank,
                unique_options=group.unique_options,
            )

        ctx = ScoringContext(
            payload=req.payload,
            group=group,
            spec=spec,
            pipeline=pipeline,
            tolerance=tolerance,
            paragraph_labels=req.paragraph_labels,
        )
        return PRIMITIVES[spec.primitive.value](req.response, req.key, ctx)
