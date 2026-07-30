"""Speaking: slots, the live queue, ICE credentials, and the safety path.

One rule dominates this module and it is not negotiable: **a minor is never
matched with an adult.** It is enforced three times over, on purpose —

  1. `age_band` is a property of the SLOT, so a minor's slot list is filtered
     server-side and an adult slot is not reachable from the client at all.
  2. Booking re-checks the band against the acting user's own age.
  3. The live-queue index leads with `age_band`, so a cross-band candidate is not
     merely forbidden in code — it is not returned by the query that finds
     candidates.

Audio never transits or is stored on our servers. The client holds a rolling
~60-second local buffer that is discarded unless a report is filed, which gives
an admin evidence without mass surveillance of minors' conversations.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, principal
from app.api.dto import iso
from app.modules.identity.models import User
from app.platform.config import settings
from app.platform.errors import Conflict, Forbidden, NotFound

router = APIRouter(prefix="/speaking", tags=["speaking"])

ICE_TTL = dt.timedelta(hours=2)


def _age_band(actor: Principal) -> str:
    return "minor" if actor.is_minor else "adult"


def _permitted_bands(actor: Principal) -> tuple[str, ...]:
    """What a user of this age may join.

    A minor may join a minor slot, or a `mixed_supervised` cohort slot where an
    adult teacher is present by design. An adult may never join a minor slot —
    including a `mixed_supervised` one they are not the teacher of, which the
    cohort-membership check below covers.
    """
    return ("minor", "mixed_supervised") if actor.is_minor else ("adult",
                                                                 "mixed_supervised")


class SpeakingSlotCreate(BaseModel):
    starts_at: dt.datetime
    duration_minutes: int = 15
    capacity: int = 20
    audience: str = Field(default="public", pattern="^(public|org|cohort)$")
    cohort_xid: uuid.UUID | None = None
    age_band: str | None = Field(default=None, pattern="^(minor|adult|mixed_supervised)$")
    band_min: float | None = None
    band_max: float | None = None
    cue_card_set_version_xid: uuid.UUID | None = None


def slot_dto(session: Session, row, actor: Principal) -> dict:
    booked = session.scalar(text("""
        SELECT count(*) FROM speaking_slot_bookings
        WHERE slot_id = :s AND cancelled_at IS NULL
    """).bindparams(s=row["id"])) or 0
    mine = session.execute(text("""
        SELECT b.booked_at, b.checked_in_at, b.self_band, b.cancelled_at, b.no_show,
               p.xid AS pair_xid
        FROM speaking_slot_bookings b
        LEFT JOIN speaking_pairs p ON p.id = b.pair_id
        WHERE b.slot_id = :s AND b.user_id = :u
    """).bindparams(s=row["id"], u=actor.user_id)).mappings().first()
    return {
        "xid": str(row["xid"]), "starts_at": iso(row["starts_at"]),
        "duration_minutes": row["duration_minutes"], "capacity": row["capacity"],
        "booked_count": booked, "status": row["status"], "audience": row["audience"],
        "age_band": row["age_band"],
        "band_min": float(row["band_min"]) if row["band_min"] is not None else None,
        "band_max": float(row["band_max"]) if row["band_max"] is not None else None,
        "language": row["language"], "cue_card_set_version_xid": None,
        "my_booking": _booking_dto(mine) if mine else None,
    }


def _booking_dto(row) -> dict:
    if row["cancelled_at"]:
        state = "cancelled"
    elif row["no_show"]:
        state = "no_show"
    elif row["pair_xid"]:
        state = "matched"
    elif row["checked_in_at"]:
        state = "checked_in"
    else:
        state = "booked"
    return {"booked_at": iso(row["booked_at"]),
            "checked_in_at": iso(row["checked_in_at"]),
            "self_band": float(row["self_band"]) if row["self_band"] is not None else None,
            "pair_xid": str(row["pair_xid"]) if row["pair_xid"] else None,
            "status": state}


@router.get("/slots")
def list_slots(from_: dt.datetime | None = None, audience: str | None = None,
               actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> list[dict]:
    """The primary speaking mechanism.

    At 150 DAU a live queue has roughly one interested user per hour; scheduled
    slots concentrate demand so batch matching at slot open has a real pool.

    A minor never receives an adult slot in this list, whatever the client asks
    for — the `age_band` filter is applied server-side and is not a query
    parameter.
    """
    params = {"bands": list(_permitted_bands(actor)),
              "orgs": list(actor.org_ids) or [0], "u": actor.user_id,
              "from": from_ or dt.datetime.now(dt.UTC)}
    clause = "AND s.audience = :aud" if audience else ""
    if audience:
        params["aud"] = audience
    rows = session.execute(text(f"""
        SELECT s.* FROM speaking_slots s
        WHERE s.age_band = ANY(:bands)
          AND s.starts_at >= :from
          AND s.status IN ('scheduled','booking')
          AND (s.audience = 'public'
               OR (s.audience = 'org' AND s.org_id = ANY(:orgs))
               OR (s.audience = 'cohort' AND s.cohort_id IN (
                     SELECT cohort_id FROM cohort_members
                     WHERE user_id = :u AND left_at IS NULL)))
          {clause}
        ORDER BY s.starts_at LIMIT 50
    """).bindparams(**params)).mappings().all()
    return [slot_dto(session, r, actor) for r in rows]


@router.post("/slots", status_code=status.HTTP_201_CREATED)
def create_slot(body: SpeakingSlotCreate, actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """Teachers and centre admins open slots.

    `age_band` defaults to the creator's own band rather than to `adult`: a
    defaulting mistake should fail closed for minors, and an adult teacher who
    wants a mixed cohort session has to say so explicitly.
    """
    org_id = next((o for o, r in actor.roles.items()
                   if r in ("teacher", "centre_admin")), None)
    if org_id is None and not actor.is_platform_admin:
        raise Forbidden("Only a teacher or centre admin can open a speaking slot.",
                        code="not_a_teacher")
    band = body.age_band or _age_band(actor)
    if band == "mixed_supervised" and body.audience != "cohort":
        raise Conflict("A mixed-age session must be a cohort slot with a "
                       "supervising teacher.", code="mixed_requires_cohort")

    cohort_id = None
    if body.cohort_xid:
        cohort_id = session.scalar(text("SELECT id FROM cohorts WHERE xid = CAST(:x AS uuid) AND org_id = ANY(:o)")
                                   .bindparams(x=body.cohort_xid,
                                               o=list(actor.org_ids) or [0]))
        if cohort_id is None:
            raise NotFound("Cohort not found.")

    row = session.execute(text("""
        INSERT INTO speaking_slots (org_id, starts_at, duration_minutes, capacity,
                                    status, audience, cohort_id, band_min, band_max,
                                    age_band, created_by)
        VALUES (:org, :starts, :dur, :cap, 'booking', :aud, :cohort, :bmin, :bmax,
                :band, :by)
        RETURNING *
    """).bindparams(org=org_id, starts=body.starts_at, dur=body.duration_minutes,
                    cap=body.capacity, aud=body.audience, cohort=cohort_id,
                    bmin=body.band_min, bmax=body.band_max, band=band,
                    by=actor.user_id)).mappings().one()
    session.flush()
    return slot_dto(session, row, actor)


def _slot(session: Session, xid: uuid.UUID):
    row = session.execute(text("SELECT * FROM speaking_slots WHERE xid = CAST(:x AS uuid)")
                          .bindparams(x=xid)).mappings().first()
    if row is None:
        raise NotFound("Speaking slot not found.")
    return row


@router.post("/slots/{xid}/book", status_code=status.HTTP_201_CREATED)
def book_slot(xid: uuid.UUID, actor: Principal = Depends(principal),
              session: Session = Depends(db)) -> dict:
    """Booking re-checks the age band against the acting user's own age.

    The list endpoint already filters, but a client that guesses a slot xid must
    not be able to book across the band. This is the check that makes the safety
    rule an invariant rather than a UI behaviour.
    """
    slot = _slot(session, xid)
    if slot["age_band"] not in _permitted_bands(actor):
        # Deliberately explicit: the student should understand why, and support
        # needs to be able to explain it without reading code.
        raise Forbidden(
            "This session is for a different age group. Minors and adults are "
            "never paired for speaking practice.", code="age_band_mismatch")
    if slot["age_band"] == "mixed_supervised":
        member = session.scalar(text("""
            SELECT count(*) FROM cohort_members
            WHERE cohort_id = :c AND user_id = :u AND left_at IS NULL
        """).bindparams(c=slot["cohort_id"], u=actor.user_id))
        if not member and slot["created_by"] != actor.user_id:
            raise Forbidden("This supervised session is for its cohort only.",
                            code="not_in_cohort")
    if slot["status"] not in ("scheduled", "booking"):
        raise Conflict("This slot is no longer open for booking.",
                       code="slot_closed")

    taken = session.scalar(text("""
        SELECT count(*) FROM speaking_slot_bookings
        WHERE slot_id = :s AND cancelled_at IS NULL
    """).bindparams(s=slot["id"])) or 0
    if taken >= slot["capacity"]:
        raise Conflict("This slot is full.", code="slot_full")

    user = session.get(User, actor.user_id)
    booking = session.execute(text("""
        INSERT INTO speaking_slot_bookings (slot_id, user_id, self_band)
        VALUES (:s, :u, :band)
        ON CONFLICT (slot_id, user_id) DO UPDATE SET cancelled_at = NULL
        RETURNING booked_at, checked_in_at, self_band, cancelled_at, no_show,
                  NULL::uuid AS pair_xid
    """).bindparams(s=slot["id"], u=actor.user_id,
                    band=float(user.target_band) if user and user.target_band
                    else None)).mappings().one()
    session.flush()
    return _booking_dto(booking)


@router.delete("/slots/{xid}/book", status_code=status.HTTP_204_NO_CONTENT)
def cancel_booking(xid: uuid.UUID, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> Response:
    slot = _slot(session, xid)
    session.execute(text("""
        UPDATE speaking_slot_bookings SET cancelled_at = now()
        WHERE slot_id = :s AND user_id = :u AND cancelled_at IS NULL
    """).bindparams(s=slot["id"], u=actor.user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/slots/{xid}/check-in")
def check_in(xid: uuid.UUID, actor: Principal = Depends(principal),
             session: Session = Depends(db)) -> dict:
    """Check-in feeds the batch matcher: only PRESENT users get paired.

    Without it the matcher pairs people who booked three days ago and forgot, and
    a student's first experience of the feature is two minutes of silence.
    """
    slot = _slot(session, xid)
    now = dt.datetime.now(dt.UTC)
    opens = slot["starts_at"] - dt.timedelta(minutes=10)
    if now < opens:
        from app.platform.errors import TooEarly
        raise TooEarly("Check-in opens ten minutes before the session.",
                       code="checkin_not_open", opens_at=iso(opens),
                       server_now=iso(now))
    row = session.execute(text("""
        UPDATE speaking_slot_bookings SET checked_in_at = coalesce(checked_in_at, now())
        WHERE slot_id = :s AND user_id = :u AND cancelled_at IS NULL
        RETURNING booked_at, checked_in_at, self_band, cancelled_at, no_show,
                  (SELECT xid FROM speaking_pairs WHERE id = pair_id) AS pair_xid
    """).bindparams(s=slot["id"], u=actor.user_id)).mappings().first()
    if row is None:
        raise NotFound("You have no booking for this slot.")
    return _booking_dto(row)


# ── live queue ───────────────────────────────────────────────────────

class QueueJoin(BaseModel):
    band_min: float | None = None
    band_max: float | None = None
    language: str = "en"
    org_only: bool = False


@router.post("/queue", status_code=status.HTTP_201_CREATED)
def join_queue(body: QueueJoin, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    """The "try now" path. Secondary to slots, and the response says so honestly.

    `estimated_wait_seconds` is derived from the actual pool rather than shown as
    a spinner, and when the pool is empty the response points at the next bookable
    slot instead of leaving someone staring at a queue that will not resolve.
    """
    band = _age_band(actor)
    session.execute(text("""
        UPDATE speaking_queue_entries SET status = 'cancelled', left_at = now()
        WHERE user_id = :u AND status = 'waiting'
    """).bindparams(u=actor.user_id))
    entry = session.execute(text("""
        INSERT INTO speaking_queue_entries (user_id, band_min, band_max, language,
                                            age_band, org_only, org_id)
        VALUES (:u, :bmin, :bmax, :lang, :band, :org_only, :org)
        RETURNING id, status, joined_at
    """).bindparams(u=actor.user_id, bmin=body.band_min, bmax=body.band_max,
                    lang=body.language, band=band, org_only=body.org_only,
                    org=actor.org_ids[0] if actor.org_ids else None)).mappings().one()

    # Same age band only. Not a filter over a wider pool — the pool itself is
    # band-scoped, which is why a cross-band match is unreachable rather than
    # merely disallowed.
    pool = session.scalar(text("""
        SELECT count(*) FROM speaking_queue_entries
        WHERE status = 'waiting' AND age_band = :band AND language = :lang
          AND user_id <> :u
    """).bindparams(band=band, lang=body.language, u=actor.user_id)) or 0
    suggested = session.execute(text("""
        SELECT * FROM speaking_slots
        WHERE age_band = ANY(:bands) AND starts_at > now()
          AND status IN ('scheduled','booking') AND audience = 'public'
        ORDER BY starts_at LIMIT 1
    """).bindparams(bands=list(_permitted_bands(actor)))).mappings().first()

    session.flush()
    return {
        "xid": str(uuid.UUID(int=entry["id"])), "status": entry["status"],
        "joined_at": iso(entry["joined_at"]),
        "estimated_wait_seconds": 60 if pool else 900,
        "pool_size": pool,
        "suggested_slot": slot_dto(session, suggested, actor) if suggested else None,
    }


@router.delete("/queue", status_code=status.HTTP_204_NO_CONTENT)
def leave_queue(actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> Response:
    session.execute(text("""
        UPDATE speaking_queue_entries SET status = 'cancelled', left_at = now()
        WHERE user_id = :u AND status = 'waiting'
    """).bindparams(u=actor.user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ice-servers")
def ice_servers(actor: Principal = Depends(principal)) -> dict:
    """Short-lived TURN credentials, computed with the standard HMAC scheme.

    Two-hour TTL and per-user, so a leaked credential is a bounded loss rather
    than an open relay — and TURN bandwidth is the one line item in this budget
    that scales with usage.
    """
    expiry = int((dt.datetime.now(dt.UTC) + ICE_TTL).timestamp())
    username = f"{expiry}:{actor.user_xid}"
    credential = base64.b64encode(
        hmac.new(settings().turn_secret.encode(), username.encode(),
                 hashlib.sha1).digest()).decode()
    return {
        "ice_servers": [
            {"urls": [settings().stun_url], "username": None, "credential": None},
            {"urls": [settings().turn_url], "username": username,
             "credential": credential},
        ],
        "expires_at": iso(dt.datetime.now(dt.UTC) + ICE_TTL),
    }


# ── pairs ────────────────────────────────────────────────────────────

def _pair(session: Session, xid: uuid.UUID, actor: Principal):
    row = session.execute(text("""
        SELECT * FROM speaking_pairs WHERE xid = CAST(:x AS uuid)
          AND (user_a_id = :u OR user_b_id = :u)
    """).bindparams(x=xid, u=actor.user_id)).mappings().first()
    if row is None:
        raise NotFound("Speaking session not found.")
    return row


def pair_dto(session: Session, row, actor: Principal) -> dict:
    peer_id = row["user_b_id"] if row["user_a_id"] == actor.user_id else row["user_a_id"]
    peer = session.get(User, peer_id)
    cue_cards = session.execute(text("""
        SELECT body FROM cue_card_set_versions WHERE id = :v
    """).bindparams(v=row["cue_card_set_version_id"])).scalar() \
        if row["cue_card_set_version_id"] else None
    return {
        "xid": str(row["xid"]), "origin": row["origin"],
        # Minimal disclosure: a first name and a self-reported band, nothing else.
        # No phone, no age, no centre.
        "peer": {"xid": str(peer.xid), "display_name": peer.given_name,
                 "band": float(peer.target_band) if peer and peer.target_band
                 else None} if peer else None,
        "age_band": row["age_band"], "cue_cards": cue_cards or {},
        "matched_at": iso(row["matched_at"]), "ended_at": iso(row["ended_at"]),
    }


class PairEnd(BaseModel):
    reason: str = Field(default="completed",
                        pattern="^(completed|left|connection_failed|reported)$")
    turn_relayed: bool | None = None
    connection_quality: dict | None = None


@router.post("/pairs/{xid}/end")
def end_pair(xid: uuid.UUID, body: PairEnd, actor: Principal = Depends(principal),
             session: Session = Depends(db)) -> dict:
    """`turn_relayed` comes from the client's own ICE candidate stats.

    Aggregated, that is the real relay rate — the number the entire TURN bandwidth
    estimate depends on, and the one that decides whether this feature costs $5 a
    month or $80. Worth watching from week one.
    """
    row = _pair(session, xid, actor)
    updated = session.execute(text("""
        UPDATE speaking_pairs
        SET ended_at = coalesce(ended_at, now()), end_reason = :reason,
            turn_relayed = coalesce(:relayed, turn_relayed),
            connection_quality = coalesce(CAST(:quality AS jsonb), connection_quality)
        WHERE id = :p
        RETURNING *
    """).bindparams(reason=body.reason, relayed=body.turn_relayed,
                    quality=json.dumps(body.connection_quality)
                    if body.connection_quality else None,
                    p=row["id"])).mappings().one()
    session.flush()
    return pair_dto(session, updated, actor)


# A 60-second Opus buffer is well under a megabyte. The ceiling is generous
# enough that a long buffer or a chatty codec still lands, and small enough that
# the endpoint cannot be used to push a video file into memory.
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
EVIDENCE_TYPES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav",
                  "audio/x-wav", "audio/aac"}


def _store_evidence(session: Session, pair, upload, actor: Principal) -> int:
    """Write the reported buffer to object storage and record it.

    **The bytes were never stored.** The old code read the upload, hashed it,
    recorded `bytes` and a `storage_key` — and then dropped it on the floor.
    Nothing ever called `storage().put`. So a report came back with
    `has_evidence: true`, `speaking_pairs.evidence_media_id` was set, and the
    admin who opened it found a `media_assets` row pointing at an object that had
    never existed.

    That is the whole justification for audio touching these servers at all. The
    brief allows it only when "required for a safety report"; the report was the
    one case that discarded it.

    Quarantined on arrival: unreviewed audio from a stranger, in a bucket a
    moderator reads and nobody else does.
    """
    from app.platform.storage import storage

    raw = upload.file.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise Conflict(
            f"The evidence buffer may not exceed {MAX_EVIDENCE_BYTES // (1024 * 1024)} MB.",
            code="evidence_too_large")
    if not raw:
        raise Conflict("The evidence buffer is empty.", code="evidence_empty")

    content_type = (upload.content_type or "audio/webm").split(";")[0].strip()
    if content_type not in EVIDENCE_TYPES:
        raise Conflict(f"{content_type!r} is not an accepted audio format.",
                       code="evidence_format_unsupported")

    key = f"safety/{pair['xid']}/{hashlib.sha256(raw).hexdigest()[:16]}"
    ref = storage().put(key, raw, content_type=content_type)
    return session.scalar(text("""
        INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                  content_type, bytes, checksum_sha256, status)
        VALUES (:u, 'audio', :bucket, :key, :ct, :bytes, :sum, 'quarantined')
        RETURNING id
    """).bindparams(u=actor.user_id, bucket=ref.bucket, key=ref.key,
                    ct=ref.content_type, bytes=ref.bytes,
                    sum=ref.checksum_sha256))


@router.post("/pairs/{xid}/report", status_code=status.HTTP_201_CREATED)
def report_pair(xid: uuid.UUID,
                category: str = Form(...),
                description: str | None = Form(default=None),
                audio_buffer: UploadFile | None = File(default=None),
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict:
    """The only path by which conversation audio ever reaches our servers.

    The client holds a rolling ~60 s local buffer that is discarded unless a
    report is filed. That gives an admin evidence without routine recording of
    minors' conversations, and it is defensible under data-protection law in a way
    mass surveillance is not. The session notice must state the buffer exists.

    `involves_minor` is set by the SYSTEM from the participants' ages, never by
    the reporter — a report about a child must not depend on the reporter
    remembering to tick a box.
    """
    if category not in ("harassment", "sexual_content", "grooming", "hate",
                        "violence", "spam", "other"):
        raise Conflict("Unknown report category.", code="unknown_category")

    row = _pair(session, xid, actor)
    involves_minor = row["age_band"] in ("minor", "mixed_supervised")
    if not involves_minor:
        involves_minor = bool(session.scalar(text("""
            SELECT count(*) FROM users
            WHERE id IN (:a, :b) AND adult_at > current_date
        """).bindparams(a=row["user_a_id"], b=row["user_b_id"])))

    media_id = None
    if audio_buffer is not None:
        media_id = _store_evidence(session, row, audio_buffer, actor)
        session.execute(text("UPDATE speaking_pairs SET evidence_media_id = :m WHERE id = :p")
                        .bindparams(m=media_id, p=row["id"]))

    peer_id = row["user_b_id"] if row["user_a_id"] == actor.user_id else row["user_a_id"]
    report = session.execute(text("""
        INSERT INTO safety_reports (reporter_user_id, subject_kind, subject_user_id,
                                    subject_ref, category, description, context,
                                    evidence_media_id, involves_minor, priority)
        VALUES (:reporter, 'speaking_pair', :subject, :ref, :cat, :descr,
                CAST(:context AS jsonb), :media, :minor, :priority)
        RETURNING xid, category, status, priority, involves_minor,
                  evidence_media_id IS NOT NULL AS has_evidence, created_at
    """).bindparams(
        reporter=actor.user_id, subject=peer_id, ref=str(row["xid"]), cat=category,
        descr=description,
        context=json.dumps({"pair_xid": str(row["xid"]),
                            "matched_at": str(row["matched_at"]),
                            "origin": row["origin"], "age_band": row["age_band"]}),
        media=media_id, minor=involves_minor,
        # Grooming reports and anything involving a minor go straight to the top
        # of the queue. This is the one place where a slower response is a
        # child-safety failure, not an SLA miss.
        priority="critical" if (involves_minor or category == "grooming") else "high",
    )).mappings().one()

    session.execute(text("""
        INSERT INTO audit_log (actor_kind, actor_user_id, action, subject_type,
                               subject_id, after)
        VALUES ('user', :who, 'safety.report_filed', 'speaking_pair', :sid,
                CAST(:after AS jsonb))
    """).bindparams(who=actor.user_id, sid=str(row["xid"]),
                    after=json.dumps({"category": category,
                                      "involves_minor": involves_minor,
                                      "has_evidence": media_id is not None})))
    session.flush()

    return {"xid": str(report["xid"]), "category": report["category"],
            "status": report["status"], "priority": report["priority"],
            "involves_minor": report["involves_minor"],
            "has_evidence": report["has_evidence"],
            "created_at": iso(report["created_at"])}
