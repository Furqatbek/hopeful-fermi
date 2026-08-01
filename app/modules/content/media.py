"""Media: resumable upload, ingest, and delivery. PRIVATE to content.

Three things here are decisions rather than plumbing.

**Bytes never touch the app server on upload.** The client PUTs parts straight to
object storage using presigned URLs; we hand out the URLs and record what came
back. On a 4 vCPU box shared with the exam endpoints, proxying a 40 MB WAV
through gunicorn during a mock is how you turn one teacher's upload into forty
students' timeouts.

**The master is kept forever; delivery is derived.** `media_assets.derived_from_id`
points a delivery file back at the upload it came from, so changing codec or
loudness target in a year is a re-transcode rather than an email asking two
hundred teachers to upload everything again.

**A copyright attestation is captured per upload**, with the statement's hash and
the uploader's identity. "They ticked a box" is not a defence; "they ticked THIS
box, whose text hashed to X, at this time, from this address" is. Assume some
centre will upload a published Cambridge paper, and design so the evidence exists
before anyone asks for it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform.errors import Conflict, NotFound, ValidationFailed
from app.platform.findings import Report
from app.platform.storage import PART_SIZE, ObjectRef, Storage

log = structlog.get_logger()

UPLOAD_TTL = dt.timedelta(hours=6)
PART_URL_TTL = 3600

# What a teacher can actually produce. Anything else is a mistake worth catching
# at the door rather than after a 40 MB upload on a metered connection.
ALLOWED_AUDIO = {
    "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3",
    "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/ogg",
    "audio/opus", "audio/flac", "audio/x-flac", "audio/webm",
}
ALLOWED_IMAGE = {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}
ALLOWED_VIDEO = {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}
ALLOWED_DOCUMENT = {"application/pdf", "text/plain",
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"}
ALLOWED_ARCHIVE = {"application/zip", "application/x-zip-compressed"}
MAX_AUDIO_BYTES = 500 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

#: kind -> (permitted content types, byte ceiling). Every kind
#: `media_assets.kind` allows appears here.
#:
#: **This was `ALLOWED_AUDIO if kind == "audio" else ALLOWED_IMAGE`**, so a
#: `document` or an `archive` — both permitted by the table's CHECK constraint —
#: was validated against the IMAGE allowlist and refused with "application/pdf is
#: not a supported document format. Supported: image/jpeg, image/png…". Only the
#: audio path has a caller today, which is why nobody had met it; a binary
#: either/or over a column with four values is a bug waiting for the second
#: caller.
KINDS: dict[str, tuple[frozenset[str], int]] = {
    "audio": (frozenset(ALLOWED_AUDIO), MAX_AUDIO_BYTES),
    "image": (frozenset(ALLOWED_IMAGE), MAX_IMAGE_BYTES),
    # Stored and served as uploaded. No transcode: the audio pipeline exists
    # because exam audio must be loudness-normalised and play-once, which are
    # properties of a listening section rather than of a file, and no equivalent
    # requirement has been stated for video. Two gigabytes because a phone
    # recording of a speaking lesson is large and re-encoding it is the
    # uploader's problem, not this server's.
    "video": (frozenset(ALLOWED_VIDEO), MAX_VIDEO_BYTES),
    "document": (frozenset(ALLOWED_DOCUMENT), MAX_DOCUMENT_BYTES),
    "archive": (frozenset(ALLOWED_ARCHIVE), MAX_ARCHIVE_BYTES),
}

# The exact wording an uploader affirms. Versioned and hashed, because the
# question a lawyer will ask is "what did they agree to", not "did they agree".
ATTESTATION_STATEMENTS = {
    "upload": {
        "version": "1",
        "text": ("I confirm this material is my own original work, or that I hold "
                 "a licence permitting its use on this platform, and that "
                 "uploading it does not infringe anyone's copyright. I understand "
                 "this affirmation is recorded with my account."),
    },
}
VALID_CLAIMS = {"original", "licensed", "public_domain", "permitted_excerpt"}

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class UploadSession:
    xid: str
    media_xid: str
    part_size: int
    expected_bytes: int
    parts_received: list[int]
    presigned_urls: list[dict[str, Any]]
    expires_at: dt.datetime
    status: str


def open_upload(session: Session, storage: Storage, *, kind: str, filename: str,
                content_type: str, bytes_: int, owner_user_id: int,
                org_id: int | None, attestation: dict,
                now: dt.datetime,
                declared_checksum: str | None = None) -> tuple[int, UploadSession]:
    """Create the asset row and a resumable multipart upload.

    Validation happens BEFORE the upload starts. Rejecting a 40 MB file after it
    has been sent, on a connection the teacher is paying for by the megabyte, is
    the kind of thing that loses a centre.

    `declared_checksum` is what the client says its file hashes to. It is stored,
    not trusted: `ingest_audio` recomputes from the bytes that actually arrived
    and refuses the asset if the two differ. `AudioTrackCreate` has declared this
    field since the contract was drafted and the router dropped it on the floor.
    """
    _validate_request(kind, content_type, bytes_, attestation, declared_checksum)
    declared_checksum = _normalise_checksum(declared_checksum)

    key = _storage_key(kind, owner_user_id, filename, now)
    asset_id, asset_xid = _insert_asset(
        session, kind=kind, key=key, content_type=content_type, bytes_=bytes_,
        owner_user_id=owner_user_id, org_id=org_id)

    upload_id = storage.create_multipart(key, content_type=content_type)
    part_count = max(1, math.ceil(bytes_ / PART_SIZE))
    expires = now + UPLOAD_TTL

    row = session.execute(text("""
        INSERT INTO media_uploads (media_asset_id, provider_upload_id, part_size,
                                   expected_bytes, declared_checksum_sha256,
                                   status, created_by, expires_at)
        VALUES (:a, :p, :size, :bytes, :sum, 'open', :u, :exp)
        RETURNING id, xid
    """).bindparams(a=asset_id, p=upload_id, size=PART_SIZE, bytes=bytes_,
                    sum=declared_checksum, u=owner_user_id,
                    exp=expires)).mappings().one()

    record_attestation(session, subject_type="media_asset", subject_id=asset_id,
                       user_id=owner_user_id, org_id=org_id,
                       attestation=attestation)

    parts = storage.presign_parts(key, upload_id, count=part_count,
                                  ttl_seconds=PART_URL_TTL)
    log.info("upload_opened", media_xid=str(asset_xid), parts=part_count,
             bytes=bytes_)
    return asset_id, UploadSession(
        xid=str(row["xid"]), media_xid=str(asset_xid), part_size=PART_SIZE,
        expected_bytes=bytes_, parts_received=[],
        presigned_urls=[{"n": p.part_number, "url": p.url,
                         "offset": p.offset, "length": p.length} for p in parts],
        expires_at=expires, status="open")


def _normalise_checksum(value: str | None) -> str | None:
    """Hex is case-insensitive; a client shouting it is not an error."""
    cleaned = (value or "").strip().lower()
    return cleaned or None


def _validate_request(kind: str, content_type: str, bytes_: int,
                      attestation: dict, declared_checksum: str | None = None) -> None:
    report = Report()
    if kind not in KINDS:
        # Refused rather than defaulted. `media_assets.kind` has a CHECK
        # constraint, so an unknown kind would fail at INSERT anyway — but it
        # would fail after the upload was opened, as a 500, having told the
        # teacher nothing.
        report.add("MEDIA_KIND_UNSUPPORTED",
                   f"{kind!r} is not a kind of file this platform stores.",
                   path="kind",
                   fix_hint=f"Supported: {', '.join(sorted(KINDS))}.")
        raise ValidationFailed("This upload was refused.", report.errors)
    allowed, limit = KINDS[kind]

    if content_type.split(";")[0].strip().lower() not in allowed:
        report.add("MEDIA_TYPE_UNSUPPORTED",
                   f"{content_type} is not a supported {kind} format.",
                   path="content_type",
                   fix_hint=f"Supported: {', '.join(sorted(allowed))}.")
    if bytes_ <= 0:
        report.add("MEDIA_EMPTY", "This file is empty.", path="bytes",
                   fix_hint="Check the export completed before uploading.")
    if bytes_ > limit:
        report.add("MEDIA_TOO_LARGE",
                   f"This file is {bytes_ // (1024 * 1024)} MB; the limit is "
                   f"{limit // (1024 * 1024)} MB.", path="bytes",
                   fix_hint="Export at a lower bitrate, or split the recording.")

    # Checked at the door, like everything else here. A malformed digest is a
    # client bug, and the alternative — store it and let every upload fail
    # thirty seconds later in a worker — spends the teacher's bandwidth to
    # deliver the same news.
    cleaned = _normalise_checksum(declared_checksum)
    if cleaned is not None and not _HEX_SHA256.fullmatch(cleaned):
        report.add("CHECKSUM_MALFORMED",
                   "checksum_sha256 must be 64 hexadecimal characters.",
                   path="checksum_sha256",
                   fix_hint="Send the sha256 digest as hex, or omit the field.")

    claim = (attestation or {}).get("claim")
    if claim not in VALID_CLAIMS:
        # Refused, not defaulted. A missing attestation that quietly becomes
        # "original" is worse than no attestation at all — it manufactures a
        # claim the uploader never made, which is the opposite of evidence.
        report.add("ATTESTATION_REQUIRED",
                   "A copyright attestation is required for every upload.",
                   path="attestation.claim",
                   fix_hint=f"One of: {', '.join(sorted(VALID_CLAIMS))}.")
    if claim == "licensed" and not (attestation or {}).get("licence_note"):
        report.add("LICENCE_NOTE_REQUIRED",
                   "A licensed upload must say what the licence is.",
                   path="attestation.licence_note",
                   fix_hint="Name the licence or the agreement.")

    if report.errors:
        raise ValidationFailed("This upload was refused.", report.errors)


def record_attestation(session: Session, *, subject_type: str, subject_id: int,
                       user_id: int, org_id: int | None, attestation: dict,
                       ip: str | None = None,
                       user_agent: str | None = None) -> None:
    """The evidence row. Stores the statement's HASH, not a boolean."""
    statement = ATTESTATION_STATEMENTS["upload"]
    # `ip` is `inet` in the schema, and a proxy can put anything in the header it
    # comes from. An unparseable value must not lose the attestation — the claim
    # is the evidence, the address is context.
    ip = _as_inet(ip)
    session.execute(text("""
        INSERT INTO content_attestations (subject_type, subject_id, user_id, org_id,
                                          claim, licence_note, statement_key,
                                          statement_version, statement_hash, ip,
                                          user_agent_hash)
        VALUES (:st, :sid, :u, :o, :claim, :note, 'upload', :ver, :hash,
                CAST(:ip AS inet), :ua)
    """).bindparams(
        st=subject_type, sid=subject_id, u=user_id, o=org_id,
        claim=attestation["claim"], note=attestation.get("licence_note"),
        ver=statement["version"],
        hash=hashlib.sha256(statement["text"].encode()).hexdigest(),
        ip=ip,
        ua=hashlib.sha256((user_agent or "").encode()).hexdigest()[:32]))


