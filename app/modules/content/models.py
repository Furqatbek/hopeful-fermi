"""PRIVATE to the content module.

Covers the authoring subset phases 5-8 actually read and write. Media, cue cards,
grants, exposure and takedowns exist in the schema (migrations 0006-0010) but have
no mapper yet — they are written by paths not built in this phase, and an unused
mapper is a lie about what the code does.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ARRAY, BigInteger, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin
from app.platform.ids import new_xid


class Passage(IdMixin, Base):
    __tablename__ = "passages"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # DEFAULT org_private: the contractual promise is a column default, not a
    # code-review note.
    visibility: Mapped[str] = mapped_column(Text, default="org_private")
    title: Mapped[str] = mapped_column(Text, default="")
    skill: Mapped[str] = mapped_column(Text, default="reading")
    topic: Mapped[str | None] = mapped_column(Text, default=None)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class PassageVersion(IdMixin, Base):
    __tablename__ = "passage_versions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    passage_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("passages.id"))
    version_no: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(Text, default="draft")
    title: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Assigned server-side on every save, so matching-headings references can
    # never drift from the passage the author is looking at.
    paragraph_labels: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    word_count: Mapped[int] = mapped_column(default=0)
    checksum: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    published_by: Mapped[int | None] = mapped_column(BigInteger, default=None)


class AudioTrack(IdMixin, Base):
    __tablename__ = "audio_tracks"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    visibility: Mapped[str] = mapped_column(Text, default="org_private")
    title: Mapped[str] = mapped_column(Text, default="")
    accent: Mapped[str | None] = mapped_column(Text, default=None)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    master_media_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    delivery_media_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    # Measured at ingest. The column existed in the schema and was missing from
    # this mapping, so the API could never report it.
    loudness_lufs: Mapped[float | None] = mapped_column(Numeric(5, 2), default=None)
    status: Mapped[str] = mapped_column(Text, default="draft")
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class Question(IdMixin, Base):
    """Question IDENTITY, first-class and outliving any test — item analysis and
    exposure both ask "how does THIS item behave" across every test it appeared in."""

    __tablename__ = "questions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    visibility: Mapped[str] = mapped_column(Text, default="org_private")
    type_key: Mapped[str] = mapped_column(Text, default="")
    skill: Mapped[str] = mapped_column(Text, default="reading")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    difficulty_hint: Mapped[float | None] = mapped_column(Numeric(3, 2), default=None)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class QuestionVersion(IdMixin, Base):
    __tablename__ = "question_versions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    question_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("questions.id"))
    version_no: Mapped[int] = mapped_column(default=1)
    type_key: Mapped[str] = mapped_column(Text, default="")
    type_version: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Extracted server-side from the payload; the client does not get to declare
    # these, or the publish gate would be checking the client's own claim.
    slot_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    points: Mapped[float] = mapped_column(Numeric(6, 2), default=1)
    status: Mapped[str] = mapped_column(Text, default="draft")
    checksum: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class AnswerKeyVersion(IdMixin, Base):
    """The table that makes regrade possible without breaking immutability: the
    question version stays frozen and a new key version supersedes the old one."""

    __tablename__ = "answer_key_versions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    question_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("question_versions.id"))
    version_no: Mapped[int] = mapped_column(default=1)
    key: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tolerance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_current: Mapped[bool] = mapped_column(default=True)
    reason: Mapped[str] = mapped_column(Text, default="initial")
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    superseded_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    superseded_by_id: Mapped[int | None] = mapped_column(BigInteger, default=None)


class QuestionGroup(IdMixin, Base):
    __tablename__ = "question_groups"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    visibility: Mapped[str] = mapped_column(Text, default="org_private")
    title: Mapped[str] = mapped_column(Text, default="")
    skill: Mapped[str] = mapped_column(Text, default="reading")
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class QuestionGroupVersion(IdMixin, Base):
    """Carries what an IELTS question set shares: instructions, the word-limit
    rule the scorer enforces, and the option bank for matching/word-bank types."""

    __tablename__ = "question_group_versions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("question_groups.id"))
    version_no: Mapped[int] = mapped_column(default=1)
    instructions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    word_limit: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    option_bank: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, default=None)
    display: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    diagram_media_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    hotspots: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, default=None)
    status: Mapped[str] = mapped_column(Text, default="draft")
    checksum: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class QuestionGroupItem(IdMixin, Base):
    __tablename__ = "question_group_items"

    group_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("question_group_versions.id"))
    question_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("question_versions.id"))
    position: Mapped[int] = mapped_column(default=1)


class BandMap(IdMixin, Base):
    __tablename__ = "band_maps"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    name: Mapped[str] = mapped_column(Text, default="")
    skill: Mapped[str] = mapped_column(Text, default="reading")
    variant: Mapped[str] = mapped_column(Text, default="academic")
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class BandMapVersion(IdMixin, Base):
    __tablename__ = "band_map_versions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    band_map_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("band_maps.id"))
    version_no: Mapped[int] = mapped_column(default=1)
    mapping: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    max_raw: Mapped[int] = mapped_column(default=40)
    status: Mapped[str] = mapped_column(Text, default="draft")
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class Test(IdMixin, Base):
    __tablename__ = "tests"
    __test__ = False   # an ORM class, not a pytest collection target

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    visibility: Mapped[str] = mapped_column(Text, default="org_private")
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str | None] = mapped_column(Text, default=None)
    kind: Mapped[str] = mapped_column(Text, default="mock")
    variant: Mapped[str] = mapped_column(Text, default="academic")
    skills: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    licence: Mapped[str] = mapped_column(Text, default="proprietary")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    current_published_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class TestVersion(IdMixin, Base):
    __tablename__ = "test_versions"
    __test__ = False

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    test_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tests.id"))
    version_no: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(Text, default="draft")
    title: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    band_map_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    total_questions: Mapped[int] = mapped_column(default=0)
    max_raw: Mapped[float] = mapped_column(Numeric(7, 2), default=0)
    # Materialized at publish: the whole resolved student-facing tree, so serving
    # a test is ONE row read. Contains no answer keys and no transcript.
    #
    # Deferred, because this is the paper (~200 KB, docs/design/0005 §4) and
    # most readers of a TestVersion want a scalar — `status` at start,
    # `band_map_version_id` at submit and in the sweeper, `title`/`xid` once per
    # row of the assignment and library listings. Loaded by default, a 25-row
    # student home screen moved 25 papers out of Postgres to emit 25 titles, and
    # every submit moved one to read an integer. The serving paths undefer it on
    # the same SELECT (`ExamSession.payload`, the competition lobby), so
    # `payload()` is still one row read; a reader that forgets gets one extra
    # SELECT by primary key, not a wrong answer.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None,
                                                            deferred=True)
    snapshot_bytes: Mapped[int | None] = mapped_column(default=None)
    checksum: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    submitted_for_review_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    published_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    published_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    cloned_from_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)


class TestVersionSection(IdMixin, Base):
    __test__ = False

    """Composition by REFERENCE to an asset version — this is what "pull an
    existing passage into a new test without copying it" means concretely."""

    __tablename__ = "test_version_sections"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    test_version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("test_versions.id"))
    position: Mapped[int] = mapped_column(default=1)
    skill: Mapped[str] = mapped_column(Text, default="reading")
    title: Mapped[str] = mapped_column(Text, default="")
    passage_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    audio_track_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    time_limit_seconds: Mapped[int | None] = mapped_column(default=None)
    declared_question_count: Mapped[int | None] = mapped_column(default=None)
    play_once: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class TestVersionGroup(IdMixin, Base):
    __tablename__ = "test_version_groups"
    __test__ = False

    section_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("test_version_sections.id"))
    group_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("question_group_versions.id"))
    position: Mapped[int] = mapped_column(default=1)
    number_start: Mapped[int] = mapped_column(default=1)
    audio_start_ms: Mapped[int | None] = mapped_column(default=None)
    audio_end_ms: Mapped[int | None] = mapped_column(default=None)


class TestVersionValidation(IdMixin, Base):
    __test__ = False

    """Persisted rather than returned-and-forgotten, so an author can close the
    tab and come back to the full problem list."""

    __tablename__ = "test_version_validations"

    test_version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("test_versions.id"))
    passed: Mapped[bool] = mapped_column(default=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error_count: Mapped[int] = mapped_column(default=0)
    warning_count: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    run_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    run_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class ImportJob(IdMixin, Base):
    __tablename__ = "import_jobs"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    source_format: Mapped[str] = mapped_column(Text, default="json")
    source_media_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    target_test_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    status: Mapped[str] = mapped_column(Text, default="received")
    # The parsed canonical Import JSON. Stored so a commit applies exactly what
    # the author reviewed in the dry run, not a re-parse that might differ.
    canonical: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    parse_error: Mapped[str | None] = mapped_column(Text, default=None)
    dry_run_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    committed_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    committed_test_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())
