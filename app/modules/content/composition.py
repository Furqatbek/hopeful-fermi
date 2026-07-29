"""The composition tree the publish gate reads.

Deliberately DB-free value objects rather than ORM rows. The gate is then a pure
function of the composition, which means it is exhaustively testable without a
database and can be re-run at any time — including on a draft the author is still
editing, which is exactly when they want to see the problems.

The repository layer builds one of these from `test_versions` and its children.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MediaRef:
    xid: str
    status: str = "ready"
    duration_ms: int | None = None
    has_attestation: bool = True
    under_takedown: bool = False


@dataclass(frozen=True, slots=True)
class PassageRef:
    xid: str
    title: str
    paragraph_labels: tuple[str, ...] = ()
    blocks: tuple[dict[str, Any], ...] = ()
    has_attestation: bool = True
    under_takedown: bool = False


@dataclass(frozen=True, slots=True)
class BandMapRef:
    xid: str
    max_raw: int
    mapping: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionNode:
    xid: str
    type_key: str
    type_version: int
    payload: dict[str, Any]
    slot_keys: tuple[str, ...]
    points: float = 1.0
    key: dict[str, Any] | None = None
    key_version_xid: str | None = None
    tolerance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroupNode:
    xid: str
    number_start: int
    questions: tuple[QuestionNode, ...]
    instructions: dict[str, Any] = field(default_factory=dict)
    word_limit: dict[str, Any] | None = None
    option_bank: tuple[dict[str, Any], ...] = ()
    unique_options: bool | None = None
    diagram_media: MediaRef | None = None
    hotspots: tuple[dict[str, Any], ...] = ()
    audio_start_ms: int | None = None
    audio_end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SectionNode:
    position: int
    skill: str
    title: str
    groups: tuple[GroupNode, ...]
    passage: PassageRef | None = None
    audio: MediaRef | None = None
    time_limit_seconds: int | None = None
    declared_question_count: int | None = None
    play_once: bool = True


@dataclass(frozen=True, slots=True)
class TestComposition:
    xid: str
    title: str
    sections: tuple[SectionNode, ...]
    band_map: BandMapRef | None = None

    def questions(self):
        for s in self.sections:
            for g in s.groups:
                for q in g.questions:
                    yield s, g, q

    @property
    def total_slots(self) -> int:
        return sum(len(q.slot_keys) for _, _, q in self.questions())
