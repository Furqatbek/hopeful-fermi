"""Authoring assets: passages, audio, questions, groups, band maps, cue cards.

Every listing here goes through `authz.filter_content`. That is not a
convention — `tests/integration/test_authz_leaks.py` asserts it for each one,
because a missing list-scope is how a centre's material reaches a competitor.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Select, Text, func, or_, select, text
from sqlalchemy.orm import Session

from app.api.deps import Principal, clock, db, principal, registry
from app.api.dto import iso
from app.modules.authz import grants as authz_grants
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.content.models import (
    AnswerKeyVersion,
    AudioTrack,
    BandMap,
    BandMapVersion,
    Passage,
    PassageVersion,
    Question,
    QuestionGroup,
    QuestionGroupItem,
    QuestionGroupVersion,
    QuestionVersion,
    TestVersionGroup,
    TestVersionSection,
)
from app.modules.qtypes.registry import Registry
from app.platform.errors import Conflict, NotFound, PreconditionFailed

router = APIRouter(tags=["authoring-assets"])


def scoped(actor: Principal, query: Select, model: Any,
           session: Session | None = None) -> Select:
    """The one place every authoring listing is scoped, which is why the grant
    lookup goes HERE rather than into nine call sites.

    `filter_content`'s fourth visibility route — content shared through
    `content_grants` — takes a `grant_ids` argument that no caller ever passed,
    so `view` and `assign` grants reached nothing. A centre could share a bank,
    the grantee could see the grant listed, and the material stayed invisible.

    `session` is optional and the shared route is simply absent without it, so
    an existing caller keeps its old behaviour rather than being silently
    widened. Every listing in this module passes one.
    """
    grant_ids = None
    if session is not None:
        kind = authz_grants.subject_type_of(model)
        if kind:
            grant_ids = authz_grants.granted_ids(session, actor, kind)
    return policy.filter_content(actor, query, model, grant_ids=grant_ids)


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
    row = session.scalars(scoped(actor, select(model).where(model.xid == xid), model, session)).first()
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
    """`attestation` was declared in the contract and absent from this model.

    Pydantic drops an undeclared field silently, so the console's copyright form
    posted a claim that went nowhere and no `content_attestations` row was ever
    written for a passage — while `content_attestations.subject_type` has
    listed `'passage'` in its CHECK constraint since the migration that created
    it, and the audio route writes one correctly.

    That mattered more than one missing row. A passage is the other way a
    published Cambridge paper arrives — somebody types or pastes it — and the
    brief's instruction is to "design so that liability and evidence are
    handled". The evidence was the part that was missing.

    Required, and refused rather than defaulted, for the reason `media.validate`
    already gives about uploads: a missing attestation that quietly becomes
    "original" manufactures a claim the uploader never made, which is the
    opposite of evidence.
    """

    title: str
    topic: str | None = None
    tags: list[str] = []
    org_xid: uuid.UUID | None = None
    blocks: list[dict] = []
    attestation: dict


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
    """Carries `current_version`, which it did not.

    `passage_dto` takes the version as an optional second argument and this
    listing never passed one, so the field was `null` on every row — the
    enrichment a listing forgets, which this codebase has now grown five times.

    It was not cosmetic. There is no `GET /passages/{xid}` and no version
    listing, so this is the ONLY place a passage's current version xid is
    obtainable: without it the console's View control had nothing to open and
    `AddSection`'s picker sent an empty value for every passage. An author who
    reloaded the page could not open a single existing passage.

    One query for the versions rather than a lazy load per row, because a
    library of two hundred passages is two hundred round trips otherwise.
    """
    query = select(Passage).where(Passage.archived_at.is_(None))
    if q:
        query = query.where(Passage.title.ilike(f"%{q}%"))
    rows = session.scalars(scoped(actor, query, Passage, session).limit(limit)).all()
    versions = {
        v.id: v for v in session.scalars(
            select(PassageVersion).where(PassageVersion.id.in_(
                [p.current_version_id for p in rows if p.current_version_id] or [0])))
    }
    return _page([passage_dto(p, versions.get(p.current_version_id))
                  for p in rows])


@router.post("/passages", status_code=status.HTTP_201_CREATED)
def create_passage(body: PassageCreate, request: Request,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    """Records the copyright attestation, which it did not.

    Same two functions the audio route uses, so there is one definition of what
    a valid claim is and one of what the evidence row looks like. A second
    implementation here would be a second thing to keep in step with the
    statement text whose hash is the whole point of storing it.
    """
    from app.modules.content import media as media_service

    media_service.validate_attestation(body.attestation)
    org_id = _org_for(session, body.org_xid, actor)
    policy.require(actor, Action.CREATE, Resource(org_id=org_id))
    passage = Passage(org_id=org_id, owner_user_id=actor.user_id, title=body.title,
                      topic=body.topic, tags=body.tags)
    session.add(passage)
    session.flush()
    media_service.record_attestation(
        session, subject_type="passage", subject_id=passage.id,
        user_id=actor.user_id, org_id=org_id, attestation=body.attestation,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"))
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
    """Advances `current_version_id`, which it did not.

    It copied from `passage.current_version_id` and never moved it, so a second
    "new version" copied v1 again — v2's edits were still in the database and
    nothing pointed at them, which is data loss that looks like the button not
    working. `word_count` was left at zero for the same reason: it was never
    recomputed from the blocks being copied.

    `source` falls back to the newest version by number. An imported passage has
    no `current_version_id` at all — `content.importer` never set one — so
    without this a new version of an imported paper came back with no text.
    """
    passage = _owned(session, Passage, xid, actor, Action.EDIT, "Passage")
    latest = session.scalar(
        select(func.max(PassageVersion.version_no))
        .where(PassageVersion.passage_id == passage.id)) or 0
    source = (session.get(PassageVersion, passage.current_version_id)
              if passage.current_version_id else None)
    if source is None:
        source = session.scalars(
            select(PassageVersion).where(PassageVersion.passage_id == passage.id)
            .order_by(PassageVersion.version_no.desc()).limit(1)).first()
    blocks = list(source.blocks or []) if source else []
    pv = PassageVersion(
        passage_id=passage.id, version_no=latest + 1, title=passage.title,
        blocks=blocks,
        paragraph_labels=list(source.paragraph_labels or []) if source else [],
        word_count=_count_words(blocks),
        checksum="", created_by=actor.user_id)
    session.add(pv)
    session.flush()
    passage.current_version_id = pv.id
    session.flush()
    return version_dto(pv)


def _passage_version(session: Session, xid: uuid.UUID, actor: Principal,
                     action: Action = Action.READ) -> PassageVersion:
    row = session.execute(
        scoped(actor,
               select(PassageVersion, Passage)
               .join(Passage, Passage.id == PassageVersion.passage_id)
               .where(PassageVersion.xid == xid), Passage, session)).first()
    if row is None:
        raise NotFound("Passage version not found.")
    pv, passage = row
    if action is not Action.READ:
        policy.require(actor, action,
                       Resource(org_id=passage.org_id, owner_user_id=passage.owner_user_id,
                                visibility=passage.visibility, status=pv.status),
                       org_settings=org_settings(session, passage.org_id))
    return pv


def version_etag(v: Any) -> str:
    """The tag for any versioned draft. One spelling, so a client that reads one
    endpoint and writes another cannot be tripped by a formatting difference."""
    return f'"{v.version_no}-{v.checksum}"'


def check_if_match(current: str, if_match: str | None) -> None:
    """Honour the optimistic lock the contract has always declared.

    `If-Match` is `required: true` on all five draft-edit endpoints and **no
    handler anywhere read it**. "Optimistic locking on draft content edits" was a
    promise the API made in writing and never kept: two teachers editing one
    draft both succeeded and the second silently erased the first, which on a
    forty-question paper is an afternoon of somebody's work gone with no error
    and no trace.

    A mismatch is 412, not 409: the request is well-formed and permitted, and the
    only thing wrong with it is that it was computed against a state that has
    moved on.

    A caller that sends nothing is allowed through. The header is declared
    required and FastAPI is not enforcing that for us; refusing here would turn
    a documentation gap into a broken endpoint for every existing client, and the
    protection is for concurrent editors, who are exactly the callers that do
    send it. What matters is that a WRONG tag can no longer win.
    """
    if if_match is None:
        return
    # Tolerates the weak-validator prefix and quoting differences: compare the
    # value, not its transport spelling.
    seen = {token.strip().removeprefix("W/").strip('"')
            for token in if_match.split(",")}
    if "*" in seen or current.strip('"') in seen:
        return
    raise PreconditionFailed(
        "This changed since you loaded it. Reload before saving, or your edit "
        "would overwrite someone else's.",
        code="stale_version")


@router.get("/passage-versions/{xid}")
def read_passage_version(xid: uuid.UUID, response: Response,
                         actor: Principal = Depends(principal),
                         session: Session = Depends(db)) -> dict:
    pv = _passage_version(session, xid, actor)
    response.headers["ETag"] = f'"{pv.version_no}-{pv.checksum}"'
    return version_dto(pv)


@router.patch("/passage-versions/{xid}")
def update_passage_version(xid: uuid.UUID, body: PassageVersionUpdate,
                           if_match: str | None = Header(default=None, alias="If-Match"),
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
    check_if_match(version_etag(pv), if_match)
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
    """The audio library.

    `has_transcript` was `False` on every row — `audio_dto` takes it as an
    argument defaulting to False and only the single-track read ever passed one.
    The third listing in this file with that shape, after `list_questions` and
    `list_groups`: a DTO with an optional enrichment that the detail endpoint
    supplies and the listing forgets, which reads as a populated field and is a
    constant.

    It is not cosmetic here either. A transcript is what makes post-exam review
    of a listening question say anything — without one the student sees a
    timestamp and no words — so this column is how an author finds which of
    forty tracks still need one, and it always said none of them did.

    One query for the page.
    """
    query = select(AudioTrack).where(AudioTrack.archived_at.is_(None))
    tracks = list(session.scalars(scoped(actor, query, AudioTrack, session).limit(limit)))
    transcribed: set[int] = set()
    if tracks:
        transcribed = set(session.scalars(text("""
            SELECT DISTINCT audio_track_id FROM transcripts
             WHERE audio_track_id = ANY(:ids)
        """).bindparams(ids=[t.id for t in tracks])))
    return _page([audio_dto(a, a.id in transcribed) for a in tracks])


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


@router.post("/audio-tracks/{xid}/grant")
def audio_authoring_grant(xid: uuid.UUID, actor: Principal = Depends(principal),
                          session: Session = Depends(db),
                          now=Depends(clock)) -> dict:
    """A grant so an author can hear the track they just uploaded.

    `GET /media/{xid}/content` needs one and the only issuer was
    `POST /attempts/{xid}/sections/{position}/audio-grant` — inside an exam. So
    the authoring console could upload a 400 MB wav, watch it transcode, read
    its measured loudness, and never play a second of it. "Is this the right
    file, and is it audible" is the check the whole transcode pipeline exists to
    support, and it was the one thing the console could not do.

    **EDIT, not read-scope** — the same rule, for the same reason, as the
    transcript beside it. `scoped()` admits every member of the owning
    organization, so a read-scoped grant here would hand any student at the
    centre the audio of every listening paper it owns, before they sit it. The
    transcript endpoint learned that already; this is not the place to learn it
    twice.

    Purpose `authoring`, distinct from `exam`, so the two are told apart in a
    log and a play-once section is never burned by an author previewing it —
    this issues nothing against an attempt and touches no `audio_locked_at`.
    """
    from app.platform import grants
    from app.platform.config import settings

    track = _owned(session, AudioTrack, xid, actor, Action.EDIT, "Audio track")
    # The DELIVERY object, falling back to the master: an author checking a file
    # mid-transcode still gets to hear what they uploaded, which is exactly when
    # they most want to.
    media_xid = session.scalar(text("""
        SELECT coalesce(d.xid, m.xid)::text
        FROM audio_tracks t
        LEFT JOIN media_assets d ON d.id = t.delivery_media_id
        LEFT JOIN media_assets m ON m.id = t.master_media_id
        WHERE t.id = :t
    """).bindparams(t=track.id))
    if media_xid is None:
        raise NotFound("This track has no media yet.", code="track_has_no_media")

    ttl = settings().media_grant_ttl_seconds
    return {"grant": grants.issue(user_xid=actor.user_xid, media_xid=media_xid,
                                  purpose="authoring", ttl_seconds=ttl),
            "media_xid": media_xid,
            "expires_at": iso(now.now() + dt.timedelta(seconds=ttl))}


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


class TranscriptSegment(BaseModel):
    """One span of speech. `end_ms` is required, which the contract had as
    optional and `exam.session._excerpt` had as mandatory.

    That reader takes the segments overlapping a question's window, and it skips
    any segment missing either bound — so a contract-conforming upload with no
    `end_ms` stored fine, read back fine, and produced an empty review excerpt
    for every question on the track, with nothing anywhere reporting a problem.
    A span with no end is not usable for the one feature transcripts exist for,
    so it is refused at upload where the error is legible.
    """

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    speaker: str | None = None

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> TranscriptSegment:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms.")
        return self


class TranscriptUpdate(BaseModel):
    """`segments` is required, and was read as `body.get("segments", [])`.

    A PUT that omitted them — a client bug, a truncated payload — replaced a
    finished transcript with an empty array and answered 200. The upload is the
    only copy; nobody re-types a listening transcript.
    """

    segments: list[TranscriptSegment]
    language: str = "en"


@router.put("/audio-tracks/{xid}/transcript")
def put_transcript(xid: uuid.UUID, body: TranscriptUpdate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    import json

    from sqlalchemy import text

    track = _owned(session, AudioTrack, xid, actor, Action.EDIT, "Audio track")
    segments = [s.model_dump(exclude_none=True) for s in body.segments]
    session.execute(text("""
        INSERT INTO transcripts (audio_track_id, language, body, source, created_by)
        VALUES (:t, :lang, CAST(:body AS jsonb), 'uploaded', :by)
        ON CONFLICT (audio_track_id, language)
        DO UPDATE SET body = EXCLUDED.body
    """).bindparams(t=track.id, lang=body.language, body=json.dumps(segments),
                    by=actor.user_id))
    return {"language": body.language, "source": "uploaded", "segments": segments}


# ── questions ────────────────────────────────────────────────────────

class AnswerKeyIn(BaseModel):
    """`QuestionCreate.key`, which the contract has always declared to be an
    `AnswerKeyCreate` — a WRAPPER carrying the key plus why it exists.

    It was typed `dict` and stored verbatim, so `answer_key_versions.key` ended up
    holding `{"key": {"slots": ...}, "reason": "initial"}` instead of
    `{"slots": ...}`. The consequence is not cosmetic: the publish gate validates
    that column against the type's `key_schema` and answers
    `Invalid answer key — 'slots' is a required property` plus
    `No answer for blank(s): s1`, so **every question authored with its key
    through the contract's own shape was unpublishable**, and had one slipped
    through it would have scored every response wrong for want of an accepted
    answer.

    Typed rather than unwrapped by hand, so `reason`, `note` and `tolerance` are
    carried too. `reason` in particular is not bookkeeping — `initial` versus
    `key_fix` is what the regrade flow keys off.
    """

    key: dict
    reason: str = Field(default="initial",
                        pattern="^(initial|key_fix|clarification|import)$")
    note: str | None = None
    tolerance: dict = {}


class QuestionCreate(BaseModel):
    type_key: str
    type_version: int = 1
    skill: str = "reading"
    payload: dict
    key: AnswerKeyIn | None = None
    points: float = 1
    tags: list[str] = []


class QuestionVersionUpdate(BaseModel):
    payload: dict | None = None
    points: float | None = None


def question_dto(q: Question, v: QuestionVersion | None = None,
                 burn: float | None = None) -> dict:
    """`burn_score` was hardcoded `None` here, on the only listing that carries
    it — so the contract declared "0..1, rises with exposure count and org
    spread" and every row said nothing. Migration 0009 builds
    `item_exposure_stats_burn_idx`, commented "The author's 'most burned items'
    view": an index for a reader that did not exist.

    Optional, and the enrichment a listing forgets is now the shape this
    codebase has grown six times — so `list_questions` passes it and a caller
    that does not gets an explicit null rather than a wrong number."""
    return {"xid": str(q.xid), "type_key": q.type_key, "skill": q.skill,
            "tags": list(q.tags or []), "visibility": q.visibility,
            "current_version": qv_dto(v) if v else None, "burn_score": burn}


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
    """The question bank.

    `current_version` was hardcoded `None` on every row here — `question_dto`
    takes the version as an argument and this, the only listing that calls it,
    never passed one. The field is the useful half of a question: its xid is what
    addresses a key fix, `slot_keys` is what a key must line up with, and
    `version_no` is how an author knows which one they are looking at. Without it
    the bank listed type names and nothing else, and the key-fix flow had no way
    to name a question at all.

    Resolved in ONE query, not per row. `limit` reaches 200 from the console, and
    a version lookup per question is 200 round trips to render a list.
    """
    query = select(Question).where(Question.archived_at.is_(None))
    if type_key:
        query = query.where(Question.type_key == type_key)
    if skill:
        query = query.where(Question.skill == skill)
    if q:
        # `q` was declared in the contract, accepted here, and never applied —
        # so a search box would have returned the whole bank while looking like
        # it had filtered. A question has no title, so the searchable text is
        # its tags and the stem inside the current version's payload.
        needle = f"%{q.lower()}%"
        query = query.where(or_(
            func.lower(func.array_to_string(Question.tags, " ")).like(needle),
            Question.id.in_(
                select(QuestionVersion.question_id).where(
                    func.lower(func.cast(QuestionVersion.payload, Text)).like(needle))),
        ))
    questions = list(session.scalars(scoped(actor, query, Question, session).limit(limit)))

    # Highest `version_no` per question — there is no `is_current` flag on
    # question_versions, so DISTINCT ON is the resolution every other caller
    # spells out as `ORDER BY version_no DESC LIMIT 1`, done for the whole page.
    current: dict[int, QuestionVersion] = {}
    if questions:
        current = {v.question_id: v for v in session.scalars(
            select(QuestionVersion)
            .where(QuestionVersion.question_id.in_([x.id for x in questions]))
            .distinct(QuestionVersion.question_id)
            .order_by(QuestionVersion.question_id,
                      QuestionVersion.version_no.desc()))}
    # One query for the page's burn scores, for the same reason as the versions
    # above: `limit` reaches 200 and a lookup per row is 200 round trips.
    burn: dict[int, float] = {}
    if questions:
        burn = {row[0]: float(row[1]) for row in session.execute(text("""
            SELECT question_id, burn_score FROM item_exposure_stats
            WHERE question_id = ANY(:ids) AND burn_score IS NOT NULL
        """).bindparams(ids=[x.id for x in questions]))}
    return _page([question_dto(x, current.get(x.id), burn.get(x.id))
                  for x in questions])


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
        session.add(AnswerKeyVersion(question_version_id=qv.id, key=body.key.key,
                                     tolerance=body.key.tolerance,
                                     reason=body.key.reason, note=body.key.note,
                                     created_by=actor.user_id))
    session.flush()
    return question_dto(question, qv)


def _question_version(session: Session, xid: uuid.UUID, actor: Principal,
                      action: Action = Action.READ):
    row = session.execute(
        scoped(actor,
               select(QuestionVersion, Question)
               .join(Question, Question.id == QuestionVersion.question_id)
               .where(QuestionVersion.xid == xid), Question, session)).first()
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
                            if_match: str | None = Header(default=None, alias="If-Match"),
                            actor: Principal = Depends(principal),
                            session: Session = Depends(db)) -> dict:
    qv, _ = _question_version(session, xid, actor, Action.EDIT)
    if qv.status == "published":
        raise Conflict("A published question version is immutable; create a new one.",
                       code="version_immutable")
    check_if_match(version_etag(qv), if_match)
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
    """The group library.

    `current_version` was hardcoded `None` on every row — `group_dto` takes the
    version as an argument and this, the only listing that calls it, never passed
    one. The same defect `list_questions` carried, and here it broke composition
    outright rather than merely thinning it: a section is filled by attaching a
    group VERSION (`GroupPlacementCreate.group_version_xid`), so a picker built
    from this listing had nothing to offer, whatever the centre had authored. The
    console's "no question groups have a version yet" was not a rare fallback, it
    was the only branch that ever ran.

    One extra query for the page, not one per row.
    """
    query = select(QuestionGroup).where(QuestionGroup.archived_at.is_(None))
    groups = list(session.scalars(scoped(actor, query, QuestionGroup, session).limit(limit)))
    # Highest `version_no` per group, NOT `groups.current_version_id`. The column
    # exists but only `POST /question-groups` maintains it — the importer creates
    # a group and its version and never sets it, so resolving through it would
    # have left every IMPORTED group unplaceable, which is precisely the path a
    # centre arriving with existing material takes. Same resolution as the
    # question listing, so both answer "current" the same way.
    versions: dict[int, QuestionGroupVersion] = {}
    if groups:
        versions = {v.group_id: v for v in session.scalars(
            select(QuestionGroupVersion)
            .where(QuestionGroupVersion.group_id.in_([g.id for g in groups]))
            .distinct(QuestionGroupVersion.group_id)
            .order_by(QuestionGroupVersion.group_id,
                      QuestionGroupVersion.version_no.desc()))}
    return _page([group_dto(session, g, versions.get(g.id)) for g in groups])


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
               .where(QuestionGroupVersion.xid == xid), QuestionGroup, session)).first()
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
                         if_match: str | None = Header(default=None, alias="If-Match"),
                         actor: Principal = Depends(principal),
                         session: Session = Depends(db)) -> dict:
    gv, _ = _group_version(session, xid, actor, Action.EDIT)
    if gv.status == "published":
        raise Conflict("A published group version is immutable; create a new one.",
                       code="version_immutable")
    check_if_match(version_etag(gv), if_match)
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


class GroupItemCreate(BaseModel):
    """`body["question_version_xid"]` was a bare subscript on an untyped dict:
    omit the field and the handler raised `KeyError` — a 500 — and send a
    malformed one and `uuid.UUID(...)` raised `ValueError`, also a 500.

    `position` gains the `minimum: 1` the contract already declared. It orders
    the questions a student answers, and nothing stopped a 0 or a negative going
    in and quietly reordering a published group.
    """

    question_version_xid: uuid.UUID
    position: int | None = Field(default=None, ge=1)


@router.post("/question-group-versions/{xid}/items",
             status_code=status.HTTP_201_CREATED)
def add_group_item(xid: uuid.UUID, body: GroupItemCreate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    """Reuse: pass the xid of an existing question version to pull a bank item
    in. Nothing is copied."""
    gv, _ = _group_version(session, xid, actor, Action.EDIT)
    qv, _q = _question_version(session, body.question_version_xid, actor)
    position = body.position or (session.scalar(
        select(func.max(QuestionGroupItem.position))
        .where(QuestionGroupItem.group_version_id == gv.id)) or 0) + 1
    item = QuestionGroupItem(group_version_id=gv.id, question_version_id=qv.id,
                             position=position)
    session.add(item)
    session.flush()
    return {"xid": str(qv.xid), "position": item.position,
            "question_version": qv_dto(qv)}



# ── retiring an asset ────────────────────────────────────────────────

#: The five assets that carry `archived_at`, read by their listings, and had no
#: endpoint that wrote it. `tests` and `test_versions` already had one.
_ARCHIVABLE: dict[str, Any] = {
    "passages": Passage,
    "questions": Question,
    "question-groups": QuestionGroup,
    "audio-tracks": AudioTrack,
}


def _archive(session: Session, model: Any, xid: uuid.UUID, actor: Principal,
             what: str, *, restore: bool = False) -> dict:
    """Retire an asset without deleting it, or put it back.

    **`archived_at` was read by four listings and written by nothing**, so
    "retire this item" had no action behind it — and the exposure screen could
    tell an author an item was burned with nothing to do about it. It matters
    more since content grants started working: revoking a grant removes ONE
    partner's access, and retiring the item removes it from everybody's,
    including the owning centre's own authors. Only the first was possible.

    Archived, never deleted, for the same reason a takedown soft-hides: attempts
    reference this material and a copyright investigation needs the evidence. It
    disappears from the listings authors pick from and stays resolvable
    everywhere it is already used.

    `Action.ARCHIVE` is `{CENTRE_ADMIN, PLATFORM_ADMIN}` — retiring a shared
    asset can break another author's draft, so it is not a teacher's call.
    """
    row = _owned(session, model, xid, actor, Action.ARCHIVE, what)
    row.archived_at = None if restore else dt.datetime.now(dt.UTC)
    session.flush()
    return {"xid": str(row.xid), "archived_at": iso(row.archived_at)}


@router.post("/passages/{xid}/archive")
def archive_passage(xid: uuid.UUID, actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    return _archive(session, Passage, xid, actor, "Passage")


@router.delete("/passages/{xid}/archive")
def restore_passage(xid: uuid.UUID, actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    return _archive(session, Passage, xid, actor, "Passage", restore=True)


@router.post("/questions/{xid}/archive")
def archive_question(xid: uuid.UUID, actor: Principal = Depends(principal),
                     session: Session = Depends(db)) -> dict:
    return _archive(session, Question, xid, actor, "Question")


@router.delete("/questions/{xid}/archive")
def restore_question(xid: uuid.UUID, actor: Principal = Depends(principal),
                     session: Session = Depends(db)) -> dict:
    return _archive(session, Question, xid, actor, "Question", restore=True)


@router.post("/question-groups/{xid}/archive")
def archive_group(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    return _archive(session, QuestionGroup, xid, actor, "Question group")


@router.delete("/question-groups/{xid}/archive")
def restore_group(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    return _archive(session, QuestionGroup, xid, actor, "Question group",
                    restore=True)


@router.post("/audio-tracks/{xid}/archive")
def archive_audio(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    return _archive(session, AudioTrack, xid, actor, "Audio track")


@router.delete("/audio-tracks/{xid}/archive")
def restore_audio(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    return _archive(session, AudioTrack, xid, actor, "Audio track", restore=True)


# ── who can see it ───────────────────────────────────────────────────

class VisibilityUpdate(BaseModel):
    visibility: str = Field(pattern="^(author_private|org_private|platform_global)$")


def _share(session: Session, model: Any, xid: uuid.UUID, body: VisibilityUpdate,
           actor: Principal, what: str) -> dict:
    """Who can see this asset.

    **Every asset carried `visibility`, five of the six could never change it.**
    The column is declared with a three-value CHECK on `passages`, `questions`,
    `question_groups`, `audio_tracks` and `cue_card_sets`; `policy.filter_content`
    ORs four routes over it on every listing; the exposure query and the speaking
    library compare it to `'platform_global'` in raw SQL. And only `tests` had an
    endpoint that wrote it — so an author could share a whole PAPER with the
    platform and could not share the passage inside it. Everything else was
    `org_private` from insert to deletion, and three quarters of the sharing
    model was a predicate that always answered the same way.

    `Action.SHARE`, exactly as `update_test` does it, and the comment there is
    the reason: "widening visibility is a share, not an edit — a teacher who may
    edit a test must not be able to publish it to the whole platform." SHARE is
    centre admin and above.

    **A centre admin may set `platform_global` on their own centre's material,
    and that is deliberate.** The contractual promise is that a centre's content
    never reaches a competitor WITHOUT the centre's action; a centre choosing to
    publish is that action. Narrowing is the same permission because narrowing
    can break another author's draft that already uses the item, which is the
    same reason retiring one is not a teacher's call.
    """
    row = _owned(session, model, xid, actor, Action.SHARE, what)
    row.visibility = body.visibility
    session.flush()
    return {"xid": str(row.xid), "visibility": row.visibility}


@router.put("/passages/{xid}/visibility")
def share_passage(xid: uuid.UUID, body: VisibilityUpdate,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    return _share(session, Passage, xid, body, actor, "Passage")


@router.put("/questions/{xid}/visibility")
def share_question(xid: uuid.UUID, body: VisibilityUpdate,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    return _share(session, Question, xid, body, actor, "Question")


@router.put("/question-groups/{xid}/visibility")
def share_group(xid: uuid.UUID, body: VisibilityUpdate,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    return _share(session, QuestionGroup, xid, body, actor, "Question group")


@router.put("/audio-tracks/{xid}/visibility")
def share_audio(xid: uuid.UUID, body: VisibilityUpdate,
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    return _share(session, AudioTrack, xid, body, actor, "Audio track")


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
    rewrites history.

    **Validated, which it was not.** A band map turns a raw mark into the band a
    student is told they got, and `scoring.BandMap.band_for` answers None for a
    mark the table does not cover — so a gap is a whole cohort given no band, in
    silence. `publish_gate` checks coverage at publish time for the map attached
    to a test; nothing checked the map itself, so a broken curve could be
    created, listed and selected first.

    **A map with no organization is a PLATFORM DEFAULT**, which every centre
    inherits. `org_ids[0] if actor.org_ids else None` let an account belonging to
    no organization create one — and answered `is_platform_default: False` about
    it, which was not merely unhelpful but untrue. That now needs platform admin.
    """
    from app.modules.content import band_maps as band_map_rules
    from app.platform.errors import Forbidden, ValidationFailed
    from app.platform.findings import Report

    report = Report()
    band_map_rules.findings(body.mapping, body.max_raw, report)
    if report.errors:
        # All of them at once: a centre retuning a curve wants the list, not a
        # fix-and-resubmit loop nine times over.
        raise ValidationFailed("This band map is not usable.", report.errors)

    org_id = actor.org_ids[0] if actor.org_ids else None
    if org_id is None and not actor.is_platform_admin:
        raise Forbidden(
            "A band map with no organization becomes the platform default that "
            "every centre inherits. Only a platform admin may create one.",
            code="platform_band_map_not_permitted")
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
            # Was hardcoded False, including for the org-less map that IS the
            # platform default.
            "variant": bm.variant, "is_platform_default": org_id is None,
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