def _as_inet(value: str | None) -> str | None:
    import ipaddress

    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def resume(session: Session, storage: Storage, upload_xid, user_id: int,
           now: dt.datetime) -> UploadSession:
    """Which parts we already hold. This is what makes a 40 MB upload survivable
    on a connection that drops every ninety seconds."""
    row = _upload(session, upload_xid, user_id)
    received = [int(p["n"]) for p in (row["parts"] or [])]
    total = max(1, math.ceil((row["expected_bytes"] or 0) / row["part_size"]))
    outstanding = [n for n in range(1, total + 1) if n not in received]

    parts = storage.presign_parts(row["storage_key"], row["provider_upload_id"],
                                  count=total, ttl_seconds=PART_URL_TTL)
    return UploadSession(
        xid=str(row["xid"]), media_xid=str(row["media_xid"]),
        part_size=row["part_size"], expected_bytes=row["expected_bytes"] or 0,
        parts_received=received,
        # Only the parts still owed. Re-sending URLs for parts already stored
        # invites a client to upload them again on a metered connection.
        presigned_urls=[{"n": p.part_number, "url": p.url,
                         "offset": p.offset, "length": p.length}
                        for p in parts if p.part_number in outstanding],
        expires_at=row["expires_at"], status=row["status"])


def complete(session: Session, storage: Storage, upload_xid, user_id: int,
             parts: list[dict], now: dt.datetime) -> dict:
    """Assemble the object and hand off to ingest.

    Idempotent: a second call on a completed upload returns the same answer
    rather than trying to complete a multipart that no longer exists.
    """
    row = _upload(session, upload_xid, user_id)
    if row["status"] == "completed":
        return _asset_dto(session, row["media_asset_id"])
    if row["status"] in ("aborted", "expired"):
        raise Conflict(f"This upload was {row['status']} and cannot be completed.",
                       code="upload_not_open")
    if not parts:
        raise Conflict("No parts were reported for this upload.",
                       code="upload_no_parts")

    ref = storage.complete_multipart(row["storage_key"], row["provider_upload_id"],
                                     parts)
    session.execute(text("""
        UPDATE media_uploads
        SET status = 'completed', parts = CAST(:parts AS jsonb),
            received_bytes = :bytes, updated_at = :now
        WHERE id = :id
    """).bindparams(parts=json.dumps(parts), bytes=ref.bytes, now=now,
                    id=row["id"]))
    session.execute(text("""
        UPDATE media_assets SET bytes = :bytes, status = 'processing', updated_at = :now
        WHERE id = :id
    """).bindparams(bytes=ref.bytes, now=now, id=row["media_asset_id"]))

    # In the SAME transaction as the status change, so an upload that completed
    # without an ingest job is not representable.
    emit(session, "media.uploaded", str(row["media_xid"]),
         {"media_asset_id": row["media_asset_id"]})
    session.flush()
    log.info("upload_completed", media_xid=str(row["media_xid"]), bytes=ref.bytes)
    return _asset_dto(session, row["media_asset_id"])


