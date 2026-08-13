"""Seed a real database with one published Reading paper, for driving the whole
flow end to end against a running stack.

    createdb livedemo
    DATABASE_URL=... alembic upgrade head
    DATABASE_URL=... python scripts/seed_demo.py

Prints the student's phone and both assignment xids. With `PILOT_OPEN_SIGNIN=true`
the one-time code comes back in the sign-in response and is shown on screen, so a
browser can drive the real flow with no SMS provider. See docs/design/0014.

NOT for production: it creates a known phone number with no consent record.

The body is lifted from `tests/integration/conftest.py::seed` — the same ORM
calls, the same field names — rather than reimplemented, because a seed that
disagrees with the fixtures is a seed that tests something other than the
product. Only three things differ: the student's phone is a known number so it
can be signed in to, the content is real prose rather than "Maps are old.", and
two assignments are added because the fixture has no reason to.
"""
import datetime as dt
import os
import uuid
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.modules.billing.models import EntitlementRow
from app.modules.content import repo as content_repo
from app.modules.content.models import (
    AnswerKeyVersion,
    BandMap,
    BandMapVersion,
    Passage,
    PassageVersion,
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
from app.modules.identity.models import Organization, OrgMembership, User

PHONE = "+998901112233"
NOW = dt.datetime.now(dt.UTC)

engine = create_engine(os.environ["DATABASE_URL"], future=True)

with Session(engine) as db:
    org = Organization(name="Tashkent Prep", slug=f"tp-{uuid.uuid4().hex[:6]}", status="active")
    author = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Dilnoza",
                  date_of_birth=datetime(1995, 4, 1).date())
    student = User(phone=PHONE, given_name="Aziza",
                   date_of_birth=datetime(2004, 3, 1).date())
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
                 {"raw_min": 3, "raw_max": 3, "band": 7.0}])
    db.add(bmv)

    passage = Passage(org_id=org.id, owner_user_id=author.id, title="The Dead Sea")
    db.add(passage)
    db.flush()
    pv = PassageVersion(
        passage_id=passage.id, title="The Dead Sea", status="published",
        blocks=[
            {"id": "b1", "type": "paragraph", "label": "A", "runs": [{"t": "text", "v":
             "The Dead Sea lies 430 metres below sea level, making its shores the lowest dry "
             "land on Earth. Its water is roughly ten times saltier than ordinary seawater, a "
             "concentration that no fish and almost no plant can survive."}]},
            {"id": "b2", "type": "paragraph", "label": "B", "runs": [{"t": "text", "v":
             "That same salinity is what makes it famous. A bather does not swim so much as "
             "float, held on the surface by the density of the water."}]},
            {"id": "b3", "type": "paragraph", "label": "C", "runs": [{"t": "text", "v":
             "The sea is shrinking. The River Jordan is diverted upstream for agriculture, and "
             "the surface has dropped more than thirty metres in a century."}]},
        ],
        paragraph_labels=["A", "B", "C"], word_count=92, checksum="x",
        created_by=author.id)
    db.add(pv)

    group = QuestionGroup(org_id=org.id, owner_user_id=author.id,
                          title="Questions 1-3", skill="reading")
    db.add(group)
    db.flush()
    gv = QuestionGroupVersion(
        group_id=group.id, status="published", checksum="x", created_by=author.id,
        instructions={"en": "Complete each sentence."},
        word_limit={"max_words": 2, "allow_number": True})
    db.add(gv)
    db.flush()

    items = [
        ("The Dead Sea lies {{s1}} metres below sea level.", "430"),
        ("Its water is about {{s1}} times saltier than seawater.", "ten"),
        ("The surface has dropped more than {{s1}} metres in a century.", "thirty"),
    ]
    for i, (prompt, answer) in enumerate(items, start=1):
        q = Question(org_id=org.id, owner_user_id=author.id,
                     type_key="sentence_completion", skill="reading")
        db.add(q)
        db.flush()
        qv = QuestionVersion(
            question_id=q.id, type_key="sentence_completion", type_version=1,
            payload={"text": prompt, "slots": ["s1"]},
            slot_keys=["s1"], status="published", checksum="x", created_by=author.id)
        db.add(qv)
        db.flush()
        db.add(AnswerKeyVersion(question_version_id=qv.id, version_no=1,
                                key={"slots": {"s1": {"accept": [answer]}}},
                                created_by=author.id))
        db.add(QuestionGroupItem(group_version_id=gv.id, question_version_id=qv.id,
                                 position=i))

    test = Test(org_id=org.id, owner_user_id=author.id, title="Academic Reading 4",
                kind="mock", skills=["reading"])
    db.add(test)
    db.flush()
    tv = TestVersion(test_id=test.id, version_no=1, title="Academic Reading 4",
                     status="draft", band_map_version_id=bmv.id, created_by=author.id,
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
    db.execute(text("""
        INSERT INTO content_attestations (subject_type, subject_id, user_id, org_id,
                                          claim, statement_key, statement_version,
                                          statement_hash)
        VALUES ('passage', :p, :u, :o, 'original', 'upload', '1', repeat('a', 64))
    """).bindparams(p=passage.id, u=author.id, o=org.id))
    db.flush()

    content_repo.publish(db, tv.id, author.id, NOW)
    db.flush()

    db.add(EntitlementRow(subject_kind="user", subject_id=student.id,
                          feature="mock.unlimited", source_kind="order",
                          starts_at=NOW - dt.timedelta(days=1)))

    # `users`, not `user` — a named list of students rather than a cohort. The
    # DB check constraint is the authority here (cohort|users|self_serve).
    exam = Assignment(org_id=org.id, test_version_id=tv.id, assigned_by=author.id,
                      target_kind="users", opens_at=NOW - dt.timedelta(hours=2),
                      closes_at=NOW + dt.timedelta(hours=6), time_limit_seconds=3600,
                      max_attempts=2, mode="exam", allow_review_after="submit")
    practice = Assignment(org_id=org.id, test_version_id=tv.id, assigned_by=author.id,
                          target_kind="users", opens_at=NOW - dt.timedelta(days=1),
                          closes_at=NOW + dt.timedelta(days=3), time_limit_seconds=1800,
                          max_attempts=3, mode="practice", allow_review_after="submit")
    db.add_all([exam, practice])
    db.flush()
    db.add_all([AssignmentTarget(assignment_id=exam.id, user_id=student.id),
                AssignmentTarget(assignment_id=practice.id, user_id=student.id)])
    db.commit()

    print(f"student phone : {PHONE}")
    print(f"exam assign   : {exam.xid}")
    print(f"practice      : {practice.xid}")
