"""The authoring surface as an author actually drives it.

The contract-smoke suite proves nothing crashes. This proves the two things in
the composition layer that are genuinely easy to get wrong:

  * **Numbering is test-wide and derived.** A group's `number_start` is a
    function of everything before it. Getting this wrong produces a test numbered
    1-13, 1-13 — which looks fine in the editor and is obviously broken to a
    student halfway through.
  * **Cloning copies the composition, not the assets.** A clone that deep-copied
    passages would double storage per copy and, worse, silently fork a shared
    passage so a later correction reached only one of them.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.deps import issue_access_token
from app.modules.content.models import (
    Passage,
    PassageVersion,
    QuestionGroupItem,
    QuestionGroupVersion,
    QuestionVersion,
    TestVersion,
    TestVersionGroup,
    TestVersionSection,
)


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin(db, seed):
    """The author, promoted so publishing is not the thing under test."""
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}


@pytest.fixture
def reviewer(db, seed):
    """A second centre_admin — the other pair of eyes a review needs."""
    import datetime as dt

    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Kamola",
                date_of_birth=dt.date(1982, 6, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return {"Authorization": f"Bearer {issue_access_token(str(user.xid))}"}


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


class TestNumberingIsTestWide:
    def test_a_second_section_continues_the_numbering(self, client, admin, db, seed):
        """Section 2's first question is 4, not 1.

        The seed's section 1 holds three single-slot questions, so the next
        section must start at 4. This is the assertion that catches "each section
        restarts at 1", the default behaviour of numbering per section.
        """
        tv = seed["test_version"]
        second = _ok(client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                                 json={"position": 2, "skill": "reading",
                                       "title": "Passage 2"}, headers=admin), 201)
        gv = _new_group_with_questions(db, seed, count=2)
        _ok(client.post(f"/api/v1/sections/{second['xid']}/groups",
                        json={"group_version_xid": str(gv.xid), "position": 1},
                        headers=admin), 201)

        sections = _ok(client.get(f"/api/v1/test-versions/{tv.xid}/sections",
                                  headers=admin))
        assert [g["number_start"] for g in sections[0]["groups"]] == [1]
        assert [g["number_start"] for g in sections[1]["groups"]] == [4]

    def test_a_multi_slot_question_consumes_one_number_per_slot(self, client, admin,
                                                                db, seed):
        """A three-blank sentence completion is questions n, n+1, n+2.

        Counting questions rather than slots is the other classic numbering bug,
        and it only shows up on the types where it matters.
        """
        tv = seed["test_version"]
        section = _ok(client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                                  json={"position": 2, "skill": "reading",
                                        "title": "Passage 2"}, headers=admin), 201)
        wide = _new_group_with_questions(db, seed, count=1, slots=3)
        _ok(client.post(f"/api/v1/sections/{section['xid']}/groups",
                        json={"group_version_xid": str(wide.xid), "position": 1},
                        headers=admin), 201)

        third = _ok(client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                                json={"position": 3, "skill": "reading",
                                      "title": "Passage 3"}, headers=admin), 201)
        tail = _new_group_with_questions(db, seed, count=1)
        _ok(client.post(f"/api/v1/sections/{third['xid']}/groups",
                        json={"group_version_xid": str(tail.xid), "position": 1},
                        headers=admin), 201)

        sections = _ok(client.get(f"/api/v1/test-versions/{tv.xid}/sections",
                                  headers=admin))
        # 1-3 in section 1, 4-6 in the single three-slot question, so 7 next.
        assert [s["groups"][0]["number_start"] for s in sections] == [1, 4, 7]

    def test_inserting_a_section_renumbers_everything_after_it(self, client, admin,
                                                              db, seed):
        """The insert case, which is where a naive implementation breaks.

        There is no reorder-sections endpoint, so inserting at an occupied
        position shifts the occupant rather than failing — and the numbers have to
        follow.
        """
        tv = seed["test_version"]
        tail = _ok(client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                               json={"position": 2, "skill": "reading",
                                     "title": "Last"}, headers=admin), 201)
        gv = _new_group_with_questions(db, seed, count=2)
        _ok(client.post(f"/api/v1/sections/{tail['xid']}/groups",
                        json={"group_version_xid": str(gv.xid), "position": 1},
                        headers=admin), 201)

        inserted = _ok(client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                                   json={"position": 2, "skill": "reading",
                                         "title": "Middle"}, headers=admin), 201)
        middle = _new_group_with_questions(db, seed, count=4)
        _ok(client.post(f"/api/v1/sections/{inserted['xid']}/groups",
                        json={"group_version_xid": str(middle.xid), "position": 1},
                        headers=admin), 201)

        sections = _ok(client.get(f"/api/v1/test-versions/{tv.xid}/sections",
                                  headers=admin))
        assert [s["title"] for s in sections] == ["Passage 1", "Middle", "Last"]
        # 1-3, then the inserted four (4-7), then the two that were 4-5 -> 8-9.
        assert [s["groups"][0]["number_start"] for s in sections] == [1, 4, 8]

    def test_reordering_groups_renumbers_the_section(self, client, admin, db, seed):
        tv = seed["test_version"]
        section = seed["section"]
        second = _new_group_with_questions(db, seed, count=2)
        _ok(client.post(f"/api/v1/sections/{section.xid}/groups",
                        json={"group_version_xid": str(second.xid), "position": 2},
                        headers=admin), 201)

        before = _ok(client.get(f"/api/v1/test-versions/{tv.xid}/sections",
                                headers=admin))[0]["groups"]
        assert [g["number_start"] for g in before] == [1, 4]

        flipped = _ok(client.post(f"/api/v1/sections/{section.xid}/reorder",
                                  json={"group_placement_xids":
                                        [before[1]["xid"], before[0]["xid"]]},
                                  headers=admin))
        # The two-question group now leads, so the three-question group starts at 3.
        assert [g["number_start"] for g in flipped] == [1, 3]

    def test_a_partial_reorder_is_refused(self, client, admin, db, seed):
        """Naming only some of the groups would silently drop the rest to the end.
        Refusing is the only safe answer; the client has the full list already."""
        section = seed["section"]
        extra = _new_group_with_questions(db, seed, count=1)
        _ok(client.post(f"/api/v1/sections/{section.xid}/groups",
                        json={"group_version_xid": str(extra.xid), "position": 2},
                        headers=admin), 201)
        r = client.post(f"/api/v1/sections/{section.xid}/reorder",
                        json={"group_placement_xids": [str(extra.xid)]}, headers=admin)
        assert r.status_code == 409
        assert r.json()["code"] == "reorder_incomplete"


class TestCloneCopiesCompositionNotAssets:
    def test_the_clone_references_the_same_passage_version(self, client, admin, db,
                                                           seed):
        passages_before = db.scalar(select(func.count()).select_from(Passage))
        versions_before = db.scalar(select(func.count()).select_from(PassageVersion))

        clone = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                                json={"title": "Mock 1 (copy)"}, headers=admin), 201)

        assert db.scalar(select(func.count()).select_from(Passage)) == passages_before
        assert db.scalar(
            select(func.count()).select_from(PassageVersion)) == versions_before

        cloned_tv = db.scalars(
            select(TestVersion)
            .join(TestVersionSection,
                  TestVersionSection.test_version_id == TestVersion.id)
            .where(TestVersion.cloned_from_version_id == seed["test_version"].id)).first()
        section = db.scalars(
            select(TestVersionSection)
            .where(TestVersionSection.test_version_id == cloned_tv.id)).one()
        assert section.passage_version_id == seed["passage_version"].id
        assert clone["title"] == "Mock 1 (copy)"

    def test_editing_the_clone_does_not_touch_the_original(self, client, admin, db,
                                                           seed):
        _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/clone", json={},
                        headers=admin), 201)
        cloned = db.scalars(
            select(TestVersion)
            .where(TestVersion.cloned_from_version_id == seed["test_version"].id)).one()
        _ok(client.patch(f"/api/v1/test-versions/{cloned.xid}",
                         json={"title": "Rewritten"}, headers=admin))
        db.refresh(seed["test_version"])
        assert seed["test_version"].title == "Mock 1 v1"


class TestPublishedVersionsAreFrozen:
    def test_a_published_version_refuses_structural_edits(self, client, admin,
                                                          published):
        tv = published["test_version"]
        r = client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                        json={"position": 9, "skill": "reading", "title": "Late"},
                        headers=admin)
        assert r.status_code == 409
        assert r.json()["code"] == "version_immutable"

    def test_a_new_draft_version_seeds_from_the_published_one(self, client, admin,
                                                              db, published):
        draft = _ok(client.post(f"/api/v1/tests/{published['test'].xid}/versions",
                                json={"from_version_xid":
                                      str(published["test_version"].xid)},
                                headers=admin), 201)
        assert draft["status"] == "draft"
        assert draft["version_no"] == 2
        rows = db.scalars(
            select(TestVersionSection)
            .join(TestVersion, TestVersion.id == TestVersionSection.test_version_id)
            .where(TestVersion.xid == uuid.UUID(draft["xid"]))).all()
        assert len(rows) == 1
        assert rows[0].passage_version_id == published["passage_version"].id

    def test_a_test_with_published_versions_cannot_be_deleted(self, client, admin,
                                                              published):
        r = client.delete(f"/api/v1/tests/{published['test'].xid}", headers=admin)
        assert r.status_code == 409
        assert r.json()["code"] == "published_content_not_deletable"


class TestReviewGate:
    def test_submitting_for_review_runs_the_publish_gate_first(self, client, admin,
                                                              db, seed):
        """A reviewer's time is the scarcest resource a small centre has.

        The seed test declares three questions and has three, so it passes; the
        assertion that matters is that the gate RAN and recorded its findings.
        """
        from app.modules.content.models import TestVersionValidation

        body = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/submit-review",
            json={"notes": "ready"}, headers=admin))
        assert body["state"] == "requested"
        assert db.scalar(
            select(func.count()).select_from(TestVersionValidation)
            .where(TestVersionValidation.test_version_id == seed["test_version"].id))

    def test_a_broken_test_is_refused_before_a_human_sees_it(self, client, admin,
                                                             db, seed):
        seed["section"].declared_question_count = 40
        db.flush()
        r = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/submit-review",
            json={}, headers=admin)
        assert r.status_code == 422
        assert r.json()["findings"], "every finding is returned, not just the first"

    def test_approval_does_not_publish(self, client, admin, reviewer, db, seed):
        """Approval says the content is ready, not that it is live.

        Publishing stays a separate, separately audited act — otherwise the
        reviewer's click is also a deploy, and there is no moment to stop it.

        Two accounts now: this used to submit and approve as the same user, which
        is the thing `self_approval` refuses. That the test read naturally that
        way is the point — one person walking the whole review flow was the
        obvious path through it.
        """
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}"
                        "/submit-review", json={}, headers=admin))
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/review",
                        json={"decision": "approved"}, headers=reviewer))
        db.refresh(seed["test_version"])
        assert seed["test_version"].status == "in_review"
        assert seed["test_version"].published_at is None


class TestExport:
    def test_json_export_omits_keys_unless_asked(self, client, admin, published):
        plain = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/export",
            headers=admin)
        assert plain.status_code == 200
        assert "answer_keys" not in json.loads(plain.text)

    def test_with_include_keys_the_keys_are_present(self, client, admin, published):
        keyed = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/export"
            "?include_keys=true", headers=admin)
        document = json.loads(keyed.text)
        assert document["answer_keys"]
        assert document["export"]["includes_keys"] is True

    @pytest.mark.parametrize("fmt", ["json", "csv"])
    def test_the_template_round_trips_through_the_importer(self, client, db, fmt):
        """The template we hand a centre must actually import.

        A template that does not parse is worse than no template — it teaches the
        centre that the import feature is broken.

        `registry(db)`, not `registry()`. It is a FastAPI dependency —
        `def registry(session: Session = Depends(db))` — so calling it bare hands
        `refresh_from_db` a `Depends` object where it wants a Session, and these
        three tests died on that rather than on anything they were written to
        check. FastAPI only resolves those defaults for a request it is routing.
        """
        from app.api.deps import registry
        from app.modules.content import importer

        raw = client.get(f"/api/v1/imports/template?format={fmt}").content
        result = importer.parse(raw, fmt, registry(db))
        assert result.ok, [f.as_dict() for f in result.report.findings]

    def test_a_csv_export_re_imports(self, client, db, admin, published):
        """The round-trip the contract promises, actually exercised.

        Export and import must speak ONE format; a CSV export in its own private
        shape exports fine and fails on the way back in.
        """
        from app.api.deps import registry
        from app.modules.content import importer

        exported = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/export"
            "?format=csv&include_keys=true", headers=admin)
        assert exported.status_code == 200, exported.text
        result = importer.parse(exported.content, "csv", registry(db))
        assert result.ok, [f.as_dict() for f in result.report.findings]
        assert result.counts["questions"] == 3


def _new_group_with_questions(db, seed, count: int, slots: int = 1):
    """A published group holding `count` questions of `slots` slots each."""
    from app.modules.content.models import AnswerKeyVersion, Question

    group_version = QuestionGroupVersion(
        group_id=seed["group_version"].group_id, status="published", checksum="x",
        created_by=seed["author"].id, version_no=_next_version(db, seed),
        instructions={"en": "Complete each sentence."},
        word_limit={"max_words": 2, "allow_number": True})
    db.add(group_version)
    db.flush()
    slot_keys = [f"s{i}" for i in range(1, slots + 1)]
    for position in range(1, count + 1):
        question = Question(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                            type_key="sentence_completion", skill="reading")
        db.add(question)
        db.flush()
        qv = QuestionVersion(
            question_id=question.id, type_key="sentence_completion", type_version=1,
            payload={"text": " ".join(f"{{{{{k}}}}}" for k in slot_keys),
                     "slots": slot_keys},
            slot_keys=slot_keys, status="published", checksum="x",
            created_by=seed["author"].id)
        db.add(qv)
        db.flush()
        db.add(AnswerKeyVersion(
            question_version_id=qv.id, version_no=1, created_by=seed["author"].id,
            key={"slots": {k: {"accept": ["bike"]} for k in slot_keys}}))
        db.add(QuestionGroupItem(group_version_id=group_version.id,
                                 question_version_id=qv.id, position=position))
    db.flush()
    return group_version


def _next_version(db, seed) -> int:
    return (db.scalar(
        select(func.max(QuestionGroupVersion.version_no))
        .where(QuestionGroupVersion.group_id == seed["group_version"].group_id)) or 0) + 1


def _placements(db, test_version_id: int):
    return db.scalars(
        select(TestVersionGroup)
        .join(TestVersionSection,
              TestVersionSection.id == TestVersionGroup.section_id)
        .where(TestVersionSection.test_version_id == test_version_id)
        .order_by(TestVersionSection.position, TestVersionGroup.position)).all()
