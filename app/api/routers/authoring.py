"""Authoring endpoints: the publish gate, the key fix, and import.

The three authoring operations that are not CRUD. Composition — tests, versions,
sections, group placement and numbering — lives in `tests_authoring.py`, and the
asset library in `assets.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
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
from app.platform.findings import Report

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
                   now=Depends(clock),
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
            # Was `SELECT created_at FROM test_versions LIMIT 1` — an unrelated
            # row's timestamp, whichever the planner happened to return first, and
            # NULL on an empty table. "When did this key change" is the first
            # question of any regrade dispute.
            .values(is_current=False, superseded_at=now.now()))
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
        # DISTINCT: one attempt can hold several slots of the same question, and
        # "42 attempts affected" must not read as 126 because it was a three-blank
        # sentence completion.
        select(func.count(func.distinct(Attempt.id))).select_from(ItemScore)
        .join(ScoreRun, ScoreRun.id == ItemScore.score_run_id)
        .join(Attempt, Attempt.id == ScoreRun.attempt_id)
        .where(ItemScore.question_version_id == question_version_id,
               ScoreRun.is_current.is_(True), Attempt.mode != "preview")) or 0
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


# A whole test as DOCX or CSV. Generous — a 40-question paper with images
# described in the document is still small — and bounded, because `file.read()`
# with no ceiling is a way to put an arbitrary file into memory on a 4 vCPU box.
MAX_IMPORT_BYTES = 32 * 1024 * 1024


@router.post("/imports", status_code=status.HTTP_202_ACCEPTED)
def start_import(request: Request,
                 file: UploadFile = File(...),
                 source_format: str = Form(default="", alias="format"),
                 attestation: str = Form(default=""),
                 target_test_xid: uuid.UUID | None = Form(default=None),
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 reg: Registry = Depends(registry)) -> dict:
    """Always a dry run first. Nothing is written to content here.

    **Two things were missing, and the second is a stated constraint.**

    There was no authorization at all. `Action.IMPORT` is in the policy matrix —
    teacher and above — and was called from nowhere, so any authenticated user
    including a student could import a test and commit it into their centre's org.
    Import was the one door into the content library with no lock on it.

    And no copyright attestation was captured. "Any uploader must affirm the
    material is original or licensed, and that attestation is logged with the
    upload... assume some centres WILL try to upload published Cambridge papers."
    A bulk import IS that upload — it is the likeliest single route to that
    liability — and every part of the machinery already existed:
    `content_attestations` lists `'import_job'` in its `subject_type` CHECK,
    `media.record_attestation` stores the statement hash rather than a boolean, and
    the OpenAPI form declares the field. Only the call was absent.

    The form field is `format`, which is what the schema has always said; the
    handler read `source_format`, so a client written against the contract got a
    422 for a field the contract does not mention.
    """
    from app.modules.authz import policy
    from app.modules.authz.policy import Action, Resource
    from app.modules.content.media import record_attestation

    org_id = actor.org_ids[0] if actor.org_ids else None
    policy.require(actor, Action.IMPORT, Resource(org_id=org_id))
    # Checked against the adapters BEFORE the row is built. `import_jobs`
    # constrains `source_format` to the three supported values, so an unknown one
    # used to reach the INSERT and come back as a CheckViolation — a 500 for a
    # typo, where `importer.parse` was already prepared to report it properly.
    if source_format not in importer.ADAPTERS:
        _refuse("FORMAT_UNSUPPORTED",
                f"Unsupported format {source_format!r}." if source_format
                else "An import must say what format it is.",
                "format", f"One of: {', '.join(sorted(importer.ADAPTERS))}.")
    claim = _attestation(attestation)

    raw = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        _refuse("IMPORT_TOO_LARGE",
                f"An import may not exceed {MAX_IMPORT_BYTES // (1024 * 1024)} MB.",
                "file", "Split the paper, or export without embedded media.")
    result = importer.parse(raw, source_format, reg)

    job = ImportJob(
        org_id=org_id,
        created_by=actor.user_id, source_format=source_format,
        status="validated" if result.ok else "failed",
        canonical=result.canonical or None,
        # Accepted and dropped, though `import_jobs.target_test_id` exists for it
        # and its column comment says what it is for: "import as a new VERSION of
        # an existing test". That is the offline round trip the export half
        # promises — export, edit in Word, import back — and it landed as an
        # unrelated new test every time.
        target_test_id=_target_test(session, target_test_xid, actor),
        report=result.as_dict(),
    )
    session.add(job)
    session.flush()
    # Recorded even when the parse fails. The affirmation was made when the file
    # was handed over, and a failed parse does not un-make it — a centre that
    # repeatedly uploads material it cannot attest to is precisely the pattern an
    # investigation looks for.
    record_attestation(session, subject_type="import_job", subject_id=job.id,
                       user_id=actor.user_id, org_id=org_id, attestation=claim,
                       ip=request.client.host if request.client else None,
                       user_agent=request.headers.get("user-agent"))
    session.flush()
    return jsonify({"xid": str(job.xid), "status": job.status,
                    "source_format": job.source_format, "report": job.report})


def _target_test(session: Session, xid: uuid.UUID | None,
                 actor: Principal) -> int | None:
    """Scoped, like every other reference resolved on the way in: importing over
    someone else's test would be an overwrite of their material."""
    from app.api.routers.assets import scoped
    from app.modules.content.models import Test

    if xid is None:
        return None
    test = session.scalars(
        scoped(actor, select(Test).where(Test.xid == xid), Test)).first()
    if test is None:
        raise NotFound("Test not found.")
    return test.id


