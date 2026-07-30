"""Exam endpoints. Thin: they map DTOs and call `ExamSession`, nothing more.

Every rule about time, ordering and freezing lives in the exam module, so these
handlers stay short enough to read in one screen and the rules stay testable
without a web framework.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dto import iso, jsonify
from app.api.deps import (
    Idempotency, Principal, db, entitlements, exam_session, idempotency, principal,
)
from app.modules.billing.entitlements import Entitlements
from app.modules.content.models import TestVersion
from app.modules.exam.models import Attempt
from app.modules.exam.session import AnswerDelta, ExamSession
from app.platform.errors import Conflict, Forbidden, NotFound, TooEarly

router = APIRouter(prefix="/attempts", tags=["exam"])


class AttemptCreate(BaseModel):
    test_version_xid: uuid.UUID | None = None
    assignment_xid: uuid.UUID | None = None
    mode: str = Field(default="exam", pattern="^(exam|practice|preview)$")


class AnswerDeltaIn(BaseModel):
    question_version_xid: uuid.UUID
    slot_key: str
    response: Any = None
    client_seq: int
    time_spent_ms: int = 0


class AnswerBatch(BaseModel):
    deltas: list[AnswerDeltaIn] = Field(min_length=1, max_length=200)


def _attempt(session: Session, xid: uuid.UUID, actor: Principal) -> Attempt:
    attempt = session.scalars(select(Attempt).where(Attempt.xid == xid)).first()
    if attempt is None:
        raise NotFound("Attempt not found.")
    if attempt.user_id != actor.user_id:
        # Deliberately 404, not 403: confirming an attempt exists tells a prober
        # something they should not learn.
        raise NotFound("Attempt not found.")
    return attempt


@router.post("", status_code=status.HTTP_201_CREATED)
def start_attempt(body: AttemptCreate,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db),
                  exam: ExamSession = Depends(exam_session),
                  ents: Entitlements = Depends(entitlements),
                  idem: Idempotency = Depends(idempotency)) -> dict:
    """Start or resume.

    Two paths, and `assignment_xid` is the primary one — `test_version_xid` is
    self-serve practice, as the schema says. **The assignment path was accepted by
    the model and never read**, so a student opening work their teacher set had to
    fall through to self-serve: an attempt with `assignment_id = NULL`, carrying
    none of the assignment's time limit, mode or review rule, invisible to the
    teacher's progress view, uncounted against `max_attempts` — and charged to the
    student's own entitlement, which inverts the whole B2B model.

    Entitlement is checked HERE, once, through the single call site — no feature
    code re-implements "has this student paid". An ASSIGNED attempt is not checked
    against the student at all: the centre paid for it when the work was set
    ("a school pays per seat and its students never see a paywall for work the
    school set"), and re-charging the student here would be that paywall.
    """
    if replayed := idem.replay("attempts.start", body.model_dump(mode="json")):
        return replayed

    assignment = (_assignment_for(session, body.assignment_xid, actor)
                  if body.assignment_xid else None)
    if assignment is not None:
        return _start_assigned(assignment, actor, session, exam, idem, body)

    if body.test_version_xid is None:
        raise NotFound("A test version or assignment is required.")
    tv = session.scalars(
        select(TestVersion).where(TestVersion.xid == body.test_version_xid)).first()
    if tv is None:
        raise NotFound("Test version not found.")

    if body.mode != "preview":
        ents.require(user_xid=str(actor.user_id), feature="mock.unlimited",
                     org_xids=[str(o) for o in actor.org_ids])

    attempt = exam.start(user_id=actor.user_id, test_version_id=tv.id, mode=body.mode)
    payload = _attempt_dto(attempt, exam)
    idem.store(body.model_dump(mode="json"), payload, status.HTTP_201_CREATED)
    return payload


def _assignment_for(session: Session, xid: uuid.UUID, actor: Principal):
    """The assignment, if it is this student's to sit.

    Audience is `assignment_targets` — materialized at creation — OR current
    membership of the cohort it was set for. Targets alone would lock out a student
    whose cohort row landed after the work was set; the cohort alone would let
    someone who has since joined pick up work they were never given.

    A 404 rather than a 403, consistently with `_attempt`: confirming an assignment
    exists tells a prober which centres run which papers.
    """
    from app.modules.exam.models import Assignment, AssignmentTarget
    from app.modules.identity.models import CohortMember

    assignment = session.scalars(
        select(Assignment).where(Assignment.xid == xid)).first()
    if assignment is None:
        raise NotFound("Assignment not found.")
    targeted = session.scalar(
        select(func.count()).select_from(AssignmentTarget)
        .where(AssignmentTarget.assignment_id == assignment.id,
               AssignmentTarget.user_id == actor.user_id)) or 0
    if not targeted and assignment.cohort_id is not None:
        targeted = session.scalar(
            select(func.count()).select_from(CohortMember)
            .where(CohortMember.cohort_id == assignment.cohort_id,
                   CohortMember.user_id == actor.user_id,
                   CohortMember.left_at.is_(None))) or 0
    if not targeted:
        raise NotFound("Assignment not found.")
    return assignment


def _start_assigned(assignment, actor: Principal, session: Session,
                    exam: ExamSession, idem: Idempotency, body: AttemptCreate) -> dict:
    """The assigned path: the assignment decides, not the request.

    Mode, time limit and org context all come from the assignment. A client asking
    for `practice` against an exam-mode assignment would otherwise get free replay
    of a paper it is about to be marked on.
    """
    now = exam._clock.now()
    if now < assignment.opens_at:
        raise TooEarly("This assignment has not opened yet.",
                       code="assignment_not_open", opens_at=iso(assignment.opens_at),
                       server_now=iso(now))
    if now >= assignment.closes_at:
        raise Conflict("This assignment has closed.", code="assignment_closed",
                       closed_at=iso(assignment.closes_at))

    # `max_attempts` has been on every assignment since the first migration and was
    # enforced nowhere — there was no way to create an attempt against an
    # assignment to enforce it on. Resuming is not a new attempt: `ExamSession.start`
    # returns the live one, so only FINISHED attempts count against the limit.
    used = session.scalar(
        select(func.count()).select_from(Attempt)
        .where(Attempt.user_id == actor.user_id,
               Attempt.assignment_id == assignment.id,
               Attempt.status.notin_(("issued", "in_progress")))) or 0
    live = session.scalar(
        select(Attempt.id)
        .where(Attempt.user_id == actor.user_id,
               Attempt.assignment_id == assignment.id,
               Attempt.status.in_(("issued", "in_progress"))))
    if live is None and used >= assignment.max_attempts:
        raise Conflict(
            f"You have used all {assignment.max_attempts} attempt(s) for this "
            "assignment.", code="attempt_limit_reached")

    attempt = exam.start(user_id=actor.user_id,
                         test_version_id=assignment.test_version_id,
                         mode=assignment.mode, assignment_id=assignment.id,
                         org_context_id=assignment.org_id,
                         time_limit_seconds=assignment.time_limit_seconds)
    payload = _attempt_dto(attempt, exam)
    idem.store(body.model_dump(mode="json"), payload, status.HTTP_201_CREATED)
    return payload


@router.get("/{xid}")
def read_attempt(xid: uuid.UUID,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 exam: ExamSession = Depends(exam_session)) -> dict:
    return _attempt_dto(_attempt(session, xid, actor), exam)


@router.get("/{xid}/payload")
def read_payload(xid: uuid.UUID, response: Response,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 exam: ExamSession = Depends(exam_session)) -> dict:
    """One row read. No answer keys, no transcript — this is the document that
    goes to the device."""
    attempt = _attempt(session, xid, actor)
    snapshot = exam.payload(attempt)
    tv = session.get(TestVersion, attempt.test_version_id)
    if tv and tv.checksum:
        response.headers["ETag"] = f'"{tv.checksum}"'
    return snapshot


@router.post("/{xid}/answers")
def save_answers(xid: uuid.UUID, body: AnswerBatch,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 exam: ExamSession = Depends(exam_session),
                 idem: Idempotency = Depends(idempotency)) -> dict:
    """The load-bearing endpoint. The response doubles as the clock sync, which
    is why exam timing needs no WebSocket."""
    scope = f"attempts.answers:{xid}"
    if replayed := idem.replay(scope, body.model_dump(mode="json")):
        return replayed

    attempt = _attempt(session, xid, actor)
    result = exam.save_answers(attempt, [
        AnswerDelta(question_version_xid=str(d.question_version_xid),
                    slot_key=d.slot_key, response=d.response,
                    client_seq=d.client_seq, time_spent_ms=d.time_spent_ms)
        for d in body.deltas
    ])
    payload = jsonify({
        "accepted": result.accepted,
        "rejected": result.rejected,
        "last_accepted_seq": result.last_accepted_seq,
        "server_now": result.server_now,
        "expires_at": result.expires_at,
        "seconds_remaining": result.seconds_remaining,
    })
    idem.store(body.model_dump(mode="json"), payload)
    return payload


@router.post("/{xid}/sections/{position}/enter")
def enter_section(xid: uuid.UUID, position: int,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db),
                  exam: ExamSession = Depends(exam_session)) -> dict:
    row = exam.enter_section(_attempt(session, xid, actor), position)
    return {"position": row.position, "entered_at": iso(row.entered_at),
            "audio_locked": row.audio_locked_at is not None}


@router.post("/{xid}/sections/{position}/audio-grant")
def audio_grant(xid: uuid.UUID, position: int,
                actor: Principal = Depends(principal),
                session: Session = Depends(db),
                exam: ExamSession = Depends(exam_session)) -> dict:
    """Play-once is enforced here, server-side. A client-side play counter is a
    suggestion."""
    return jsonify(exam.audio_grant(_attempt(session, xid, actor), position))


@router.post("/{xid}/submit")
def submit(xid: uuid.UUID,
           actor: Principal = Depends(principal),
           session: Session = Depends(db),
           exam: ExamSession = Depends(exam_session),
           idem: Idempotency = Depends(idempotency)) -> dict:
    scope = f"attempts.submit:{xid}"
    if replayed := idem.replay(scope, {}):
        return replayed
    attempt = _attempt(session, xid, actor)
    run = exam.submit(attempt)
    payload = _result_dto(run, attempt)
    idem.store({}, payload)
    return payload


@router.get("/{xid}/result")
def read_result(xid: uuid.UUID,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    from app.modules.exam.models import ScoreRun

    attempt = _attempt(session, xid, actor)
    run = session.scalars(select(ScoreRun).where(ScoreRun.attempt_id == attempt.id,
                                                 ScoreRun.is_current.is_(True))).first()
    if run is None:
        raise NotFound("This attempt has not been scored.")
    return _result_dto(run, attempt)


@router.get("/{xid}/review")
def read_review(xid: uuid.UUID,
                actor: Principal = Depends(principal),
                session: Session = Depends(db),
                exam: ExamSession = Depends(exam_session)) -> dict:
    """Per-item, with the marking explanation.

    Gated by the assignment's `allow_review_after`; a self-serve practice attempt
    is always reviewable, because there is nobody to keep it from.
    """
    from app.modules.exam.models import Assignment

    attempt = _attempt(session, xid, actor)
    if attempt.assignment_id:
        assignment = session.get(Assignment, attempt.assignment_id)
        if assignment and assignment.allow_review_after == "never":
            raise Forbidden("Review is not permitted for this assignment.",
                            code="review_not_permitted")
        # `submitted_at is None` is the unsubmitted case, and it used to be
        # compared straight to `closes_at` — `datetime > None` raises TypeError,
        # so a student who tapped Review before submitting got a 500. An
        # unsubmitted paper is exactly the case this gate exists to refuse.
        if assignment and assignment.allow_review_after == "close":
            if attempt.submitted_at is None or assignment.closes_at > attempt.submitted_at:
                raise Forbidden("Review opens when the assignment closes.",
                                code="review_not_yet_open")
    return jsonify({"attempt_xid": str(attempt.xid), "items": exam.review(attempt)})


def _attempt_dto(attempt: Attempt, exam: ExamSession) -> dict:
    now = exam._clock.now()
    return {
        "xid": str(attempt.xid),
        "status": attempt.status,
        "mode": attempt.mode,
        "attempt_no": attempt.attempt_no,
        "expires_at": iso(attempt.expires_at),
        # Render countdowns from this delta, never from the device clock.
        "server_now": iso(now),
        "seconds_remaining": (None if attempt.expires_at is None
                              else max(0, int((attempt.expires_at - now).total_seconds()))),
    }


def _result_dto(run, attempt: Attempt | None = None) -> dict:
    """`attempt_xid` is `required` in the AttemptResult schema and was hardcoded
    `None`, so every result this API returned was invalid against its own contract
    — and a client holding two results could not tell which paper either was."""
    return {
        "attempt_xid": str(attempt.xid) if attempt else None,
        "score_run_xid": str(run.xid),
        "status": "scored",
        "raw_score": float(run.raw_score),
        "max_raw": float(run.max_raw),
        "band": float(run.band) if run.band is not None else None,
        "per_section": run.per_section,
        "engine_version": run.engine_version,
    }
