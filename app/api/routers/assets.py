"""Authoring assets: passages, audio, questions, groups, band maps, cue cards.

Every listing here goes through `authz.filter_content`. That is not a
convention — `tests/integration/test_authz_leaks.py` asserts it for each one,
because a missing list-scope is how a centre's material reaches a competitor.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, principal, registry
from app.api.dto import iso
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.content.models import (
    AnswerKeyVersion, AudioTrack, BandMap, BandMapVersion, Passage, PassageVersion,
    Question, QuestionGroup, QuestionGroupItem, QuestionGroupVersion, QuestionVersion,
    TestVersionGroup, TestVersionSection,
)
from app.modules.qtypes.registry import Registry
from app.platform.errors import Conflict, NotFound

router = APIRouter(tags=["authoring-assets"])


def scoped(actor: Principal, query: Select, model: Any) -> Select:
    return policy.filter_content(actor, query, model)


def _letters(n: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(n)]


def org_settings(session: Session, org_id: int | None) -> dict:
    """Read once per request and passed explicitly, so the policy engine stays a
    pure function of its arguments and remains testable without a database.

    This module never passed it. `tests_authoring.py` did, so `teacher_can_publish`
    and `content_edit_others` worked for a TEST version and silently did nothing
    for the passages, questions and groups inside it — a centre that switched
    either on found it half-working. The direction of the bug was safe (too
    restrictive, never too permissive), which is why nothing noticed.
    """
    from app.modules.identity.models import Organization

    if org_id is None:
        return {}
    org = session.get(Organization, org_id)
    return dict(org.settings or {}) if org else {}


def _owned(session: Session, model: Any, xid: uuid.UUID, actor: Principal,
           action: Action = Action.READ, what: str = "Resource"):
    row = session.scalars(scoped(actor, select(model).where(model.xid == xid), model)).first()
    if row is None:
        raise NotFound(f"{what} not found.")
    if action is not Action.READ:
        policy.require(actor, action,
                       Resource(org_id=row.org_id, owner_user_id=row.owner_user_id,
                                visibility=row.visibility),
                       org_settings=org_settings(session, row.org_id))
    return row


def _page(items: list[dict]) -> dict:
    return {"items": items, "next_cursor": None}


# ── passages ─────────────────────────────────────────────────────────

class PassageCreate(BaseModel):
    title: str
    topic: str | None = None
    tags: list[str] = []
    org_xid: uuid.UUID | None = None
    blocks: list[dict] = []


class PassageVersionUpdate(BaseModel):
    title: str | None = None
    blocks: list[dict] | None = None


def passage_dto(p: Passage, version: PassageVersion | None = None) -> dict:
    return {"xid": str(p.xid), "title": p.title, "skill": p.skill, "topic": p.topic,
            "tags": list(p.tags or []), "visibility": p.visibility,
            "current_version": version_dto(version) if version else None}


def version_dto(v: PassageVersion) -> dict:
    return {"xid": str(v.xid), "version_no": v.version_no, "status": v.status,
            "title": v.title, "blocks": list(v.blocks or []),
            "paragraph_labels": list(v.paragraph_labels or []),
            "word_count": v.word_count}


@router.get("/passages")
def list_passages(q: str | None = None, limit: int = 25,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    query = select(Passage).where(Passage.archived_at.is_(None))
    if q:
        query = query.where(Passage.title.ilike(f"%{q}%"))
    rows = session.scalars(scoped(actor, query, Passage).limit(limit)).all()
    return _page([passage_dto(p) for p in rows])


@router.post("/passages", status_code=status.HTTP_201_CREATED)
def create_passage(body: PassageCreate, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    org_id = _org_for(session, body.org_xid, actor)
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))
    passage = Passage(org_id=org_id, owner_user_id=actor.user_id, title=body.title,
                      topic=body.topic, tags=body.tags)
    session.add(passage)
    session.flush()
    pv = PassageVersion(passage_id=passage.id, title=body.title, blocks=body.blocks,
                        paragraph_labels=_letters(len(body.blocks)),
                        word_count=_count_words(body.blocks), checksum="",
                        created_by=actor.user_id)
    session.add(pv)
    session.flush()
    passage.current_version_id = pv.id
    session.flush()
    return passage_dto(passage, pv)


def _count_words(blocks: list[dict]) -> int:
    total = 0
    for block in blocks or []:
        for run in block.get("runs", []):
            total += len(str(run.get("v", "")).split())
    return total


@router.post("/passages/{xid}/versions", status_code=status.HTTP_201_CREATED)
def new_passage_version(xid: uuid.UUID, actor: Principal = Depends(principal),
                        session: Session = Depends(db)) -> dict:
    passage = _owned(session, Passage, xid, actor, Action.EDIT, "Passage")
    latest = session.scalar(
        select(func.max(PassageVersion.version_no))
        .where(PassageVersion.passage_id == passage.id)) or 0
    source = session.get(PassageVersion, passage.current_version_id)
    pv = PassageVersion(
        passage_id=passage.id, version_no=latest + 1, title=passage.title,
        blocks=list(source.blocks or []) if source else [],
        paragraph_labels=list(source.paragraph_labels or []) if source else [],
        checksum="", created_by=actor.user_id)
    session.add(pv)
    session.flush()
    return version_dto(pv)


def _passage_version(session: Session, xid: uuid.UUID, actor: Principal,
                     action: Action = Action.READ) -> PassageVersion:
    row = session.execute(
        scoped(actor,
               select(PassageVersion, Passage)
               .join(Passage, Passage.id == PassageVersion.passage_id)
               .where(PassageVersion.xid == xid), Passage)).first()
    if row is None:
        raise NotFound("Passage version not found.")
    pv, passage = row
    if action is not Action.READ:
        policy.require(actor, action,
                       Resource(org_id=passage.org_id, owner_user_id=passage.owner_user_id,
                                visibility=passage.visibility, status=pv.status),
                       org_settings=org_settings(session, passage.org_id))
    return pv


@router.get("/passage-versions/{xid}")
def read_passage_version(xid: uuid.UUID, response: Response,
                         actor: Principal = Depends(principal),
                         session: Session = Depends(db)) -> dict:
    pv = _passage_version(session, xid, actor)
    response.headers["ETag"] = f'"{pv.version_no}-{pv.checksum}"'
    return version_dto(pv)


@router.patch("/passage-versions/{xid}")
def update_passage_version(xid: uuid.UUID, body: PassageVersionUpdate,
                           actor: Principal = Depends(principal),
                           session: Session = Depends(db)) -> dict:
    """Paragraph letters are assigned SERVER-SIDE on every save.

    Matching-headings questions reference those letters, so letting the client
    supply them would mean the publish gate validates the client's own claim
    rather than the passage the author is looking at.
    """
    pv = _passage_version(session, xid, actor, Action.EDIT)
    if pv.status == "published":
        raise Conflict("A published passage version is immutable; create a new one.",
                       code="version_immutable")
    if body.title is not None:
        pv.title = body.title
    if body.blocks is not None:
        pv.blocks = body.blocks
        pv.paragraph_labels = _letters(
            sum(1 for b in body.blocks if b.get("type") == "paragraph") or len(body.blocks))
        pv.word_count = _count_words(body.blocks)
    session.flush()
    return version_dto(pv)


@router.post("/passage-versions/{xid}/publish")
def publish_passage_version(xid: uuid.UUID, actor: Principal = Depends(principal),
                            session: Session = Depends(db)) -> dict:
    pv = _passage_version(session, xid, actor, Action.PUBLISH)
    pv.status = "published"
    pv.published_at = dt.datetime.now(dt.UTC)
    pv.published_by = actor.user_id
    session.flush()
    return version_dto(pv)


@router.get("/passage-versions/{xid}/usage")
def passage_usage(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    """Shown BEFORE an author edits a shared asset, so "I changed one passage and
    broke four published mocks" cannot happen by surprise."""
    from app.modules.content.models import TestVersion

    pv = _passage_version(session, xid, actor)
    rows = session.execute(
        select(TestVersion)
        .join(TestVersionSection, TestVersionSection.test_version_id == TestVersion.id)
        .where(TestVersionSection.passage_version_id == pv.id)).scalars().all()
    return _usage(rows)


