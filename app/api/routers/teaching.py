"""Assignments and regrades — what a teacher actually does after authoring.

The regrade half is the one worth reading. A bad answer key is the fastest way to
lose a school client, so the flow here is deliberately three-step:

    stage (dry run)  →  read the impact  →  apply

Nothing is recomputed on the way in and nothing is applied without a human seeing
the numbers first. `apply` refuses outright while a finished competition's
ranking would move, because a leaderboard that changes by itself looks like
fraud.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Idempotency, Principal, db, entitlements, idempotency, principal
from app.api.dto import iso, jsonify
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.billing.entitlements import SEAT_BUNDLE, Entitlements, Reason, most_informative
from app.modules.content.models import QuestionVersion, Test, TestVersion
from app.modules.exam.models import (
    Assignment,
    AssignmentTarget,
    Attempt,
    ItemScore,
    Outbox,
    RegradeJob,
    ScoreRun,
)
from app.modules.identity.models import Cohort, CohortMember, OrgMembership, User
from app.platform.errors import Conflict, Forbidden, NotFound, PaymentRequired

router = APIRouter(tags=["assignments"])
regrades = APIRouter(tags=["regrade"])


# ── assignments ──────────────────────────────────────────────────────

class AssignmentCreate(BaseModel):
    test_version_xid: uuid.UUID
    target_kind: str = Field(pattern="^(cohort|users|self_serve)$")
    cohort_xid: uuid.UUID | None = None
    user_xids: list[uuid.UUID] = []
    opens_at: dt.datetime
    closes_at: dt.datetime
    time_limit_seconds: int | None = None
    max_attempts: int = 1
    mode: str = Field(default="exam", pattern="^(exam|practice)$")
    allow_review_after: str = Field(default="close", pattern="^(never|submit|close)$")


def assignment_dto(session: Session, row: Assignment,
                   actor: Principal | None = None) -> dict:
    tv = session.get(TestVersion, row.test_version_id)
    cohort = session.get(Cohort, row.cohort_id) if row.cohort_id else None
    used = 0
    if actor is not None:
        used = session.scalar(
            select(func.count()).select_from(Attempt)
            .where(Attempt.assignment_id == row.id,
                   Attempt.user_id == actor.user_id)) or 0
    return {
        "xid": str(row.xid),
        "test_version_xid": str(tv.xid) if tv else None,
        "test_title": tv.title if tv else "",
        "cohort": ({"xid": str(cohort.xid), "name": cohort.name,
                    "academic_year": cohort.academic_year,
                    "member_count": session.scalar(
                        select(func.count()).select_from(CohortMember)
                        .where(CohortMember.cohort_id == cohort.id,
                               CohortMember.left_at.is_(None))) or 0,
                    "status": cohort.status} if cohort else None),
        "opens_at": iso(row.opens_at), "closes_at": iso(row.closes_at),
        "time_limit_seconds": row.time_limit_seconds,
        "max_attempts": row.max_attempts, "mode": row.mode,
        "allow_review_after": row.allow_review_after,
        "my_attempts_used": used,
    }


@router.get("/assignments")
def list_assignments(cohort_xid: uuid.UUID | None = None, state: str | None = None,
                     limit: int = 25, actor: Principal = Depends(principal),
                     session: Session = Depends(db)) -> dict:
    """A student sees what is due; a teacher sees what they set.

    Two different queries behind one path, chosen from the actor's role rather
    than from a client-supplied flag — a student passing `?scope=all` gets their
    own assignments, not the centre's.
    """
    now = dt.datetime.now(dt.UTC)
    teaches = [org for org, role in actor.roles.items()
               if role in ("teacher", "centre_admin")]

    query = select(Assignment).where(Assignment.status == "active")
    if teaches or actor.is_platform_admin:
        scope = Assignment.org_id.in_(teaches) if teaches else None
        mine = Assignment.id.in_(
            select(AssignmentTarget.assignment_id)
            .where(AssignmentTarget.user_id == actor.user_id))
        query = query.where(mine if scope is None else (scope | mine))
    else:
        # A student's assignments are the union of "targeted directly" and
        # "member of the cohort it was set for".
        cohorts = select(CohortMember.cohort_id).where(
            CohortMember.user_id == actor.user_id, CohortMember.left_at.is_(None))
        query = query.where(
            Assignment.id.in_(select(AssignmentTarget.assignment_id)
                              .where(AssignmentTarget.user_id == actor.user_id))
            | Assignment.cohort_id.in_(cohorts))

    if cohort_xid is not None:
        query = query.where(Assignment.cohort_id == _cohort(session, cohort_xid, actor).id)
    if state == "open":
        query = query.where(Assignment.opens_at <= now, Assignment.closes_at > now)
    elif state == "upcoming":
        query = query.where(Assignment.opens_at > now)
    elif state == "closed":
        query = query.where(Assignment.closes_at <= now)

    rows = session.scalars(query.order_by(Assignment.closes_at).limit(limit)).all()
    return {"items": [assignment_dto(session, a, actor) for a in rows],
            "next_cursor": None}


def _cohort(session: Session, xid: uuid.UUID, actor: Principal) -> Cohort:
    cohort = session.scalars(select(Cohort).where(Cohort.xid == xid)).first()
    if cohort is None or (cohort.org_id not in actor.org_ids
                          and not actor.is_platform_admin):
        raise NotFound("Cohort not found.")
    return cohort


@router.post("/assignments", status_code=status.HTTP_201_CREATED)
def create_assignment(body: AssignmentCreate,
                      actor: Principal = Depends(principal),
                      session: Session = Depends(db),
                      ents: Entitlements = Depends(entitlements),
                      idem: Idempotency = Depends(idempotency)) -> dict:
    """Setting an assignment consumes the centre's seats, not the student's quota.

    That is the whole B2B billing model in one line: a school pays per seat and
    its students never see a paywall for work the school set.

    **The second half of that sentence was true and the first was not.** The
    entitlement check ran against the TEACHER and stopped there, so a centre with
    a ten-seat licence could assign to four hundred students and every one of them
    would sit the paper. `entitlements.check` has the rule — "a seat licence only
    covers users who actually hold a seat; without this, buying 10 seats would
    entitle a 400-student centre" — and the assigned path was the one route that
    never asked it about a student.

    `POST /attempts` deliberately does not re-check the student
    (`exam.py`), on the stated grounds that the centre paid when the work was set.
    That is the right division and it is what makes the omission matter: nothing
    downstream was ever going to catch it, and the contract has documented the
    402 here as "the organization has no seat or entitlement covering **these
    students**" from the beginning.
    """
    if replayed := idem.replay("assignments.create", body.model_dump(mode="json")):
        return replayed
    if body.closes_at <= body.opens_at:
        raise Conflict("An assignment must close after it opens.",
                       code="invalid_window")

    row = session.execute(
        select(TestVersion, Test).join(Test, Test.id == TestVersion.test_id)
        .where(TestVersion.xid == body.test_version_xid)).first()
    if row is None:
        raise NotFound("Test version not found.")
    tv, test = row
    if tv.status != "published":
        raise Conflict("Only a published version can be assigned.",
                       code="version_not_published")
    # Assigning is reading someone's content plus writing your own: the read half
    # goes through the policy so a centre cannot assign a competitor's test it
    # merely stumbled upon.
    policy.require(actor, Action.READ,
                   Resource(org_id=test.org_id, owner_user_id=test.owner_user_id,
                            visibility=test.visibility))

    org_id = next((o for o, r in actor.roles.items()
                   if r in ("teacher", "centre_admin")), None)
    if org_id is None and not actor.is_platform_admin:
        raise Forbidden("Only a teacher or centre admin can set an assignment.",
                        code="not_a_teacher")
    ents.require(user_xid=str(actor.user_id), feature="org.assignments",
                 org_xids=[str(org_id)] if org_id else [])

    cohort = _cohort(session, body.cohort_xid, actor) if body.cohort_xid else None
    # Resolved BEFORE the assignment row exists, because the audience is now part
    # of whether this assignment may be created at all.
    targets = _resolve_targets(session, body, cohort, actor)
    _require_covered(session, ents, targets, org_id)

    assignment = Assignment(
        org_id=org_id, cohort_id=cohort.id if cohort else None,
        test_version_id=tv.id, assigned_by=actor.user_id,
        target_kind=body.target_kind, opens_at=body.opens_at, closes_at=body.closes_at,
        time_limit_seconds=body.time_limit_seconds, max_attempts=body.max_attempts,
        mode=body.mode, allow_review_after=body.allow_review_after)
    session.add(assignment)
    session.flush()

    for user_id in targets:
        session.add(AssignmentTarget(assignment_id=assignment.id, user_id=user_id))
    session.flush()

    session.add(Outbox(aggregate_type="assignment", aggregate_id=str(assignment.id),
                       event_type="assignment.created",
                       payload={"assignment_xid": str(assignment.xid),
                                "targets": len(targets),
                                "opens_at": iso(body.opens_at)}))
    session.flush()

    payload = assignment_dto(session, assignment, actor)
    idem.store(body.model_dump(mode="json"), payload, status.HTTP_201_CREATED)
    return payload


def _require_covered(session: Session, ents: Entitlements, targets: list[int],
                     org_id: int | None) -> None:
    """Every student the work is set for must be covered by the centre's licence.

    `SEAT_BUNDLE`, not `org.assignments`. Two subjects, two questions, which is
    how the rest of the module already reads them: `org.assignments` is the
    capability the centre bought — "setting work is a capability the centre
    bought, not a seat the teacher occupies" — and the bundle is what licenses a
    student to sit the paper. `products.features` is a list, so the B2B plan
    grants both. Why the bundle may never absorb `org.assignments` is written
    down where the bundle is.

    Refused rather than silently narrowed. A teacher told "8 of these 30 have no
    seat" can assign seats or shorten the list; an assignment quietly missing
    eight children is discovered at results.

    **The reason is now the decision's, not a constant.** This raised a
    hardcoded `no_seat` whatever had actually happened, so a centre whose site
    licence had simply expired was told its students had no seats — and the fix
    for `no_seat` is to buy seats, which for that centre changes nothing and
    costs money. `Decision` has carried the truthful reason from the beginning
    and this threw it away. The most informative one across the uncovered
    students wins, by the same rule `check()` uses within one student: `expired`
    beats `no_seat` beats `no_entitlement`, because the most specific fault is
    the one worth acting on.

    One `Entitlements.check_any` per target, deliberately, rather than a batched
    query of the same rule. "Every gated action in the system calls
    `Entitlements.check()`... there is no second implementation of 'has this
    student paid' hidden in feature code" is the requirement this module exists to
    satisfy, and a bulk variant is how a second implementation starts. The count
    is a CLASS, not the user base — `target_kind` is `cohort` or a named list —
    so it does not grow with scale, and setting an assignment is a weekly action,
    not a request path.
    """
    if org_id is None or not targets:
        return
    denied: dict[int, Reason] = {}
    for user_id in targets:
        decision = ents.check_any(user_xid=str(user_id), features=SEAT_BUNDLE,
                                  org_xids=[str(org_id)])
        if not decision.allowed:
            denied[user_id] = decision.reason
    if not denied:
        return

    reason = most_informative(denied.values())
    xids = list(session.scalars(select(User.xid).where(User.id.in_(denied))))
    # Two different faults, and the client's call to action differs. `no_seat`
    # means the licence is real and these students are outside it — offer "assign
    # seats" against exactly them. `no_entitlement` means the centre holds nothing
    # in the bundle at all: a misconfigured plan, where the answer is a product
    # row and no amount of seat-assigning will help.
    detail = (f"{len(denied)} of these {len(targets)} students are not covered "
              "by this centre's licence.")
    if reason is Reason.NO_ENTITLEMENT:
        detail = ("This centre's licence does not cover mock sittings, so none "
                  f"of these {len(targets)} students can be assigned one.")
    raise PaymentRequired(
        detail,
        feature=SEAT_BUNDLE[0], reason=reason.value,
        uncovered_count=len(denied),
        uncovered_user_xids=[str(x) for x in xids])


def _resolve_targets(session: Session, body: AssignmentCreate,
                     cohort: Cohort | None, actor: Principal) -> list[int]:
    """Materialized at creation.

    A cohort's membership changes; the assignment's audience does not. Resolving
    lazily would mean a student who joins next week is silently late for work set
    before they arrived.
    """
    if body.target_kind == "cohort":
        if cohort is None:
            raise Conflict("A cohort assignment needs a cohort.", code="cohort_required")
        return list(session.scalars(
            select(CohortMember.user_id)
            .where(CohortMember.cohort_id == cohort.id,
                   CohortMember.left_at.is_(None))))
    if body.target_kind == "users":
        ids = list(session.scalars(
            select(User.id).where(User.xid.in_(body.user_xids or []),
                                  User.deleted_at.is_(None))))
        if len(ids) != len(set(body.user_xids)):
            raise NotFound("One or more of those users does not exist.")
        # A teacher may only assign to their own centre's students.
        #
        # A centre's roster is `org_memberships`. This asked `cohort_members`,
        # which is a different question and a narrower one: a student enrolled at
        # the centre but not yet in any class was refused as an outsider — and
        # "not in a class yet" is the whole reason this target kind exists rather
        # than `target_kind="cohort"`. `identity.add_cohort_members` asks it
        # correctly, and this now matches it, `left_at` and all.
        if not actor.is_platform_admin:
            members = set(session.scalars(
                select(OrgMembership.user_id)
                .where(OrgMembership.user_id.in_(ids),
                       OrgMembership.org_id.in_(actor.org_ids or [0]),
                       OrgMembership.status == "active",
                       OrgMembership.left_at.is_(None))))
            if set(ids) - members:
                raise Forbidden("You can only assign to students in your own centre.",
                                code="student_not_in_org")
        return ids
    return []


@router.get("/assignments/{xid}/progress")
def assignment_progress(xid: uuid.UUID, actor: Principal = Depends(principal),
                        session: Session = Depends(db)) -> dict:
    """Live per-student state, which is what a teacher watches during a mock.

    `answered` counts distinct answered slots, so a student who typed and cleared
    an answer reads as unanswered — matching what they see on screen.
    """
    assignment = session.scalars(select(Assignment).where(Assignment.xid == xid)).first()
    if assignment is None:
        raise NotFound("Assignment not found.")
    # Org MEMBERSHIP is not enough. This response carries every classmate's live
    # progress and band, so it needs a teaching role at the centre that set it —
    # a student in the same org is exactly who must not see it.
    teaches = actor.roles.get(assignment.org_id) in ("teacher", "centre_admin")
    if not (teaches or actor.is_platform_admin
            or assignment.assigned_by == actor.user_id):
        raise NotFound("Assignment not found.")

    tv = session.get(TestVersion, assignment.test_version_id)
    total = tv.total_questions if tv else 0
    rows = session.execute(text("""
        SELECT u.xid, u.given_name, u.family_name, u.phone, u.locale, u.timezone,
               a.xid AS attempt_xid, a.status AS attempt_status, a.expires_at,
               (SELECT count(*) FROM attempt_answers aa
                 WHERE aa.attempt_id = a.id AND aa.response IS NOT NULL) AS answered,
               r.band
        FROM assignment_targets t
        JOIN users u ON u.id = t.user_id
        LEFT JOIN LATERAL (
            SELECT * FROM attempts x
            WHERE x.assignment_id = t.assignment_id AND x.user_id = t.user_id
            ORDER BY x.attempt_no DESC LIMIT 1
        ) a ON true
        LEFT JOIN score_runs r ON r.attempt_id = a.id AND r.is_current
        WHERE t.assignment_id = :a
        ORDER BY u.given_name
    """).bindparams(a=assignment.id)).mappings().all()

    def state(row) -> str:
        if row["attempt_status"] is None:
            return "not_started"
        if row["band"] is not None:
            return "scored"
        return "submitted" if row["attempt_status"] in ("submitted", "scored") \
            else "in_progress"

    students = [{
        "user": {"xid": str(r["xid"]), "given_name": r["given_name"],
                 "family_name": r["family_name"], "phone": r["phone"],
                 "locale": r["locale"], "timezone": r["timezone"]},
        "status": state(r), "answered": r["answered"] or 0, "total": total,
        "expires_at": iso(r["expires_at"]),
        "band": float(r["band"]) if r["band"] is not None else None,
        # The handle for `/attempts/{xid}/review`, which teaching staff may now
        # read. Without it this response named a band and gave no way to ask how
        # it was arrived at — the endpoint was open to staff and unreachable by
        # them, because nothing in the product ever told them an attempt's xid.
        # Null for a student who has not started; there is no attempt yet.
        "attempt_xid": str(r["attempt_xid"]) if r["attempt_xid"] else None,
    } for r in rows]

    return jsonify({
        "assignment_xid": str(assignment.xid),
        "server_now": dt.datetime.now(dt.UTC),
        "summary": {
            "assigned": len(students),
            "not_started": sum(1 for s in students if s["status"] == "not_started"),
            "in_progress": sum(1 for s in students if s["status"] == "in_progress"),
            "submitted": sum(1 for s in students
                             if s["status"] in ("submitted", "scored")),
        },
        "students": students,
    })


# ── regrades ─────────────────────────────────────────────────────────

class RegradeCreate(BaseModel):
    trigger: str = Field(pattern="^(answer_key_change|band_map_change|engine_fix|manual)$")
    subject_type: str = Field(
        pattern="^(question_version|test_version|band_map_version|attempt)$")
    subject_xid: uuid.UUID
    reason: str
    scope: dict = {}


def regrade_dto(job: RegradeJob) -> dict:
    return {
        "xid": str(job.xid), "trigger": job.trigger, "status": job.status,
        "dry_run": job.dry_run,
        "impact": {
            "attempts_total": job.attempts_total,
            "scores_changed": job.scores_changed,
            "bands_changed": job.bands_changed,
            # Band changes only. Notifying on every raw-score wobble trains
            # students to ignore the channel you need for what matters.
            "students_to_notify": job.bands_changed,
            "competition_impact": list(job.competition_impact or []),
        },
        "attempts_processed": job.attempts_processed,
        "created_at": iso(job.created_at), "finished_at": iso(job.finished_at),
    }


@regrades.get("/regrades")
def list_regrades(
        # Declared as `status`, implemented as `status_filter`. The generated
        # client can only send the declared name, so `?status=ready` was
        # silently ignored and the console's filter did nothing.
        status_filter: str | None = Query(None, alias="status"),
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> list[dict]:
    policy.require(actor, Action.REGRADE, Resource(
        org_id=actor.org_ids[0] if actor.org_ids else None))
    query = select(RegradeJob)
    if status_filter:
        query = query.where(RegradeJob.status == status_filter)
    if not actor.is_platform_admin:
        # A centre sees the jobs it started, not the platform's. Regrade jobs
        # carry no org column, so `initiated_by` is the scope.
        query = query.where(RegradeJob.initiated_by == actor.user_id)
    return [regrade_dto(j) for j in session.scalars(
        query.order_by(RegradeJob.created_at.desc()).limit(50))]


@regrades.post("/regrades", status_code=status.HTTP_201_CREATED)
def stage_regrade(body: RegradeCreate, actor: Principal = Depends(principal),
                  session: Session = Depends(db),
                  idem: Idempotency = Depends(idempotency)) -> dict:
    """Stages a DRY RUN. Nothing is recomputed and nothing is written to scores.

    The planning pass runs in a worker because a popular item can carry ten
    thousand sat attempts, and a request that recomputes ten thousand attempts is
    a request that times out.
    """
    policy.require(actor, Action.REGRADE, Resource(
        org_id=actor.org_ids[0] if actor.org_ids else None))
    if replayed := idem.replay("regrades.create", body.model_dump(mode="json")):
        return replayed

    subject_id, from_key, to_key = _resolve_subject(session, body, actor)
    job = RegradeJob(
        trigger=body.trigger, subject_type=body.subject_type, subject_id=subject_id,
        from_key_version_id=from_key, to_key_version_id=to_key,
        initiated_by=actor.user_id, reason=body.reason,
        # `include_competitions` is accepted and ignored: a finished contest is
        # never swept into a bulk regrade, it gets its own recorded decision.
        scope={k: v for k, v in (body.scope or {}).items()
               if k != "include_competitions"},
        dry_run=True, status="planning",
        attempts_total=_affected_count(session, body.subject_type, subject_id))
    session.add(job)
    session.flush()
    session.add(Outbox(aggregate_type="regrade_job", aggregate_id=str(job.id),
                       event_type="regrade.plan_requested",
                       payload={"regrade_job_xid": str(job.xid)}))
    session.flush()

    payload = regrade_dto(job)
    idem.store(body.model_dump(mode="json"), payload, status.HTTP_201_CREATED)
    return payload


def _resolve_subject(session: Session, body: RegradeCreate,
                     actor: Principal) -> tuple[int, int | None, int | None]:
    from app.modules.content.models import AnswerKeyVersion, BandMapVersion

    if body.subject_type == "question_version":
        qv = session.scalars(
            select(QuestionVersion).where(QuestionVersion.xid == body.subject_xid)).first()
        if qv is None:
            raise NotFound("Question version not found.")
        keys = session.scalars(
            select(AnswerKeyVersion)
            .where(AnswerKeyVersion.question_version_id == qv.id)
            .order_by(AnswerKeyVersion.version_no.desc()).limit(2)).all()
        current = keys[0] if keys else None
        previous = keys[1] if len(keys) > 1 else None
        return qv.id, (previous.id if previous else None), (current.id if current else None)

    if body.subject_type == "test_version":
        tv = session.scalars(
            select(TestVersion).where(TestVersion.xid == body.subject_xid)).first()
        if tv is None:
            raise NotFound("Test version not found.")
        return tv.id, None, None

    if body.subject_type == "band_map_version":
        bmv = session.scalars(
            select(BandMapVersion).where(BandMapVersion.xid == body.subject_xid)).first()
        if bmv is None:
            raise NotFound("Band map version not found.")
        return bmv.id, None, None

    attempt = session.scalars(
        select(Attempt).where(Attempt.xid == body.subject_xid)).first()
    if attempt is None:
        raise NotFound("Attempt not found.")
    return attempt.id, None, None


def _affected_count(session: Session, subject_type: str, subject_id: int) -> int:
    """A count, not a plan. Cheap enough to run inline and it is the number the
    author actually wants first: "how many students does this touch"."""
    if subject_type == "question_version":
        return session.scalar(
            select(func.count(func.distinct(ScoreRun.attempt_id)))
            .select_from(ItemScore)
            .join(ScoreRun, ScoreRun.id == ItemScore.score_run_id)
            .join(Attempt, Attempt.id == ScoreRun.attempt_id)
            .where(ItemScore.question_version_id == subject_id,
                   ScoreRun.is_current.is_(True), Attempt.mode != "preview")) or 0
    if subject_type == "test_version":
        return session.scalar(
            select(func.count()).select_from(Attempt)
            .where(Attempt.test_version_id == subject_id,
                   Attempt.mode != "preview",
                   Attempt.status.in_(("submitted", "scored")))) or 0
    if subject_type == "band_map_version":
        # Was missing, so this fell through to `return 0` — the widest-reaching
        # regrade in the system reported an impact of none. A band map is the
        # curve every test version using it is scored against, and the human
        # deciding whether to run the job reads this number.
        #
        # Scoped exactly as `exam.planner` scopes the job itself: attempts on test
        # versions that use THIS band map. A count that disagrees with the plan is
        # worse than no count.
        return session.scalar(
            select(func.count()).select_from(Attempt)
            .where(Attempt.test_version_id.in_(
                       select(TestVersion.id)
                       .where(TestVersion.band_map_version_id == subject_id)),
                   Attempt.mode != "preview",
                   Attempt.status.in_(("submitted", "scored")))) or 0
    if subject_type == "attempt":
        return 1
    # Unreachable: `RegradeCreate.subject_type` allows exactly the four handled
    # above. Kept loud rather than silent — a `return 0` that a fifth subject type
    # could fall into is precisely how `band_map_version` came to report an impact
    # of none, and the next person to widen that pattern should hear about it.
    raise Conflict(f"No impact count is defined for '{subject_type}'.",
                   code="unknown_regrade_subject")            # pragma: no cover


@regrades.get("/regrades/{xid}")
def read_regrade(xid: uuid.UUID, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    job = session.scalars(select(RegradeJob).where(RegradeJob.xid == xid)).first()
    if job is None or (job.initiated_by != actor.user_id and not actor.is_platform_admin):
        raise NotFound("Regrade job not found.")
    return regrade_dto(job)


@regrades.post("/regrades/{xid}/apply", status_code=status.HTTP_202_ACCEPTED)
def apply_regrade(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db),
                  idem: Idempotency = Depends(idempotency)) -> dict:
    """Commit a planned regrade.

    Refused while any affected competition still lacks a recorded decision — the
    same rule `exam.regrade.apply()` enforces in the domain, checked here so the
    caller gets a 409 with the list of contests rather than a failed worker job.
    """
    if replayed := idem.replay(f"regrades.apply:{xid}", {}):
        return replayed

    job = session.scalars(select(RegradeJob).where(RegradeJob.xid == xid)).first()
    if job is None or (job.initiated_by != actor.user_id and not actor.is_platform_admin):
        raise NotFound("Regrade job not found.")
    if job.status != "ready":
        raise Conflict(f"This job is '{job.status}'. Only a planned job can be applied.",
                       code="regrade_not_ready")

    undecided = _undecided_competitions(session, job)
    if undecided:
        raise Conflict(
            "This regrade would change finished competition rankings; a platform "
            "admin must decide first.",
            code="competition_decision_required", competitions=undecided)

    job.dry_run = False
    job.status = "running"
    job.started_at = dt.datetime.now(dt.UTC)
    session.add(Outbox(aggregate_type="regrade_job", aggregate_id=str(job.id),
                       event_type="regrade.apply_requested",
                       payload={"regrade_job_xid": str(job.xid),
                                "initiated_by": actor.user_id}))
    session.flush()

    payload = regrade_dto(job)
    idem.store({}, payload, status.HTTP_202_ACCEPTED)
    return payload


def _undecided_competitions(session: Session, job: RegradeJob) -> list[str]:
    impacted = {c.get("competition_xid") for c in (job.competition_impact or [])
                if c.get("decision_required")}
    if not impacted:
        return []
    decided = set(session.scalars(text("""
        SELECT c.xid::text FROM competition_regrade_decisions d
        JOIN competitions c ON c.id = d.competition_id
        WHERE d.regrade_job_id = :j AND d.decision IS NOT NULL
    """).bindparams(j=job.id)))
    return sorted(x for x in impacted if x and x not in decided)
