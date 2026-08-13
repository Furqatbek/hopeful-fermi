"""Seed a LISTENING paper with real playable audio and a multi-select question.

    DATABASE_URL=... python scripts/seed_listening_demo.py

Adds a second paper to the org `scripts/seed_demo.py` created, so run that one
first. It exists because the two hardest things to verify in the student client
cannot be reached from the reading demo at all:

  * a section that carries audio, which needs a real `media_assets` row and real
    bytes on disk for the play-once grant to resolve and stream;
  * an `mcq_multi` question, the one type whose answer is a list.

The audio is generated here rather than committed: a few seconds of a quiet
sine tone, written as a PCM WAV with the standard library. It is genuinely
playable in a browser, which is the point — a zero-byte placeholder would let
the grant succeed and the element fail, testing the half that was never in doubt.

NOT for production.
"""
import datetime as dt
import hashlib
import math
import os
import struct
import uuid
import wave
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.modules.content import repo as content_repo
from app.modules.content.models import (
    AnswerKeyVersion,
    AudioTrack,
    BandMap,
    BandMapVersion,
    Question,
    QuestionGroup,
    QuestionGroupItem,
    QuestionGroupVersion,
    QuestionVersion,
    Test,
    TestVersion,
    TestVersionGroup,
    TestVersionSection,
)
from app.modules.exam.models import Assignment, AssignmentTarget
from app.modules.identity.models import Organization, User
from app.platform.config import settings

NOW = dt.datetime.now(dt.UTC)
# The CONFIGURED bucket, not a name of our own: `FileStorage` resolves
# `storage_root / cfg.s3_bucket / key` and never reads `media_assets.bucket`, so
# a row naming a different bucket streams zero bytes with a correct
# Content-Length — which uvicorn then reports as a protocol error rather than a
# missing file. (That the column is written and never read on the delivery path
# is itself worth a look; it is not this script's business to work around.)
BUCKET = settings().s3_bucket
SECONDS = 8