def abort(session: Session, storage: Storage, upload_xid, user_id: int) -> None:
    row = _upload(session, upload_xid, user_id)
    if row["status"] == "open":
        storage.abort_multipart(row["storage_key"], row["provider_upload_id"])
    session.execute(text("""
        UPDATE media_uploads SET status = 'aborted' WHERE id = :id
    """).bindparams(id=row["id"]))
    session.execute(text("""
        UPDATE media_assets SET status = 'removed' WHERE id = :id AND status = 'uploading'
    """).bindparams(id=row["media_asset_id"]))


# ── ingest ───────────────────────────────────────────────────────────

def ingest_audio(session: Session, storage: Storage, media_asset_id: int, *,
                 now: dt.datetime, scratch) -> dict:
    """Probe, validate, normalise, encode, store the derived file.

    Idempotent by status guard: an asset already `ready` is a redelivery and
    returns without re-encoding. That matters — the relay is at-least-once and
    re-encoding a 30-minute file is a minute of CPU each time.
    """
    from pathlib import Path

    from app.platform import audio as ffmpeg

    row = session.execute(text("""
        SELECT a.id, a.xid, a.bucket, a.storage_key, a.content_type, a.kind,
               a.org_id, a.owner_user_id, a.status,
               u.expected_bytes, u.declared_checksum_sha256
        FROM media_assets a
        LEFT JOIN media_uploads u ON u.media_asset_id = a.id
        WHERE a.id = :id
        ORDER BY u.id DESC
        LIMIT 1
    """).bindparams(id=media_asset_id)).mappings().first()
    if row is None:
        raise NotFound("Media asset not found.")
    if row["status"] == "ready":
        return _asset_dto(session, media_asset_id)
    if row["kind"] != "audio":
        return _asset_dto(session, media_asset_id)

    master = Path(scratch) / f"master-{row['xid']}"
    output = Path(scratch) / f"delivery-{row['xid']}{ffmpeg.DELIVERY_SUFFIX}"
    try:
        storage.download(row["storage_key"], master)
        # Before ffmpeg touches it. Both of these are cheap, both catch a file
        # that is not the one the teacher chose, and transcoding a broken master
        # for a minute to produce a broken delivery helps nobody.
        if (arrived := _declaration_broken(row, master)) is not None:
            return _fail(session, media_asset_id, arrived, now)
        checksum = _checksum(master)
        if (mismatch := _checksum_broken(row, checksum)) is not None:
            return _fail(session, media_asset_id, mismatch, now)

        probe = ffmpeg.probe(master)
        measured = ffmpeg.measure(master)

        problems = ffmpeg.validate(probe, measured)
        if problems:
            return _fail(session, media_asset_id, "; ".join(problems), now)

        result = ffmpeg.transcode(master, output, measured=measured)
        derived_key = _derived_key(row["storage_key"])
        ref = storage.upload_file(derived_key, output,
                                  content_type=ffmpeg.DELIVERY_CONTENT_TYPE)

        derived_id = _insert_derived(session, parent=row, ref=ref, result=result,
                                     now=now)
        session.execute(text("""
            UPDATE media_assets
            SET status = 'ready', duration_ms = :ms, sample_rate = :sr,
                channels = :ch, loudness_lufs = :lufs, checksum_sha256 = :sum,
                processing_error = NULL, updated_at = :now
            WHERE id = :id
        """).bindparams(ms=probe.duration_ms, sr=probe.sample_rate,
                        ch=probe.channels,
                        lufs=round(measured.integrated_lufs, 2),
                        sum=checksum, now=now, id=media_asset_id))
        _sync_track(session, media_asset_id, derived_id, probe.duration_ms, now)
        session.flush()
        log.info("audio_ingested", media_asset_id=media_asset_id,
                 derived_id=derived_id, duration_ms=probe.duration_ms)
        return _asset_dto(session, media_asset_id)

    except ffmpeg.AudioError as exc:
        # A failed transcode is content feedback, not an incident. The author
        # needs to know what was wrong with THEIR file, so the message is stored
        # where the API can show it rather than only in a log the teacher will
        # never see.
        log.warning("audio_ingest_failed", media_asset_id=media_asset_id,
                    error=exc.message, detail=exc.detail[:300])
        return _fail(session, media_asset_id, exc.message, now)
    finally:
        master.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def _declaration_broken(row, master) -> str | None:
    """Did all the bytes arrive?

    `media_uploads` records `expected_bytes` at open and `received_bytes` at
    complete, **side by side, and nothing compared them.** A teacher whose
    connection drops after part 1 of 2 gets a client that reports the parts it
    managed, an upload marked `completed`, and a listening section silently half
    the length it should be. The exam then plays 55 seconds of a 90-second
    recording, questions 8-14 refer to audio nobody heard, and the server — the
    sole authority on scoring — marks them wrong.

    Checked against the master on disk rather than against `received_bytes`,
    because the file is what ffmpeg is about to read. The two agree when the
    store is behaving, and the point of the check is the case where something is
    not.
    """
    expected = row["expected_bytes"]
    if not expected:
        return None
    arrived = master.stat().st_size
    if arrived == expected:
        return None
    short = expected - arrived
    return (f"This upload is incomplete: {arrived} bytes arrived of the "
            f"{expected} declared"
            + (f" ({short} missing)." if short > 0 else ", which is more.")
            + " Upload the file again.")


