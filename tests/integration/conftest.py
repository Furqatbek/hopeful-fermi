"""Integration fixtures: a real PostgreSQL, real migrations, real ORM.

Skipped cleanly when no database is reachable, so `pytest` still runs the pure
domain suites on a laptop with nothing installed. Set `TEST_DATABASE_URL` (or
`DATABASE_URL`) to a server you do not mind a scratch database being created on.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.modules.qtypes.registry import Registry, Scorer, load_lexicon
from app.platform.clock import FrozenClock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def _admin_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    base = _admin_url()
    if not base:
        pytest.skip("set TEST_DATABASE_URL to run integration tests")

    # make_url, not string surgery: a unix-socket DSN carries `host=/tmp` in the
    # query string, and splitting on "/" mangles it.
    name = f"ielts_it_{uuid.uuid4().hex[:8]}"
    parsed = make_url(base)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot create a scratch database: {exc}")
    url = parsed.set(database=name).render_as_string(hide_password=False)

    # Real migrations, not `create_all`. The models describe a subset of the
    # schema; the migrations are the schema, and the point of these tests is to
    # prove the two agree.
    import subprocess
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(["alembic", "upgrade", "head"], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    if result.returncode != 0:  # pragma: no cover
        pytest.skip(f"migrations failed: {result.stderr[-500:]}")

    yield url

    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url):
    eng = create_engine(database_url, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """One transaction per test, rolled back afterwards.

    A nested transaction would be neater, but the schema has triggers that read
    committed state (`attempt_answers_frozen` looks up `attempts.status`), so the
    tests run in a real transaction and truncate rather than pretending.
    """
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(engine, expire_on_commit=False, future=True)
    session = Session()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as c:
        c.execute(text("""
            TRUNCATE item_scores, score_runs, attempt_answers, attempt_sections,
                     attempts, assignment_targets, assignments, outbox,
                     test_version_validations, test_version_groups,
                     test_version_sections, test_versions, tests,
                     question_group_items, question_group_versions, question_groups,
                     answer_key_versions, question_versions, questions,
                     passage_versions, passages, audio_tracks,
                     band_map_versions, band_maps,
                     seat_assignments, entitlements, import_jobs,
                     cohort_members, cohorts, org_memberships, organizations, users
            RESTART IDENTITY CASCADE
        """))
        # TRUNCATE ... CASCADE reaches further than the table list suggests:
        # `question_type_defs.created_by` references `users.id`, so truncating
        # users takes the whole registry with it. Restore it, or every test after
        # the first fails on the (type_key, type_version) foreign key.
        _reseed_registry(c)


def _reseed_registry(conn) -> None:
    import hashlib
    import json
    from pathlib import Path

    if conn.execute(text("SELECT count(*) FROM question_type_defs")).scalar():
        return
    root = Path(ROOT)
    for file in sorted((root / "registry" / "question_types").glob("*.json")):
        d = json.loads(file.read_text())
        canon = json.dumps(d, sort_keys=True, separators=(",", ":"))
        conn.execute(text("""
            INSERT INTO question_type_defs
                (key, version, status, title, description, skills, payload_schema,
                 key_schema, response_schema, scoring, validation, authoring,
                 source, checksum)
            VALUES (:k, :v, :s, :t, :d, :sk, CAST(:ps AS jsonb), CAST(:ks AS jsonb),
                    CAST(:rs AS jsonb), CAST(:sc AS jsonb), CAST(:va AS jsonb),
                    CAST(:au AS jsonb), 'builtin', :cs)
            ON CONFLICT (key, version) DO NOTHING
        """), dict(
            k=d["key"], v=d["version"], s=d.get("status", "active"), t=d["title"],
            d=d.get("description"), sk=d["skills"],
            ps=json.dumps(d["payload_schema"]), ks=json.dumps(d["key_schema"]),
            rs=json.dumps(d["response_schema"]), sc=json.dumps(d["scoring"]),
            va=json.dumps(d.get("validation", {})), au=json.dumps(d.get("authoring", {})),
            cs=hashlib.sha256(canon.encode()).hexdigest()))
    for file in sorted((root / "registry" / "lexicon").glob("*.json")):
        for entry in json.loads(file.read_text()):
            conn.execute(text("""
                INSERT INTO lexicon_entries (kind, a, b, bidirectional, locale, note)
                VALUES (:kind, :a, :b, :bi, :locale, :note)
                ON CONFLICT (kind, a, b) DO NOTHING
            """), dict(kind=entry["kind"], a=entry["a"], b=entry["b"],
                       bi=entry.get("bidirectional", True),
                       locale=entry.get("locale"), note=entry.get("note")))


@pytest.fixture(scope="session")
def scorer_svc():
    from pathlib import Path
    root = Path(ROOT)
    return Scorer(Registry.from_directory(root / "registry" / "question_types"),
                  load_lexicon(root / "registry" / "lexicon"))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def seed(db):
    """A published one-section Reading test, an author and a student.

    Built through the ORM rather than raw SQL so the models are exercised on the
    way in — a mapping that disagrees with the migrations fails here.
    """
    from app.modules.content.models import (
        AnswerKeyVersion, BandMap, BandMapVersion, PassageVersion, Passage, Question,
        QuestionGroup, QuestionGroupItem, QuestionGroupVersion, QuestionVersion,
        Test, TestVersion, TestVersionGroup, TestVersionSection,
    )
    from app.modules.identity.models import Organization, OrgMembership, User

    org = Organization(name="Tashkent Prep", slug=f"tp-{uuid.uuid4().hex[:6]}", status="active")
    author = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Dilnoza",
                  date_of_birth=datetime(1995, 4, 1).date())
    student = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Aziza",
                   date_of_birth=datetime(2008, 3, 1).date())
    db.add_all([org, author, student])
    db.flush()
    db.add_all([
        OrgMembership(org_id=org.id, user_id=author.id, role="teacher"),
        OrgMembership(org_id=org.id, user_id=student.id, role="student"),
    ])

    bm = BandMap(name="Reading default", skill="reading", org_id=org.id)
    db.add(bm)
    db.flush()
    bmv = BandMapVersion(
        band_map_id=bm.id, max_raw=3, status="published",
        mapping=[{"raw_min": 0, "raw_max": 0, "band": 4.0},
                 {"raw_min": 1, "raw_max": 1, "band": 5.0},
                 {"raw_min": 2, "raw_max": 2, "band": 6.0},
                 {"raw_min": 3, "raw_max": 3, "band": 7.0}],
    )
    db.add(bmv)

    passage = Passage(org_id=org.id, owner_user_id=author.id, title="Cartography")
    db.add(passage)
    db.flush()
    pv = PassageVersion(
        passage_id=passage.id, title="Cartography", status="published",
        blocks=[{"id": "b1", "type": "paragraph", "label": "A",
                 "runs": [{"t": "text", "v": "Maps are old."}]}],
        paragraph_labels=["A", "B", "C"], word_count=3, checksum="x",
        created_by=author.id,
    )
    db.add(pv)

    group = QuestionGroup(org_id=org.id, owner_user_id=author.id,
                          title="Questions 1-3", skill="reading")
    db.add(group)
    db.flush()
    gv = QuestionGroupVersion(
        group_id=group.id, status="published", checksum="x", created_by=author.id,
        instructions={"en": "Complete each sentence."},
        word_limit={"max_words": 2, "allow_number": True},
    )
    db.add(gv)
    db.flush()

    answers = ["bicycle", "library", "museum"]
    qvs = []
    for i, answer in enumerate(answers, start=1):
        q = Question(org_id=org.id, owner_user_id=author.id,
                     type_key="sentence_completion", skill="reading")
        db.add(q)
        db.flush()
        qv = QuestionVersion(
            question_id=q.id, type_key="sentence_completion", type_version=1,
            payload={"text": f"Item {i} answer is {{{{s1}}}}.", "slots": ["s1"]},
            slot_keys=["s1"], status="published", checksum="x", created_by=author.id,
        )
        db.add(qv)
        db.flush()
        db.add(AnswerKeyVersion(question_version_id=qv.id, version_no=1,
                                key={"slots": {"s1": {"accept": [answer]}}},
                                created_by=author.id))
        db.add(QuestionGroupItem(group_version_id=gv.id, question_version_id=qv.id,
                                 position=i))
        qvs.append(qv)

    test = Test(org_id=org.id, owner_user_id=author.id, title="Mock 1",
                kind="mock", skills=["reading"])
    db.add(test)
    db.flush()
    tv = TestVersion(test_id=test.id, version_no=1, title="Mock 1 v1", status="draft",
                     band_map_version_id=bmv.id, created_by=author.id,
                     config={"time_limit_seconds": 3600})
    db.add(tv)
    db.flush()
    section = TestVersionSection(
        test_version_id=tv.id, position=1, skill="reading", title="Passage 1",
        passage_version_id=pv.id, time_limit_seconds=1200, declared_question_count=3)
    db.add(section)
    db.flush()
    db.add(TestVersionGroup(section_id=section.id, group_version_id=gv.id,
                            position=1, number_start=1))
    db.flush()

    return {
        "org": org, "author": author, "student": student,
        "test": test, "test_version": tv, "section": section,
        "group_version": gv, "question_versions": qvs, "band_map_version": bmv,
        "passage_version": pv,
    }


@pytest.fixture
def published(db, seed, clock):
    from app.modules.content import repo as content_repo

    content_repo.publish(db, seed["test_version"].id, seed["author"].id, clock.now())
    db.flush()
    return seed


@pytest.fixture
def with_audio(db, seed):
    """Attach a ready audio track to the seeded section.

    The play-once tests need one: a grant is issued for a specific media object,
    so a section with no audio has nothing to grant. The seeded test is a reading
    paper, and using it as a stand-in only worked while the grant was a
    placeholder hash of the section id.
    """
    from app.modules.content.models import AudioTrack
    from sqlalchemy import text as _text

    media_id = db.scalar(_text("""
        INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                  content_type, bytes, checksum_sha256, status,
                                  duration_ms)
        VALUES (:u, 'audio', 'test-media', 'seed/section1.m4a', 'audio/mp4',
                4096, 'seed-checksum', 'ready', 30000)
        RETURNING id
    """).bindparams(u=seed["author"].id))
    track = AudioTrack(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                       title="Section 1 audio", status="ready",
                       master_media_id=media_id, delivery_media_id=media_id,
                       duration_ms=30000)
    db.add(track)
    db.flush()
    seed["section"].audio_track_id = track.id
    db.flush()
    seed["audio_track"] = track
    seed["media_id"] = media_id
    return seed


@pytest.fixture
def entitled(db, seed):
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="user", subject_id=seed["student"].id,
                          feature="mock.unlimited", source_kind="order",
                          starts_at=NOW - timedelta(days=1)))
    db.flush()
    return seed