def _refuse(code: str, message: str, path: str, fix_hint: str) -> None:
    """One finding, in the same shape the media upload path returns."""
    report = Report()
    report.add(code, message, path=path, fix_hint=fix_hint)
    raise ValidationFailed("This import was refused.", report.errors)


def _attestation(raw: str) -> dict:
    """Parse and check the affirmation before anything else happens to the file.

    Same rules as the media upload path, deliberately: refused rather than
    defaulted. "A missing attestation that quietly becomes 'original' is worse than
    no attestation at all — it manufactures a claim the uploader never made, which
    is the opposite of evidence."
    """
    import json as _json

    from app.modules.content.media import VALID_CLAIMS

    try:
        parsed = _json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    if parsed.get("claim") not in VALID_CLAIMS:
        _refuse("ATTESTATION_REQUIRED",
                "A copyright attestation is required for every upload.",
                "attestation.claim", f"One of: {', '.join(sorted(VALID_CLAIMS))}.")
    if parsed["claim"] == "licensed" and not parsed.get("licence_note"):
        _refuse("LICENCE_NOTE_REQUIRED",
                "A licensed upload must say what the licence is.",
                "attestation.licence_note", "Name the licence or the agreement.")
    return parsed


@router.get("/imports/{xid}")
def read_import(xid: uuid.UUID,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    job = session.scalars(select(ImportJob).where(ImportJob.xid == xid)).first()
    if job is None or job.created_by != actor.user_id:
        raise NotFound("Import job not found.")
    # Was hardcoded `None`, though `committed_test_version_xid` is declared in the
    # ImportJob schema and `commit_import` sets the column three lines from here —
    # so an author who committed an import could never learn what it produced.
    committed = (session.get(TestVersion, job.committed_test_version_id)
                 if job.committed_test_version_id else None)
    return {"xid": str(job.xid), "status": job.status,
            "source_format": job.source_format, "report": job.report,
            "committed_test_version_xid": str(committed.xid) if committed else None}


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
                         owner_user_id=actor.user_id, now=now.now(), registry=reg,
                         target_test_id=job.target_test_id)
    job.status = "committed"
    job.committed_at = now.now()
    job.committed_test_version_id = tv.id
    job.confirmed_by = actor.user_id
    session.flush()

    payload = {"xid": str(job.xid), "status": job.status,
               "source_format": job.source_format,
               "committed_test_version_xid": str(tv.xid),
               "test_version_status": tv.status}
    idem.store({}, payload)
    return payload