def _checksum_broken(row, computed: str) -> str | None:
    """Is it the same file?

    Integrity, not authenticity — the client picks both the bytes and the digest,
    so this proves nothing about who made the recording. What it catches is the
    failure the byte count cannot: parts PUT to the wrong presigned URL. That
    assembles to exactly the right length out of exactly the right pieces, in the
    wrong order, and ffmpeg will happily transcode the result into a listening
    section whose sentences are shuffled.
    """
    declared = _normalise_checksum(row["declared_checksum_sha256"])
    if declared is None or declared == computed:
        return None
    return ("This file does not match the checksum declared when the upload was "
            f"opened (declared {declared[:12]}…, received {computed[:12]}…). "
            "Something changed it in transit; upload it again.")


def _fail(session: Session, media_asset_id: int, message: str,
          now: dt.datetime) -> dict:
    session.execute(text("""
        UPDATE media_assets SET status = 'failed', processing_error = :why,
                                updated_at = :now
        WHERE id = :id
    """).bindparams(why=message[:1000], now=now, id=media_asset_id))
    session.execute(text("""
        UPDATE audio_tracks SET status = 'failed'
        WHERE master_media_id = :id OR delivery_media_id = :id
    """).bindparams(id=media_asset_id))
    session.flush()
    return _asset_dto(session, media_asset_id)


