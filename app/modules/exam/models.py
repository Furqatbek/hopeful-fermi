"""PRIVATE to the exam module."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin
from app.platform.ids import new_xid


class Assignment(IdMixin, Base):
    __tablename__ = "assignments"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cohort_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    test_version_id: Mapped[int] = mapped_column(BigInteger, default=0)
    assigned_by: Mapped[int] = mapped_column(BigInteger, default=0)
    target_kind: Mapped[str] = mapped_column(Text, default="cohort")
    opens_at: Mapped[dt.datetime] = mapped_column()
    closes_at: Mapped[dt.datetime] = mapped_column()
    time_limit_seconds: Mapped[int | None] = mapped_column(default=None)
    max_attempts: Mapped[int] = mapped_column(default=1)
    mode: Mapped[str] = mapped_column(Text, default="exam")
    allow_review_after: Mapped[str] = mapped_column(Text, default="close")
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class AssignmentTarget(Base):
    __tablename__ = "assignment_targets"

    assignment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assignments.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class Attempt(IdMixin, Base):
    __tablename__ = "attempts"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    test_version_id: Mapped[int] = mapped_column(BigInteger, default=0)
    assignment_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    competition_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # NULL = the student's own private practice, which never appears in a centre's
    # analytics. Set from the assignment when there is one.
    org_context_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    mode: Mapped[str] = mapped_column(Text, default="exam")
    attempt_no: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(Text, default="issued")
    issued_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # Absolute server deadline. The client never computes this and is never
    # trusted with it.
    expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    scored_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    submitted_via: Mapped[str | None] = mapped_column(Text, default=None)
    # Recorded, not punished: four seconds late on a mobile network is a hiccup.
    late_by_ms: Mapped[int | None] = mapped_column(default=None)
    current_score_run_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    client: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class AttemptSection(IdMixin, Base):
    """Where play-once is enforced server-side: the grant is issued once and
    `audio_locked_at` closes the door."""

    __tablename__ = "attempt_sections"

    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("attempts.id"))
    section_id: Mapped[int] = mapped_column(BigInteger, default=0)
    position: Mapped[int] = mapped_column(default=1)
    entered_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    completed_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    audio_play_count: Mapped[int] = mapped_column(default=0)
    audio_started_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    audio_locked_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class AttemptAnswer(IdMixin, Base):
    """Current answer state, upserted by autosave and frozen at submit by a
    database trigger — not by application discipline."""

    __tablename__ = "attempt_answers"

    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("attempts.id"))
    question_version_id: Mapped[int] = mapped_column(BigInteger, default=0)
    slot_key: Mapped[str] = mapped_column(Text, default="s1")
    response: Mapped[Any] = mapped_column(JSONB, default=None)
    revision: Mapped[int] = mapped_column(default=1)
    # Monotonic per slot. A delta below the stored revision is ignored, so
    # out-of-order delivery cannot resurrect an older answer.
    client_seq: Mapped[int | None] = mapped_column(BigInteger, default=None)
    client_ts: Mapped[dt.datetime | None] = mapped_column(default=None)
    first_answered_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    time_spent_ms: Mapped[int] = mapped_column(default=0)


class ScoreRun(IdMixin, Base):
    """Immutable. A regrade INSERTS a new run and supersedes the old one, so the
    history of what a student was told survives."""

    __tablename__ = "score_runs"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("attempts.id"))
    reason: Mapped[str] = mapped_column(Text, default="initial")
    regrade_job_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    engine_version: Mapped[str] = mapped_column(Text, default="1.0.0")
    band_map_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # {question_version_id: answer_key_version_id} — the exact inputs, and the
    # column that makes a score reproducible years later.
    key_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    raw_score: Mapped[float] = mapped_column(Numeric(7, 2), default=0)
    max_raw: Mapped[float] = mapped_column(Numeric(7, 2), default=0)
    band: Mapped[float | None] = mapped_column(Numeric(2, 1), default=None)
    per_section: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_current: Mapped[bool] = mapped_column(default=True)
    superseded_by_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    computed_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    computed_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)


class ItemScore(IdMixin, Base):
    __tablename__ = "item_scores"

    score_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("score_runs.id"))
    question_id: Mapped[int] = mapped_column(BigInteger, default=0)
    question_version_id: Mapped[int] = mapped_column(BigInteger, default=0)
    answer_key_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    slot_key: Mapped[str] = mapped_column(Text, default="s1")
    awarded: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    max_points: Mapped[float] = mapped_column(Numeric(6, 2), default=1)
    verdict: Mapped[str] = mapped_column(Text, default="incorrect")
    raw_response: Mapped[str | None] = mapped_column(Text, default=None)
    normalized_response: Mapped[str | None] = mapped_column(Text, default=None)
    matched_alternative: Mapped[str | None] = mapped_column(Text, default=None)
    # Which normalizers ran and what was compared to what: the answer to "why was
    # my answer marked wrong", and the most useful support artefact we produce.
    explain: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class RegradeJob(IdMixin, Base):
    __tablename__ = "regrade_jobs"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    trigger: Mapped[str] = mapped_column(Text, default="answer_key_change")
    subject_type: Mapped[str] = mapped_column(Text, default="question_version")
    subject_id: Mapped[int] = mapped_column(BigInteger, default=0)
    from_key_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    to_key_version_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    initiated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Starts as a dry run: nothing is applied until someone has seen the numbers.
    dry_run: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(Text, default="planning")
    attempts_total: Mapped[int] = mapped_column(default=0)
    attempts_processed: Mapped[int] = mapped_column(default=0)
    scores_changed: Mapped[int] = mapped_column(default=0)
    bands_changed: Mapped[int] = mapped_column(default=0)
    competition_impact: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, default=None)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    finished_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class Outbox(IdMixin, Base):
    """Written in the SAME transaction as the domain change. This is why a
    regrade enqueued alongside a key fix cannot be lost, and why Kafka is not
    needed (ADR-0001 §6)."""

    __tablename__ = "outbox"

    aggregate_type: Mapped[str] = mapped_column(Text, default="")
    aggregate_id: Mapped[str] = mapped_column(Text, default="")
    event_type: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    available_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    dispatched_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class IdempotencyKey(IdMixin, Base):
    """The dedupe anchor for every write a flaky mobile client may retry."""

    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(Text, default="")
    key: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    request_hash: Mapped[str] = mapped_column(Text, default="")
    response_status: Mapped[int | None] = mapped_column(default=None)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[dt.datetime] = mapped_column()
