from __future__ import annotations

import pytest

from app.modules.content.composition import (
    BandMapRef,
    GroupNode,
    MediaRef,
    PassageRef,
    QuestionNode,
    SectionNode,
    TestComposition,
)

BAND_MAP = BandMapRef(
    xid="bm-1", max_raw=40,
    mapping=tuple({"raw_min": lo, "raw_max": lo, "band": 5.0} for lo in range(0, 41)),
)


def question(**kw) -> QuestionNode:
    base = dict(
        xid="q-1", type_key="sentence_completion", type_version=1,
        payload={"text": "The answer is {{s1}}.", "slots": ["s1"]},
        slot_keys=("s1",), key={"slots": {"s1": {"accept": ["library"]}}},
        key_version_xid="k-1",
    )
    base.update(kw)
    return QuestionNode(**base)


def group(questions=None, **kw) -> GroupNode:
    base = dict(
        xid="g-1", number_start=1,
        questions=tuple([question()] if questions is None else questions),
        word_limit={"max_words": 2, "allow_number": True},
    )
    base.update(kw)
    return GroupNode(**base)


def section(groups=None, **kw) -> SectionNode:
    base = dict(
        position=1, skill="reading", title="Reading Passage 1",
        groups=tuple([group()] if groups is None else groups),
        passage=PassageRef(xid="p-1", title="Cartography",
                           paragraph_labels=("A", "B", "C", "D")),
        time_limit_seconds=1200, declared_question_count=1,
    )
    base.update(kw)
    return SectionNode(**base)


def composition(sections=None, **kw) -> TestComposition:
    base = dict(xid="tv-1", title="Mock 1", band_map=BAND_MAP,
                sections=tuple([section()] if sections is None else sections))
    base.update(kw)
    return TestComposition(**base)


def listening_section(groups=None, **kw) -> SectionNode:
    base = dict(
        position=1, skill="listening", title="Listening Part 1",
        groups=tuple([group()] if groups is None else groups),
        audio=MediaRef(xid="a-1", status="ready", duration_ms=600_000),
        time_limit_seconds=600, declared_question_count=1,
    )
    base.update(kw)
    return SectionNode(**base)


@pytest.fixture
def valid_test() -> TestComposition:
    return composition()