def write_tone(path: Path) -> tuple[int, int]:
    """A quiet 8-second tone. Returns (bytes, duration_ms)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 22_050
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for i in range(rate * SECONDS):
            # A gentle A440 at low amplitude — audible, and not unpleasant to
            # anyone testing this for the twentieth time.
            value = int(6000 * math.sin(2 * math.pi * 440 * (i / rate)))
            frames += struct.pack("<h", value)
        out.writeframes(bytes(frames))
    return path.stat().st_size, SECONDS * 1000


engine = create_engine(os.environ["DATABASE_URL"], future=True)

with Session(engine) as db:
    org = db.query(Organization).order_by(Organization.id).first()
    if org is None:
        raise SystemExit("No organization. Run scripts/seed_demo.py first.")
    author = db.query(User).filter(User.given_name == "Dilnoza").first()
    student = db.query(User).filter(User.phone == "+998901112233").first()
    if author is None or student is None:
        raise SystemExit("No seeded author/student. Run scripts/seed_demo.py first.")

    # ── the audio, on disk and in the table ──────────────────────────────
    key = f"audio/{uuid.uuid4().hex}.wav"
    path = Path(settings().storage_root) / BUCKET / key
    size, duration = write_tone(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    media = db.execute(text("""
        INSERT INTO media_assets (org_id, owner_user_id, kind, bucket, storage_key,
                                  content_type, bytes, checksum_sha256, duration_ms,
                                  sample_rate, channels, status)
        VALUES (:o, :u, 'audio', :b, :k, 'audio/wav', :n, :c, :d, 22050, 1, 'ready')
        RETURNING id, xid
    """).bindparams(o=org.id, u=author.id, b=BUCKET, k=key, n=size, c=digest,
                    d=duration)).mappings().one()

    track = AudioTrack(org_id=org.id, owner_user_id=author.id,
                       title="Section 1 — a conversation", status="ready",
                       master_media_id=media["id"], delivery_media_id=media["id"],
                       duration_ms=duration)
    db.add(track)
    db.flush()

    db.execute(text("""
        INSERT INTO content_attestations (subject_type, subject_id, user_id, org_id,
                                          claim, statement_key, statement_version,
                                          statement_hash)
        VALUES ('audio_track', :s, :u, :o, 'original', 'upload', '1', repeat('b', 64))
    """).bindparams(s=track.id, u=author.id, o=org.id))

    # ── band map: 3 raw marks ────────────────────────────────────────────
    bm = BandMap(name="Listening default", skill="listening", org_id=org.id)
    db.add(bm)
    db.flush()
    bmv = BandMapVersion(
        band_map_id=bm.id, max_raw=3, status="published",
        mapping=[{"raw_min": 0, "raw_max": 0, "band": 4.0},
                 {"raw_min": 1, "raw_max": 1, "band": 5.5},
                 {"raw_min": 2, "raw_max": 2, "band": 6.5},
                 {"raw_min": 3, "raw_max": 3, "band": 8.0}])
    db.add(bmv)

    group = QuestionGroup(org_id=org.id, owner_user_id=author.id,
                          title="Questions 1-2", skill="listening")
    db.add(group)
    db.flush()
    gv = QuestionGroupVersion(
        group_id=group.id, status="published", checksum="x", created_by=author.id,
        instructions={"en": "Listen and answer."},
        word_limit={"max_words": 2, "allow_number": True})
    db.add(gv)
    db.flush()

    # ── the multi-select: ONE question worth TWO marks ───────────────────
    mcq = Question(org_id=org.id, owner_user_id=author.id,
                   type_key="mcq_multi", skill="listening")
    db.add(mcq)
    db.flush()
    mcq_v = QuestionVersion(
        question_id=mcq.id, type_key="mcq_multi", type_version=1,
        payload={"stem": "Which TWO facilities does the speaker mention?",
                 "select_count": 2,
                 "options": [{"id": "A", "text": "a swimming pool"},
                             {"id": "B", "text": "a library"},
                             {"id": "C", "text": "a car park"},
                             {"id": "D", "text": "a cafe"},
                             {"id": "E", "text": "a gym"}]},
        slot_keys=["s1"], status="published", checksum="x", created_by=author.id)
    db.add(mcq_v)
    db.flush()
    db.add(AnswerKeyVersion(question_version_id=mcq_v.id, version_no=1,
                            key={"correct": ["B", "D"]}, created_by=author.id))
    db.add(QuestionGroupItem(group_version_id=gv.id, question_version_id=mcq_v.id,
                             position=1))

    # ── plus one gap-fill, so the paper is not a single question ─────────
    gap = Question(org_id=org.id, owner_user_id=author.id,
                   type_key="sentence_completion", skill="listening")
    db.add(gap)
    db.flush()
    gap_v = QuestionVersion(
        question_id=gap.id, type_key="sentence_completion", type_version=1,
        payload={"text": "The centre opens at {{s1}}.", "slots": ["s1"]},
        slot_keys=["s1"], status="published", checksum="x", created_by=author.id)
    db.add(gap_v)
    db.flush()
    db.add(AnswerKeyVersion(question_version_id=gap_v.id, version_no=1,
                            key={"slots": {"s1": {"accept": ["nine", "9"]}}},
                            created_by=author.id))
    db.add(QuestionGroupItem(group_version_id=gv.id, question_version_id=gap_v.id,
                             position=2))

    test = Test(org_id=org.id, owner_user_id=author.id, title="Listening 1",
                kind="mock", skills=["listening"])
    db.add(test)
    db.flush()
    tv = TestVersion(test_id=test.id, version_no=1, title="Listening 1",
                     status="draft", band_map_version_id=bmv.id,
                     created_by=author.id, config={"time_limit_seconds": 1800})
    db.add(tv)
    db.flush()
    section = TestVersionSection(
        test_version_id=tv.id, position=1, skill="listening", title="Section 1",
        audio_track_id=track.id, play_once=True, time_limit_seconds=900,
        declared_question_count=3)
    db.add(section)
    db.flush()
    db.add(TestVersionGroup(section_id=section.id, group_version_id=gv.id,
                            position=1, number_start=1))
    db.flush()

    content_repo.publish(db, tv.id, author.id, NOW)
    db.flush()

    # Exam AND practice, because play-once is the difference between them and
    # both halves need to be sittable to be checked.
    exam = Assignment(org_id=org.id, test_version_id=tv.id, assigned_by=author.id,
                      target_kind="users", opens_at=NOW - dt.timedelta(hours=2),
                      closes_at=NOW + dt.timedelta(days=2), time_limit_seconds=1800,
                      max_attempts=20, mode="exam", allow_review_after="submit")
    practice = Assignment(org_id=org.id, test_version_id=tv.id, assigned_by=author.id,
                          target_kind="users", opens_at=NOW - dt.timedelta(hours=2),
                          closes_at=NOW + dt.timedelta(days=2),
                          time_limit_seconds=1800, max_attempts=20, mode="practice",
                          allow_review_after="submit")
    db.add_all([exam, practice])
    db.flush()
    db.add_all([AssignmentTarget(assignment_id=exam.id, user_id=student.id),
                AssignmentTarget(assignment_id=practice.id, user_id=student.id)])
    db.commit()

    print(f"audio file    : {path}  ({size} bytes, {duration} ms)")
    print(f"listening exam: {exam.xid}")
    print(f"     practice : {practice.xid}")