def _insert_derived(session: Session, *, parent, ref: ObjectRef, result,
                    now: dt.datetime) -> int:
    return session.scalar(text("""
        INSERT INTO media_assets (org_id, owner_user_id, kind, bucket, storage_key,
                                  content_type, bytes, checksum_sha256, duration_ms,
                                  sample_rate, channels, loudness_lufs,
                                  derived_from_id, status)
        VALUES (:org, :owner, 'audio', :bucket, :key, :ct, :bytes, :sum, :ms, :sr,
                :ch, :lufs, :parent, 'ready')
        RETURNING id
    """).bindparams(
        org=parent["org_id"], owner=parent["owner_user_id"], bucket=ref.bucket,
        key=ref.key, ct=ref.content_type, bytes=ref.bytes, sum=ref.checksum_sha256,
        ms=result.probe.duration_ms, sr=result.probe.sample_rate,
        ch=result.probe.channels,
        # The DELIVERY file's loudness is the target, by construction — that is
        # what the normalisation was for.
        lufs=_target_lufs(), parent=parent["id"]))


def _sync_track(session: Session, master_id: int, delivery_id: int,
                duration_ms: int, now: dt.datetime) -> None:
    """Point the authoring-facing `audio_tracks` row at the finished delivery.

    An author sees "processing" until this runs and "ready" after, which is the
    only signal they get that their upload worked.
    """
    session.execute(text("""
        UPDATE audio_tracks
        SET status = 'ready', delivery_media_id = :delivery, duration_ms = :ms,
            loudness_lufs = :lufs, updated_at = :now
        WHERE master_media_id = :master
    """).bindparams(delivery=delivery_id, ms=duration_ms, lufs=_target_lufs(),
                    now=now, master=master_id))


