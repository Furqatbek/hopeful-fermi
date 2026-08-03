"""Registry, media, governance, safety, billing, analytics, realtime.

Grouped into one module because each is a handful of endpoints over the same
repositories; splitting them into eight files would be filing, not structure.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Idempotency, Principal, db, idempotency, principal, registry
from app.api.dto import iso, jsonify
from app.modules.analytics.stats import exposure_recommendation
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.billing.entitlements import SEAT_BUNDLE
from app.modules.qtypes.registry import Registry
from app.platform import realtime as rt
from app.platform.config import settings
from app.platform.errors import Conflict, Forbidden, NotFound, ServiceUnavailable

reg_router = APIRouter(tags=["registry"])
media_router = APIRouter(tags=["media"])
gov_router = APIRouter(tags=["governance"])
safety_router = APIRouter(tags=["safety"])
billing_router = APIRouter(tags=["billing"])
analytics_router = APIRouter(tags=["analytics"])
realtime_router = APIRouter(tags=["realtime"])


def _admin(actor: Principal) -> None:
    if not actor.is_platform_admin:
        raise Forbidden("Platform admin only.", code="admin_only")


# ── registry ─────────────────────────────────────────────────────────

@reg_router.get("/question-types")
def list_question_types(skill: str | None = None, include_deprecated: bool = False,
                        response: Response = None,
                        reg: Registry = Depends(registry)) -> list[dict]:
    """Drives the authoring UI's type picker AND its form renderer.

    Each definition carries `authoring` hints from which the teacher-facing
    editor is generated — which is why adding a type needs no frontend deploy
    either, not just no migration.
    """
    defs = reg.for_skill(skill) if skill else reg.all()
    return [_type_dto(d) for d in defs
            if include_deprecated or d.status == "active"]


def _type_dto(d) -> dict:
    return {"key": d.key, "version": d.version, "status": d.status, "title": d.title,
            "description": d.description, "skills": list(d.skills),
            "payload_schema": d.payload_schema, "key_schema": d.key_schema,
            "response_schema": d.response_schema,
            "scoring": {"primitive": d.scoring.primitive.value,
                        "options": d.scoring.options,
                        "normalizers": list(d.scoring.normalizers)},
            "validation": d.validation, "authoring": d.authoring}


@reg_router.get("/question-types/{key}/{version}")
def read_question_type(key: str, version: int,
                       reg: Registry = Depends(registry)) -> dict:
    try:
        return _type_dto(reg.get(key, version))
    except Exception:
        raise NotFound(f"Question type {key}@v{version} is not registered.") from None


@reg_router.post("/admin/question-types", status_code=status.HTTP_201_CREATED)
def register_question_type(body: dict, actor: Principal = Depends(principal),
                           session: Session = Depends(db),
                           reg: Registry = Depends(registry)) -> dict:
    """The endpoint the whole registry exists for: adding a question type to a
    RUNNING production system, with no migration and no redeploy.

    `body: dict` on purpose. The body IS a question type definition — arbitrary
    JSON whose shape is the registry's own schema — and `QuestionTypeDef.from_dict`
    validates it into a `Report` carrying every finding at once. A Pydantic model
    here would either duplicate that schema, and then drift from it, or flatten a
    full report into the first error Pydantic happened to hit.
    """
    from app.modules.qtypes.schemas import QuestionTypeDef

    _admin(actor)
    try:
        definition = QuestionTypeDef.from_dict(body)
    except (KeyError, ValueError) as exc:
        from app.platform.errors import ValidationFailed
        from app.platform.findings import Report
        report = Report()
        report.add("DEFINITION_INVALID", str(exc), path="scoring",
                   fix_hint="Check the definition against an existing type.")
        raise ValidationFailed("This definition is not valid.", report.errors) from None

    exists = session.scalar(text(
        "SELECT count(*) FROM question_type_defs WHERE key = :k AND version = :v"
    ).bindparams(k=definition.key, v=definition.version))
    if exists:
        raise Conflict(f"{definition.ref} already exists. Bump the version — "
                       "definitions are never edited in place.",
                       code="type_version_exists")

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    session.execute(text("""
        INSERT INTO question_type_defs (key, version, status, title, description, skills,
            payload_schema, key_schema, response_schema, scoring, validation, authoring,
            source, checksum, created_by)
        VALUES (:k, :v, :s, :t, :d, :sk, CAST(:ps AS jsonb), CAST(:ks AS jsonb),
                CAST(:rs AS jsonb), CAST(:sc AS jsonb), CAST(:va AS jsonb),
                CAST(:au AS jsonb), 'custom', :cs, :by)
    """).bindparams(k=definition.key, v=definition.version, s=definition.status,
                    t=definition.title, d=body.get("description"),
                    sk=list(definition.skills),
                    ps=json.dumps(definition.payload_schema),
                    ks=json.dumps(definition.key_schema),
                    rs=json.dumps(definition.response_schema),
                    sc=json.dumps(body["scoring"]),
                    va=json.dumps(definition.validation),
                    au=json.dumps(definition.authoring),
                    cs=hashlib.sha256(canonical.encode()).hexdigest(),
                    by=actor.user_id))
    reg.register(definition)   # live immediately; no restart
    return _type_dto(definition)


@reg_router.post("/admin/question-types/validate")
def validate_question_type(body: dict, actor: Principal = Depends(principal)) -> dict:
    from app.modules.qtypes.schemas import QuestionTypeDef
    from app.platform.findings import Report

    _admin(actor)
    report = Report()
    try:
        QuestionTypeDef.from_dict(body)
    except (KeyError, ValueError) as exc:
        report.add("DEFINITION_INVALID", str(exc), path="scoring",
                   fix_hint="Check the definition against an existing type.")
    return report.as_dict()


@reg_router.get("/admin/lexicon")
def list_lexicon(kind: str | None = None, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> list[dict]:
    _admin(actor)
    sql = "SELECT kind, a, b, bidirectional, locale, note FROM lexicon_entries"
    if kind:
        sql += " WHERE kind = :kind"
    rows = session.execute(text(sql).bindparams(**({"kind": kind} if kind else {}))
                           ).mappings().all()
    return [dict(r) for r in rows]


class LexiconEntry(BaseModel):
    """Declared rather than read out of a `dict`.

    The kinds mirror the database CHECK constraint, and that duplication is the
    point: taking `body: dict` meant an unrecognised `kind` reached PostgreSQL,
    came back as an `IntegrityError`, and surfaced as **500 "Something went wrong
    on our side"** — for a caller who had simply typed `spelling` instead of
    `spelling_variant`. Worse, the failed statement aborts the transaction, so
    every later query on that session fails too and the real cause is three
    errors back.
    """

    kind: str = Field(pattern=r"^(spelling_variant|number_word|contraction|"
                              r"article|unit_form)$")
    a: str = Field(min_length=1)
    b: str = Field(min_length=1)
    bidirectional: bool = True
    locale: str | None = None
    note: str | None = None


@reg_router.post("/admin/lexicon", status_code=status.HTTP_201_CREATED)
def add_lexicon(body: LexiconEntry, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """Adding a missing UK/US pair is ONE ROW, not a deploy. Typical trigger: an
    `item_stats.common_wrong` entry shows students writing a form the key
    rejects."""
    _admin(actor)
    session.execute(text("""
        INSERT INTO lexicon_entries (kind, a, b, bidirectional, locale, note, created_by)
        VALUES (:kind, :a, :b, :bi, :locale, :note, :by)
        ON CONFLICT (kind, a, b) DO NOTHING
    """).bindparams(kind=body.kind, a=body.a, b=body.b,
                    bi=body.bidirectional, locale=body.locale,
                    note=body.note, by=actor.user_id))
    return body.model_dump()


# ── media ────────────────────────────────────────────────────────────

@media_router.get("/media/{xid}/content")
async def read_media(xid: uuid.UUID, grant: str, request: Request,
                     session: Session = Depends(db)) -> Response:
    """Stream media against a short-TTL, per-user grant.

    `async def`, which is the exception ADR-0001 §5.7 carved out: media streaming
    and the WebSocket gateway, and nothing else. A 14 MB listening section held
    open for four minutes on a sync handler would occupy a threadpool worker for
    four minutes, and forty students doing that is the whole pool.

    Two delivery modes, chosen by config rather than by a rewrite:

      * `proxy` (default) — bytes flow through this process. Every byte is origin
        egress and CDN cache hits are zero, which is the deliberate trade named
        in Deliverable 3: per-user tokenisation is worth more than cache at
        ~20 GB/month. Revisit above ~500 GB/month.
      * `redirect` — 302 to a short-TTL presigned object URL. Much cheaper, and
        the grant still gates the redirect, but the presigned URL that comes back
        is no longer bound to the user.

    Range requests are supported in both. Without them an audio element cannot
    seek, and on iOS Safari it will not play at all.
    """
    from app.modules.content import media as media_service
    from app.platform import grants
    from app.platform.storage import storage

    # The grant identifies the user; there is no bearer token on an <audio> src,
    # because a media element cannot set headers. That is exactly why the grant
    # is bound to the user, the object and a two-minute expiry.
    claim = grants.verify(grant, user_xid=_grant_user(grant),
                          media_xid=str(xid))
    _assert_grant_matches_session(session, claim)

    asset = media_service.deliverable(session, xid)
    store = storage()

    if settings().media_delivery == "redirect":
        return Response(status_code=status.HTTP_302_FOUND, headers={
            "Location": store.presign_get(
                asset["storage_key"],
                ttl_seconds=settings().media_grant_ttl_seconds),
            "Cache-Control": "private, no-store"})

    total = asset["bytes"] or 0
    start, end = _range(request.headers.get("range"), total)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        # `no-store`, not `private`: a shared device in a computer lab must not
        # keep an exam section in its disk cache after the student logs out.
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    }
    code = status.HTTP_200_OK
    if request.headers.get("range"):
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        code = status.HTTP_206_PARTIAL_CONTENT

    return StreamingResponse(
        _chunks(store, asset["storage_key"], start, end),
        status_code=code, media_type=asset["content_type"], headers=headers)


def _grant_user(token: str) -> str:
    """Read the claimed user out of the token BEFORE verifying it.

    Verification then re-checks that value against the signature, so a forged
    claim fails — this only avoids needing the user's identity from somewhere
    else in a request that deliberately carries no bearer token.
    """
    import base64
    import json as _json

    try:
        packed = token.split(".")[1]
        padding = "=" * (-len(packed) % 4)
        return _json.loads(base64.urlsafe_b64decode(packed + padding))["u"]
    except Exception:
        raise Forbidden("This media grant is malformed.",
                        code="grant_malformed") from None


def _assert_grant_matches_session(session: Session, claim) -> None:
    """A grant naming a suspended or deleted account is not honoured.

    The grant is valid for two minutes; a safety ban must take effect inside
    those two minutes, not after them.
    """
    active = session.scalar(text("""
        SELECT count(*) FROM users
        WHERE xid = CAST(:x AS uuid) AND status = 'active' AND deleted_at IS NULL
    """).bindparams(x=claim.user_xid))
    if not active:
        raise Forbidden("This account is not active.", code="account_inactive")


def _range(header: str | None, total: int) -> tuple[int, int]:
    """Parse `Range: bytes=start-end`. Open-ended and suffix forms included,
    because that is what real players send."""
    if not header or not header.startswith("bytes=") or total <= 0:
        return 0, max(0, total - 1)
    spec = header[len("bytes="):].split(",")[0].strip()
    try:
        raw_start, _, raw_end = spec.partition("-")
        if not raw_start:                      # bytes=-500: the LAST 500 bytes
            length = int(raw_end)
            return max(0, total - length), total - 1
        start = int(raw_start)
        end = int(raw_end) if raw_end else total - 1
    except ValueError:
        return 0, total - 1
    start = max(0, min(start, total - 1))
    end = max(start, min(end, total - 1))
    return start, end


async def _chunks(store, key: str, start: int, end: int):
    """Blocking storage reads pushed to a thread, so one slow client cannot stall
    the event loop that every other stream shares."""
    import anyio

    iterator = store.get(key, start=start, end=end)
    while True:
        chunk = await anyio.to_thread.run_sync(lambda: next(iterator, None))
        if chunk is None:
            return
        yield chunk


@media_router.get("/uploads/{xid}")
def read_upload(xid: uuid.UUID, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """The resume manifest: which parts the server already holds, and fresh
    presigned URLs for the ones it does not. This is what makes a 40 MB wav
    survivable on a dropping 4G connection."""
    from app.modules.content import media as media_service
    from app.platform.storage import storage

    found = media_service.resume(session, storage(), xid, actor.user_id,
                                 dt.datetime.now(dt.UTC))
    return _upload_dto(found)


def _upload_dto(found) -> dict:
    return {"xid": found.xid, "media_xid": found.media_xid,
            "part_size": found.part_size, "expected_bytes": found.expected_bytes,
            "received_bytes": found.part_size * len(found.parts_received),
            "parts_received": found.parts_received,
            "presigned_urls": found.presigned_urls,
            "expires_at": iso(found.expires_at), "status": found.status}


class UploadComplete(BaseModel):
    parts: list[dict] = Field(default_factory=list)


@media_router.post("/uploads/{xid}", status_code=status.HTTP_202_ACCEPTED)
def complete_upload(xid: uuid.UUID, body: UploadComplete,
                    actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    """Assemble the object and queue ingest.

    202, not 200: the file exists but is not yet playable. The ingest event is
    written in the SAME transaction, so an upload that completed without a
    transcode job is not representable.
    """
    from app.modules.content import media as media_service
    from app.platform.storage import storage

    return media_service.complete(session, storage(), xid, actor.user_id,
                                  body.parts, dt.datetime.now(dt.UTC))


@media_router.delete("/uploads/{xid}", status_code=status.HTTP_204_NO_CONTENT)
def abort_upload(xid: uuid.UUID, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> Response:
    from app.modules.content import media as media_service
    from app.platform.storage import storage

    media_service.abort(session, storage(), xid, actor.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── governance ───────────────────────────────────────────────────────

class GrantCreate(BaseModel):
    subject_type: str
    subject_xid: uuid.UUID
    grantee_kind: str
    grantee_xid: uuid.UUID | None = None
    permission: str
    expires_at: dt.datetime | None = None
    note: str | None = None


_SUBJECT_TABLES = {"test": "tests", "passage": "passages", "audio_track": "audio_tracks",
                   "question_group": "question_groups", "question": "questions",
                   "cue_card_set": "cue_card_sets", "band_map": "band_maps"}


@gov_router.post("/content-grants", status_code=status.HTTP_201_CREATED)
def create_grant(body: GrantCreate, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    """The marketplace seam.

    Today a centre admin grants another org `view`/`assign`/`copy`; a future
    purchase creates the same row, so selling a test bank needs no schema change.
    """
    table = _SUBJECT_TABLES.get(body.subject_type)
    if table is None:
        raise NotFound("Unknown subject type.")
    subject = session.execute(text(
        f"SELECT id, org_id, owner_user_id, visibility FROM {table} WHERE xid = CAST(:x AS uuid)"
    ).bindparams(x=body.subject_xid)).mappings().first()
    if subject is None:
        raise NotFound("Subject not found.")
    policy.require(actor, Action.SHARE,
                   Resource(org_id=subject["org_id"],
                            owner_user_id=subject["owner_user_id"],
                            visibility=subject["visibility"]))
    if body.grantee_kind == "public" and not actor.is_platform_admin:
        # Content can never become world-visible without a platform-admin review.
        # That single rule is most of the copyright containment.
        raise Forbidden("Only a platform admin may share content publicly.",
                        code="public_share_not_permitted")

    grantee_id = None
    if body.grantee_xid:
        lookup = "organizations" if body.grantee_kind == "org" else "users"
        grantee_id = session.execute(text(f"SELECT id FROM {lookup} WHERE xid = CAST(:x AS uuid)")
                                     .bindparams(x=body.grantee_xid)).scalar()
    row = session.execute(text("""
        INSERT INTO content_grants (subject_type, subject_id, grantee_kind, grantee_id,
                                    permission, granted_by, expires_at, note)
        VALUES (:st, :sid, :gk, :gid, :perm, :by, :exp, :note)
        RETURNING xid, granted_at
    """).bindparams(st=body.subject_type, sid=subject["id"], gk=body.grantee_kind,
                    gid=grantee_id, perm=body.permission, by=actor.user_id,
                    exp=body.expires_at, note=body.note)).mappings().one()
    return {"xid": str(row["xid"]), "subject_type": body.subject_type,
            "subject_xid": str(body.subject_xid), "grantee_kind": body.grantee_kind,
            "grantee_xid": str(body.grantee_xid) if body.grantee_xid else None,
            "permission": body.permission, "granted_at": iso(row["granted_at"])}


@gov_router.delete("/content-grants/{xid}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_grant(xid: uuid.UUID, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> Response:
    session.execute(text("""
        UPDATE content_grants SET revoked_at = now(), revoked_by = :by
        WHERE xid = CAST(:x AS uuid) AND revoked_at IS NULL
    """).bindparams(x=xid, by=actor.user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@gov_router.get("/questions/{xid}/exposure")
def question_exposure(xid: uuid.UUID, actor: Principal = Depends(principal),
                      session: Session = Depends(db)) -> dict:
    """`burn_score` rises with count and org spread. Items burn once they
    circulate, and this is where you watch it happen."""
    from app.modules.content.models import Question

    question = session.scalars(
        policy.filter_content(actor, select(Question).where(Question.xid == xid),
                              Question)).first()
    if question is None:
        raise NotFound("Question not found.")
    policy.require(actor, Action.VIEW_EXPOSURE,
                   Resource(org_id=question.org_id,
                            owner_user_id=question.owner_user_id,
                            visibility=question.visibility))
    row = session.execute(text("""
        SELECT times_sat, distinct_users, distinct_orgs, first_seen_at, last_seen_at,
               burn_score
        FROM item_exposure_stats WHERE question_id = :q
    """).bindparams(q=question.id)).mappings().first()
    stats = dict(row) if row else {
        "times_sat": 0, "distinct_users": 0, "distinct_orgs": 0,
        "first_seen_at": None, "last_seen_at": None, "burn_score": 0}
    burn = float(stats["burn_score"] or 0)
    return jsonify({
        "question_xid": str(question.xid), **stats, "burn_score": burn,
        # `stats.exposure_recommendation`, not a second copy of its thresholds.
        # This endpoint and `competitions.assess_paper` now answer "how burned is
        # too burned" from the same function, which is the point: an author told
        # an item is `watch` here and a contest refused for `retire` there must be
        # reading one rule.
        "recommendation": exposure_recommendation(burn),
    })


class TakedownCreate(BaseModel):
    claimant_name: str
    claimant_org: str | None = None
    claimant_email: str
    claimant_phone: str | None = None
    rights_basis: str
    sworn_statement: bool
    subject_type: str
    subject_xid: uuid.UUID
    description: str


@gov_router.post("/takedowns", status_code=status.HTTP_201_CREATED)
def file_takedown(body: TakedownCreate, session: Session = Depends(db)) -> dict:
    """Unauthenticated on purpose.

    A rights holder must not need an account to file, and I would rather absorb
    some spam than be unreachable to Cambridge's lawyers. On receipt the subject
    is SOFT-HIDDEN pending review and never hard-deleted — destroying the
    material would destroy the evidence with it.
    """
    if not body.sworn_statement:
        raise Forbidden("A sworn statement is required to file a takedown.",
                        code="sworn_statement_required")
    table = _SUBJECT_TABLES.get(body.subject_type)
    subject_id = None
    if table:
        subject_id = session.execute(text(f"SELECT id FROM {table} WHERE xid = CAST(:x AS uuid)")
                                     .bindparams(x=body.subject_xid)).scalar()
    row = session.execute(text("""
        INSERT INTO takedown_requests (claimant_name, claimant_org, claimant_email,
            claimant_phone, rights_basis, sworn_statement, subject_type, subject_id,
            description, hidden_at)
        VALUES (:n, :o, :e, :p, :rb, :sw, :st, :sid, :d, now())
        RETURNING xid, status, hidden_at, received_at
    """).bindparams(n=body.claimant_name, o=body.claimant_org, e=body.claimant_email,
                    p=body.claimant_phone, rb=body.rights_basis,
                    sw=body.sworn_statement, st=body.subject_type,
                    sid=subject_id or 0, d=body.description)).mappings().one()
    return {"xid": str(row["xid"]), "status": row["status"],
            "hidden_at": iso(row["hidden_at"]), "received_at": iso(row["received_at"]),
            "outcome_note": None}


class TakedownDecision(BaseModel):
    """The enum is the contract's, and it was enforced nowhere.

    `body["status"]` was a bare subscript — omit it and the handler answered 500
    — and any string at all went into the UPDATE. "Assume some centres WILL try
    to upload published Cambridge papers, and design so that liability and
    evidence are handled": this row IS that evidence, and a typo'd status is a
    takedown whose outcome the log cannot state.
    """

    status: str = Field(pattern=r"^(reviewing|upheld|rejected|counter_noticed"
                                 r"|withdrawn)$")
    outcome_note: str | None = None


@gov_router.patch("/admin/takedowns/{xid}")
def decide_takedown(xid: uuid.UUID, body: TakedownDecision,
                    actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    _admin(actor)
    row = session.execute(text("""
        UPDATE takedown_requests
        SET status = :s, outcome_note = :note, actioned_at = now(), actioned_by = :by
        WHERE xid = CAST(:x AS uuid)
        RETURNING xid, status, hidden_at, received_at, outcome_note
    """).bindparams(s=body.status, note=body.outcome_note,
                    by=actor.user_id, x=xid)).mappings().first()
    if row is None:
        raise NotFound("Takedown request not found.")
    return {"xid": str(row["xid"]), "status": row["status"],
            "hidden_at": iso(row["hidden_at"]), "received_at": iso(row["received_at"]),
            "outcome_note": row["outcome_note"]}


# ── safety ───────────────────────────────────────────────────────────

class ReportCreate(BaseModel):
    subject_kind: str
    subject_xid: uuid.UUID | None = None
    category: str
    description: str | None = None
    context: dict = {}


@safety_router.post("/reports", status_code=status.HTTP_201_CREATED)
def file_report(body: ReportCreate, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """`involves_minor` is set by the SYSTEM from participant ages, never by the
    reporter, and routes the report to a separate higher-priority queue."""
    from app.modules.identity.models import User

    subject_user_id = None
    involves_minor = actor.is_minor
    if body.subject_kind == "user" and body.subject_xid:
        subject = session.scalars(select(User).where(User.xid == body.subject_xid)).first()
        if subject is not None:
            subject_user_id = subject.id
            involves_minor = involves_minor or (
                subject.adult_at > dt.datetime.now(dt.UTC).date())

    row = session.execute(text("""
        INSERT INTO safety_reports (reporter_user_id, subject_kind, subject_user_id,
            subject_ref, category, description, context, involves_minor, priority)
        VALUES (:r, :sk, :su, :ref, :cat, :d, CAST(:ctx AS jsonb), :minor, :prio)
        RETURNING xid, status, priority, involves_minor, created_at
    """).bindparams(r=actor.user_id, sk=body.subject_kind, su=subject_user_id,
                    ref=str(body.subject_xid) if body.subject_xid else None,
                    cat=body.category, d=body.description,
                    ctx=json.dumps(body.context), minor=involves_minor,
                    prio="critical" if involves_minor and body.category in
                    ("grooming", "sexual_content") else
                    "high" if involves_minor else "normal")).mappings().one()
    return {"xid": str(row["xid"]), "category": body.category, "status": row["status"],
            "priority": row["priority"], "involves_minor": row["involves_minor"],
            "has_evidence": False, "created_at": iso(row["created_at"])}


@safety_router.get("/blocks")
def list_blocks(actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> list[dict]:
    from app.modules.identity.models import User, UserBlock

    rows = session.execute(
        select(UserBlock, User).join(User, User.id == UserBlock.blocked_user_id)
        .where(UserBlock.blocker_user_id == actor.user_id)).all()
    return [{"xid": str(u.xid), "user": {"xid": str(u.xid), "given_name": u.given_name},
             "created_at": iso(b.created_at)} for b, u in rows]


class BlockCreate(BaseModel):
    """Blocking is a safety control a student reaches for in the moment, often a
    minor, often right after something went wrong in a call.

    It was `uuid.UUID(str(body["user_xid"]))`: no field, `KeyError`; a malformed
    one, `ValueError` — both 500s, both indistinguishable to the person tapping
    the button from "the block did not happen". They would then be re-matched.
    """

    user_xid: uuid.UUID
    reason: str | None = None


@safety_router.post("/blocks", status_code=status.HTTP_201_CREATED)
def create_block(body: BlockCreate, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    """Enforced in BOTH directions by the matcher: A blocking B also stops B
    being matched with A. A one-way block would let the blocked party keep
    reaching the person who blocked them by re-queuing."""
    from app.modules.identity.models import User, UserBlock

    target = session.scalars(
        select(User).where(User.xid == body.user_xid)).first()
    if target is None:
        raise NotFound("User not found.")
    if target.id == actor.user_id:
        raise Conflict("You cannot block yourself.", code="self_block")
    existing = session.scalars(
        select(UserBlock).where(UserBlock.blocker_user_id == actor.user_id,
                                UserBlock.blocked_user_id == target.id)).first()
    if existing is None:
        existing = UserBlock(blocker_user_id=actor.user_id, blocked_user_id=target.id,
                             reason=body.reason)
        session.add(existing)
        session.flush()
    return {"xid": str(target.xid),
            "user": {"xid": str(target.xid), "given_name": target.given_name},
            "created_at": iso(existing.created_at)}


@safety_router.delete("/blocks/{xid}", status_code=status.HTTP_204_NO_CONTENT)
def remove_block(xid: uuid.UUID, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> Response:
    session.execute(text("""
        DELETE FROM user_blocks
        WHERE blocker_user_id = :me
          AND blocked_user_id = (SELECT id FROM users WHERE xid = CAST(:x AS uuid))
    """).bindparams(me=actor.user_id, x=xid))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@safety_router.get("/admin/reports")
def moderation_queue(queue: str = "general", status_filter: str | None = None,
                     limit: int = 25, actor: Principal = Depends(principal),
                     session: Session = Depends(db)) -> dict:
    """`queue=minors` is a DISTINCT higher-priority queue backed by a partial
    index, with its own response SLA — not a filter on the general list."""
    _admin(actor)
    sql = """
        SELECT xid, category, status, priority, involves_minor, created_at
        FROM safety_reports WHERE 1=1
    """
    params: dict[str, Any] = {}
    if queue == "minors":
        sql += " AND involves_minor AND status <> 'dismissed'"
    if status_filter:
        sql += " AND status = :status"
        params["status"] = status_filter
    sql += " ORDER BY priority DESC, created_at LIMIT :limit"
    params["limit"] = limit
    rows = session.execute(text(sql).bindparams(**params)).mappings().all()
    return {"items": [{"xid": str(r["xid"]), "category": r["category"],
                       "status": r["status"], "priority": r["priority"],
                       "involves_minor": r["involves_minor"], "has_evidence": False,
                       "created_at": iso(r["created_at"])} for r in rows],
            "next_cursor": None}


class ModerationActionCreate(BaseModel):
    """An entry in the immutable audit log for safety events, so every field of
    it is evidence.

    `body["action"]` and `body["reason"]` were bare subscripts, and the action
    was compared against `("suspend", "ban")` without ever being checked against
    the set of actions that exist. A typo'd `"bann"` matched neither branch: no
    sessions revoked, no account suspended, and an audit row saying the user was
    dealt with. That is the worst of the three possible outcomes — worse than
    refusing, and worse than acting — because the queue then shows the report as
    handled.

    `reason` has a minimum length for the same reason the row is immutable:
    somebody reads this months later, possibly a regulator, and `""` is not a
    reason.
    """

    action: str = Field(pattern=r"^(warn|mute|suspend|ban|content_hide"
                                 r"|content_remove|shadow_limit)$")
    reason: str = Field(min_length=1)
    target_user_xid: uuid.UUID | None = None
    # Which content, and which report. Three of the seven actions in the CHECK
    # constraint are content actions, `moderation_actions` has had
    # `target_subject_type`, `target_subject_id` and `report_id` since the
    # migration that created it — with a partial index over the first two,
    # `WHERE target_subject_id IS NOT NULL`, built for precisely this query — and
    # the handler wrote none of them. A `content_remove` recorded that content
    # was removed and not WHICH, in the log that exists to answer that question
    # when a rights holder's lawyers ask it.
    #
    # `check_schema_conformance.py` named all three the moment this model
    # existed. They were equally unread before, inside a `body: dict` where the
    # script could not see them.
    target_subject_type: str | None = None
    target_subject_xid: uuid.UUID | None = None
    report_xid: uuid.UUID | None = None
    # Typed, so `"soon"` is refused here rather than reaching a timestamptz column
    # mid-transaction.
    expires_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _content_actions_name_their_content(self) -> ModerationActionCreate:
        if self.action.startswith("content_") and self.target_subject_xid is None:
            raise ValueError("A content action must name the content it acts on.")
        if (self.target_subject_xid is None) != (self.target_subject_type is None):
            raise ValueError("Give both target_subject_type and target_subject_xid, "
                             "or neither.")
        return self


@safety_router.post("/admin/moderation-actions", status_code=status.HTTP_201_CREATED)
def take_moderation_action(body: ModerationActionCreate,
                           actor: Principal = Depends(principal),
                           session: Session = Depends(db)) -> dict:
    """A suspend or ban revokes every live session immediately — which is why
    refresh tokens are opaque and stored rather than stateless JWTs."""
    from app.modules.identity.models import AuthSession, User

    _admin(actor)
    target_id = None
    if body.target_user_xid:
        target = session.scalars(
            select(User).where(User.xid == body.target_user_xid)).first()
        target_id = target.id if target else None

    revoked = 0
    if body.action in ("suspend", "ban") and target_id:
        revoked = session.execute(
            AuthSession.__table__.update()
            .where(AuthSession.user_id == target_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC),
                    revoked_reason=body.action)).rowcount
        session.execute(text("UPDATE users SET status = 'suspended' WHERE id = :id")
                        .bindparams(id=target_id))
        # Through the outbox, in this transaction, so a ban that rolls back does
        # not push a `session.revoked` to a user who was not banned. `target.xid`
        # is safe to read here: `target_id` is only set when `target` was found.
        #
        # The refresh token is already dead by this point, but the ACCESS token
        # lives for another fifteen minutes and an open socket outlives both.
        # `RtSessionRevoked` exists precisely because "refresh tokens are stored
        # and revocable — a stateless JWT could not deliver this".
        session.execute(text("""
            INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
            VALUES ('user', :agg, 'identity.session_revoked', CAST(:payload AS jsonb))
        """).bindparams(agg=str(target.xid),
                        payload=json.dumps({"user_xid": str(target.xid),
                                            "reason": "banned" if body.action == "ban"
                                            else "suspended",
                                            "until": iso(body.expires_at)})))

    subject_id = None
    if body.target_subject_xid is not None:
        table = _SUBJECT_TABLES.get(body.target_subject_type or "")
        if table is None:
            raise NotFound("Unknown subject type.")
        subject_id = session.execute(text(
            f"SELECT id FROM {table} WHERE xid = CAST(:x AS uuid)"
        ).bindparams(x=body.target_subject_xid)).scalar()
        if subject_id is None:
            raise NotFound("Subject not found.")

    report_id = None
    if body.report_xid is not None:
        report_id = session.execute(text(
            "SELECT id FROM safety_reports WHERE xid = CAST(:x AS uuid)"
        ).bindparams(x=body.report_xid)).scalar()
        if report_id is None:
            # A foreign key, so an unknown one would otherwise be an
            # IntegrityError -- a 500, and an aborted transaction that takes the
            # session revocation above down with it.
            raise NotFound("Report not found.")

    row = session.execute(text("""
        INSERT INTO moderation_actions (target_user_id, target_subject_type,
                                        target_subject_id, report_id, action, reason,
                                        actor_user_id, expires_at)
        VALUES (:t, :sty, :sid, :rep, :a, :r, :by, :exp) RETURNING xid, created_at
    """).bindparams(t=target_id, sty=body.target_subject_type, sid=subject_id,
                    rep=report_id, a=body.action, r=body.reason,
                    by=actor.user_id, exp=body.expires_at)).mappings().one()
    return {"xid": str(row["xid"]), "action": body.action, "reason": body.reason,
            "created_at": iso(row["created_at"]), "sessions_revoked": revoked}


# ── billing ──────────────────────────────────────────────────────────

@billing_router.get("/products")
def list_products(session: Session = Depends(db)) -> list[dict]:
    rows = session.execute(text("""
        SELECT p.xid, p.code, p.kind, p.name, p.description,
               coalesce(json_agg(json_build_object(
                   'xid', pr.id, 'currency', pr.currency,
                   'amount_minor', pr.amount_minor, 'interval', pr.interval))
                   FILTER (WHERE pr.id IS NOT NULL), '[]') AS prices
        FROM products p LEFT JOIN prices pr ON pr.product_id = p.id AND pr.active
        WHERE p.active GROUP BY p.id
    """)).mappings().all()
    return [{"xid": str(r["xid"]), "code": r["code"], "kind": r["kind"],
             "name": r["name"], "description": r["description"],
             "prices": r["prices"]} for r in rows]


class OrderCreate(BaseModel):
    price_xid: int
    quantity: int = 1
    provider: str
    org_xid: uuid.UUID | None = None
    return_url: str | None = None


@billing_router.post("/orders", status_code=status.HTTP_201_CREATED)
def create_order(body: OrderCreate, actor: Principal = Depends(principal),
                 session: Session = Depends(db),
                 idem: Idempotency = Depends(idempotency)) -> dict:
    """Amounts are in TIYIN throughout, because that is what Click and Payme
    transact in; a decimal round trip through a provider is how money goes
    missing. `reference` is what daily reconciliation joins on."""
    if replayed := idem.replay("orders.create", body.model_dump(mode="json")):
        return replayed

    price = session.execute(text("""
        SELECT id, product_id, currency, amount_minor FROM prices
        WHERE id = :id AND active
    """).bindparams(id=body.price_xid)).mappings().first()
    if price is None:
        raise NotFound("Price not found.")

    org_id = None
    if body.org_xid:
        org_id = session.execute(text("SELECT id FROM organizations WHERE xid = CAST(:x AS uuid)")
                                 .bindparams(x=body.org_xid)).scalar()
    reference = f"ORD-{secrets.token_hex(8).upper()}"
    row = session.execute(text("""
        INSERT INTO orders (user_id, org_id, product_id, price_id, quantity,
                            amount_minor, currency, status, provider, reference,
                            metadata, expires_at)
        VALUES (:u, :o, :p, :pr, :q, :amt, :cur, 'awaiting_payment', :prov, :ref,
                CAST(:meta AS jsonb), now() + interval '1 hour')
        RETURNING xid, status, created_at
    """).bindparams(u=None if org_id else actor.user_id, o=org_id,
                    p=price["product_id"], pr=price["id"], q=body.quantity,
                    # `return_url` was accepted and dropped, so a provider had
                    # nowhere to send the payer back to.
                    meta=json.dumps({"return_url": body.return_url}
                                    if body.return_url else {}),
                    amt=price["amount_minor"] * body.quantity,
                    cur=price["currency"], prov=body.provider,
                    ref=reference)).mappings().one()
    payload = {
        "order": {"xid": str(row["xid"]), "reference": reference,
                  "status": row["status"],
                  "amount_minor": price["amount_minor"] * body.quantity,
                  "currency": price["currency"], "provider": body.provider,
                  "created_at": iso(row["created_at"]), "paid_at": None},
        "redirect_url": f"https://checkout.{body.provider}.uz/pay?order={reference}",
    }
    idem.store(body.model_dump(mode="json"), payload, status.HTTP_201_CREATED)
    return payload


@billing_router.get("/orders/{xid}")
def read_order(xid: uuid.UUID, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    row = session.execute(text("""
        SELECT xid, reference, status, amount_minor, currency, provider,
               created_at, paid_at, user_id, org_id
        FROM orders WHERE xid = CAST(:x AS uuid)
    """).bindparams(x=xid)).mappings().first()
    if row is None or (row["user_id"] != actor.user_id
                       and row["org_id"] not in actor.org_ids
                       and not actor.is_platform_admin):
        raise NotFound("Order not found.")
    return {"xid": str(row["xid"]), "reference": row["reference"],
            "status": row["status"], "amount_minor": row["amount_minor"],
            "currency": row["currency"], "provider": row["provider"],
            "created_at": iso(row["created_at"]), "paid_at": iso(row["paid_at"])}


@billing_router.get("/me/entitlements")
def my_entitlements(actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> list[dict]:
    from app.modules.billing.models import EntitlementRow

    rows = session.scalars(select(EntitlementRow).where(
        EntitlementRow.revoked_at.is_(None),
        ((EntitlementRow.subject_kind == "user")
         & (EntitlementRow.subject_id == actor.user_id))
        | ((EntitlementRow.subject_kind == "org")
           & (EntitlementRow.subject_id.in_(actor.org_ids or [0]))))).all()
    return [{"feature": e.feature, "subject_kind": e.subject_kind,
             "source_kind": e.source_kind, "quantity": e.quantity,
             "remaining": None if e.quantity is None else max(0, e.quantity - e.consumed),
             "starts_at": iso(e.starts_at), "expires_at": iso(e.expires_at)}
            for e in rows]


def _click_response(error: int, note: str, **extra) -> dict:
    return {"error": error, "error_note": note, **extra}


class ClickCallback(BaseModel):
    """Click posts `application/x-www-form-urlencoded`.

    Declared as fields rather than read off the raw request: FastAPI parses and
    validates the form, the shape is documented, and a malformed callback fails
    at the boundary instead of surfacing as a `KeyError` inside the state machine.
    """

    click_trans_id: str
    service_id: str = ""
    merchant_trans_id: str = ""
    merchant_prepare_id: str | None = None
    amount: str = "0"
    action: str = "0"
    sign_time: str = ""
    sign_string: str = ""
    error: str = "0"

    @classmethod
    def form(cls, click_trans_id: str = Form(...), service_id: str = Form(default=""),
             merchant_trans_id: str = Form(default=""),
             merchant_prepare_id: str | None = Form(default=None),
             amount: str = Form(default="0"), action: str = Form(default="0"),
             sign_time: str = Form(default=""), sign_string: str = Form(default=""),
             error: str = Form(default="0")) -> "ClickCallback":
        return cls(click_trans_id=click_trans_id, service_id=service_id,
                   merchant_trans_id=merchant_trans_id,
                   merchant_prepare_id=merchant_prepare_id, amount=amount,
                   action=action, sign_time=sign_time, sign_string=sign_string,
                   error=error)


@billing_router.post("/payments/click/prepare")
def click_prepare(body: ClickCallback = Depends(ClickCallback.form),
                  session: Session = Depends(db)) -> dict:
    """Phase one of Click's two-step flow.

    The raw call is written to `payment_events` BEFORE it is acted on, so a
    replay is answered from the stored response rather than re-executed.
    """
    return _click_phase(session, body, phase="prepare")


@billing_router.post("/payments/click/complete")
def click_complete(body: ClickCallback = Depends(ClickCallback.form),
                   session: Session = Depends(db)) -> dict:
    return _click_phase(session, body, phase="complete")


def _click_signature(body: ClickCallback, *, phase: str) -> str:
    """Click's documented concatenation, MD5-hashed.

    Verified before anything is written. Their callback arrives from an arbitrary
    IP, so the signature — not an allowlist — is what makes it trustworthy.
    """
    prepare_id = body.merchant_prepare_id or ""
    action = "0" if phase == "prepare" else "1"
    raw = (f"{body.click_trans_id}{body.service_id}{settings().click_secret_key}"
           f"{body.merchant_trans_id}{prepare_id}{body.amount}{action}"
           f"{body.sign_time}")
    return hashlib.md5(raw.encode()).hexdigest()   # noqa: S324 — their spec, not ours


def _click_phase(session: Session, body: ClickCallback, *, phase: str) -> dict:
    form = body.model_dump()
    reference = body.merchant_trans_id
    txn = body.click_trans_id
    idempotency_key = f"{phase}:{txn}:{reference}"

    stored = session.execute(text("""
        SELECT response FROM payment_events
        WHERE provider = 'click' AND idempotency_key = :k
    """).bindparams(k=idempotency_key)).scalar()
    if stored:
        return stored   # "webhooks arrive twice": replay, never re-execute

    # An unconfigured secret is not a weak secret, it is a published one: the
    # signature becomes computable by anyone who has read this file. Refuse
    # rather than compare.
    signature_ok = bool(settings().click_secret_key) and hmac.compare_digest(
        _click_signature(body, phase=phase), body.sign_string.lower())
    if not signature_ok:
        response = _click_response(-1, "SIGN CHECK FAILED")
        _record_click(session, txn, phase, form, response, idempotency_key,
                      signature_ok=False, result="signature_failed")
        return response

    order = session.execute(text("""
        SELECT id, amount_minor, status FROM orders WHERE reference = :r
    """).bindparams(r=reference)).mappings().first()
    if order is None:
        response = _click_response(-5, "Order not found")
    else:
        state = "authorized" if phase == "prepare" else "captured"
        session.execute(text("""
            INSERT INTO payments (order_id, provider, provider_txn_id, state,
                                  amount_minor, currency, authorized_at, captured_at)
            VALUES (:o, 'click', :txn, :state, :amt, 'UZS',
                    CASE WHEN :state = 'authorized' THEN now() END,
                    CASE WHEN :state = 'captured' THEN now() END)
            ON CONFLICT (provider, provider_txn_id) DO UPDATE
            SET state = EXCLUDED.state,
                captured_at = coalesce(payments.captured_at, EXCLUDED.captured_at)
        """).bindparams(o=order["id"], txn=txn, state=state,
                        amt=order["amount_minor"]))
        if phase == "complete":
            session.execute(text("""
                UPDATE orders SET status = 'paid', paid_at = now() WHERE id = :id
            """).bindparams(id=order["id"]))
        response = _click_response(0, "Success", click_trans_id=txn,
                                   merchant_trans_id=reference)

    _record_click(session, txn, phase, form, response, idempotency_key,
                  signature_ok=True, result="ok")
    return response


def _record_click(session: Session, txn: str, phase: str, form: dict, response: dict,
                  idempotency_key: str, *, signature_ok: bool, result: str) -> None:
    """Every callback is recorded, including rejected ones.

    A provider dispute is settled by showing exactly what they sent and exactly
    what we answered — which is only possible if the failures are stored too.
    """
    session.execute(text("""
        INSERT INTO payment_events (provider, provider_txn_id, method, payload,
                                    response, signature_ok, idempotency_key,
                                    processed_at, result)
        VALUES ('click', :txn, :method, CAST(:payload AS jsonb),
                CAST(:response AS jsonb), :sig, :k, now(), :result)
        ON CONFLICT (provider, idempotency_key) DO NOTHING
    """).bindparams(txn=txn, method=phase.capitalize(), payload=json.dumps(form),
                    response=json.dumps(response), sig=signature_ok,
                    k=idempotency_key, result=result))


PAYME_UNAUTHORIZED = -32504     # their code for "insufficient privileges"


def _payme_authorized(request: Request) -> bool:
    """Payme authenticates with HTTP Basic: `Paycom:<merchant key>`.

    **This was not checked at all.** `payme_merchant_key` sat in the config,
    unread by anything, while the endpoint accepted any caller — so an
    unauthenticated `PerformTransaction` naming a known order reference marked
    that order paid and granted the entitlement. Verified against a real database
    before it was fixed: 200, `state: 2`, `orders.status = 'paid'`.

    The OpenAPI document has said `security: [paymeBasic]` all along.
    """
    key = settings().payme_merchant_key
    if not key:
        # No credential configured means no way to tell Payme from anyone else.
        # Refusing is the only safe answer for an endpoint that moves money.
        return False

    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(decoded, f"Paycom:{key}")


@billing_router.post("/payments/payme")
def payme_rpc(body: dict, request: Request, session: Session = Depends(db)) -> dict:
    """Deliberately ONE endpoint, not several REST routes.

    `body: dict` is deliberate here too, and it is the only request body in this
    application that stays loose. Every other one is a Pydantic model so that a
    malformed request is a 422 with a problem document — but Payme does not read
    HTTP status codes, it reads a numeric `error.code` in a 200 body. A Pydantic
    model would make FastAPI answer 422 with problem+json to a caller that
    cannot parse either, and the transaction would hang in their state machine
    rather than fail cleanly. Reading it loosely and answering in THEIR protocol
    is the correct handling of a foreign contract.

    Payme drives a JSON-RPC transaction state machine where the provider calls us
    with CheckPerformTransaction / CreateTransaction / PerformTransaction /
    CancelTransaction / CheckTransaction / GetStatement. Modelling it as REST
    resources would fight the protocol and break on their error-code contract —
    which uses numeric codes in the body, not HTTP status.
    """
    if not _payme_authorized(request):
        return {"id": body.get("id"),
                "error": {"code": PAYME_UNAUTHORIZED,
                          "message": {"en": "Insufficient privileges",
                                      "ru": "Недостаточно привилегий"},
                          "data": "auth"}}
    method = body.get("method", "")
    params = body.get("params", {}) or {}
    rpc_id = body.get("id")
    account = params.get("account", {}) or {}
    reference = account.get("order") or account.get("reference", "")
    txn = params.get("id", "")

    order = session.execute(text("""
        SELECT id, amount_minor, status FROM orders WHERE reference = :r
    """).bindparams(r=reference)).mappings().first()

    def error(code: int, message: str) -> dict:
        return {"id": rpc_id, "error": {"code": code,
                                        "message": {"en": message, "ru": message},
                                        "data": "order"}}

    def amount() -> int:
        """`int(params.get("amount", 0))` on input from another company's server.

        A string, a null or a list raises, and an exception here is a 500 — which
        Payme cannot interpret, so the transaction hangs on their side instead of
        failing with a code they understand. Anything unreadable is simply not
        the order's amount, which is exactly what `-31001` says.
        """
        try:
            return int(params.get("amount", 0))
        except (TypeError, ValueError):
            return -1

    if method == "CheckPerformTransaction":
        if order is None:
            return error(-31050, "Order not found")
        if amount() != order["amount_minor"]:
            return error(-31001, "Wrong amount")
        return {"id": rpc_id, "result": {"allow": True}}

    if method == "CreateTransaction":
        if order is None:
            return error(-31050, "Order not found")
        # Amount checked here as well as in CheckPerformTransaction. The provider
        # is supposed to call Check first, but "the other side always calls the
        # methods in order" is an assumption, and the one that is wrong is the
        # one that books a 5,000,000 soum pack for 100.
        if amount() != order["amount_minor"]:
            return error(-31001, "Wrong amount")
        session.execute(text("""
            INSERT INTO payments (order_id, provider, provider_txn_id, state,
                                  amount_minor, currency, authorized_at)
            VALUES (:o, 'payme', :txn, 'authorized', :amt, 'UZS', now())
            ON CONFLICT (provider, provider_txn_id) DO NOTHING
        """).bindparams(o=order["id"], txn=txn, amt=order["amount_minor"]))
        return {"id": rpc_id, "result": {"create_time": int(dt.datetime.now(dt.UTC)
                                                            .timestamp() * 1000),
                                         "transaction": txn, "state": 1}}

    if method == "PerformTransaction":
        # Capture only a transaction that was actually created. The UPDATE alone
        # matched zero rows for an unknown txn and the code then marked the ORDER
        # paid regardless — so a Perform naming a transaction that never existed
        # granted the entitlement without a payment row to account for it.
        captured = session.execute(text("""
            UPDATE payments SET state = 'captured',
                                captured_at = coalesce(captured_at, now())
            WHERE provider = 'payme' AND provider_txn_id = :txn
              AND state IN ('authorized', 'captured')
            RETURNING order_id
        """).bindparams(txn=txn)).scalar()
        if captured is None:
            return error(-31003, "Transaction not found")
        session.execute(text("""
            UPDATE orders SET status = 'paid', paid_at = coalesce(paid_at, now())
            WHERE id = :id
        """).bindparams(id=captured))
        return {"id": rpc_id, "result": {"perform_time": int(dt.datetime.now(dt.UTC)
                                                             .timestamp() * 1000),
                                         "transaction": txn, "state": 2}}

    if method == "CancelTransaction":
        session.execute(text("""
            UPDATE payments SET state = 'cancelled', cancelled_at = now(),
                                cancel_reason = :reason
            WHERE provider = 'payme' AND provider_txn_id = :txn
        """).bindparams(txn=txn, reason=str(params.get("reason", ""))))
        return {"id": rpc_id, "result": {"cancel_time": int(dt.datetime.now(dt.UTC)
                                                            .timestamp() * 1000),
                                         "transaction": txn, "state": -1}}

    if method == "CheckTransaction":
        row = session.execute(text("""
            SELECT state, authorized_at, captured_at FROM payments
            WHERE provider = 'payme' AND provider_txn_id = :txn
        """).bindparams(txn=txn)).mappings().first()
        if row is None:
            return error(-31003, "Transaction not found")
        return {"id": rpc_id, "result": {
            "transaction": txn,
            "state": 2 if row["state"] == "captured" else 1}}

    if method == "GetStatement":
        # The reconciliation endpoint: Payme asks us what WE think happened, and
        # the daily job asks Payme the same in reverse. That pair is the answer
        # to "webhooks arrive ... or never".
        rows = session.execute(text("""
            SELECT p.provider_txn_id, p.amount_minor, o.reference
            FROM payments p JOIN orders o ON o.id = p.order_id
            WHERE p.provider = 'payme' AND p.state = 'captured'
        """)).mappings().all()
        return {"id": rpc_id, "result": {"transactions": [
            {"id": r["provider_txn_id"], "amount": r["amount_minor"],
             "account": {"order": r["reference"]}} for r in rows]}}

    return error(-32601, "Method not found")


@billing_router.get("/orgs/{xid}/seats")
def read_seats(xid: uuid.UUID, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    return _seat_summary(session, _org_id(session, xid, actor))


class SeatAssign(BaseModel):
    """`body["user_xids"]` was a bare subscript, and every element went through
    `uuid.UUID(str(u))` — an omitted field or one bad element was a 500 on a
    screen a centre reaches while trying to give somebody access.

    `max_length` because the list has no natural bound: it becomes an `IN` clause
    and then a row-by-row insert loop, and the largest legitimate request is a
    centre seating one intake. A thousand is well past that and well short of
    what makes a single request expensive.
    """

    user_xids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


@billing_router.post("/orgs/{xid}/seats")
def assign_seats(xid: uuid.UUID, body: SeatAssign,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> dict:
    """A seat licence only covers users who hold a seat — otherwise ten seats
    would entitle a four-hundred-student centre."""
    from app.modules.billing.models import SeatAssignment
    from app.modules.identity.models import User

    org_id = _org_id(session, xid, actor)
    policy.require(actor, Action.MANAGE_ORG, Resource(org_id=org_id))
    entitlement = _seat_licence(session, org_id)
    if entitlement is None:
        raise NotFound("This organization has no seat licence for mock exams.")

    assigned = session.scalar(
        select(func.count()).select_from(SeatAssignment)
        .where(SeatAssignment.entitlement_id == entitlement.id,
               SeatAssignment.released_at.is_(None))) or 0
    users = session.scalars(
        select(User).where(User.xid.in_(body.user_xids))).all()
    if entitlement.quantity is not None and assigned + len(users) > entitlement.quantity:
        raise Conflict(
            f"Only {entitlement.quantity - assigned} seat(s) remain.",
            code="not_enough_seats")
    for user in users:
        exists = session.scalars(
            select(SeatAssignment).where(SeatAssignment.entitlement_id == entitlement.id,
                                         SeatAssignment.user_id == user.id)).first()
        if exists is None:
            session.add(SeatAssignment(entitlement_id=entitlement.id, user_id=user.id,
                                       assigned_by=actor.user_id))
    session.flush()
    return _seat_summary(session, org_id)


def _org_id(session: Session, xid: uuid.UUID, actor: Principal) -> int:
    """Seat management is a centre-admin action, not a member one.

    Org membership alone would let any student read who holds the centre's seats
    and how many are left, which is commercial information about their school.
    """
    org_id = session.execute(text("SELECT id FROM organizations WHERE xid = CAST(:x AS uuid)")
                             .bindparams(x=xid)).scalar()
    if org_id is None or (org_id not in actor.org_ids and not actor.is_platform_admin):
        raise NotFound("Organization not found.")
    policy.require(actor, Action.MANAGE_ORG, Resource(org_id=org_id))
    return org_id


def _seat_licence(session: Session, org_id: int):
    """The centre's mock seat licence — the one the coverage gate reads.

    **`feature` was not in this query.** It selected on `source_kind = 'seat'`
    alone and took `.first()`, so "seats" meant whatever seat-shaped row came
    back first, while `teaching._require_covered` asked about `SEAT_BUNDLE`. Two
    subsystems, no shared key: a centre could buy seats, watch this endpoint
    report them assigned, and have every assignment refused with `no_seat` —
    which is the advice this screen had just given them. Nothing caught it
    because nothing joined the two, and the suite's own fixture sold three seats
    for a feature named `mock_exams` that exists nowhere else in the product.

    Ordered rather than `.first()` on an unordered query, because a centre that
    renews has two rows. Live first, then the later one: reporting last year's
    exhausted licence to a centre that has just paid is the same class of wrong
    answer, arrived at more expensively.
    """
    from app.modules.billing.models import EntitlementRow

    now = dt.datetime.now(dt.UTC)
    return session.scalars(
        select(EntitlementRow)
        .where(EntitlementRow.subject_kind == "org",
               EntitlementRow.subject_id == org_id,
               EntitlementRow.source_kind == "seat",
               EntitlementRow.feature.in_(SEAT_BUNDLE),
               EntitlementRow.revoked_at.is_(None))
        .order_by(((EntitlementRow.expires_at.is_(None))
                   | (EntitlementRow.expires_at > now)).desc(),
                  EntitlementRow.id.desc())).first()


def _seat_summary(session: Session, org_id: int) -> dict:
    from app.modules.billing.models import SeatAssignment
    from app.modules.identity.models import User

    entitlement = _seat_licence(session, org_id)
    if entitlement is None:
        return {"entitlement_xid": None, "total": 0, "assigned": 0, "remaining": 0,
                "expires_at": None, "members": []}
    members = session.scalars(
        select(User).join(SeatAssignment, SeatAssignment.user_id == User.id)
        .where(SeatAssignment.entitlement_id == entitlement.id,
               SeatAssignment.released_at.is_(None))).all()
    total = entitlement.quantity or 0
    return {"entitlement_xid": str(entitlement.xid), "total": total,
            "assigned": len(members), "remaining": max(0, total - len(members)),
            "expires_at": iso(entitlement.expires_at),
            "members": [{"xid": str(u.xid), "given_name": u.given_name,
                         "phone": u.phone, "locale": u.locale} for u in members]}


# ── analytics ────────────────────────────────────────────────────────

@analytics_router.get("/cohorts/{xid}/progress")
def cohort_progress(xid: uuid.UUID, actor: Principal = Depends(principal),
                    session: Session = Depends(db)) -> dict:
    """Weekly aggregates. Author previews and students' PRIVATE practice are both
    excluded: the centre sees the work it set, not what a student did at 1 a.m.
    on their own account."""
    cohort_id = _cohort_id(session, xid, actor)
    weeks = session.execute(text("""
        SELECT week, sum(attempts) AS attempts, round(avg(avg_band), 1) AS avg_band,
               round(avg(reading_band), 1) AS reading_band,
               round(avg(listening_band), 1) AS listening_band
        FROM mv_cohort_progress WHERE cohort_id = :c GROUP BY week ORDER BY week
    """).bindparams(c=cohort_id)).mappings().all()
    students = session.execute(text("""
        SELECT u.xid, u.given_name, u.family_name, u.locale,
               count(*) AS attempts, min(avg_band) AS first_band,
               max(best_band) AS latest_band
        FROM mv_cohort_progress m JOIN users u ON u.id = m.user_id
        WHERE m.cohort_id = :c GROUP BY u.id
    """).bindparams(c=cohort_id)).mappings().all()
    return jsonify({
        "cohort_xid": str(xid),
        "weeks": [dict(w) for w in weeks],
        "students": [{"user": {"xid": str(s["xid"]), "given_name": s["given_name"],
                               "family_name": s["family_name"], "locale": s["locale"]},
                      "attempts": s["attempts"], "first_band": s["first_band"],
                      "latest_band": s["latest_band"],
                      "delta": (float(s["latest_band"]) - float(s["first_band"]))
                      if s["first_band"] and s["latest_band"] else None,
                      "weak_types": []} for s in students],
    })


def _cohort_id(session: Session, xid: uuid.UUID, actor: Principal) -> int:
    """A TEACHING role at the cohort's centre, not merely membership of it.

    These endpoints return every classmate's bands, attendance and weak areas. A
    student in the same organization is precisely the person who must not see
    them, so org membership is the wrong test — the role is.
    """
    row = session.execute(text("SELECT id, org_id FROM cohorts WHERE xid = CAST(:x AS uuid)")
                          .bindparams(x=xid)).mappings().first()
    if row is None:
        raise NotFound("Cohort not found.")
    teaches = actor.roles.get(row["org_id"]) in ("teacher", "centre_admin")
    if not (teaches or actor.is_platform_admin):
        raise NotFound("Cohort not found.")
    return row["id"]


@analytics_router.get("/cohorts/{xid}/attendance")
def cohort_attendance(xid: uuid.UUID, actor: Principal = Depends(principal),
                      session: Session = Depends(db)) -> dict:
    """Assigned / started / completed / late per student. This is what a centre
    shows parents."""
    cohort_id = _cohort_id(session, xid, actor)
    rows = session.execute(text("""
        SELECT u.xid, u.given_name, u.family_name, u.locale,
               count(*) FILTER (WHERE a.assigned) AS assigned,
               count(*) FILTER (WHERE a.started) AS started,
               count(*) FILTER (WHERE a.completed) AS completed,
               count(*) FILTER (WHERE a.late) AS late
        FROM attendance_facts a JOIN users u ON u.id = a.user_id
        WHERE a.cohort_id = :c GROUP BY u.id
    """).bindparams(c=cohort_id)).mappings().all()
    return {"cohort_xid": str(xid), "rows": [
        {"user": {"xid": str(r["xid"]), "given_name": r["given_name"],
                  "family_name": r["family_name"], "locale": r["locale"]},
         "assigned": r["assigned"], "started": r["started"],
         "completed": r["completed"], "late": r["late"],
         "completion_rate": round(r["completed"] / r["assigned"], 2)
         if r["assigned"] else 0.0} for r in rows]}


@analytics_router.get("/test-versions/{xid}/item-analysis")
def item_analysis(xid: uuid.UUID, org_scope: str = "mine",
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    """Difficulty, discrimination, distractor distribution and `common_wrong`.

    `common_wrong` is how a broken key is found automatically. Spelling and
    number variants never reach it — the tolerance lexicon absorbs them — so what
    surfaces is a genuine missing alternative.

    **This used to be a second, worse implementation of `analytics.stats`.** It
    aggregated `item_scores` in SQL, pinned `discrimination`, `mean_time_ms` and
    `option_distribution` to constants, and emitted one of the four flag reasons
    — while `stats.analyse()` computed all of it, correctly, for the projection
    that `flagged-items` reads. Two implementations of one rule, and the endpoint
    an author actually opens was the poorer one.

    It now gathers responses and calls `analyse()`. Live rather than reading
    `item_stats`, because that projection is a rolling 90-day window refreshed by
    a job: a teacher who ran a mock this morning needs the numbers this morning,
    and the arithmetic is the same function either way.
    """
    from app.modules.analytics import stats as item_stats

    tv_id, numbers = _analysable_version(session, xid, actor)
    _global = org_scope == "global"
    rows = session.execute(text(f"""
        SELECT s.question_id, q.xid AS question_xid, q.type_key,
               qv.xid AS question_version_xid, r.attempt_id,
               bool_and(s.verdict = 'correct') AS correct,
               max(s.raw_response) AS raw_response,
               max(r.raw_score) AS total_score,
               max(t.time_spent_ms) AS time_ms
        FROM item_scores s
        JOIN score_runs r ON r.id = s.score_run_id AND r.is_current
        JOIN attempts a ON a.id = r.attempt_id AND a.mode <> 'preview'
        JOIN questions q ON q.id = s.question_id
        JOIN question_versions qv ON qv.id = s.question_version_id
        LEFT JOIN LATERAL (
            SELECT sum(aa.time_spent_ms) AS time_spent_ms FROM attempt_answers aa
            WHERE aa.attempt_id = a.id
              AND aa.question_version_id = s.question_version_id
        ) t ON true
        WHERE a.test_version_id = :tv {"" if _global else _MINE}
        -- One Response per STUDENT per item, not one per slot. A three-blank
        -- sentence completion is one item that a student either got right or did
        -- not; counting its slots separately would treat one student as three and
        -- make every p-value on the paper a different question's answer.
        GROUP BY s.question_id, q.xid, q.type_key, qv.xid, r.attempt_id
    """).bindparams(tv=tv_id,
                    # Bound only when the clause is there: `bindparams` rejects a
                    # parameter the statement does not mention.
                    **({} if _global else {"orgs": list(actor.org_ids) or [0]}))
    ).mappings().all()

    grouped: dict[str, list] = {}
    meta: dict[str, dict] = {}
    for row in rows:
        key = str(row["question_version_xid"])
        meta.setdefault(key, {"question_xid": str(row["question_xid"]),
                              "type_key": row["type_key"]})
        grouped.setdefault(key, []).append(item_stats.Response(
            user_xid=str(row["attempt_id"]), correct=bool(row["correct"]),
            total_score=float(row["total_score"] or 0),
            raw_response=row["raw_response"],
            # 0 is the column default and what a client that reports no timing
            # sends, so it means "not reported" and not "answered instantly".
            # `analyse` averages what it is given: a confident 0 ms would read as
            # an item every student skipped.
            time_ms=row["time_ms"] or None))

    items = [_item_dto(numbers.get(key, 10_000), meta[key],
                       item_stats.analyse(responses))
             for key, responses in grouped.items()]
    items.sort(key=lambda i: i["number"])
    return {"test_version_xid": str(xid),
            "n_attempts": len({r["attempt_id"] for r in rows}), "items": items}


# `mine` is the default because "how did MY cohort do" is the question an author
# opens this page with; the platform average over every centre that has sat the
# item is a different and less actionable one. An attempt with no org context is
# self-serve practice and belongs to neither centre.
_MINE = "AND a.org_context_id = ANY(:orgs)"


def _item_dto(number: int, meta: dict, stats) -> dict:
    return {
        "number": number, "question_xid": meta["question_xid"],
        "type_key": meta["type_key"], "n_responses": stats.n_responses,
        "p_value": stats.p_value, "discrimination": stats.discrimination,
        "mean_time_ms": stats.mean_time_ms,
        "option_distribution": stats.option_distribution,
        # Most common first. It was `sorted(set(wrong))[:5]` — the five
        # alphabetically-first distinct answers, so the one thing an author
        # opens this page to see could be absent because it starts with 'w'.
        "common_wrong": [{"value": w["value"], "count": w["count"]}
                         for w in stats.common_wrong],
        "flagged": stats.flagged, "flag_reasons": stats.flag_reasons,
    }


def _analysable_version(session: Session, xid: uuid.UUID,
                        actor: Principal) -> tuple[int, dict[str, int]]:
    """Resolve the version, check authority, and number the items as the paper does.

    Three things this did not do. It resolved a bare `WHERE xid = :xid` with no
    scope and no policy call, so any authenticated user could read a competitor
    centre's difficulty analysis — and `common_wrong` is literally a list of what
    students typed. `Action.VIEW_EXPOSURE` is the matching row in the matrix
    ("view exposure / burn stats", teacher and above), and it keeps students out:
    an item's p-value tells you which questions to spend time on.

    And `number` was `enumerate()` over an unordered `GROUP BY`, so it was neither
    the question's number on the paper nor stable between two calls. It comes from
    the composition now, by the same rule `build_snapshot` numbers with — an author
    who reads "question 7 is flagged" needs question 7 to be question 7.
    """
    from app.api.routers.assets import scoped
    from app.api.routers.tests_authoring import _resource, _settings
    from app.modules.content import repo as content_repo
    from app.modules.content.models import Test, TestVersion

    row = session.execute(
        scoped(actor,
               select(TestVersion, Test).join(Test, Test.id == TestVersion.test_id)
               .where(TestVersion.xid == xid), Test)).first()
    if row is None:
        raise NotFound("Test version not found.")
    tv, test = row
    policy.require(actor, Action.VIEW_EXPOSURE, _resource(test, tv.status),
                   org_settings=_settings(session, test))

    numbers: dict[str, int] = {}
    number = 1
    for _section, _group, question in content_repo.load_composition(
            session, tv.id).questions():
        numbers[str(question.xid)] = number
        number += len(question.slot_keys)
    return tv.id, numbers


@analytics_router.get("/content/flagged-items")
def flagged_items(actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> list[dict]:
    """Near-zero p-value, negative discrimination, or a high unanswered rate.

    Negative discrimination especially: strong students getting an item wrong
    more often than weak ones is almost always a bad key, not a hard question.

    `suggested_action` was the string `"review_key"` on every row and `test_title`
    the empty string — so the column that tells an author WHAT to do said the same
    thing about an item nobody could answer as about one everybody could, and the
    column that says where to go said nothing. `stats.suggested_action` has
    distinguished the four cases since it was written.
    """
    from app.modules.analytics import stats as item_stats

    rows = session.execute(text("""
        SELECT s.question_id, q.xid, q.type_key, s.p_value, s.discrimination,
               s.mean_time_ms, s.option_distribution, s.n_responses, s.flag_reasons,
               s.common_wrong, t.title AS test_title, t.number
        FROM item_stats s
        JOIN questions q ON q.id = s.question_id
        -- Where an author would go to fix it, and what it is called when they get
        -- there. A question can sit in several tests; the most recently published
        -- one is the one they are thinking of. "Mock 3, question 7" is a place;
        -- the empty string and a zero, which is what this returned, are not.
        LEFT JOIN LATERAL (
            SELECT tst.title,
                   tvg.number_start + coalesce((
                       SELECT sum(cardinality(qv2.slot_keys))
                       FROM question_group_items qgi2
                       JOIN question_versions qv2 ON qv2.id = qgi2.question_version_id
                       WHERE qgi2.group_version_id = qgi.group_version_id
                         AND qgi2.position < qgi.position
                   ), 0) AS number
            FROM question_group_items qgi
            JOIN test_version_groups tvg
                 ON tvg.group_version_id = qgi.group_version_id
            JOIN test_version_sections sec ON sec.id = tvg.section_id
            JOIN test_versions tv ON tv.id = sec.test_version_id
            JOIN tests tst ON tst.id = tv.test_id
            WHERE qgi.question_version_id = s.question_version_id
            ORDER BY tv.published_at DESC NULLS LAST, tv.id DESC
            LIMIT 1
        ) t ON true
        WHERE s.flagged AND (q.org_id = ANY(:orgs) OR q.visibility = 'platform_global')
        ORDER BY s.p_value NULLS LAST LIMIT 50
    """).bindparams(orgs=list(actor.org_ids) or [0])).mappings().all()
    return [{"number": r["number"] or 0,
             "question_xid": str(r["xid"]), "type_key": r["type_key"],
             "n_responses": r["n_responses"],
             "p_value": float(r["p_value"]) if r["p_value"] is not None else None,
             "discrimination": float(r["discrimination"])
             if r["discrimination"] is not None else None,
             "mean_time_ms": r["mean_time_ms"],
             "option_distribution": dict(r["option_distribution"] or {}),
             "common_wrong": r["common_wrong"], "flagged": True,
             "flag_reasons": list(r["flag_reasons"] or []),
             "test_title": r["test_title"] or "",
             "suggested_action": item_stats.action_for(r["flag_reasons"] or [])}
            for r in rows]


@analytics_router.get("/me/progress")
def my_progress(actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    rows = session.execute(text("""
        SELECT r.band, r.per_section
        FROM score_runs r JOIN attempts a ON a.id = r.attempt_id
        WHERE a.user_id = :u AND r.is_current AND a.mode <> 'preview'
        ORDER BY r.computed_at
    """).bindparams(u=actor.user_id)).mappings().all()
    bands = [float(r["band"]) for r in rows if r["band"] is not None]
    return {"attempts": len(rows),
            "latest_band": bands[-1] if bands else None,
            "best_band": max(bands) if bands else None,
            "by_skill": {}, "weak_types": []}


# ── realtime ─────────────────────────────────────────────────────────

@realtime_router.post("/realtime/ticket")
def realtime_ticket(actor: Principal = Depends(principal)) -> dict:
    """Single-use, 30 s, bound to the user.

    Browsers cannot set headers on a WebSocket handshake, so the access token
    must not go in the query string where it lands in every proxy log. The
    gateway exchanges this ticket for a session and burns it immediately.

    **This used to return a random string it stored nowhere.** Thirty-two bytes
    of `secrets.token_urlsafe`, an `expires_at` computed and discarded, and a
    `_ = settings()` with a comment saying the gateway would store it — so the
    endpoint minted a credential that nothing could ever verify, which is
    invisible for exactly as long as there is no gateway. `platform.realtime`
    stores it now, with the TTL that comment described, and
    `tests/integration/test_realtime.py` proves a ticket is burned by redeeming
    it twice.

    Refuses rather than degrades when Redis is unreachable: a ticket that cannot
    be stored cannot be verified, and the only alternative to a 503 here is a
    gateway that admits an unverifiable one.
    """
    try:
        ticket = rt.mint(actor.user_xid)
    except rt.BusUnavailable:
        raise ServiceUnavailable(
            "The realtime service is not reachable. The rest of the API is "
            "unaffected — retry shortly.", code="realtime_unavailable") from None
    return {"ticket": ticket.token, "url": settings().realtime_url,
            "expires_at": iso(ticket.expires_at)}
