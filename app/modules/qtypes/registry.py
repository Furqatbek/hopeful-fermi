"""The question-type registry and the scorer that consumes it.

This module is the only place in the system that knows question types exist as a
concept. The exam engine calls `Scorer.score_item()` and never names a type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from app.platform.errors import RegistryError

from .lexicon import Lexicon, StaticLexiconSource
from .normalizers import Pipeline
from .primitives import PRIMITIVES, ScoringContext
from .schemas import GroupRules, ItemScore, QuestionTypeDef

ENGINE_VERSION = "1.0.0"
"""Recorded on every score run. Without it, fixing a normalizer bug would make
old and new scores silently incomparable."""

_VALIDATORS: dict[str, Draft202012Validator] = {}
"""Compiled per-slot response validators, keyed by `definition.ref`.

Module-level rather than per-Registry because a definition is immutable and bound
to an exact `(key, version)`: `sentence_completion@v1` means one thing for ever,
so its validator can be built once for the process.
"""


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

    def validate_response(self, key: str, version: int, value: Any) -> str | None:
        """Check ONE slot's answer against the type. Returns why it is invalid.

        The enforcement the contract has always described and nothing performed.
        `response_schema` was loaded, stored in `question_type_defs`, served over
        `/question-types`, and read by no validator anywhere — so the one field
        naming what a student is allowed to send constrained nothing, and the
        answers endpoint accepted any JSON at all.

        Refusing is the point. An answer the server cannot score must not be
        stored as though it were scoreable: the failure mode of accepting it is
        not an error message, it is a wrong band on a real exam, arrived at
        silently.

        A type whose shape `slot_response_schema` cannot read is refused rather
        than waved through. That direction matters — a new type added with an
        unfamiliar response shape should fail loudly here on its first answer,
        not accept everything until somebody notices the marks are wrong.
        """
        if value is None:
            # **Clearing is an operation, not an answer.** "Null clears the
            # answer" is a wire-level rule the contract states for every type, and
            # it has to outrank the type's answer shape: `mcq_multi` declares
            # `{"type": "array"}`, which does not admit null, so schema-checking a
            # clear would tell a student who unticked every box that their
            # deselection was malformed and leave the old picks stored.
            return None
        definition = self.get(key, version)
        schema = definition.slot_response_schema
        if schema is None:
            return (f"{definition.ref} does not declare a per-slot response shape, "
                    "so an answer for it cannot be checked.")
        validator = _VALIDATORS.get(definition.ref)
        if validator is None:
            # Compiled once per type. A batch carries up to 200 deltas and a
            # sitting is thousands, so rebuilding the validator per answer would
            # put schema compilation on the exam's hot path.
            validator = _VALIDATORS[definition.ref] = Draft202012Validator(schema)
        error = best_match(validator.iter_errors(value))
        return None if error is None else error.message

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

    def validate_response(self, key: str, version: int, value: Any) -> str | None:
        """Delegated so callers holding a `Scorer` need not also hold a Registry.

        The exam engine has a scorer and nothing else from this module, which is
        the boundary working: it asks "can this be scored" of the same object that
        would score it.
        """
        return self._registry.validate_response(key, version, value)

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


# ── the process-wide instance ────────────────────────────────────────

# The registry is a few hundred kilobytes of JSON that never changes within a
# process, and both composition roots need it — the API to validate an authored
# question, a worker to rescore ten thousand attempts. It is cached HERE rather
# than in either root, because a worker importing `app.api.deps` to borrow the
# API's cache would be a background job depending on HTTP transport, which the
# import contracts forbid and which would be wrong even if they did not.
REGISTRY_ROOT = Path(__file__).resolve().parents[3] / "registry"

_registry: Registry | None = None
_scorer: Scorer | None = None
#: The database generation these were built from. `None` means "files only",
#: which is what a process that has never seen a database still gets.
_generation: tuple[int, int] | None = None
_checked_at: float = 0.0

#: How stale a definition may be, in seconds. The contract promises "scorer
#: caches invalidate within seconds"; this is that number. One tiny query per
#: worker per interval, off the per-answer path.
RELOAD_AFTER_SECONDS = 5.0


def default_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry.from_directory(REGISTRY_ROOT / "question_types")
    return _registry


def default_scorer() -> Scorer:
    global _scorer
    if _scorer is None:
        _scorer = Scorer(default_registry(), load_lexicon(REGISTRY_ROOT / "lexicon"))
    return _scorer


def refresh_from_db(session, *, force: bool = False) -> bool:
    """Rebuild the process-wide registry and lexicon from the database.

    **Two endpoints wrote tables that nothing ever read.**

    `POST /admin/question-types` inserted into `question_type_defs` and then
    mutated this module's singleton in place. `default_registry()` loads a
    DIRECTORY and never read that table — so with `--workers 4` a newly
    registered type was live in one process out of four, and after a restart in
    none. Meanwhile `question_versions` carries a foreign key on
    `(type_key, type_version)`, so content authored against a custom type
    outlived the definition that scores it. "A new question type with no
    migration and no redeploy" is the core architectural bet of this product and
    it did not survive a deploy.

    `POST /admin/lexicon` was the same shape and worse in effect: the scorer's
    lexicon came from `StaticLexiconSource` over JSON files, so a UK/US pair
    added through the API was recorded and never marked anything. That endpoint
    is the whole remedy for "38 students wrote a form the key does not accept",
    and it did nothing at all.

    Returns True when something was rebuilt.

    The database wins on a `(key, version)` collision, and that is the point: a
    file is what shipped, a row is what an operator did afterwards. The files
    stay the boot-time floor so a fresh install and every unit test still work
    with no database at all.

    Invalidation is a GENERATION STAMP rather than a broadcast: `max(id)` and
    `count(*)` over the two tables, read at most once every
    `RELOAD_AFTER_SECONDS`. Deliberately not Redis pub/sub — four workers on one
    box do not need a message bus to notice a row, and the brief's answer to new
    infrastructure is "not yet". `count(*)` is in the stamp because a row can be
    deleted, which `max(id)` alone would miss.
    """
    global _registry, _scorer, _generation, _checked_at

    import time

    from sqlalchemy import text as _text

    now = time.monotonic()
    if not force and _generation is not None and now - _checked_at < RELOAD_AFTER_SECONDS:
        return False
    _checked_at = now

    stamp = session.execute(_text("""
        SELECT coalesce((SELECT max(id) FROM question_type_defs), 0)
               + coalesce((SELECT count(*) FROM question_type_defs), 0) AS types,
               coalesce((SELECT max(id) FROM lexicon_entries), 0)
               + coalesce((SELECT count(*) FROM lexicon_entries), 0) AS lex
    """)).one()
    generation = (int(stamp.types), int(stamp.lex))
    if not force and generation == _generation:
        return False

    _registry = _registry_from(session)
    _scorer = Scorer(_registry, _lexicon_from(session))
    _generation = generation
    return True


def _registry_from(session) -> Registry:
    """Files first, then rows over the top."""
    from sqlalchemy import text as _text

    from .schemas import QuestionTypeDef

    defs = {(d.key, d.version): d
            for d in Registry.from_directory(REGISTRY_ROOT / "question_types").all()}
    for row in session.execute(_text("""
        SELECT key, version, status, title, description, skills, payload_schema,
               key_schema, response_schema, scoring, validation, authoring
        FROM question_type_defs
    """)).mappings():
        try:
            definition = QuestionTypeDef.from_dict({
                "key": row["key"], "version": row["version"],
                "status": row["status"], "title": row["title"],
                "description": row["description"], "skills": list(row["skills"] or ()),
                "payload_schema": row["payload_schema"] or {},
                "key_schema": row["key_schema"] or {},
                "response_schema": row["response_schema"],
                "scoring": row["scoring"] or {},
                "validation": row["validation"] or {},
                "authoring": row["authoring"] or {},
            })
        except (KeyError, ValueError):
            # One malformed row must not take the whole registry down with it:
            # this is what every exam in progress is scored against. The row is
            # skipped and the file-shipped types survive.
            structlog.get_logger().warning(
                "question_type_def_unloadable",
                key=row["key"], version=row["version"])
            continue
        defs[(definition.key, definition.version)] = definition
    return Registry(list(defs.values()))


def _lexicon_from(session) -> Lexicon:
    """Files, then rows. Both go through `StaticLexiconSource` so one function
    decides what a row means and `bidirectional` cannot default differently on
    the two paths."""
    from sqlalchemy import text as _text

    rows: list[dict] = []
    for file in sorted((REGISTRY_ROOT / "lexicon").glob("*.json")):
        rows.extend(json.loads(file.read_text(encoding="utf-8")))
    rows.extend(
        {"kind": r["kind"], "a": r["a"], "b": r["b"],
         "bidirectional": bool(r["bidirectional"])}
        for r in session.execute(_text(
            "SELECT kind, a, b, bidirectional FROM lexicon_entries"
        )).mappings())
    return Lexicon(StaticLexiconSource(rows).entries())
