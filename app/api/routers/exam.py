"""Exam endpoints. Thin: they map DTOs and call `ExamSession`, nothing more.

Every rule about time, ordering and freezing lives in the exam module, so these
handlers stay short enough to read in one screen and the rules stay testable
without a web framework.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import (
    Idempotency,
    Principal,
    db,
    entitlements,
    exam_session,
    idempotency,
    principal,
)
from app.api.dto import iso, jsonify
from app.modules.analytics import projections
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.billing.entitlements import SEAT_BUNDLE, Entitlements
from app.modules.content.models import Test, TestVersion
from app.modules.exam.models import Attempt
from app.modules.exam.session import AnswerDelta, ExamSession
from app.platform.errors import Conflict, Forbidden, NotFound, TooEarly

router = APIRouter(prefix="/attempts", tags=["exam"])


class AttemptCreate(BaseModel):
    test_version_xid: uuid.UUID | None = None
    assignment_xid: uuid.UUID | None = None
    # **`preview` is not startable here, and used to be.** It is an AUTHORING
    # action with its own authorized route — `POST /test-versions/{xid}/preview`,
    # which requires `Action.EDIT` on the version — and accepting it from any
    # client on this route made it two things at once:
    #
    #   * the entitlement check read `if body.mode != "preview"`, so a student who
    #     sent `preview` sat the paper for free, for ever;
    #   * `ExamSession.start` reads `if tv.status != "published" and mode !=
    #     "preview"`, so the same request reached UNPUBLISHED drafts.
    #
    # Both branches are correct for the authoring route they were written for.
    # This route is the one that let anybody take them. A rejected `mode` is a 422
    # from the model, before any handler runs.
    mode: str = Field(default="exam", pattern="^(exam|practice)$")


class AnswerDeltaIn(BaseModel):
    """One slot's answer.

    `response` was `Any`, and the contract has always declared it
    `string | array of strings | null`. Nothing enforced that, and the gap was not
    a tolerated looseness — it silently produced **wrong marks**:

        sent "bicycle"            -> normalized 'bicycle'       -> correct
        sent {"text": "bicycle"}  -> normalized 'text bicycle'  -> INCORRECT

    `_as_text` stringifies whatever it is given, so an object answer became its
    Python repr, and the repr — including the KEY NAME — was what got normalized
    and compared against the accepted alternatives. A client sending the wrong
    shape got 200 OK and a student got a wrong band, with no error raised
    anywhere. "The server is the sole authority on scoring" has to mean it
    refuses what it cannot score, not that it scores it anyway.

    A list is legitimate: `mcq_multi` answers with the set of chosen option ids.

    Note this is a PER-SLOT value, which is why it is not validated against the
    type's `response_schema` — those describe an assembled whole-question response
    (`{"slots": {...}}`, `{"selected": [...]}`), and a slot value is one leaf
    inside one.
    """

    question_version_xid: uuid.UUID
    slot_key: str
    response: str | list[str] | None = None
    client_seq: int
    time_spent_ms: int = 0


class AnswerBatch(BaseModel):
    deltas: list[AnswerDeltaIn] = Field(min_length=1, max_length=200)


def _attempt(session: Session, xid: uuid.UUID, actor: Principal) -> Attempt:
    """The SITTER's own attempt, and nobody else's.

    This guards eight endpoints and six of them act on the exam in progress:
    `/payload` serves the paper, `/answers` writes responses, `/submit` ends the
    sitting, `/audio-grant` mints a per-user media token. Teaching staff read a
    student's marking through `_readable_attempt` below instead, and the split is
    the point — widening this one function to let a teacher see a result would
    also have let them answer and submit a student's exam.
    """
    attempt = session.scalars(select(Attempt).where(Attempt.xid == xid)).first()
    if attempt is None:
        raise NotFound("Attempt not found.")
    if attempt.user_id != actor.user_id:
        # Deliberately 404, not 403: confirming an attempt exists tells a prober
        # something they should not learn.
        raise NotFound("Attempt not found.")
    return attempt


def _readable_attempt(session: Session, xid: uuid.UUID,
                      actor: Principal) -> tuple[Attempt, bool]:
    """Resolve an attempt for READING its outcome. Returns `(attempt, as_staff)`.

    The sitter, or teaching staff at the centre that set the work. "Why was my
    answer marked wrong" is the most useful support question in the product and
    the person who fields it at a prep centre is the teacher, not the student —
    who until now was the only human who could open the answer.

    Three limits make that a widening of reading and not of authority:

    **Only work the centre actually set.** An attempt with no `assignment_id` is
    self-serve practice a student chose to do on their own, and no staff member
    has business in it. The centre's claim comes from having assigned the paper,
    so it reaches exactly as far as the assignment does.

    **The same predicate that already guards the progress view**, which carries
    every classmate's live band — a teaching role at the org that set the work,
    the person who set it, or a platform admin. Deliberately not org MEMBERSHIP:
    a student at the same centre is precisely who must not read a classmate's
    paper. One rule, written once, rather than a second copy that agrees on the
    day it is written.

    **Still 404, never 403.** A stranger learns nothing about whether an attempt
    exists, and the answer to a probe is identical to the answer for a real xid
    they may not read.
    """
    from app.modules.exam.models import Assignment

    attempt = session.scalars(select(Attempt).where(Attempt.xid == xid)).first()
    if attempt is None:
        raise NotFound("Attempt not found.")
    if attempt.user_id == actor.user_id:
        return attempt, False
    if attempt.assignment_id is not None:
        assignment = session.get(Assignment, attempt.assignment_id)
        if assignment is not None and (
                actor.roles.get(assignment.org_id) in ("teacher", "centre_admin")
                or assignment.assigned_by == actor.user_id
                or actor.is_platform_admin):
            return attempt, True
    raise NotFound("Attempt not found.")


def _audit_staff_read(session: Session, actor: Principal, attempt: Attempt,
                      action: str) -> None:
    """Record a staff member opening a student's paper.

    A student reading their own marking writes nothing — it is their paper. Staff
    reading someone else's is a different act and leaves a row, because these are
    minors' exam responses under a promise to handle their data carefully, and
    "who looked at my child's paper" is a question that needs an answer rather
    than an assurance.

    Written on the READ, which is unusual in this log and correct here: the
    disclosure IS the event, and there is no later write to hang it on.
    """
    session.execute(text("""
        INSERT INTO audit_log (actor_kind, actor_user_id, org_id, action,
                               subject_type, subject_id, after)
        VALUES ('user', :who, :org, :action, 'attempt', :sid, CAST(:after AS jsonb))
    """).bindparams(
        who=actor.user_id,
        org=actor.org_ids[0] if actor.org_ids else None,
        action=action, sid=str(attempt.xid),
        after=json.dumps({"student_user_id": attempt.user_id,
                          "assignment_id": attempt.assignment_id})))


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

    **Self-serve asks two questions, and used to ask neither properly.** May you
    read this paper, and have you paid for it. The second was a hardcoded
    `"mock.unlimited"` next to a `SEAT_BUNDLE` the assigned path already uses; the
    first was not asked at all. `create_assignment` has always run the read half
    through the policy so "a centre cannot assign a competitor's test it merely
    stumbled upon" — and this route let a student SIT that same test. Content
    defaults to `org_private`, which is a contractual promise, and an opaque xid
    is not an authorization check.
    """
    if replayed := idem.replay("attempts.start", body.model_dump(mode="json")):
        return replayed

    assignment = (_assignment_for(session, body.assignment_xid, actor)
                  if body.assignment_xid else None)
    if assignment is not None:
        return _start_assigned(assignment, actor, session, exam, idem, body)

    if body.test_version_xid is None:
        raise NotFound("A test version or assignment is required.")
    row = session.execute(
        select(TestVersion, Test).join(Test, Test.id == TestVersion.test_id)
        .where(TestVersion.xid == body.test_version_xid)).first()
    if row is None:
        raise NotFound("Test version not found.")
    tv, test = row
    # The same call `create_assignment` makes about the same object. A student may
    # sit what the platform sells (`platform_global`) or what their own centre
    # owns — not a rival centre's paper, whoever passed them the id.
    policy.require(actor, Action.READ,
                   Resource(org_id=test.org_id, owner_user_id=test.owner_user_id,
                            visibility=test.visibility))

    ents.require_any(user_xid=str(actor.user_id), features=SEAT_BUNDLE,
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
    """The resume endpoint, and now it can actually resume one.

    It returned the clock and nothing else, so a client coming back to an
    in-progress attempt had no way to read its own saved answers — see
    `ExamSession.resume_state` for why that silently destroyed every answer
    typed after a refresh.
    """
    attempt = _attempt(session, xid, actor)
    return {**_attempt_dto(attempt, exam), **exam.resume_state(attempt)}


@router.get("/{xid}/payload", response_model=None)
def read_payload(xid: uuid.UUID, request: Request, response: Response,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 exam: ExamSession = Depends(exam_session)) -> dict | Response:
    """One row read. No answer keys, no transcript — this is the document that
    goes to the device.

    **Reading this records the exposure**, which the contract has said since it
    was drafted and nothing did. `item_exposures` was written only when an
    attempt was SCORED, on the reasoning that an abandoned attempt "did not
    expose anything" — false the moment this endpoint answered 200, and false in
    exactly the pattern the table exists to catch: start an attempt, pull the
    paper, never submit. It feeds `burn_score` and the anomaly index on
    `(user_id, occurred_at)`, so a scraped item read as pristine.

    Recorded before the conditional below, not after. A 304 means the client
    already holds the document, and it only holds it because a 200 exposed it —
    but the guard is per attempt, so putting the write on the uncacheable path
    would be one more thing to get wrong later for nothing.

    **`ETag` was set and `If-None-Match` was never read**, so every revalidation
    re-sent the whole paper. This is the largest response in the product and its
    audience is on Uzbek mobile data; the client already stores it for offline
    resilience, which is what makes a conditional request the normal case rather
    than an optimisation.

    `private, no-cache` is the pair that makes that safe: revalidate every time,
    and no shared cache may hold an exam paper. Not `no-store`, which would
    forbid the client copy the offline design depends on.
    """
    attempt = _attempt(session, xid, actor)
    snapshot = exam.payload(attempt)
    projections.record_payload_exposure(
        session, snapshot=snapshot, attempt_id=attempt.id,
        test_version_id=attempt.test_version_id, user_id=actor.user_id,
        org_id=attempt.org_context_id,
        context=("competition" if attempt.competition_id else attempt.mode),
        now=dt.datetime.now(dt.UTC))

    tv = session.get(TestVersion, attempt.test_version_id)
    response.headers["Cache-Control"] = "private, no-cache"
    if not (tv and tv.checksum):
        return snapshot
    etag = f'"{tv.checksum}"'
    response.headers["ETag"] = etag
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag,
                                 "Cache-Control": "private, no-cache"})
    return snapshot


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 `If-None-Match`: a list, and `*` matches anything that exists.

    A bare `==` against the header would miss both — a client sending two
    candidates, and the `*` a resumed download uses — and each miss is the whole
    paper over a mobile connection.
    """
    if not header:
        return False
    candidates = [c.strip() for c in header.split(",")]
    # Weak validators compare equal for If-None-Match; `W/"x"` and `"x"` are the
    # same document as far as this comparison is concerned.
    return "*" in candidates or any(
        c.removeprefix("W/") == etag for c in candidates)


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
    """The current score. A voided attempt has none to show.

    Voiding is an operator invalidating the attempt, and a band it produced is a
    claim arising from it — the same reasoning that took the paper away in
    `ExamSession.payload`. `GET /attempts/{xid}` still returns the attempt with
    `status: voided`, so the client can say what happened rather than showing a
    score nobody stands behind.

    Readable by the student who sat it and by teaching staff at the centre that
    set the work — see `_readable_attempt`.
    """
    attempt, as_staff = _readable_attempt(session, xid, actor)
    _refuse_voided(attempt)
    run = _current_run(session, attempt)
    if run is None:
        raise NotFound("This attempt has not been scored.")
    if as_staff:
        _audit_staff_read(session, actor, attempt, "exam.result_read")
    return _result_dto(run, attempt)


def _current_run(session: Session, attempt: Attempt):
    from app.modules.exam.models import ScoreRun

    return session.scalars(
        select(ScoreRun).where(ScoreRun.attempt_id == attempt.id,
                               ScoreRun.is_current.is_(True))).first()


def _refuse_voided(attempt: Attempt) -> None:
    """One sentence, one code, three endpoints.

    `payload` enforces the same rule inside `ExamSession` because it is a rule
    about serving content. These two are read models assembled in the router, so
    the check lives beside them — but it is one function rather than three
    copies, because three copies of "is this attempt still valid" is how two of
    them end up disagreeing.
    """
    if attempt.status == "voided":
        raise Conflict("This attempt was voided.", code="attempt_voided")


@router.get("/{xid}/review")
def read_review(xid: uuid.UUID,
                actor: Principal = Depends(principal),
                session: Session = Depends(db),
                exam: ExamSession = Depends(exam_session)) -> dict:
    """Per-item, with the marking explanation.

    Gated by the assignment's `allow_review_after`; a self-serve practice attempt
    is always reviewable, because there is nobody to keep it from.

    **And by the contest clock, which it was not.** This returns
    `accepted_answers` for every item — the answer key. An entrant who finished
    early could read it while the contest was still live and pass it to everyone
    still sitting. Every other part of the competition design exists to stop
    exactly that: the payload is AES-GCM encrypted in the lobby, the key is a
    hundred bytes released at T-0, the fetch is jittered so nobody can time the
    start from traffic. All of it is decoration if the answers are one request
    away the moment a fast entrant submits.

    So: no review until the contest has ended. Not until it is *ranked* —
    `ends_at` is when the last entrant's clock stops, and making a student wait
    for a leaderboard job to run would be punishing them for our scheduling.
    """
    from app.modules.exam.models import Assignment

    attempt, as_staff = _readable_attempt(session, xid, actor)
    _refuse_voided(attempt)
    # One `now` for both gates below. Two calls could straddle a boundary and
    # answer two different questions about the same request.
    now = dt.datetime.now(dt.UTC)
    # **Any contest on this PAPER, not just this attempt's.** Gating on
    # `attempt.competition_id` alone asked "has MY contest ended", and nothing
    # stops two competitions sharing a `test_version_id` — there is no unique
    # index and no check at creation. So a contest that finished at noon released
    # the answer key and the listening transcript to its entrants while a second
    # contest on the identical paper was still running. Measured: contest A
    # ended, contest B live, and A's entrant read back
    # `accepted_answers: ['bicycle']` and `transcript_excerpt: "I came by
    # bicycle."`.
    #
    # LIVE only — started and not yet ended — rather than any contest not yet
    # over. A contest scheduled for next term would otherwise defer review for
    # everyone who has ever sat the paper, for months, and no gate on review
    # fixes that case anyway: the paper is already in circulation among everyone
    # who sat it. The answer there is not to reuse it, which `burn_score`
    # already says. This window is the contest's own duration, which is hours.
    live = session.execute(text("""
        SELECT title, ends_at FROM competitions
        WHERE test_version_id = :v AND starts_at <= :now AND ends_at > :now
        ORDER BY ends_at DESC LIMIT 1
    """).bindparams(v=attempt.test_version_id, now=now)).first()
    if live is not None:
        raise TooEarly(
            f"Review opens when '{live.title}' finishes.",
            code="competition_still_live", ends_at=iso(live.ends_at),
            server_now=iso(now))
    if attempt.assignment_id and not as_staff:
        # **`allow_review_after` binds the STUDENT, not the staff room.** The
        # setting is presented to a centre as "students may review their answers",
        # and its two restrictive values exist to stop answers travelling between
        # classmates while a cohort is still sitting — `never`, and `close`, which
        # holds review until the window shuts.
        #
        # Applying it to staff would mean a teacher who set review to `never`, to
        # keep the paper quiet, had also blinded themselves to their own class's
        # marking; and on the DEFAULT setting no teacher could answer "why was
        # this wrong" until the window closed, which is exactly when they are
        # asked. It also protects nothing: `accepted_answers` is the answer key,
        # and teaching staff at the centre can already read the key through
        # authoring.
        #
        # The contest gate above is NOT skipped for staff, and that asymmetry is
        # deliberate: it guards a leak with a route out of the centre. A contest
        # can be public and cross-org, its paper is not necessarily this centre's
        # to author, and a coach who can read the key mid-contest is the leak that
        # gate exists to close.
        #
        # `attempts.assignment_id` is a foreign key, so this cannot be None. The
        # previous `if assignment and ...` meant a missing row would OPEN the
        # gate, which is the wrong direction for a gate to fail even when the
        # database makes it unreachable.
        assignment = session.get(Assignment, attempt.assignment_id)
        if assignment.allow_review_after == "never":
            raise Forbidden("Review is not permitted for this assignment.",
                            code="review_not_permitted")
        # **`close` means "when the assignment closes", and this never asked the
        # clock.** It compared the deadline to the SUBMISSION —
        #
        #     if attempt.submitted_at is None or assignment.closes_at > attempt.submitted_at
        #
        # — which answers "did you submit before the deadline?", a different
        # question whose answer never changes. So a student who submitted on time
        # was refused for ever: at T+8d the comparison is still `T+7d > T`, and
        # nothing about waiting makes it false. Verified against a real database
        # — an on-time submitter whose assignment closed two days earlier got
        # `review_not_yet_open`.
        #
        # It was accidentally right for exactly one person: whoever submitted
        # LATE, because then the deadline had necessarily passed. So the gate
        # served the students who missed it and refused the ones who did not, on
        # the DEFAULT setting for every assignment in the product.
        #
        # The `submitted_at is None` guard was added to stop `datetime > None`
        # raising a TypeError. It fixed the 500 and preserved the wrong question.
        # Comparing against `now` needs no such guard: an unsubmitted attempt
        # falls through to `exam.review`, which refuses it with `not_scored`,
        # which is what it is.
        if (assignment.allow_review_after == "close"
                and now < assignment.closes_at):
            raise Forbidden("Review opens when the assignment closes.",
                            code="review_not_yet_open",
                            opens_at=iso(assignment.closes_at),
                            server_now=iso(now))
    items = exam.review(attempt)
    if as_staff:
        # After `exam.review`, so a call that raises `not_scored` does not leave a
        # row claiming a paper was read when nothing was disclosed.
        _audit_staff_read(session, actor, attempt, "exam.review_read")
    # `band` is declared at the top of `AttemptReview` and was never returned.
    # The review screen shows the marking next to the score it produced; without
    # it the client has to call `/result` as well to render its own heading, and
    # the two answers can disagree if a regrade lands between them.
    run = _current_run(session, attempt)
    return jsonify({
        "attempt_xid": str(attempt.xid),
        "band": float(run.band) if run is not None and run.band is not None else None,
        "items": items,
    })


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
    — and a client holding two results could not tell which paper either was.

    Three more fields were declared and never returned, and each is a question a
    student asks:

    * `scored_at` — when. Without it a result has no age, and a regraded one is
      indistinguishable from the original.
    * `late_by_ms` — `attempts.late_by_ms` is written on submit and was read by
      nothing. Its own column comment is "Recorded, not punished: four seconds
      late on a mobile network is a hiccup", which only means anything if the
      student can see the four seconds were noticed and cost nothing.
    * `regraded` — "True when a later score run superseded the original". This is
      the student-facing half of the regrade flow. A band that changed by itself
      with no signal that anybody changed it is the thing that makes a school
      stop trusting the platform, and "bad keys are the fastest way to lose a
      school client" is the reason the whole regrade pipeline exists.
    """
    return {
        "attempt_xid": str(attempt.xid) if attempt else None,
        "score_run_xid": str(run.xid),
        "status": "scored",
        "raw_score": float(run.raw_score),
        "max_raw": float(run.max_raw),
        "band": float(run.band) if run.band is not None else None,
        "per_section": run.per_section,
        "engine_version": run.engine_version,
        "scored_at": iso(run.computed_at),
        "late_by_ms": attempt.late_by_ms if attempt else None,
        # `reason` is "initial" for the first run and names the trigger for every
        # later one, so this reads the run's own provenance rather than counting
        # rows — a count would call a dry-run plan a regrade.
        "regraded": run.reason != "initial",
    }