def _usage(rows) -> dict:
    published = [r for r in rows if r.status == "published"]
    return {
        "published_count": len(published),
        "draft_count": len(rows) - len(published),
        "references": [{"kind": "test_version", "xid": str(r.xid), "title": r.title,
                        "status": r.status} for r in rows],
    }


# ── audio ────────────────────────────────────────────────────────────

class AudioCreate(BaseModel):
    title: str
    accent: str | None = None
    filename: str
    bytes: int
    content_type: str
    checksum_sha256: str | None = None
    attestation: dict


def audio_dto(a: AudioTrack, has_transcript: bool = False) -> dict:
    return {"xid": str(a.xid), "title": a.title, "accent": a.accent,
            "status": a.status, "duration_ms": a.duration_ms,
            "loudness_lufs": float(a.loudness_lufs)
            if a.loudness_lufs is not None else None,
            "has_transcript": has_transcript, "visibility": a.visibility}


@router.get("/audio-tracks")
def list_audio(limit: int = 25, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    query = select(AudioTrack).where(AudioTrack.archived_at.is_(None))
    return _page([audio_dto(a) for a in
                  session.scalars(scoped(actor, query, AudioTrack).limit(limit))])


@router.post("/audio-tracks", status_code=status.HTTP_201_CREATED)
def create_audio(body: AudioCreate, request: Request,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    """Create the track and open a RESUMABLE upload.

    The response carries presigned part URLs. The client PUTs parts straight to
    object storage — the bytes never pass through this process, which on a 4 vCPU
    box shared with the exam endpoints is the difference between one teacher's
    40 MB upload being free and it starving forty students mid-mock.

    A copyright attestation is REQUIRED in this request, captured per upload
    rather than once per account, with the statement's hash and the uploader's
    identity. Assume some centre will upload a published Cambridge paper: the
    evidence has to exist before anyone asks for it.

    `checksum_sha256` is optional and, until now, ignored: the schema has
    declared it since the contract was drafted and this handler read it into a
    field nothing passed on. It is stored with the upload and compared by the
    ingest worker against the bytes that actually arrived.
    """
    from app.modules.content import media as media_service
    from app.platform.storage import storage

    org_id = actor.org_ids[0] if actor.org_ids else None
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))

    now = dt.datetime.now(dt.UTC)
    asset_id, upload = media_service.open_upload(
        session, storage(), kind="audio", filename=body.filename,
        content_type=body.content_type, bytes_=body.bytes,
        owner_user_id=actor.user_id, org_id=org_id,
        attestation=body.attestation, now=now,
        declared_checksum=body.checksum_sha256)

    track = AudioTrack(org_id=org_id, owner_user_id=actor.user_id, title=body.title,
                       accent=body.accent, status="processing",
                       master_media_id=asset_id)
    session.add(track)
    session.flush()
    # Re-recorded against the TRACK as well as the media asset: a takedown is
    # filed against a track, and the evidence has to be reachable from what the
    # claimant names.
    media_service.record_attestation(
        session, subject_type="audio_track", subject_id=track.id,
        user_id=actor.user_id, org_id=org_id, attestation=body.attestation,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"))

    return {"audio_track": audio_dto(track),
            "upload": {"xid": upload.xid, "media_xid": upload.media_xid,
                       "part_size": upload.part_size,
                       "expected_bytes": upload.expected_bytes,
                       "received_bytes": 0, "parts_received": [],
                       "presigned_urls": upload.presigned_urls,
                       "expires_at": iso(upload.expires_at),
                       "status": upload.status}}


@router.get("/audio-tracks/{xid}")
def read_audio(xid: uuid.UUID, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    """Includes transcode status, measured loudness and the failure reason.

    `processing_error` reaching an author matters: "your file was silent, check
    the export" is actionable, "failed" is a support ticket.
    """
    from sqlalchemy import text

    track = _owned(session, AudioTrack, xid, actor, what="Audio track")
    has_transcript = bool(session.scalar(
        text("SELECT count(*) FROM transcripts WHERE audio_track_id = :t")
        .bindparams(t=track.id)))
    dto = audio_dto(track, has_transcript)
    if track.status == "failed" and track.master_media_id:
        dto["processing_error"] = session.scalar(
            text("SELECT processing_error FROM media_assets WHERE id = :m")
            .bindparams(m=track.master_media_id))
    return dto


@router.get("/audio-tracks/{xid}/transcript")
def read_transcript(xid: uuid.UUID, actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    """Authoring only. The transcript is the answer sheet.

    Requires EDIT on the track, not merely read-scope. It used to require only
    the latter, and `scoped()` admits every member of the owning organization —
    so **any student at the centre could read the transcript of any listening
    track it owns**, which is every answer in the paper they are about to sit.

    The live-attempt check below did not stop that: it only fires for an attempt
    already `in_progress`, so the bypass was to read the transcript first and
    start the exam second.

    Students reach transcripts through `GET /attempts/{xid}/review`, which needs
    a scored run and therefore a submitted attempt. (That endpoint does not
    return segments yet — see docs/design/0011-ci.md section 13 — so post-exam
    review of listening audio is currently unimplemented rather than leaky.)
    """
    from sqlalchemy import text

    from app.modules.exam.models import Attempt

    track = _owned(session, AudioTrack, xid, actor, Action.EDIT, "Audio track")
    live = session.scalar(
        select(func.count()).select_from(Attempt)
        .join(TestVersionSection,
              TestVersionSection.test_version_id == Attempt.test_version_id)
        .where(Attempt.user_id == actor.user_id, Attempt.status == "in_progress",
               TestVersionSection.audio_track_id == track.id))
    if live:
        from app.platform.errors import Forbidden
        raise Forbidden("The transcript is not available while you have a live attempt.",
                        code="transcript_locked_during_attempt")
    row = session.execute(text("""
        SELECT language, source, body FROM transcripts WHERE audio_track_id = :t LIMIT 1
    """).bindparams(t=track.id)).mappings().first()
    if row is None:
        raise NotFound("No transcript has been uploaded for this track.")
    return {"language": row["language"], "source": row["source"],
            "segments": row["body"]}


@router.put("/audio-tracks/{xid}/transcript")
def put_transcript(xid: uuid.UUID, body: dict, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    import json

    from sqlalchemy import text

    track = _owned(session, AudioTrack, xid, actor, Action.EDIT, "Audio track")
    language = body.get("language", "en")
    session.execute(text("""
        INSERT INTO transcripts (audio_track_id, language, body, source, created_by)
        VALUES (:t, :lang, CAST(:body AS jsonb), 'uploaded', :by)
        ON CONFLICT (audio_track_id, language)
        DO UPDATE SET body = EXCLUDED.body
    """).bindparams(t=track.id, lang=language,
                    body=json.dumps(body.get("segments", [])),
                    by=actor.user_id))
    return {"language": language, "source": "uploaded",
            "segments": body.get("segments", [])}


# ── questions ────────────────────────────────────────────────────────

class QuestionCreate(BaseModel):
    type_key: str
    type_version: int = 1
    skill: str = "reading"
    payload: dict
    key: dict | None = None
    points: float = 1
    tags: list[str] = []


class QuestionVersionUpdate(BaseModel):
    payload: dict | None = None
    points: float | None = None


def question_dto(q: Question, v: QuestionVersion | None = None) -> dict:
    return {"xid": str(q.xid), "type_key": q.type_key, "skill": q.skill,
            "tags": list(q.tags or []), "visibility": q.visibility,
            "current_version": qv_dto(v) if v else None, "burn_score": None}


def qv_dto(v: QuestionVersion) -> dict:
    return {"xid": str(v.xid), "version_no": v.version_no, "type_key": v.type_key,
            "type_version": v.type_version, "payload": v.payload,
            "slot_keys": list(v.slot_keys or []), "points": float(v.points),
            "status": v.status}


def slots_from(payload: dict) -> list[str]:
    """Extracted server-side. A client that declares its own slots could smuggle
    a mismatch past the publish gate, which compares key slots to this array."""
    import re

    found: set[str] = set()
    for value in payload.values():
        if isinstance(value, str):
            found |= set(re.findall(r"\{\{(s[0-9]+)\}\}", value))
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and "key" in entry:
                    found.add(str(entry["key"]))
                elif isinstance(entry, str):
                    found |= set(re.findall(r"\{\{(s[0-9]+)\}\}", entry))
    return sorted(found) or list(payload.get("slots") or ["s1"])


@router.get("/questions")
def list_questions(q: str | None = None, type_key: str | None = None,
                   skill: str | None = None, limit: int = 25,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    query = select(Question).where(Question.archived_at.is_(None))
    if type_key:
        query = query.where(Question.type_key == type_key)
    if skill:
        query = query.where(Question.skill == skill)
    return _page([question_dto(x) for x in
                  session.scalars(scoped(actor, query, Question).limit(limit))])


@router.post("/questions", status_code=status.HTTP_201_CREATED)
def create_question(body: QuestionCreate, actor: Principal = Depends(principal),
                    session: Session = Depends(db),
                    reg: Registry = Depends(registry)) -> dict:
    from app.platform.errors import ValidationFailed
    from app.platform.findings import Report

    org_id = actor.org_ids[0] if actor.org_ids else None
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))

    report = Report()
    try:
        definition = reg.get(body.type_key, body.type_version)
    except Exception:
        report.add("TYPE_UNKNOWN", f"Unknown question type {body.type_key!r}.",
                   path="type_key", fix_hint="Check the type list.")
        raise ValidationFailed("This question could not be created.",
                               report.errors) from None
    if body.skill not in definition.skills:
        report.add("TYPE_WRONG_SKILL",
                   f"{definition.title} cannot be used for {body.skill}.",
                   path="skill", fix_hint=f"Allowed: {', '.join(definition.skills)}.")
        raise ValidationFailed("This question could not be created.", report.errors)

    question = Question(org_id=org_id, owner_user_id=actor.user_id,
                        type_key=body.type_key, skill=body.skill, tags=body.tags)
    session.add(question)
    session.flush()
    qv = QuestionVersion(question_id=question.id, type_key=body.type_key,
                         type_version=body.type_version, payload=body.payload,
                         slot_keys=slots_from(body.payload), points=body.points,
                         checksum="", created_by=actor.user_id)
    session.add(qv)
    session.flush()
    question.current_version_id = qv.id
    if body.key:
        session.add(AnswerKeyVersion(question_version_id=qv.id, key=body.key,
                                     created_by=actor.user_id))
    session.flush()
    return question_dto(question, qv)


def _question_version(session: Session, xid: uuid.UUID, actor: Principal,
                      action: Action = Action.READ):
    row = session.execute(
        scoped(actor,
               select(QuestionVersion, Question)
               .join(Question, Question.id == QuestionVersion.question_id)
               .where(QuestionVersion.xid == xid), Question)).first()
    if row is None:
        raise NotFound("Question version not found.")
    qv, question = row
    if action is not Action.READ:
        policy.require(actor, action,
                       Resource(org_id=question.org_id,
                                owner_user_id=question.owner_user_id,
                                visibility=question.visibility, status=qv.status),
                       org_settings=org_settings(session, question.org_id))
    return qv, question


@router.get("/question-versions/{xid}")
def read_question_version(xid: uuid.UUID, response: Response,
                          actor: Principal = Depends(principal),
                          session: Session = Depends(db),
                          reg: Registry = Depends(registry)) -> dict:
    qv, _ = _question_version(session, xid, actor)
    key = session.scalars(
        select(AnswerKeyVersion).where(AnswerKeyVersion.question_version_id == qv.id,
                                       AnswerKeyVersion.is_current.is_(True))).first()
    response.headers["ETag"] = f'"{qv.version_no}-{qv.checksum}"'
    return {**qv_dto(qv),
            "current_key": key_dto(key) if key else None,
            "type_def": {"key": qv.type_key, "version": qv.type_version}}


def key_dto(k: AnswerKeyVersion) -> dict:
    return {"xid": str(k.xid), "version_no": k.version_no, "key": k.key,
            "tolerance": k.tolerance, "is_current": k.is_current, "reason": k.reason,
            "note": k.note, "created_at": iso(k.created_at),
            "superseded_at": iso(k.superseded_at)}


@router.patch("/question-versions/{xid}")
def update_question_version(xid: uuid.UUID, body: QuestionVersionUpdate,
                            actor: Principal = Depends(principal),
                            session: Session = Depends(db)) -> dict:
    qv, _ = _question_version(session, xid, actor, Action.EDIT)
    if qv.status == "published":
        raise Conflict("A published question version is immutable; create a new one.",
                       code="version_immutable")
    if body.payload is not None:
        qv.payload = body.payload
        qv.slot_keys = slots_from(body.payload)
    if body.points is not None:
        qv.points = body.points
    session.flush()
    return qv_dto(qv)


@router.get("/question-versions/{xid}/keys")
def list_keys(xid: uuid.UUID, actor: Principal = Depends(principal),
              session: Session = Depends(db)) -> list[dict]:
    """The full history of what this item was ever marked against."""
    qv, _ = _question_version(session, xid, actor)
    return [key_dto(k) for k in session.scalars(
        select(AnswerKeyVersion).where(AnswerKeyVersion.question_version_id == qv.id)
        .order_by(AnswerKeyVersion.version_no.desc()))]


@router.get("/questions/{xid}/usage")
def question_usage(xid: uuid.UUID, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    from app.modules.content.models import TestVersion

    question = _owned(session, Question, xid, actor, what="Question")
    rows = session.scalars(
        select(TestVersion)
        .join(TestVersionSection, TestVersionSection.test_version_id == TestVersion.id)
        .join(TestVersionGroup, TestVersionGroup.section_id == TestVersionSection.id)
        .join(QuestionGroupItem,
              QuestionGroupItem.group_version_id == TestVersionGroup.group_version_id)
        .join(QuestionVersion,
              QuestionVersion.id == QuestionGroupItem.question_version_id)
        .where(QuestionVersion.question_id == question.id).distinct()).all()
    return _usage(rows)


# ── question groups ──────────────────────────────────────────────────

class GroupCreate(BaseModel):
    title: str
    skill: str = "reading"
    instructions: dict = {}
    word_limit: dict | None = None
    option_bank: list[dict] | None = None


class GroupVersionUpdate(BaseModel):
    instructions: dict | None = None
    word_limit: dict | None = None
    option_bank: list[dict] | None = None
    diagram_media_xid: uuid.UUID | None = None
    hotspots: list[dict] | None = None


def group_dto(session: Session, g: QuestionGroup,
              v: QuestionGroupVersion | None = None) -> dict:
    return {"xid": str(g.xid), "title": g.title, "skill": g.skill,
            "visibility": g.visibility,
            "current_version": gv_dto(session, v) if v else None}


def gv_dto(session: Session, v: QuestionGroupVersion) -> dict:
    return {"xid": str(v.xid), "version_no": v.version_no,
            "instructions": v.instructions, "word_limit": v.word_limit,
            "option_bank": list(v.option_bank or []),
            # `diagram_media_id` is on the model and was reported as null, so a
            # labelling or map question could be authored and never rendered —
            # the publish gate checks the diagram's attestation, so the feature
            # is real and only its address was missing.
            "diagram_media_xid": _media_xid(session, v.diagram_media_id),
            "hotspots": list(v.hotspots or []),
            "status": v.status}


def _media_xid(session: Session, media_id: int | None) -> str | None:
    """Raw SQL because media assets have no ORM model in this codebase — every
    other reference to them here is a `text()` too."""
    if media_id is None:
        return None
    xid = session.scalar(text("SELECT xid FROM media_assets WHERE id = :m")
                         .bindparams(m=media_id))
    return str(xid) if xid else None


def _org_for(session: Session, org_xid: uuid.UUID | None, actor: Principal) -> int | None:
    """Which centre this asset belongs to.

    Lives here rather than in `tests_authoring`, which imports from this module —
    both need it, and a teacher who teaches at two centres has to be able to say
    which one a passage is for. `create_passage` used to take `actor.org_ids[0]`
    and ignore the `org_xid` the client sent.
    """
    from app.modules.identity.models import Organization

    if org_xid is None:
        return actor.org_ids[0] if actor.org_ids else None
    org_id = session.scalar(select(Organization.id).where(Organization.xid == org_xid))
    if org_id is None or (org_id not in actor.org_ids and not actor.is_platform_admin):
        raise NotFound("Organization not found.")
    return org_id


@router.get("/question-groups")
def list_groups(limit: int = 25, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    query = select(QuestionGroup).where(QuestionGroup.archived_at.is_(None))
    return _page([group_dto(session, g) for g in
                  session.scalars(scoped(actor, query, QuestionGroup).limit(limit))])


@router.post("/question-groups", status_code=status.HTTP_201_CREATED)
def create_group(body: GroupCreate, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    org_id = actor.org_ids[0] if actor.org_ids else None
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))
    group = QuestionGroup(org_id=org_id, owner_user_id=actor.user_id,
                          title=body.title, skill=body.skill)
    session.add(group)
    session.flush()
    gv = QuestionGroupVersion(group_id=group.id, instructions=body.instructions,
                              word_limit=body.word_limit, option_bank=body.option_bank,
                              checksum="", created_by=actor.user_id)
    session.add(gv)
    session.flush()
    group.current_version_id = gv.id
    session.flush()
    return group_dto(session, group, gv)


def _group_version(session: Session, xid: uuid.UUID, actor: Principal,
                   action: Action = Action.READ):
    row = session.execute(
        scoped(actor,
               select(QuestionGroupVersion, QuestionGroup)
               .join(QuestionGroup, QuestionGroup.id == QuestionGroupVersion.group_id)
               .where(QuestionGroupVersion.xid == xid), QuestionGroup)).first()
    if row is None:
        raise NotFound("Question group version not found.")
    gv, group = row
    if action is not Action.READ:
        policy.require(actor, action,
                       Resource(org_id=group.org_id, owner_user_id=group.owner_user_id,
                                visibility=group.visibility, status=gv.status),
                       org_settings=org_settings(session, group.org_id))
    return gv, group


@router.get("/question-group-versions/{xid}")
def read_group_version(xid: uuid.UUID, response: Response,
                       actor: Principal = Depends(principal),
                       session: Session = Depends(db)) -> dict:
    gv, _ = _group_version(session, xid, actor)
    items = session.execute(
        select(QuestionGroupItem, QuestionVersion)
        .join(QuestionVersion, QuestionVersion.id == QuestionGroupItem.question_version_id)
        .where(QuestionGroupItem.group_version_id == gv.id)
        .order_by(QuestionGroupItem.position)).all()
    response.headers["ETag"] = f'"{gv.version_no}-{gv.checksum}"'
    return {**gv_dto(session, gv),
            "items": [{"xid": str(qv.xid), "position": item.position,
                       "question_version": qv_dto(qv)} for item, qv in items]}


@router.patch("/question-group-versions/{xid}")
def update_group_version(xid: uuid.UUID, body: GroupVersionUpdate,
                         actor: Principal = Depends(principal),
                         session: Session = Depends(db)) -> dict:
    gv, _ = _group_version(session, xid, actor, Action.EDIT)
    if gv.status == "published":
        raise Conflict("A published group version is immutable; create a new one.",
                       code="version_immutable")
    data = body.model_dump(exclude_none=True)
    # `diagram_media_xid` used to be popped and discarded — accepted from the
    # client, dropped on the floor, and reported back as null. A labelling or map
    # question could be authored and never rendered.
    if data.pop("diagram_media_xid", None) is not None:
        gv.diagram_media_id = _own_media(session, body.diagram_media_xid, actor)
    for field, value in data.items():
        setattr(gv, field, value)
    session.flush()
    return gv_dto(session, gv)


def _own_media(session: Session, xid: uuid.UUID, actor: Principal) -> int:
    """Resolved through the actor's own uploads.

    Same reasoning as `_passage_ref` in composition: a reference by xid is how a
    competitor's asset would be smuggled into your content, so it is authorized on
    the way IN rather than hoped about later.
    """
    media_id = session.scalar(text("""
        SELECT id FROM media_assets
        WHERE xid = CAST(:x AS uuid) AND owner_user_id = :u AND status <> 'removed'
    """).bindparams(x=xid, u=actor.user_id))
    if media_id is None:
        raise NotFound("Media asset not found.")
    return media_id


@router.post("/question-group-versions/{xid}/items",
             status_code=status.HTTP_201_CREATED)
def add_group_item(xid: uuid.UUID, body: dict,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    """Reuse: pass the xid of an existing question version to pull a bank item
    in. Nothing is copied."""
    gv, _ = _group_version(session, xid, actor, Action.EDIT)
    qv, _q = _question_version(session, uuid.UUID(str(body["question_version_xid"])), actor)
    position = body.get("position") or (session.scalar(
        select(func.max(QuestionGroupItem.position))
        .where(QuestionGroupItem.group_version_id == gv.id)) or 0) + 1
    item = QuestionGroupItem(group_version_id=gv.id, question_version_id=qv.id,
                             position=position)
    session.add(item)
    session.flush()
    return {"xid": str(qv.xid), "position": item.position,
            "question_version": qv_dto(qv)}


# ── band maps and cue cards ──────────────────────────────────────────

class BandMapCreate(BaseModel):
    name: str
    skill: str = "reading"
    variant: str = "academic"
    max_raw: int
    mapping: list[dict]


@router.get("/band-maps")
def list_band_maps(actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> list[dict]:
    """Platform defaults (`org_id IS NULL`) plus the actor's own org maps."""
    query = select(BandMap).where(
        BandMap.org_id.is_(None) | BandMap.org_id.in_(actor.org_ids or [0]))
    out = []
    for bm in session.scalars(query):
        current = session.scalars(
            select(BandMapVersion).where(BandMapVersion.band_map_id == bm.id)
            .order_by(BandMapVersion.version_no.desc()).limit(1)).first()
        out.append({"xid": str(bm.xid), "name": bm.name, "skill": bm.skill,
                    "variant": bm.variant, "is_platform_default": bm.org_id is None,
                    # The version's own xid, not `str(id)`. Emitting the
                    # primary key here put an internal id on the public surface
                    # AND handed clients a value that
                    # `PATCH /test-versions/{xid}` rejects as not-a-uuid.
                    "current_version": ({"xid": str(current.xid),
                                         "version_no": current.version_no,
                                         "max_raw": current.max_raw,
                                         "mapping": current.mapping}
                                        if current else None)})
    return out


@router.post("/band-maps", status_code=status.HTTP_201_CREATED)
def create_band_map(body: BandMapCreate, actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    """A centre may override the platform curve for its own cohort. Every score
    records which band-map version produced it, so retuning never silently
    rewrites history."""
    org_id = actor.org_ids[0] if actor.org_ids else None
    policy.require(actor, Action.MANAGE_BAND_MAP, Resource(org_id=org_id))
    bm = BandMap(org_id=org_id, name=body.name, skill=body.skill,
                 variant=body.variant, created_by=actor.user_id)
    session.add(bm)
    session.flush()
    bmv = BandMapVersion(band_map_id=bm.id, mapping=body.mapping,
                         max_raw=body.max_raw, status="published",
                         created_by=actor.user_id)
    session.add(bmv)
    session.flush()
    bm.current_version_id = bmv.id
    session.flush()
    return {"xid": str(bm.xid), "name": bm.name, "skill": bm.skill,
            "variant": bm.variant, "is_platform_default": False,
            "current_version": {"xid": str(bmv.xid), "version_no": bmv.version_no,
                                "max_raw": bmv.max_raw, "mapping": bmv.mapping}}


@router.get("/cue-card-sets")
def list_cue_cards(actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> list[dict]:
    from sqlalchemy import text

    rows = session.execute(text("""
        SELECT s.xid, s.title, s.tags, s.visibility,
               (SELECT v.xid FROM cue_card_set_versions v
                WHERE v.set_id = s.id ORDER BY v.version_no DESC LIMIT 1)
                   AS current_version_xid
        FROM cue_card_sets s
        WHERE s.archived_at IS NULL
          AND (s.visibility = 'platform_global'
               OR s.org_id = ANY(:orgs)
               OR (s.owner_user_id = :uid AND s.visibility = 'author_private'))
    """).bindparams(orgs=list(actor.org_ids) or [0], uid=actor.user_id)).mappings().all()
    # `current_version_xid` was null here too, so a teacher could list the cue-card
    # sets and not address the version they needed to attach to a speaking slot.
    return [{"xid": str(r["xid"]), "title": r["title"], "tags": list(r["tags"] or []),
             "visibility": r["visibility"],
             "current_version_xid": str(r["current_version_xid"])
             if r["current_version_xid"] else None} for r in rows]


@router.post("/cue-card-sets", status_code=status.HTTP_201_CREATED)
def create_cue_cards(body: dict, actor: Principal = Depends(principal),
                     session: Session = Depends(db)) -> dict:
    """Speaking prompts are authored with the same versioning and visibility as
    every other content asset — that is why module 5 has no content model."""
    import json

    from sqlalchemy import text

    org_id = actor.org_ids[0] if actor.org_ids else None
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))
    set_id = session.execute(text("""
        INSERT INTO cue_card_sets (org_id, owner_user_id, title, tags)
        VALUES (:org, :uid, :title, :tags) RETURNING id, xid
    """).bindparams(org=org_id, uid=actor.user_id, title=body.get("title", ""),
                    tags=body.get("tags", []))).mappings().one()
    version_xid = session.scalar(text("""
        INSERT INTO cue_card_set_versions (set_id, version_no, body, created_by)
        VALUES (:sid, 1, CAST(:body AS jsonb), :uid)
        RETURNING xid
    """).bindparams(sid=set_id["id"], body=json.dumps(body.get("body", {})),
                    uid=actor.user_id))
    return {"xid": str(set_id["xid"]), "title": body.get("title", ""),
            "tags": body.get("tags", []), "visibility": "org_private",
            # The version was created two statements ago and its xid was thrown
            # away, so the caller could not address the thing it had just made.
            "current_version_xid": str(version_xid)}
