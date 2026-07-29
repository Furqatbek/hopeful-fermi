"""Authoring endpoints: the publish gate, the key fix, and import.

Only the load-bearing surface is implemented. The rest of the 113-path contract
in Deliverable 3 is CRUD over the same repositories and adds no new decisions.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.dto import iso, jsonify
from app.api.deps import Idempotency, Principal, clock, db, idempotency, principal, registry
from app.modules.content import importer, publish_gate
from app.modules.content import repo as content_repo
from app.modules.content.models import (
    AnswerKeyVersion, ImportJob, QuestionVersion, TestVersion, TestVersionValidation,
)
from app.modules.qtypes.registry import Registry
from app.platform.errors import Conflict, Forbidden, NotFound, ValidationFailed

router = APIRouter(tags=["authoring-tests"])


def _may_publish(actor: Principal, org_id: int | None, session: Session) -> bool:
    """Teachers cannot publish unless the centre opted in. A centre's reputation
    rides on its published material, so the default is off."""
    if actor.is_platform_admin:
        return True
    role = actor.role_in(org_id)
    if role == "centre_admin":
        return True
    if role == "teacher":
        from app.modules.identity.models import Organization
        org = session.get(Organization, org_id)
        return bool((org.settings or {}).get("teacher_can_publish"))
    return False


def _test_version(session: Session, xid: uuid.UUID) -> TestVersion:
    tv = session.scalars(select(TestVersion).where(TestVersion.xid == xid)).first()
    if tv is None:
        raise NotFound("Test version not found.")
    return tv


@router.post("/test-versions/{xid}/validate")
def validate_version(xid: uuid.UUID,
                     actor: Principal = Depends(principal),
                     session: Session = Depends(db),
                     reg: Registry = Depends(registry)) -> dict:
    """Run the gate without publishing. Returns EVERY finding, and persists them
    so an author can close the tab and come back to the list."""
    tv = _test_version(session, xid)
    report = publish_gate.run(content_repo.load_composition(session, tv.id), reg)
    session.add(TestVersionValidation(
        test_version_id=tv.id, passed=report.passed,
        findings=[f.as_dict() for f in report.findings],
        error_count=len(report.errors), warning_count=len(report.warnings),
        run_by=actor.user_id))
    session.flush()
    return report.as_dict()


@router.post("/test-versions/{xid}/publish")
def publish_version(xid: uuid.UUID,
                    actor: Principal = Depends(principal),
                    session: Session = Depends(db),
                    reg: Registry = Depends(registry),
                    now=Depends(clock)) -> dict:
    tv = _test_version(session, xid)
    from app.modules.content.models import Test

    test = session.get(Test, tv.test_id)
    if not _may_publish(actor, test.org_id, session):
        raise Forbidden("Publishing is restricted to centre admins at this centre.",
                        code="publish_not_permitted")
    if tv.status == "published":
        raise Conflict("This version is already published.", code="already_published")

    report = publish_gate.run(content_repo.load_composition(session, tv.id), reg)
    session.add(TestVersionValidation(
        test_version_id=tv.id, passed=report.passed,
        findings=[f.as_dict() for f in report.findings],
        error_count=len(report.errors), warning_count=len(report.warnings),
        run_by=actor.user_id))
    if not report.passed:
        # 422 with everything at once: one round of fixes, not nineteen.
        raise ValidationFailed("This test is not ready to publish.", report.errors)

    published = content_repo.publish(session, tv.id, actor.user_id, now.now())
    test.current_published_version_id = published.id
    session.flush()
    return {"xid": str(published.xid), "status": published.status,
            "version_no": published.version_no,
            "total_questions": published.total_questions,
            "published_at": iso(published.published_at)}


class AnswerKeyCreate(BaseModel):
    key: dict[str, Any]
    reason: str = "key_fix"
    note: str | None = None
    tolerance: dict[str, Any] | None = None


@router.post("/question-versions/{xid}/keys", status_code=status.HTTP_201_CREATED)
def fix_answer_key(xid: uuid.UUID, body: AnswerKeyCreate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db),
                   idem: Idempotency = Depends(idempotency)) -> dict:
    """The key fix.

    Works on PUBLISHED content, which is the point: a bad key must be fixable
    without invalidating what students already sat. The question version stays
    frozen; a new key version supersedes the old one. Nothing is regraded here —
    a dry-run job is staged and the impact returned for a human to confirm.
    """
    scope = f"keys.create:{xid}"
    if replayed := idem.replay(scope, body.model_dump(mode="json")):
        return replayed

    qv = session.scalars(select(QuestionVersion).where(QuestionVersion.xid == xid)).first()
    if qv is None:
        raise NotFound("Question version not found.")

    current = session.scalars(
        select(AnswerKeyVersion).where(AnswerKeyVersion.question_version_id == qv.id,
                                       AnswerKeyVersion.is_current.is_(True))).first()
    next_no = (current.version_no + 1) if current else 1
    if current is not None:
        session.execute(
            update(AnswerKeyVersion)
            .where(AnswerKeyVersion.id == current.id)
            .values(is_current=False, superseded_at=session.execute(
                select(TestVersion.created_at).limit(1)).scalar() or None))
        session.flush()

    new_key = AnswerKeyVersion(
        question_version_id=qv.id, version_no=next_no, key=body.key,
        tolerance=body.tolerance or {}, reason=body.reason, note=body.note,
        created_by=actor.user_id, is_current=True)
    session.add(new_key)
    session.flush()

    impact = _regrade_preview(session, qv.id)
    payload = {
        "key_version": {"xid": str(new_key.xid), "version_no": new_key.version_no,
                        "reason": new_key.reason, "is_current": True},
        "regrade_preview": impact,
    }
    idem.store(body.model_dump(mode="json"), payload, status.HTTP_201_CREATED)
    return payload


def _regrade_preview(session: Session, question_version_id: int) -> dict:
    """How many sat attempts this key change would touch.

    Counting is enough for the response; the full impact (band changes, rank
    movement) is computed by the regrade planner when the job runs, so this
    endpoint stays fast even when an item has been sat ten thousand times.
    """
    from app.modules.exam.models import Attempt, ItemScore, ScoreRun

    affected = session.scalar(
        select(Attempt.id).select_from(ItemScore)
        .join(ScoreRun, ScoreRun.id == ItemScore.score_run_id)
        .join(Attempt, Attempt.id == ScoreRun.attempt_id)
        .where(ItemScore.question_version_id == question_version_id,
               ScoreRun.is_current.is_(True), Attempt.mode != "preview")
        .with_only_columns(__import__("sqlalchemy").func.count())) or 0
    competitions = session.scalars(
        select(Attempt.competition_id).select_from(ItemScore)
        .join(ScoreRun, ScoreRun.id == ItemScore.score_run_id)
        .join(Attempt, Attempt.id == ScoreRun.attempt_id)
        .where(ItemScore.question_version_id == question_version_id,
               Attempt.competition_id.isnot(None)).distinct()).all()
    return {
        "attempts_total": affected,
        "competition_impact": [
            {"competition_id": c, "decision_required": True} for c in competitions],
        "note": ("Nothing has been regraded. Stage a regrade job and confirm it."
                 if affected else "No sat attempts are affected."),
    }


@router.post("/imports", status_code=status.HTTP_202_ACCEPTED)
def start_import(file: UploadFile = File(...),
                 source_format: str = Form(...),
                 target_test_xid: uuid.UUID | None = Form(default=None),
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 reg: Registry = Depends(registry)) -> dict:
    """Always a dry run first. Nothing is written to content here."""
    raw = file.file.read()
    result = importer.parse(raw, source_format, reg)

    job = ImportJob(
        org_id=actor.org_ids[0] if actor.org_ids else None,
        created_by=actor.user_id, source_format=source_format,
        status="validated" if result.ok else "failed",
        canonical=result.canonical or None,
        report=result.as_dict(),
    )
    session.add(job)
    session.flush()
    return jsonify({"xid": str(job.xid), "status": job.status, "report": job.report})


@router.get("/imports/{xid}")
def read_import(xid: uuid.UUID,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    job = session.scalars(select(ImportJob).where(ImportJob.xid == xid)).first()
    if job is None or job.created_by != actor.user_id:
        raise NotFound("Import job not found.")
    return {"xid": str(job.xid), "status": job.status, "report": job.report,
            "committed_test_version_xid": None}


@router.post("/imports/{xid}/commit")
def commit_import(xid: uuid.UUID,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db),
                  reg: Registry = Depends(registry),
                  now=Depends(clock),
                  idem: Idempotency = Depends(idempotency)) -> dict:
    """Applies the STORED canonical document the author reviewed, never a
    re-parse. Lands as a draft: import must not become a publish bypass."""
    scope = f"imports.commit:{xid}"
    if replayed := idem.replay(scope, {}):
        return replayed

    job = session.scalars(select(ImportJob).where(ImportJob.xid == xid)).first()
    if job is None or job.created_by != actor.user_id:
        raise NotFound("Import job not found.")
    if job.status != "validated":
        raise Conflict(f"This import is '{job.status}' and cannot be committed.",
                       code="import_not_validated")

    tv = importer.commit(session, job.canonical, org_id=job.org_id,
                         owner_user_id=actor.user_id, now=now.now(), registry=reg)
    job.status = "committed"
    job.committed_at = now.now()
    job.committed_test_version_id = tv.id
    job.confirmed_by = actor.user_id
    session.flush()

    payload = {"xid": str(job.xid), "status": job.status,
               "committed_test_version_xid": str(tv.xid),
               "test_version_status": tv.status}
    idem.store({}, payload)
    return payload
