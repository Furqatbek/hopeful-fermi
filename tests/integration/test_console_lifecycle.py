"""Editing and lifecycle as the console performs them.

Everything in the console was create-only: a typo in a test title meant making a
new version, a draft question could not be corrected, and a section could not be
removed. The endpoints existed; nothing called them.

The interesting part is the asymmetry, which is the versioning model itself — a
test's own fields are editable for ever, a DRAFT version's content is editable,
and a PUBLISHED version can only be archived. Each rule below is one the console
renders as the presence or absence of a control.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token


@pytest.fixture
def admin(db, seed):
    """Archiving needs `Action.ARCHIVE`, which a teacher does not hold — taking a
    published paper out of circulation is a centre-admin act."""
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201, 204)), response.text
    return response.json() if response.content else None


def _tagged(client, path: str, headers: dict) -> tuple[dict, str]:
    """Read a thing and its ETag — what every edit control does before writing."""
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json(), response.headers["ETag"]


class TestEditingATestsOwnFields:
    """Editable whatever its versions have done: the title, description and tags
    are how a paper is found in a library of forty, and a typo in one should not
    require a new version of the content."""

    def test_the_edit_form_body(self, client, seed):
        h = auth(seed["author"].xid)
        _, tag = _tagged(client, f"/api/v1/tests/{seed['test'].xid}", h)
        _ok(client.patch(f"/api/v1/tests/{seed['test'].xid}",
                         headers={**h, "If-Match": tag},
                         json={"title": "Mock 1 — Academic",
                               "description": "Reading only",
                               "tags": ["winter", "academic"]}))
        again, _ = _tagged(client, f"/api/v1/tests/{seed['test'].xid}", h)
        assert again["title"] == "Mock 1 — Academic"
        assert again["tags"] == ["winter", "academic"]

    def test_a_stale_tag_is_refused(self, client, seed):
        """Two teachers editing one test: the second must lose loudly rather than
        silently overwrite the first."""
        h = auth(seed["author"].xid)
        _, tag = _tagged(client, f"/api/v1/tests/{seed['test'].xid}", h)
        _ok(client.patch(f"/api/v1/tests/{seed['test'].xid}",
                         headers={**h, "If-Match": tag}, json={"title": "First"}))
        stale = client.patch(f"/api/v1/tests/{seed['test'].xid}",
                             headers={**h, "If-Match": tag}, json={"title": "Second"})
        assert stale.status_code == 412
        assert stale.json()["code"] == "stale_version"
        # And the first edit stands.
        again, _ = _tagged(client, f"/api/v1/tests/{seed['test'].xid}", h)
        assert again["title"] == "First"

    def test_editing_a_test_is_possible_after_publishing(self, client, seed, published):
        """The content is frozen; how you find it is not."""
        h = auth(seed["author"].xid)
        _, tag = _tagged(client, f"/api/v1/tests/{seed['test'].xid}", h)
        _ok(client.patch(f"/api/v1/tests/{seed['test'].xid}",
                         headers={**h, "If-Match": tag},
                         json={"title": "Renamed after publishing"}))


class TestEditingDraftContent:
    def test_a_draft_question_can_be_corrected(self, client, seed):
        h = auth(seed["author"].xid)
        created = _ok(client.post("/api/v1/questions", headers=h, json={
            "type_key": "sentence_completion", "type_version": 1, "skill": "reading",
            "payload": {"text": "The brige opened in {{s1}}.", "slots": ["s1"]},
            "key": {"key": {"slots": {"s1": {"accept": ["1910"]}}},
                    "reason": "initial"}}), 201)
        qv = created["current_version"]["xid"]

        _, tag = _tagged(client, f"/api/v1/question-versions/{qv}", h)
        _ok(client.patch(f"/api/v1/question-versions/{qv}",
                         headers={**h, "If-Match": tag},
                         json={"payload": {"text": "The bridge opened in {{s1}}.",
                                           "slots": ["s1"]}}))
        again, _ = _tagged(client, f"/api/v1/question-versions/{qv}", h)
        assert "bridge" in again["payload"]["text"]
        # Slot keys are re-extracted server-side from the corrected text.
        assert again["slot_keys"] == ["s1"]

    def test_moving_a_blank_moves_the_slots_the_key_must_match(self, client, seed):
        """Not cosmetic: the publish gate compares the answer key against exactly
        this array, so an edit that adds a blank leaves the key short."""
        h = auth(seed["author"].xid)
        created = _ok(client.post("/api/v1/questions", headers=h, json={
            "type_key": "sentence_completion", "type_version": 1, "skill": "reading",
            "payload": {"text": "It opened in {{s1}}.", "slots": ["s1"]}}), 201)
        qv = created["current_version"]["xid"]
        _, tag = _tagged(client, f"/api/v1/question-versions/{qv}", h)
        _ok(client.patch(f"/api/v1/question-versions/{qv}",
                         headers={**h, "If-Match": tag},
                         json={"payload": {"text": "It opened in {{s1}} near {{s2}}.",
                                           "slots": ["s1", "s2"]}}))
        again, _ = _tagged(client, f"/api/v1/question-versions/{qv}", h)
        assert again["slot_keys"] == ["s1", "s2"]

    def test_a_published_question_version_is_frozen(self, client, seed, published):
        """The console offers Edit only on a draft. Asserted here because the
        server is the authority — an attempt scored against this version has to
        keep meaning what it meant."""
        h = auth(seed["author"].xid)
        qv = published["question_versions"][0].xid
        read = client.get(f"/api/v1/question-versions/{qv}", headers=h)
        refused = client.patch(f"/api/v1/question-versions/{qv}",
                               headers={**h, "If-Match": read.headers["ETag"]},
                               json={"points": 2})
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_immutable"

    def test_a_draft_passage_can_be_rewritten(self, client, seed):
        h = auth(seed["author"].xid)
        created = _ok(client.post("/api/v1/passages", headers=h, json={
            "title": "Glass", "blocks": [{"type": "paragraph",
                                          "runs": [{"t": "text", "v": "One."}]}],
            "attestation": {"claim": "original", "statement_version": "1"}}), 201)
        pv = created["current_version"]["xid"]
        _, tag = _tagged(client, f"/api/v1/passage-versions/{pv}", h)
        _ok(client.patch(f"/api/v1/passage-versions/{pv}",
                         headers={**h, "If-Match": tag},
                         json={"title": "The history of glass",
                               "blocks": [{"type": "paragraph",
                                           "runs": [{"t": "text", "v": "One."}]},
                                          {"type": "paragraph",
                                           "runs": [{"t": "text", "v": "Two."}]}]}))
        again, _ = _tagged(client, f"/api/v1/passage-versions/{pv}", h)
        # Letters are reassigned SERVER-side on every save. Adding a paragraph in
        # the middle renumbers what follows — and a matching-headings key that
        # pointed at C now points somewhere else.
        assert again["paragraph_labels"] == ["A", "B"]

    def test_a_groups_word_limit_can_be_changed(self, client, seed):
        h = auth(seed["author"].xid)
        group = _ok(client.post("/api/v1/question-groups", headers=h, json={
            "title": "Questions 1-5", "skill": "reading",
            "instructions": {"en": "Complete the sentences."},
            "word_limit": {"max_words": 1, "allow_number": True,
                           "hyphen_counts_as_one": True,
                           "on_violation": "mark_incorrect"}}), 201)
        gv = group["current_version"]["xid"]
        _, tag = _tagged(client, f"/api/v1/question-group-versions/{gv}", h)
        _ok(client.patch(f"/api/v1/question-group-versions/{gv}",
                         headers={**h, "If-Match": tag},
                         json={"instructions": {"en": "Complete each sentence."},
                               "word_limit": {"max_words": 2, "allow_number": True,
                                              "hyphen_counts_as_one": True,
                                              "on_violation": "mark_incorrect"}}))
        again, _ = _tagged(client, f"/api/v1/question-group-versions/{gv}", h)
        # This changes how every answer in the set is MARKED, not how it reads.
        assert again["word_limit"]["max_words"] == 2
        assert again["instructions"]["en"] == "Complete each sentence."


class TestTheLifecycleMovesTheScreenOffers:
    def test_cloning_copies_the_composition_not_the_material(
            self, client, db, seed, published):
        """Next term's mock for a few hundred bytes: the clone references the
        same passage and question-group versions."""
        from app.modules.content.models import QuestionGroupVersion

        h = auth(seed["author"].xid)
        before = db.query(QuestionGroupVersion).count()
        clone = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/clone", headers=h), 201)
        assert clone["xid"] != str(seed["test"].xid)
        # No new group versions: the clone points at the originals.
        assert db.query(QuestionGroupVersion).count() == before

        # And it lands as a DRAFT, so it goes through the same gate.
        versions = _ok(client.get(f"/api/v1/tests/{clone['xid']}/versions", headers=h))
        assert [v["status"] for v in versions] == ["draft"]

    def test_editing_a_clone_does_not_touch_the_original(
            self, client, seed, published):
        h = auth(seed["author"].xid)
        clone = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/clone", headers=h), 201)
        _, tag = _tagged(client, f"/api/v1/tests/{clone['xid']}", h)
        _ok(client.patch(f"/api/v1/tests/{clone['xid']}",
                         headers={**h, "If-Match": tag}, json={"title": "Spring mock"}))
        original, _ = _tagged(client, f"/api/v1/tests/{seed['test'].xid}", h)
        assert original["title"] != "Spring mock"

    def test_archiving_stops_a_version_being_assignable(
            self, client, seed, published, admin):
        """The only lifecycle move on published content, and it must leave what
        was already sat alone. A teacher cannot: taking a published paper out of
        circulation is a centre-admin act, and the console reads that from the
        version's own `permissions` rather than guessing."""
        h = auth(seed["author"].xid)
        refused = client.post(
            f"/api/v1/test-versions/{published['test_version'].xid}/archive", headers=h)
        assert refused.status_code == 403
        assert refused.json()["reason"] == "role_teacher_cannot_archive"

        archived = _ok(client.post(
            f"/api/v1/test-versions/{published['test_version'].xid}/archive",
            headers=admin))
        assert archived["status"] == "archived"
        # The Assignments picker offers only tests carrying a published version.
        listed = _ok(client.get("/api/v1/tests?limit=50", headers=h))
        mine = next(t for t in listed["items"] if t["xid"] == str(seed["test"].xid))
        assert mine["current_published_version_xid"] is None

    def test_a_test_with_a_published_version_cannot_be_deleted(
            self, client, seed, published):
        """Attempts reference published versions for ever. The console hides the
        control; the server refuses it."""
        refused = client.delete(f"/api/v1/tests/{seed['test'].xid}",
                                headers=auth(seed["author"].xid))
        assert refused.status_code == 409

    def test_a_never_published_test_can_be_deleted(self, client, seed):
        h = auth(seed["author"].xid)
        made = _ok(client.post("/api/v1/tests", headers=h, json={
            "title": "Scratch", "kind": "mock", "variant": "academic",
            "skills": ["reading"]}), 201)
        _ok(client.delete(f"/api/v1/tests/{made['xid']}", headers=h), 204)
        # A SOFT archive: the row survives, because a takedown investigation
        # needs the evidence intact. What the console cares about is that it
        # leaves the library, which is what the listing filters on.
        listed = _ok(client.get("/api/v1/tests?limit=100", headers=h))
        assert made["xid"] not in {t["xid"] for t in listed["items"]}

    def test_removing_a_section_closes_the_gap_it_leaves(self, client, seed):
        """A hole in the sequence is invisible to the author, invisible to the
        publish gate, and a 404 to the student who tries to enter the section
        after it — mid-exam."""
        h = auth(seed["author"].xid)
        version = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions",
                                  headers=h, json={}), 201)
        for position, title in ((1, "One"), (2, "Two"), (3, "Three")):
            _ok(client.post(f"/api/v1/test-versions/{version['xid']}/sections",
                            headers=h, json={"position": position, "skill": "reading",
                                             "title": title}), 201)
        detail = _ok(client.get(f"/api/v1/test-versions/{version['xid']}", headers=h))
        middle = next(s for s in detail["sections"] if s["title"] == "Two")
        # The new version is SEEDED from the published one, so it already carries
        # that version's sections — the three added above sit in front of them.
        before = [s["title"] for s in detail["sections"]]
        assert before[:3] == ["One", "Two", "Three"]

        _ok(client.delete(f"/api/v1/sections/{middle['xid']}", headers=h), 204)
        after = _ok(client.get(f"/api/v1/test-versions/{version['xid']}", headers=h))
        # Contiguous, with no hole where "Two" was. A hole is invisible to the
        # author, invisible to the publish gate, and a 404 to the student who
        # tries to enter the section after it — mid-exam.
        positions = [s["position"] for s in after["sections"]]
        assert positions == list(range(1, len(positions) + 1))
        assert [s["title"] for s in after["sections"]][:2] == ["One", "Three"]