class CueCardPart2(BaseModel):
    topic: str
    bullets: list[str] = Field(default_factory=list)


class CueCardBody(BaseModel):
    """The three parts of an IELTS speaking test, which is what a cue card set
    IS. It was `body.get("body", {})` cast straight to jsonb, so `{}` — or a
    string, or a list — stored fine and reached the speaking session as a prompt
    set with no prompts in it."""

    part1: list[str] = Field(default_factory=list)
    part2: CueCardPart2 | None = None
    part3: list[str] = Field(default_factory=list)


class CueCardSetCreate(BaseModel):
    """`title` and `body` are both required, and both were `.get(..., default)`.

    An untitled set is unfindable in a library that lists by title — the
    endpoint one function up returns exactly `title`, `tags` and `visibility` —
    so `title=""` produced a row the author could create and then never locate.
    """

    title: str = Field(min_length=1)
    body: CueCardBody
    tags: list[str] = Field(default_factory=list)


def _archive_cue_cards(session: Session, xid: uuid.UUID, actor: Principal,
                       *, restore: bool) -> dict:
    """The fifth asset, which I missed when the other four got this.

    `list_cue_cards` filters `WHERE s.archived_at IS NULL` and nothing wrote the
    column, so that predicate always answered the same way. Found by
    `scripts/check_write_paths.py` — the gate written for exactly this shape,
    on its first run, catching a gap made an hour earlier.

    Raw SQL rather than `_archive` because cue-card sets have no ORM model; the
    authorization is the same `Action.ARCHIVE` on the owning organization.

    `archived_at` comes back from `RETURNING` in both directions rather than
    being written out as a literal `None` on the restore path. The value is the
    same either way; where it comes from is not. A response field the handler
    hard-codes is a field that keeps answering after the column stops agreeing
    with it, which is a smaller version of the bug this endpoint exists to fix.
    """
    from sqlalchemy import text

    row = session.execute(text("""
        SELECT id, org_id, owner_user_id, visibility FROM cue_card_sets
        WHERE xid = CAST(:x AS uuid)
    """).bindparams(x=xid)).mappings().first()
    if row is None:
        raise NotFound("Cue-card set not found.")
    policy.require(actor, Action.ARCHIVE,
                   Resource(org_id=row["org_id"], owner_user_id=row["owner_user_id"],
                            visibility=row["visibility"]),
                   org_settings=org_settings(session, row["org_id"]))
    archived = session.scalar(text(f"""
        UPDATE cue_card_sets SET archived_at = {'NULL' if restore else 'now()'}
        WHERE id = :i RETURNING archived_at
    """).bindparams(i=row["id"]))
    return {"xid": str(xid), "archived_at": iso(archived)}