def _target_lufs() -> float:
    from app.platform import audio as ffmpeg

    return ffmpeg.TARGET_LUFS


# ── delivery ─────────────────────────────────────────────────────────

def deliverable(session: Session, media_xid) -> dict:
    """Resolve a media xid to the object a student should actually receive.

    An author's xid names the MASTER; students must never be sent a 40 MB WAV, so
    this follows `derived_from_id` to the delivery file when one exists.
    """
    row = session.execute(text("""
        SELECT d.id, d.xid, d.bucket, d.storage_key, d.content_type, d.bytes,
               d.status, d.duration_ms
        FROM media_assets m
        LEFT JOIN media_assets d ON d.derived_from_id = m.id AND d.status = 'ready'
        WHERE m.xid = CAST(:x AS uuid)
    """).bindparams(x=str(media_xid))).mappings().first()
    if row is None or row["id"] is None:
        master = session.execute(text("""
            SELECT id, xid, bucket, storage_key, content_type, bytes, status,
                   duration_ms
            FROM media_assets WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=str(media_xid))).mappings().first()
        if master is None:
            raise NotFound("Media not found.")
        if master["status"] != "ready":
            raise Conflict("This media is still being processed.",
                           code="media_not_ready", status=master["status"])
        return dict(master)
    return dict(row)


def _upload(session: Session, upload_xid, user_id: int):
    row = session.execute(text("""
        SELECT u.id, u.xid, u.media_asset_id, u.provider_upload_id, u.part_size,
               u.expected_bytes, u.parts, u.status, u.expires_at,
               a.storage_key, a.xid AS media_xid
        FROM media_uploads u JOIN media_assets a ON a.id = u.media_asset_id
        WHERE u.xid = CAST(:x AS uuid) AND u.created_by = :u
    """).bindparams(x=str(upload_xid), u=user_id)).mappings().first()
    if row is None:
        raise NotFound("Upload not found.")
    return row


def _insert_asset(session: Session, *, kind: str, key: str, content_type: str,
                  bytes_: int, owner_user_id: int, org_id: int | None):
    row = session.execute(text("""
        INSERT INTO media_assets (org_id, owner_user_id, kind, bucket, storage_key,
                                  content_type, bytes, checksum_sha256, status)
        VALUES (:org, :owner, :kind, :bucket, :key, :ct, :bytes, '', 'uploading')
        RETURNING id, xid
    """).bindparams(org=org_id, owner=owner_user_id, kind=kind,
                    bucket=_bucket(), key=key, ct=content_type,
                    bytes=bytes_)).mappings().one()
    return row["id"], row["xid"]


def _bucket() -> str:
    from app.platform.config import settings

    return settings().s3_bucket


def _storage_key(kind: str, owner_user_id: int, filename: str,
                 now: dt.datetime) -> str:
    """Date-partitioned, owner-scoped, with a random component.

    Not derived from the filename alone: two teachers both uploading
    `section1.wav` must not collide, and a guessable key is a way around the
    grant check for anyone who can reach the bucket directly.
    """
    import secrets

    suffix = ("." + filename.rsplit(".", 1)[-1].lower()[:8]) if "." in filename else ""
    return (f"{kind}/{now:%Y/%m}/{owner_user_id}/"
            f"{secrets.token_urlsafe(16)}{suffix}")


def _derived_key(master_key: str) -> str:
    from app.platform import audio as ffmpeg

    stem = master_key.rsplit(".", 1)[0]
    return f"{stem}.delivery{ffmpeg.DELIVERY_SUFFIX}"


def _checksum(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_dto(session: Session, media_asset_id: int) -> dict:
    row = session.execute(text("""
        SELECT xid, kind, status, content_type, bytes, duration_ms, width, height,
               loudness_lufs, processing_error
        FROM media_assets WHERE id = :id
    """).bindparams(id=media_asset_id)).mappings().one()
    return {"xid": str(row["xid"]), "kind": row["kind"], "status": row["status"],
            "content_type": row["content_type"], "bytes": row["bytes"],
            "duration_ms": row["duration_ms"], "width": row["width"],
            "height": row["height"],
            "loudness_lufs": float(row["loudness_lufs"])
            if row["loudness_lufs"] is not None else None,
            "processing_error": row["processing_error"]}


def emit(session: Session, event_type: str, aggregate_id: str,
         payload: dict) -> None:
    session.execute(text("""
        INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
        VALUES ('media_asset', :agg, :type, CAST(:payload AS jsonb))
    """).bindparams(agg=aggregate_id, type=event_type,
                    payload=json.dumps(payload)))
