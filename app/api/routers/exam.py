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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dto import iso, jsonify
from app.api.deps import (
    Idempotency, Principal, db, entitlements, exam_session, idempotency, principal,
)
from app.modules.billing.entitlements import Entitlements
from app.modules.content.models import TestVersion
from app.modules.exam.models import Attempt
from app.modules.exam.session import AnswerDelta, ExamSession
from app.platform.errors import Forbidden, NotFound

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
    """Start or resume. Entitlement is checked HERE, once, through the single
    call site — no feature code re-implements "has this student paid"."""
    if replayed := idem.replay("attempts.start", body.model_dump(mode="json")):
        return replayed

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
    run = exam.submit(_attempt(session, xid, actor))
    payload = _result_dto(run)
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
    return _result_dto(run)


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
        if (assignment and assignment.allow_review_after == "close"
                and assignment.closes_at > attempt.submitted_at):
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


def _result_dto(run) -> dict:
    return {
        "attempt_xid": None,
        "score_run_xid": str(run.xid),
        "status": "scored",
        "raw_score": float(run.raw_score),
        "max_raw": float(run.max_raw),
        "band": float(run.band) if run.band is not None else None,
        "per_section": run.per_section,
        "engine_version": run.engine_version,
    }