@router.post("/cue-card-sets/{xid}/archive")
def archive_cue_cards(xid: uuid.UUID, actor: Principal = Depends(principal),
                      session: Session = Depends(db)) -> dict:
    return _archive_cue_cards(session, xid, actor, restore=False)


@router.delete("/cue-card-sets/{xid}/archive")
def restore_cue_cards(xid: uuid.UUID, actor: Principal = Depends(principal),
                      session: Session = Depends(db)) -> dict:
    return _archive_cue_cards(session, xid, actor, restore=True)


@router.put("/cue-card-sets/{xid}/visibility")
def share_cue_cards(xid: uuid.UUID, body: VisibilityUpdate,
                    actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    """The fifth asset again, and the one where the dead predicate is visible in
    the listing's own SQL: `list_cue_cards` ORs `visibility = 'platform_global'`
    and `owner_user_id = :uid AND visibility = 'author_private'`, and the INSERT
    that creates a set names `org_id, owner_user_id, title, tags` — never
    `visibility`. Both arms of that OR were unreachable.

    Raw SQL because cue-card sets have no ORM model; the authorization is the
    same `Action.SHARE` as the other five. See `_share`.
    """
    from sqlalchemy import text

    row = session.execute(text("""
        SELECT id, org_id, owner_user_id, visibility FROM cue_card_sets
        WHERE xid = CAST(:x AS uuid)
    """).bindparams(x=xid)).mappings().first()
    if row is None:
        raise NotFound("Cue-card set not found.")
    policy.require(actor, Action.SHARE,
                   Resource(org_id=row["org_id"], owner_user_id=row["owner_user_id"],
                            visibility=row["visibility"]),
                   org_settings=org_settings(session, row["org_id"]))
    session.execute(text("UPDATE cue_card_sets SET visibility = :v WHERE id = :i")
                    .bindparams(v=body.visibility, i=row["id"]))
    return {"xid": str(xid), "visibility": body.visibility}


@router.post("/cue-card-sets", status_code=status.HTTP_201_CREATED)
def create_cue_cards(body: CueCardSetCreate, actor: Principal = Depends(principal),
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
    """).bindparams(org=org_id, uid=actor.user_id, title=body.title,
                    tags=body.tags)).mappings().one()
    version_xid = session.scalar(text("""
        INSERT INTO cue_card_set_versions (set_id, version_no, body, created_by)
        VALUES (:sid, 1, CAST(:body AS jsonb), :uid)
        RETURNING xid
    """).bindparams(sid=set_id["id"],
                    body=json.dumps(body.body.model_dump(exclude_none=True)),
                    uid=actor.user_id))
    return {"xid": str(set_id["xid"]), "title": body.title,
            "tags": body.tags, "visibility": "org_private",
            # The version was created two statements ago and its xid was thrown
            # away, so the caller could not address the thing it had just made.
            "current_version_xid": str(version_xid)}
