"""Tests, versions, sections and group placement — the composition surface.

This is where "authoring is the product" becomes concrete. Three things here are
not CRUD and are worth reading:

  * `_renumber()` — IELTS numbering is test-wide and derived, never stored by the
    client. Every structural mutation recomputes it in one pass.
  * `clone` copies the COMPOSITION, not the assets. A cloned test references the
    same passage and question-group versions, so a 40-question mock clones in a
    few hundred bytes and editing the clone cannot touch the original.
  * `new_version` seeds from a published version. That is how a published test is
    "edited" without mutating anything a student has already sat.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, exam_session, principal, registry
from app.api.dto import iso, jsonify
from app.api.routers.assets import (
    _page, audio_dto, gv_dto, scoped, version_dto,
)
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.content import repo as content_repo
from app.modules.content.models import (
    AudioTrack, Passage, PassageVersion, QuestionGroup, QuestionGroupItem,
    QuestionGroupVersion, QuestionVersion, Test, TestVersion, TestVersionGroup,
    TestVersionSection, TestVersionValidation,
)
from app.modules.exam.session import ExamSession
from app.modules.qtypes.registry import Registry
from app.platform.errors import Conflict, Forbidden, NotFound

router = APIRouter(tags=["authoring-tests"])


# ── lookups ──────────────────────────────────────────────────────────

def _test(session: Session, xid: uuid.UUID, actor: Principal,
          action: Action = Action.READ) -> Test:
    row = session.scalars(
        scoped(actor, select(Test).where(Test.xid == xid), Test)).first()
    if row is None:
        raise NotFound("Test not found.")
    if action is not Action.READ:
        policy.require(actor, action, _resource(row), org_settings=_settings(session, row))
    return row


def _resource(test: Test, status_of: str | None = None) -> Resource:
    return Resource(org_id=test.org_id, owner_user_id=test.owner_user_id,
                    visibility=test.visibility, status=status_of, kind="test")


def _settings(session: Session, test: Test) -> dict:
    """Read once per request and passed explicitly, so the policy engine stays a
    pure function of its arguments and remains testable without a database."""
    from app.modules.identity.models import Organization

    if test.org_id is None:
        return {}
    org = session.get(Organization, test.org_id)
    return dict(org.settings or {}) if org else {}


def _version(session: Session, xid: uuid.UUID, actor: Principal,
             action: Action = Action.READ) -> tuple[TestVersion, Test]:
    row = session.execute(
        scoped(actor,
               select(TestVersion, Test).join(Test, Test.id == TestVersion.test_id)
               .where(TestVersion.xid == xid), Test)).first()
    if row is None:
        raise NotFound("Test version not found.")
    tv, test = row
    if action is not Action.READ:
        policy.require(actor, action, _resource(test, tv.status),
                       org_settings=_settings(session, test))
    return tv, test


def _section(session: Session, xid: uuid.UUID, actor: Principal,
             action: Action = Action.EDIT) -> tuple[TestVersionSection, TestVersion, Test]:
    row = session.execute(
        scoped(actor,
               select(TestVersionSection, TestVersion, Test)
               .join(TestVersion, TestVersion.id == TestVersionSection.test_version_id)
               .join(Test, Test.id == TestVersion.test_id)
               .where(TestVersionSection.xid == xid), Test)).first()
    if row is None:
        raise NotFound("Section not found.")
    section, tv, test = row
    if action is not Action.READ:
        policy.require(actor, action, _resource(test, tv.status),
                       org_settings=_settings(session, test))
    return section, tv, test


def _mutable(tv: TestVersion) -> TestVersion:
    """A published version is frozen. Every structural write goes through here, so
    "immutable after publish" is one check rather than eleven."""
    if tv.status in ("published", "archived"):
        raise Conflict(
            f"This version is {tv.status} and cannot be edited. Start a new version.",
            code="version_immutable")
    return tv


# ── DTOs ─────────────────────────────────────────────────────────────

def test_dto(session: Session, test: Test, actor: Principal | None = None) -> dict:
    published = (session.get(TestVersion, test.current_published_version_id)
                 if test.current_published_version_id else None)
    return {
        "xid": str(test.xid), "title": test.title, "description": test.description,
        "kind": test.kind, "variant": test.variant, "skills": list(test.skills or []),
        "visibility": test.visibility, "tags": list(test.tags or []),
        "org": _org_ref(session, test.org_id),
        "current_published_version_xid": str(published.xid) if published else None,
        "updated_at": iso(test.updated_at),
    }


def _org_ref(session: Session, org_id: int | None) -> dict | None:
    """Attribution, without the owning centre's configuration.

    The `org` field was missing from this DTO entirely, so a library listing never
    said which centre a paper came from — which for a platform admin looking at
    every centre's material at once is the one thing they need to know.

    Identity only, deliberately. `identity.org_dto` also returns `settings`, and a
    `platform_global` paper shared by one centre appears in another centre's
    library: emitting its owner's settings there would hand a competitor that
    centre's configuration. Whose paper it is, is attribution; how they have their
    centre set up, is not.
    """
    from app.modules.identity.models import Organization

    if org_id is None:
        return None
    org = session.get(Organization, org_id)
    return ({"xid": str(org.xid), "name": org.name, "slug": org.slug,
             "kind": org.kind, "status": org.status} if org else None)


def tv_dto(session: Session, tv: TestVersion) -> dict:
    from app.modules.content.models import BandMapVersion

    last = session.scalars(
        select(TestVersionValidation)
        .where(TestVersionValidation.test_version_id == tv.id)
        .order_by(TestVersionValidation.run_at.desc()).limit(1)).first()
    # Was hardcoded `None`, so a version never reported the band map it was
    # actually scored against — and the publish gate refuses a version without
    # one, which made "why won't this publish?" unanswerable from the API.
    band_map = (session.get(BandMapVersion, tv.band_map_version_id)
                if tv.band_map_version_id else None)
    return {
        "xid": str(tv.xid), "version_no": tv.version_no, "status": tv.status,
        "title": tv.title, "total_questions": tv.total_questions,
        "max_raw": float(tv.max_raw or 0),
        "band_map_version_xid": str(band_map.xid) if band_map else None,
        "published_at": iso(tv.published_at),
        "last_validation": ({"passed": last.passed, "findings": last.findings,
                             "error_count": last.error_count,
                             "warning_count": last.warning_count,
                             "run_at": iso(last.run_at),
                             "duration_ms": last.duration_ms} if last else None),
    }


def permissions(actor: Principal, test: Test, org_settings: dict,
                status_of: str | None = None) -> dict:
    """Resolved once, server-side, from the same matrix the endpoints enforce.

    The client renders buttons from this rather than re-implementing the rules —
    which is how a UI and a backend drift apart and start disagreeing about who
    may publish.
    """
    resource = _resource(test, status_of)
    return {action.value if action is not Action.CREATE else "clone":
            policy.check(actor, action, resource, org_settings=org_settings).allowed
            for action in (Action.EDIT, Action.PUBLISH, Action.ARCHIVE, Action.DELETE,
                           Action.SHARE, Action.EXPORT, Action.REGRADE, Action.CREATE)}


def section_dto(session: Session, section: TestVersionSection,
                with_groups: bool = True) -> dict:
    pv = (session.get(PassageVersion, section.passage_version_id)
          if section.passage_version_id else None)
    track = (session.get(AudioTrack, section.audio_track_id)
             if section.audio_track_id else None)
    out = {
        "xid": str(section.xid), "position": section.position, "skill": section.skill,
        "title": section.title,
        "passage_version": version_dto(pv) if pv else None,
        "audio_track": audio_dto(track) if track else None,
        "time_limit_seconds": section.time_limit_seconds,
        "declared_question_count": section.declared_question_count,
        "play_once": section.play_once,
    }
    if with_groups:
        out["groups"] = [placement_dto(session, p) for p in session.scalars(
            select(TestVersionGroup).where(TestVersionGroup.section_id == section.id)
            .order_by(TestVersionGroup.position))]
    return out


def placement_dto(session: Session, placement: TestVersionGroup) -> dict:
    gv = session.get(QuestionGroupVersion, placement.group_version_id)
    return {
        # The placement's own identity is the group version it places: a group
        # version appears at most once in a test version.
        "xid": str(gv.xid) if gv else None,
        "position": placement.position, "number_start": placement.number_start,
        "audio_start_ms": placement.audio_start_ms,
        "audio_end_ms": placement.audio_end_ms,
        "group_version": gv_dto(gv) if gv else None,
    }


# ── numbering ────────────────────────────────────────────────────────

def _renumber(session: Session, test_version_id: int) -> None:
    """Recompute test-wide IELTS numbering after any structural change.

    Derived, never client-supplied. A group's `number_start` is a function of
    everything before it, so accepting it from the client would mean two clients
    editing the same test can produce a test numbered 1-13, 1-13.

    One pass over the placements: an ordering read and a single UPDATE per group,
    which for a 4-section mock is a dozen rows.
    """
    rows = session.execute(
        select(TestVersionGroup.id, QuestionGroupItem.question_version_id,
               QuestionVersion.slot_keys)
        .join(TestVersionSection,
              TestVersionSection.id == TestVersionGroup.section_id)
        .outerjoin(QuestionGroupItem,
                   QuestionGroupItem.group_version_id == TestVersionGroup.group_version_id)
        .outerjoin(QuestionVersion,
                   QuestionVersion.id == QuestionGroupItem.question_version_id)
        .where(TestVersionSection.test_version_id == test_version_id)
        .order_by(TestVersionSection.position, TestVersionGroup.position,
                  QuestionGroupItem.position)).all()

    number = 1
    seen: set[int] = set()
    for placement_id, _qv_id, slot_keys in rows:
        if placement_id not in seen:
            # `rows` has one entry per ITEM, so a five-question group appears five
            # times. Only the first sets `number_start` — writing on every row
            # would leave the group starting at its LAST question's number.
            seen.add(placement_id)
            current = session.get(TestVersionGroup, placement_id)
            if current is not None and current.number_start != number:
                current.number_start = number
        # A question consumes one number PER SLOT: a three-blank sentence
        # completion is questions 14, 15 and 16, not question 14.
        number += len(slot_keys or []) if slot_keys is not None else 0
    session.flush()


# ── tests ────────────────────────────────────────────────────────────

class TestCreate(BaseModel):
    title: str
    description: str | None = None
    kind: str = Field(default="mock", pattern="^(mock|practice|competition|placement)$")
    variant: str = Field(default="academic", pattern="^(academic|general_training)$")
    skills: list[str] = ["reading"]
    org_xid: uuid.UUID | None = None
    tags: list[str] = []


class TestUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    visibility: str | None = Field(
        default=None, pattern="^(author_private|org_private|platform_global)$")


@router.get("/tests")
def list_tests(q: str | None = None, kind: str | None = None, skill: str | None = None,
               visibility: str | None = None, status_filter: str | None = None,
               tag: str | None = None, limit: int = 25,
               actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    """The authoring library.

    Every row has passed `policy.filter_content`. That filter — not the
    detail-level check below it — is what keeps a centre's material away from
    competitors, because real multi-tenant leaks are missing list scopes.
    """
    query = select(Test).where(Test.archived_at.is_(None))
    if q:
        query = query.where(Test.title.ilike(f"%{q}%"))
    if kind:
        query = query.where(Test.kind == kind)
    if visibility:
        query = query.where(Test.visibility == visibility)
    if skill:
        query = query.where(Test.skills.any(skill))
    if tag:
        query = query.where(Test.tags.any(tag))
    rows = session.scalars(
        scoped(actor, query, Test).order_by(Test.updated_at.desc()).limit(limit)).all()
    return _page([test_dto(session, t) for t in rows])


@router.post("/tests", status_code=status.HTTP_201_CREATED)
def create_test(body: TestCreate, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """Creates the test AND its first draft version.

    A test with no version is a state the rest of the system would have to guard
    against everywhere; making it unreachable is cheaper than handling it.
    """
    org_id = _org_for(session, body.org_xid, actor)
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))
    test = Test(org_id=org_id, owner_user_id=actor.user_id, title=body.title,
                description=body.description, kind=body.kind, variant=body.variant,
                skills=body.skills, tags=body.tags)
    session.add(test)
    session.flush()
    session.add(TestVersion(test_id=test.id, title=body.title,
                            created_by=actor.user_id))
    session.flush()
    return test_dto(session, test)


def _org_for(session: Session, org_xid: uuid.UUID | None, actor: Principal) -> int | None:
    from app.modules.identity.models import Organization

    if org_xid is None:
        return actor.org_ids[0] if actor.org_ids else None
    org_id = session.scalar(select(Organization.id).where(Organization.xid == org_xid))
    if org_id is None or (org_id not in actor.org_ids and not actor.is_platform_admin):
        raise NotFound("Organization not found.")
    return org_id


@router.get("/tests/{xid}")
def read_test(xid: uuid.UUID, actor: Principal = Depends(principal),
              session: Session = Depends(db)) -> dict:
    test = _test(session, xid, actor)
    versions = session.scalars(
        select(TestVersion).where(TestVersion.test_id == test.id)
        .order_by(TestVersion.version_no.desc())).all()
    return {**test_dto(session, test),
            "versions": [tv_dto(session, v) for v in versions],
            "permissions": permissions(actor, test, _settings(session, test))}


@router.patch("/tests/{xid}")
def update_test(xid: uuid.UUID, body: TestUpdate,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    test = _test(session, xid, actor, Action.EDIT)
    if body.visibility is not None and body.visibility != test.visibility:
        # Widening visibility is a share, not an edit: a teacher who may edit a
        # test must not be able to publish it to the whole platform.
        policy.require(actor, Action.SHARE, _resource(test),
                       org_settings=_settings(session, test))
        test.visibility = body.visibility
    for field in ("title", "description", "tags"):
        value = getattr(body, field)
        if value is not None:
            setattr(test, field, value)
    session.flush()
    return test_dto(session, test)


@router.delete("/tests/{xid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test(xid: uuid.UUID, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> Response:
    """Soft-archive, and refused outright once anything is published.

    Attempts reference published versions, and a takedown investigation needs the
    evidence intact. `policy.check` already refuses DELETE on published content;
    this second check catches the test whose *versions* are published.
    """
    test = _test(session, xid, actor, Action.DELETE)
    published = session.scalar(
        select(func.count()).select_from(TestVersion)
        .where(TestVersion.test_id == test.id, TestVersion.status == "published"))
    if published:
        raise Conflict("This test has published versions and cannot be deleted. "
                       "Archive it instead.", code="published_content_not_deletable")
    test.archived_at = dt.datetime.now(dt.UTC)
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class CloneRequest(BaseModel):
    title: str | None = None
    target_org_xid: uuid.UUID | None = None


@router.post("/tests/{xid}/clone", status_code=status.HTTP_201_CREATED)
def clone_test(xid: uuid.UUID, body: CloneRequest,
               actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    """Clones the composition, not the assets.

    The new draft references the same passage and question-group versions, so a
    40-question mock clones in a few hundred bytes and editing the clone cannot
    touch the original's material.
    """
    source = _test(session, xid, actor)
    target_org = _org_for(session, body.target_org_xid, actor)
    if target_org != source.org_id:
        # Cross-org clone is a copy of someone else's material: it needs an
        # explicit `copy` grant, not merely read access.
        _require_copy_grant(session, source, target_org, actor)
    policy.require(actor, Action.CREATE, Resource(org_id=target_org))

    latest = session.scalars(
        select(TestVersion).where(TestVersion.test_id == source.id)
        .order_by(TestVersion.version_no.desc()).limit(1)).first()

    clone = Test(org_id=target_org, owner_user_id=actor.user_id,
                 title=body.title or f"{source.title} (copy)",
                 description=source.description, kind=source.kind,
                 variant=source.variant, skills=list(source.skills or []),
                 tags=list(source.tags or []))
    session.add(clone)
    session.flush()
    draft = TestVersion(test_id=clone.id, title=clone.title,
                        config=dict(latest.config or {}) if latest else {},
                        band_map_version_id=latest.band_map_version_id if latest else None,
                        created_by=actor.user_id,
                        cloned_from_version_id=latest.id if latest else None)
    session.add(draft)
    session.flush()
    if latest is not None:
        _copy_composition(session, latest.id, draft.id)
        _renumber(session, draft.id)
    return test_dto(session, clone)


def _require_copy_grant(session: Session, source: Test, target_org: int | None,
                        actor: Principal) -> None:
    if actor.is_platform_admin or source.visibility == "platform_global":
        return
    granted = session.scalar(text("""
        SELECT count(*) FROM content_grants
        WHERE subject_type = 'test' AND subject_id = :sid AND permission = 'copy'
          AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
          AND ((grantee_kind = 'org' AND grantee_id = :org)
               OR (grantee_kind = 'user' AND grantee_id = :uid)
               OR grantee_kind = 'public')
    """).bindparams(sid=source.id, org=target_org or 0, uid=actor.user_id))
    if not granted:
        raise Forbidden("Copying this test into another organization needs a "
                        "'copy' grant from its owner.", code="copy_grant_required")


def _copy_composition(session: Session, from_version_id: int, to_version_id: int) -> None:
    """Rows, not assets. Sections and placements are cheap; the passages, audio and
    question groups they point at are shared."""
    for section in session.scalars(
        select(TestVersionSection)
        .where(TestVersionSection.test_version_id == from_version_id)
        .order_by(TestVersionSection.position)
    ):
        copy = TestVersionSection(
            test_version_id=to_version_id, position=section.position,
            skill=section.skill, title=section.title,
            passage_version_id=section.passage_version_id,
            audio_track_id=section.audio_track_id,
            time_limit_seconds=section.time_limit_seconds,
            declared_question_count=section.declared_question_count,
            play_once=section.play_once, config=dict(section.config or {}))
        session.add(copy)
        session.flush()
        for placement in session.scalars(
            select(TestVersionGroup).where(TestVersionGroup.section_id == section.id)
            .order_by(TestVersionGroup.position)
        ):
            session.add(TestVersionGroup(
                section_id=copy.id, group_version_id=placement.group_version_id,
                position=placement.position, number_start=placement.number_start,
                audio_start_ms=placement.audio_start_ms,
                audio_end_ms=placement.audio_end_ms))
    session.flush()


# ── versions ─────────────────────────────────────────────────────────

class NewVersion(BaseModel):
    from_version_xid: uuid.UUID | None = None


@router.get("/tests/{xid}/versions")
def list_versions(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> list[dict]:
    test = _test(session, xid, actor)
    return [tv_dto(session, v) for v in session.scalars(
        select(TestVersion).where(TestVersion.test_id == test.id)
        .order_by(TestVersion.version_no.desc()))]


@router.post("/tests/{xid}/versions", status_code=status.HTTP_201_CREATED)
def new_version(xid: uuid.UUID, body: NewVersion,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """How a published test is edited: seed a new draft from it and leave the
    published version untouched, because students have sat it."""
    test = _test(session, xid, actor, Action.EDIT)
    source = None
    if body.from_version_xid:
        source, _ = _version(session, body.from_version_xid, actor)
        if source.test_id != test.id:
            raise NotFound("That version does not belong to this test.")
    else:
        source = session.scalars(
            select(TestVersion).where(TestVersion.test_id == test.id)
            .order_by(TestVersion.version_no.desc()).limit(1)).first()

    next_no = (session.scalar(
        select(func.max(TestVersion.version_no))
        .where(TestVersion.test_id == test.id)) or 0) + 1
    draft = TestVersion(
        test_id=test.id, version_no=next_no, title=source.title if source else test.title,
        config=dict(source.config or {}) if source else {},
        band_map_version_id=source.band_map_version_id if source else None,
        created_by=actor.user_id,
        cloned_from_version_id=source.id if source else None)
    session.add(draft)
    session.flush()
    if source is not None:
        _copy_composition(session, source.id, draft.id)
        _renumber(session, draft.id)
    return tv_dto(session, draft)


class TestVersionUpdate(BaseModel):
    title: str | None = None
    config: dict | None = None
    band_map_version_xid: uuid.UUID | None = None


@router.get("/test-versions/{xid}")
def read_version(xid: uuid.UUID, response: Response,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    tv, test = _version(session, xid, actor)
    if tv.checksum:
        response.headers["ETag"] = f'"{tv.checksum}"'
    sections = session.scalars(
        select(TestVersionSection)
        .where(TestVersionSection.test_version_id == tv.id)
        .order_by(TestVersionSection.position)).all()
    return {**tv_dto(session, tv),
            "sections": [section_dto(session, s) for s in sections],
            "permissions": permissions(actor, test, _settings(session, test), tv.status)}


@router.patch("/test-versions/{xid}")
def update_version(xid: uuid.UUID, body: TestVersionUpdate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    from app.modules.content.models import BandMapVersion

    tv, _ = _version(session, xid, actor, Action.EDIT)
    _mutable(tv)
    if body.title is not None:
        tv.title = body.title
    if body.config is not None:
        tv.config = body.config
    if body.band_map_version_xid is not None:
        bmv = session.scalar(
            select(BandMapVersion.id)
            .where(BandMapVersion.xid == body.band_map_version_xid))
        if bmv is None:
            raise NotFound("Band map version not found.")
        tv.band_map_version_id = bmv
    session.flush()
    return tv_dto(session, tv)


class ReviewRequest(BaseModel):
    notes: str | None = None


class ReviewDecision(BaseModel):
    decision: str = Field(pattern="^(approved|changes_requested)$")
    notes: str | None = None


@router.post("/test-versions/{xid}/submit-review")
def submit_review(xid: uuid.UUID, body: ReviewRequest,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db),
                  reg: Registry = Depends(registry)) -> dict:
    """draft → in_review, and the publish gate runs first.

    Sending a reviewer a test with nineteen structural errors wastes the scarcest
    resource a small centre has. If the gate fails, the author fixes it before a
    human is involved.
    """
    from app.modules.content import publish_gate
    from app.platform.errors import ValidationFailed

    tv, _test_row = _version(session, xid, actor, Action.EDIT)
    _mutable(tv)
    report = publish_gate.run(content_repo.load_composition(session, tv.id), reg)
    session.add(TestVersionValidation(
        test_version_id=tv.id, passed=report.passed,
        findings=[f.as_dict() for f in report.findings],
        error_count=len(report.errors), warning_count=len(report.warnings),
        run_by=actor.user_id))
    if not report.passed:
        raise ValidationFailed("This test has errors that must be fixed before review.",
                               report.errors)

    tv.status = "in_review"
    tv.submitted_for_review_at = dt.datetime.now(dt.UTC)
    row = session.execute(text("""
        INSERT INTO content_reviews (test_version_id, requested_by, state, notes)
        VALUES (:tv, :by, 'requested', :notes)
        RETURNING id, state, notes, created_at
    """).bindparams(tv=tv.id, by=actor.user_id, notes=body.notes)).mappings().one()
    session.flush()
    return _review_dto(session, row, actor)


@router.post("/test-versions/{xid}/review")
def decide_review(xid: uuid.UUID, body: ReviewDecision,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    """Approving is a publish-adjacent act, so it needs publish authority.

    Otherwise a teacher who cannot publish approves their own work and a
    centre_admin rubber-stamps it — the review becomes theatre.
    """
    tv, test = _version(session, xid, actor)
    policy.require(actor, Action.PUBLISH, _resource(test, tv.status),
                   org_settings=_settings(session, test))
    if tv.status != "in_review":
        raise Conflict(f"This version is '{tv.status}', not in review.",
                       code="not_in_review")

    row = session.execute(text("""
        UPDATE content_reviews SET state = :state, notes = coalesce(:notes, notes),
               reviewer_id = :who, decided_at = now()
        WHERE id = (SELECT id FROM content_reviews
                    WHERE test_version_id = :tv AND state = 'requested'
                    ORDER BY created_at DESC LIMIT 1)
        RETURNING id, state, notes, created_at
    """).bindparams(state=body.decision, notes=body.notes, who=actor.user_id,
                    tv=tv.id)).mappings().first()
    if row is None:
        raise NotFound("No open review request for this version.")
    # `approved` does not publish. Publishing stays an explicit, separately
    # audited act — approval says the content is ready, not that it is live.
    tv.status = "draft" if body.decision == "changes_requested" else "in_review"
    session.flush()
    return _review_dto(session, row, actor)


def _review_dto(session: Session, row, actor: Principal) -> dict:
    from app.api.routers.identity import user_dto
    from app.modules.identity.models import User

    reviewer = session.get(User, actor.user_id)
    return {"xid": str(uuid.UUID(int=row["id"])), "state": row["state"],
            "notes": row["notes"], "reviewer": user_dto(reviewer) if reviewer else None,
            "created_at": iso(row["created_at"])}


@router.post("/test-versions/{xid}/archive")
def archive_version(xid: uuid.UUID, actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    """Retire a published version without deleting it.

    Existing attempts keep resolving against it; it just stops being assignable.
    This is the only correct answer to "take this test down" once it has been sat.
    """
    tv, test = _version(session, xid, actor, Action.ARCHIVE)
    tv.status = "archived"
    tv.archived_at = dt.datetime.now(dt.UTC)
    if test.current_published_version_id == tv.id:
        test.current_published_version_id = None
    session.flush()
    return tv_dto(session, tv)


@router.post("/test-versions/{xid}/preview", status_code=status.HTTP_201_CREATED)
def preview_version(xid: uuid.UUID, actor: Principal = Depends(principal),
                    session: Session = Depends(db),
                    exam: ExamSession = Depends(exam_session)) -> dict:
    """An attempt in `preview` mode against an UNPUBLISHED version.

    The author sees exactly what a student will see, timers and all — the one way
    to catch "this section is unanswerable" before a cohort does. Preview attempts
    are excluded from every statistic and from item exposure.
    """
    tv, _ = _version(session, xid, actor, Action.EDIT)
    if tv.snapshot is None:
        # Materialize on demand: the publish gate has not run, so this is
        # deliberately allowed to render a broken test.
        composition = content_repo.load_composition(session, tv.id)
        tv.snapshot = content_repo.build_snapshot(composition)
        tv.total_questions = composition.total_slots
        session.flush()
    attempt = exam.start(user_id=actor.user_id, test_version_id=tv.id, mode="preview")
    now = dt.datetime.now(dt.UTC)
    return {"xid": str(attempt.xid), "status": attempt.status, "mode": attempt.mode,
            "attempt_no": attempt.attempt_no, "expires_at": iso(attempt.expires_at),
            "server_now": iso(now),
            "seconds_remaining": (None if attempt.expires_at is None else
                                  max(0, int((attempt.expires_at - now).total_seconds())))}


# ── sections ─────────────────────────────────────────────────────────

class SectionCreate(BaseModel):
    position: int = Field(ge=1)
    skill: str = Field(pattern="^(reading|listening)$")
    title: str
    passage_version_xid: uuid.UUID | None = None
    audio_track_xid: uuid.UUID | None = None
    time_limit_seconds: int | None = None
    declared_question_count: int | None = None
    play_once: bool = True


@router.get("/test-versions/{xid}/sections")
def list_sections(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> list[dict]:
    tv, _ = _version(session, xid, actor)
    return [section_dto(session, s) for s in session.scalars(
        select(TestVersionSection).where(TestVersionSection.test_version_id == tv.id)
        .order_by(TestVersionSection.position))]


@router.post("/test-versions/{xid}/sections", status_code=status.HTTP_201_CREATED)
def create_section(xid: uuid.UUID, body: SectionCreate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    """Inserts at `position`, shifting whatever is there and everything after it.

    There is no "reorder sections" endpoint, so refusing a taken position would
    leave an author with no way to put a section in the middle of a test other
    than renumbering the rest by hand. Insert semantics are also what a UI's
    "add a section here" button needs, and they cannot fail.
    """
    tv, _ = _version(session, xid, actor, Action.EDIT)
    _mutable(tv)
    position = min(body.position, _section_count(session, tv.id) + 1)
    _make_room(session, tv.id, position)
    section = TestVersionSection(
        test_version_id=tv.id, position=position, skill=body.skill,
        title=body.title,
        passage_version_id=_passage_ref(session, body.passage_version_xid, actor),
        audio_track_id=_audio_ref(session, body.audio_track_xid, actor),
        time_limit_seconds=body.time_limit_seconds,
        declared_question_count=body.declared_question_count,
        play_once=body.play_once)
    session.add(section)
    session.flush()
    _renumber(session, tv.id)
    return section_dto(session, section)


def _section_count(session: Session, test_version_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(TestVersionSection)
        .where(TestVersionSection.test_version_id == test_version_id)) or 0


def _make_room(session: Session, test_version_id: int, position: int) -> None:
    """Shift sections at or after `position` down by one.

    Two statements, not one. `(test_version_id, position)` is a plain UNIQUE
    index, so a single `SET position = position + 1` can collide with a row it
    has not moved yet depending on the order the planner picks. Parking the
    affected rows in a negative range first makes the shift order-independent —
    and negative positions cannot collide with real ones.

    Insert semantics only. A MOVE cannot use this — see `_move_section`.
    """
    moved = session.execute(text("""
        UPDATE test_version_sections SET position = -position
        WHERE test_version_id = :tv AND position >= :p
        RETURNING id
    """).bindparams(tv=test_version_id, p=position)).rowcount
    if moved:
        session.execute(text("""
            UPDATE test_version_sections SET position = -position + 1
            WHERE test_version_id = :tv AND position < 0
        """).bindparams(tv=test_version_id))
    session.flush()


def _move_section(session: Session, section: TestVersionSection, to: int) -> None:
    """Move one section, shifting only the rows BETWEEN its old and new position.

    `update_section` used to park the moving row at `-position` and then call
    `_make_room`, which is wrong twice over. `_make_room`'s second statement is
    `WHERE position < 0`, so it swept the parked row along with the rest:

        sections 1, 2, 3 — move section 1 to position 3
        park            -1,  2,  3
        _make_room(3)   -1,  2, -3
        step two         2,  2,  4   <- duplicate key on (test_version_id, position)

    A 500 on every downhill move. Moving uphill did not raise, and was worse: it
    shifted rows that should not have moved, leaving 1, 2, 4. Nothing downstream
    complains, because `AttemptSection.position` is copied from the section and
    `ExamSession.enter_section(attempt, position)` looks it up by that number — so
    a published test with a hole at 3 is a test where a student reaches section 2,
    asks for section 3, and gets "Section not found in this attempt." Mid-exam.

    A move is not an insert: only the span the section travels over shifts, and it
    shifts TOWARDS the vacated slot. `to` is clamped to the number of sections, so
    "move to position 99" means "move to the end" rather than leaving a permanent
    gap at 4-98.
    """
    to = max(1, min(to, _section_count(session, section.test_version_id)))
    frm = section.position
    if to == frm:
        return
    # Park the mover outside the unique index so the span shift below cannot
    # collide with the slot it is vacating.
    section.position = -frm
    session.flush()
    low, high, delta = ((frm + 1, to, -1) if to > frm else (to, frm - 1, 1))
    moved = session.execute(text("""
        UPDATE test_version_sections SET position = -position
        WHERE test_version_id = :tv AND position BETWEEN :low AND :high
        RETURNING id
    """).bindparams(tv=section.test_version_id, low=low, high=high)).rowcount
    if moved:
        session.execute(text("""
            UPDATE test_version_sections SET position = -position + :delta
            WHERE test_version_id = :tv AND position < 0 AND id <> :me
        """).bindparams(tv=section.test_version_id, delta=delta, me=section.id))
    section.position = to
    session.flush()


def _close_position_gap(session: Session, test_version_id: int,
                        removed: int) -> None:
    """Pull everything after a deleted section up by one.

    Same reason as `_move_section`: a hole in the sequence is invisible to the
    author, invisible to the publish gate, and a 404 to the student who tries to
    enter the section after it.
    """
    session.execute(text("""
        UPDATE test_version_sections SET position = -position
        WHERE test_version_id = :tv AND position > :removed
    """).bindparams(tv=test_version_id, removed=removed))
    session.execute(text("""
        UPDATE test_version_sections SET position = -position - 1
        WHERE test_version_id = :tv AND position < 0
    """).bindparams(tv=test_version_id))
    session.flush()


def _passage_ref(session: Session, xid: uuid.UUID | None,
                 actor: Principal) -> int | None:
    """Resolved through the actor's own scope.

    Composition is the one place where a reference could smuggle a competitor's
    passage into a test the actor owns, so the reference is authorized on the way
    IN rather than hoped about later.
    """
    if xid is None:
        return None
    row = session.execute(
        scoped(actor,
               select(PassageVersion.id, Passage)
               .join(Passage, Passage.id == PassageVersion.passage_id)
               .where(PassageVersion.xid == xid), Passage)).first()
    if row is None:
        raise NotFound("Passage version not found.")
    return row[0]


def _audio_ref(session: Session, xid: uuid.UUID | None, actor: Principal) -> int | None:
    if xid is None:
        return None
    track = session.scalars(
        scoped(actor, select(AudioTrack).where(AudioTrack.xid == xid),
               AudioTrack)).first()
    if track is None:
        raise NotFound("Audio track not found.")
    return track.id


@router.patch("/sections/{xid}")
def update_section(xid: uuid.UUID, body: SectionCreate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    section, tv, _ = _section(session, xid, actor)
    _mutable(tv)
    if body.position != section.position:
        _move_section(session, section, body.position)
    section.skill = body.skill
    section.title = body.title
    section.time_limit_seconds = body.time_limit_seconds
    section.declared_question_count = body.declared_question_count
    section.play_once = body.play_once
    if body.passage_version_xid is not None:
        section.passage_version_id = _passage_ref(session, body.passage_version_xid, actor)
    if body.audio_track_xid is not None:
        section.audio_track_id = _audio_ref(session, body.audio_track_xid, actor)
    session.flush()
    _renumber(session, tv.id)
    return section_dto(session, section)


@router.delete("/sections/{xid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_section(xid: uuid.UUID, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> Response:
    section, tv, _ = _section(session, xid, actor)
    _mutable(tv)
    removed = section.position
    session.execute(TestVersionGroup.__table__.delete()
                    .where(TestVersionGroup.section_id == section.id))
    session.delete(section)
    session.flush()
    _close_position_gap(session, tv.id, removed)
    _renumber(session, tv.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class GroupPlacementCreate(BaseModel):
    group_version_xid: uuid.UUID
    position: int = Field(ge=1)
    audio_start_ms: int | None = None
    audio_end_ms: int | None = None


@router.post("/sections/{xid}/groups", status_code=status.HTTP_201_CREATED)
def place_group(xid: uuid.UUID, body: GroupPlacementCreate,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """Places an EXISTING group version. Nothing is copied — this is the reuse
    path that makes a question bank a bank rather than a folder of duplicates."""
    section, tv, _ = _section(session, xid, actor)
    _mutable(tv)
    row = session.execute(
        scoped(actor,
               select(QuestionGroupVersion, QuestionGroup)
               .join(QuestionGroup, QuestionGroup.id == QuestionGroupVersion.group_id)
               .where(QuestionGroupVersion.xid == body.group_version_xid),
               QuestionGroup)).first()
    if row is None:
        raise NotFound("Question group version not found.")
    gv, _group = row

    existing = session.scalar(
        select(func.count()).select_from(TestVersionGroup)
        .join(TestVersionSection,
              TestVersionSection.id == TestVersionGroup.section_id)
        .where(TestVersionSection.test_version_id == tv.id,
               TestVersionGroup.group_version_id == gv.id))
    if existing:
        raise Conflict("This group is already placed in this test version.",
                       code="group_already_placed")

    _make_room_for_group(session, section.id, body.position)
    placement = TestVersionGroup(
        section_id=section.id, group_version_id=gv.id, position=body.position,
        audio_start_ms=body.audio_start_ms, audio_end_ms=body.audio_end_ms)
    session.add(placement)
    session.flush()
    _renumber(session, tv.id)
    return placement_dto(session, placement)


def _make_room_for_group(session: Session, section_id: int, position: int) -> None:
    """The same two-step shift, for group placements within a section."""
    moved = session.execute(text("""
        UPDATE test_version_groups SET position = -position
        WHERE section_id = :s AND position >= :p
        RETURNING id
    """).bindparams(s=section_id, p=position)).rowcount
    if moved:
        session.execute(text("""
            UPDATE test_version_groups SET position = -position + 1
            WHERE section_id = :s AND position < 0
        """).bindparams(s=section_id))
    session.flush()


class Reorder(BaseModel):
    group_placement_xids: list[uuid.UUID]


@router.post("/sections/{xid}/reorder")
def reorder_groups(xid: uuid.UUID, body: Reorder,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> list[dict]:
    """Renumbers the whole test as a side effect and returns the new numbering.

    A reorder in section 2 shifts the numbers in sections 3 and 4, so returning
    only this section's placements would leave the client rendering stale numbers.
    """
    section, tv, _ = _section(session, xid, actor)
    _mutable(tv)
    placements = {
        str(gv_xid): placement
        for placement, gv_xid in session.execute(
            select(TestVersionGroup, QuestionGroupVersion.xid)
            .join(QuestionGroupVersion,
                  QuestionGroupVersion.id == TestVersionGroup.group_version_id)
            .where(TestVersionGroup.section_id == section.id)).all()
    }
    if {str(x) for x in body.group_placement_xids} != set(placements):
        raise Conflict("The reorder must list every group in this section exactly once.",
                       code="reorder_incomplete",
                       expected=sorted(placements), received=[str(x) for x in
                                                             body.group_placement_xids])
    # Park every row in the negative range before assigning final positions:
    # swapping two placements would otherwise trip `tvg_pos_uq` halfway through.
    for placement in placements.values():
        placement.position = -placement.position
    session.flush()
    for index, gv_xid in enumerate(body.group_placement_xids, start=1):
        placements[str(gv_xid)].position = index
    session.flush()
    _renumber(session, tv.id)
    return [placement_dto(session, p) for p in session.scalars(
        select(TestVersionGroup).where(TestVersionGroup.section_id == section.id)
        .order_by(TestVersionGroup.position))]


# ── export and import template ───────────────────────────────────────

@router.get("/test-versions/{xid}/export")
def export_version(xid: uuid.UUID, format: str = "json", include_keys: bool = False,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> Response:
    """Round-trips: export → edit offline → re-import as a new version.

    `include_keys` is checked against EXPORT authority, not read authority. A
    teacher at another centre holding a `view` grant can read a test in the app
    and must not be able to walk away with its answer key as a file.
    """
    tv, test = _version(session, xid, actor, Action.EXPORT)
    if include_keys:
        policy.require(actor, Action.VIEW_EXPOSURE, _resource(test, tv.status),
                       org_settings=_settings(session, test))

    composition = content_repo.load_composition(session, tv.id)
    document = content_repo.build_snapshot(composition)
    if include_keys:
        keys = {q.xid: {"key": q.key, "tolerance": q.tolerance}
                for s in composition.sections for g in s.groups for q in g.questions}
        document["answer_keys"] = keys
    document["export"] = {"format": "ielts-hub-import/1", "test_title": test.title,
                          "version_no": tv.version_no, "includes_keys": include_keys,
                          "exported_at": iso(dt.datetime.now(dt.UTC))}

    if format == "csv":
        return Response(content=_csv_export(document), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="test-{tv.version_no}.csv"'})
    return Response(content=json.dumps(jsonify(document), indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="test-{tv.version_no}.json"'})


def _csv_export(document: dict) -> str:
    """The SAME columns `content.importer.from_csv` reads.

    The round-trip claim in the contract is only true if export and import speak
    one format; a CSV export in its own private shape would export fine and fail
    on the way back in, which is the worst of both.
    """
    import csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["test_title", "section", "skill", "group", "instructions",
                     "word_limit", "type_key", "text", "answer"])
    keys = document.get("answer_keys", {})
    for section in document["sections"]:
        for group in section["groups"]:
            instructions = group.get("instructions") or {}
            for question in group["questions"]:
                key = keys.get(question["question_version_xid"], {}).get("key") or {}
                writer.writerow([
                    document["title"], section["title"], section["skill"],
                    f"Questions from {group['number_start']}",
                    instructions.get("en") or next(iter(instructions.values()), ""),
                    _word_limit_text(group.get("word_limit")),
                    question["type_key"], _question_text(question),
                    # Alternatives separated by `|`, which is what the CSV
                    # adapter splits on.
                    "|".join(_accepted(key)),
                ])
    return buffer.getvalue()


def _question_text(question: dict) -> str:
    payload = question.get("payload") or {}
    for field in ("text", "question", "statement", "prompt"):
        if isinstance(payload.get(field), str):
            return payload[field]
    return ""


def _accepted(key: dict) -> list[str]:
    out: list[str] = []
    for slot in (key.get("slots") or {}).values():
        out.extend(str(a) for a in (slot.get("accept") or []))
    return out


def _word_limit_text(limit: dict | None) -> str:
    if not limit:
        return ""
    words = {1: "ONE WORD", 2: "TWO WORDS", 3: "THREE WORDS"}.get(
        limit.get("max_words", 0), f"{limit.get('max_words')} WORDS")
    return f"{words} AND/OR A NUMBER" if limit.get("allow_number") else words


@router.get("/imports/template")
def import_template(format: str = "json") -> Response:
    """The supported import path, stated explicitly.

    Parsing an arbitrary Word file a teacher already has is an open-ended problem;
    parsing this template is a bounded one. Being blunt with centres about that up
    front is cheaper than an import feature that fails unpredictably.
    """
    if format == "csv":
        return Response(content=_CSV_TEMPLATE, media_type="text/csv",
                        headers={"Content-Disposition":
                                 'attachment; filename="ielts-hub-template.csv"'})
    return Response(content=json.dumps(_JSON_TEMPLATE, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             'attachment; filename="ielts-hub-template.json"'})


# The template a centre downloads is parsed by `content.importer` on the way back
# in, so `tests/integration/test_authoring_flow.py` imports it and asserts it
# validates. A template that does not import is worse than no template: it
# teaches the centre that the feature is broken.
_JSON_TEMPLATE = {
    "format": "ielts-hub-import/1",
    "title": "My reading mock",
    "variant": "academic",
    "sections": [{
        "title": "Section 1", "skill": "reading",
        "passage": {"title": "Passage title",
                    "blocks": [{"type": "paragraph",
                                "runs": [{"t": "text", "v": "Paragraph text."}]}]},
        "groups": [{
            "title": "Questions 1-2",
            "instructions": {"en": "Answer the questions below."},
            # Two independent limits, both enforced on the RAW answer before any
            # normalization: three words to a two-word limit is wrong, never
            # truncated.
            "word_limit": {"max_words": 2, "allow_number": True},
            "questions": [
                {"type_key": "short_answer", "type_version": 1,
                 "text": "What did they ride?",
                 # Alternatives the marker must accept. Spelling and number
                 # variants do not belong here — the tolerance lexicon absorbs
                 # those. List only genuinely different answers.
                 "accept": ["bike", "bicycle"]},
                {"type_key": "sentence_completion", "type_version": 1,
                 "text": "The museum opened in {{s1}}.",
                 "accept": ["1897"]},
            ],
        }],
    }],
}

_CSV_TEMPLATE = (
    "test_title,section,skill,group,instructions,word_limit,type_key,text,answer\n"
    "My reading mock,Section 1,reading,Questions 1-2,Answer the questions below.,"
    "TWO WORDS AND/OR A NUMBER,short_answer,What did they ride?,bike|bicycle\n"
    "My reading mock,Section 1,reading,Questions 1-2,Answer the questions below.,"
    "TWO WORDS AND/OR A NUMBER,sentence_completion,The museum opened in {{s1}}.,1897\n"
)
