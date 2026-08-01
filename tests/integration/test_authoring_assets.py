"""The authoring asset endpoints: passages, questions, groups, transcripts.

The brief called the authoring system the core of the product and said not to
treat it as CRUD over a questions table. `assets.py` is where that claim is
cashed, and it was 70% covered — the least-tested of the large routers after
`auth.py` and `platform_ops.py`.

Unlike those two, reading it closely turned up no authentication defect. What it
does carry is a set of properties that are quiet when they break:

  * **Published versions are immutable.** Three endpoints enforce it and each
    one is a single `if status == "published"`. A test scored against v1 must
    keep meaning what it meant.
  * **Slot keys are extracted server-side, never taken from the client.** The
    publish gate compares answer-key slots to this array, so a client that
    declared its own would be validating its own claim.
  * **Paragraph letters are assigned server-side too**, for the same reason:
    matching-headings questions reference them.
  * **Usage is visible before an edit**, so "I changed one passage and broke
    four published mocks" cannot happen by surprise.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def author(seed):
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}


@pytest.fixture
def student(seed):
    return {"Authorization": f"Bearer {issue_access_token(str(seed['student'].xid))}"}


PARAGRAPHS = [
    {"id": "b1", "type": "paragraph", "runs": [{"t": "text", "v": "Rivers move."}]},
    {"id": "b2", "type": "paragraph", "runs": [{"t": "text", "v": "So do glaciers."}]},
    {"id": "b3", "type": "paragraph", "runs": [{"t": "text", "v": "Ice is slower."}]},
]


class TestPassageVersions:
    def test_creating_a_passage_returns_a_first_version(self, client, author):
        response = client.post("/api/v1/passages", headers=author,
                               json={"title": "Cartography", "blocks": PARAGRAPHS})
        assert response.status_code == 201
        assert response.json()["current_version"]["version_no"] == 1

    def test_paragraph_letters_are_assigned_server_side(self, client, author):
        """Matching-headings questions reference these letters. Letting the
        client supply them would mean the publish gate validates the client's own
        claim rather than the passage the author is looking at."""
        xid = _passage(client, author)
        response = client.patch(f"/api/v1/passage-versions/{xid}", headers=author,
                                json={"blocks": PARAGRAPHS})
        assert response.json()["paragraph_labels"] == ["A", "B", "C"]

    def test_a_client_cannot_supply_its_own_letters(self, client, author):
        xid = _passage(client, author)
        response = client.patch(f"/api/v1/passage-versions/{xid}", headers=author,
                                json={"blocks": PARAGRAPHS,
                                      "paragraph_labels": ["X", "Y", "Z"]})
        assert response.json()["paragraph_labels"] == ["A", "B", "C"]

    def test_the_word_count_is_recomputed_on_save(self, client, author):
        xid = _passage(client, author)
        response = client.patch(f"/api/v1/passage-versions/{xid}", headers=author,
                                json={"blocks": PARAGRAPHS})
        assert response.json()["word_count"] == 8   # 2 + 3 + 3

    def test_a_published_version_is_immutable(self, client, author, seed, db):
        """A test scored against this passage must keep meaning what it meant."""
        xid = _passage(client, author)
        _make_publisher(db, seed)
        assert client.post(f"/api/v1/passage-versions/{xid}/publish",
                           headers=author).json()["status"] == "published"
        refused = client.patch(f"/api/v1/passage-versions/{xid}", headers=author,
                               json={"title": "Edited"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_immutable"

    def test_a_teacher_cannot_publish_without_the_org_setting(self, client, author):
        """Teachers are absent from the PUBLISH row on purpose: a centre's
        reputation rides on its published material."""
        xid = _passage(client, author)
        assert client.post(f"/api/v1/passage-versions/{xid}/publish",
                           headers=author).status_code == 403

    def test_the_org_setting_lets_a_teacher_publish(self, client, author, seed, db):
        """`teacher_can_publish` was consulted for a TEST version and not for the
        passages inside it: `assets.py` never passed `org_settings` to the policy
        engine at all, so a centre that switched the setting on found it
        half-working. Safe direction — too restrictive — which is why nothing
        noticed."""
        xid = _passage(client, author)
        _make_publisher(db, seed)
        assert client.post(f"/api/v1/passage-versions/{xid}/publish",
                           headers=author).status_code == 200

    def test_content_edit_others_lets_a_colleague_edit(self, client, db, seed):
        """The other org setting, dead for the same reason."""
        xid = _passage(client, {"Authorization":
                                f"Bearer {issue_access_token(str(seed['author'].xid))}"})
        colleague = _colleague(db, seed)
        assert client.patch(f"/api/v1/passage-versions/{xid}", headers=colleague,
                            json={"title": "Tidied"}).status_code == 403
        _set_org_setting(db, seed, content_edit_others=True)
        assert client.patch(f"/api/v1/passage-versions/{xid}", headers=colleague,
                            json={"title": "Tidied"}).status_code == 200

    def test_a_new_version_copies_the_current_one(self, client, author, db, seed):
        created = client.post("/api/v1/passages", headers=author,
                              json={"title": "Cartography",
                                    "blocks": PARAGRAPHS}).json()
        client.patch(f"/api/v1/passage-versions/{created['current_version']['xid']}",
                     headers=author, json={"blocks": PARAGRAPHS})

        second = client.post(f"/api/v1/passages/{created['xid']}/versions",
                             headers=author)
        assert second.status_code == 201
        assert second.json()["version_no"] == 2
        assert second.json()["status"] == "draft"
        assert len(second.json()["blocks"]) == len(PARAGRAPHS)

    def test_a_rival_centre_cannot_read_a_version(self, client, seed, db):
        xid = _passage(client, {"Authorization":
                                f"Bearer {issue_access_token(str(seed['author'].xid))}"})
        rival = _rival(db)
        assert client.get(f"/api/v1/passage-versions/{xid}",
                          headers=rival).status_code == 404

    def test_a_rival_centre_cannot_edit_one(self, client, seed, db, author):
        xid = _passage(client, author)
        rival = _rival(db)
        assert client.patch(f"/api/v1/passage-versions/{xid}", headers=rival,
                            json={"title": "Mine now"}).status_code == 404

    def test_an_unknown_version_is_a_404(self, client, author):
        assert client.get(f"/api/v1/passage-versions/{uuid.uuid4()}",
                          headers=author).status_code == 404


class TestPassageUsage:
    def test_an_unused_passage_reports_nothing(self, client, author):
        xid = _passage(client, author)
        response = client.get(f"/api/v1/passage-versions/{xid}/usage",
                              headers=author)
        assert response.status_code == 200
        assert response.json()["references"] == []
        assert response.json()["published_count"] == 0

    def test_a_passage_in_a_test_reports_it(self, client, author, seed):
        """"Shown BEFORE an author edits a shared asset, so 'I changed one
        passage and broke four published mocks' cannot happen by surprise." """
        response = client.get(
            f"/api/v1/passage-versions/{seed['passage_version'].xid}/usage",
            headers=author)
        assert [u["title"] for u in response.json()["references"]] == ["Mock 1 v1"]
        assert response.json()["draft_count"] == 1


class TestQuestionSlots:
    """"Extracted server-side. A client that declares its own slots could smuggle
    a mismatch past the publish gate, which compares key slots to this array." """

    def test_slots_come_from_the_question_text(self, client, author):
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "sentence_completion", "skill": "reading",
            "payload": {"text": "The river is {{s1}} and flows {{s2}}."},
            "key": {"slots": {"s1": {"accept": ["long"]},
                              "s2": {"accept": ["north"]}}}})
        assert response.status_code == 201
        assert response.json()["current_version"]["slot_keys"] == ["s1", "s2"]

    def test_a_declared_slot_list_is_ignored(self, client, author):
        """The smuggling route. Declaring `slots: [s1]` on a two-slot question
        would make the publish gate compare the key against the client's claim."""
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "sentence_completion", "skill": "reading",
            "payload": {"text": "The river is {{s1}} and flows {{s2}}.",
                        "slots": ["s1"]}})
        assert response.json()["current_version"]["slot_keys"] == ["s1", "s2"]

    def test_slots_are_found_inside_lists_of_strings(self, client, author):
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "sentence_completion", "skill": "reading",
            "payload": {"lines": ["First {{s1}}.", "Second {{s2}}."]}})
        assert response.json()["current_version"]["slot_keys"] == ["s1", "s2"]

    def test_slots_are_found_as_keyed_entries(self, client, author):
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "diagram_completion", "skill": "reading",
            "payload": {"slots": [{"key": "s1"}, {"key": "s2"}, {"key": "s3"}]}})
        assert response.json()["current_version"]["slot_keys"] == ["s1", "s2", "s3"]

    def test_a_payload_with_no_markers_falls_back_to_one_slot(self, client, author):
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep is the river?"}})
        assert response.json()["current_version"]["slot_keys"] == ["s1"]

    def test_editing_the_payload_re_extracts_them(self, client, author):
        xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "sentence_completion", "skill": "reading",
            "payload": {"text": "The river is {{s1}}."}}
        ).json()["current_version"]["xid"]
        response = client.patch(f"/api/v1/question-versions/{xid}", headers=author,
                                json={"payload": {"text": "{{s1}} and {{s2}}."}})
        assert response.json()["slot_keys"] == ["s1", "s2"]


class TestQuestionCreation:
    def test_an_unknown_type_is_refused_with_a_finding(self, client, author):
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "telepathy", "skill": "reading", "payload": {}})
        assert response.status_code == 422
        assert response.json()["findings"][0]["code"] == "TYPE_UNKNOWN"

    def test_a_reading_only_type_in_a_listening_question_is_refused(self, client,
                                                                    author):
        response = client.post("/api/v1/questions", headers=author, json={
            "type_key": "matching_headings", "skill": "listening",
            "payload": {"text": "x"}})
        assert response.json()["findings"][0]["code"] == "TYPE_WRONG_SKILL"

    def test_the_answer_key_is_stored_with_the_version(self, client, author):
        xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep?"},
            "key": {"slots": {"s1": {"accept": ["14 metres"]}}}}
        ).json()["current_version"]["xid"]
        keys = client.get(f"/api/v1/question-versions/{xid}/keys", headers=author).json()
        assert keys[0]["key"]["slots"]["s1"]["accept"] == ["14 metres"]
        assert keys[0]["is_current"] is True

    def test_a_question_can_be_created_without_a_key_yet(self, client, author):
        xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep?"}}).json()["current_version"]["xid"]
        assert client.get(f"/api/v1/question-versions/{xid}/keys",
                          headers=author).json() == []

    def test_a_student_cannot_create_one(self, client, student):
        assert client.post("/api/v1/questions", headers=student, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "x"}}).status_code == 403

    def test_a_published_question_version_is_immutable(self, client, author, db,
                                                       published):
        qv = published["question_versions"][0]
        refused = client.patch(f"/api/v1/question-versions/{qv.xid}", headers=author,
                               json={"points": 5})
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_immutable"

    def test_the_bank_search_finds_by_type(self, client, author, seed):
        response = client.get("/api/v1/questions?type_key=sentence_completion",
                              headers=author)
        assert response.status_code == 200
        assert all(q["type_key"] == "sentence_completion"
                   for q in response.json()["items"])

    def test_a_rival_centre_sees_none_of_the_bank(self, client, db, seed):
        assert client.get("/api/v1/questions", headers=_rival(db)).json()["items"] == []


class TestQuestionGroups:
    def test_a_published_group_version_is_immutable(self, client, author, db,
                                                    published):
        gv = published["group_version"]
        refused = client.patch(f"/api/v1/question-group-versions/{gv.xid}",
                               headers=author, json={"instructions": {"en": "New"}})
        assert refused.status_code == 409

    def test_editing_a_draft_group_updates_only_the_declared_fields(self, client,
                                                                    author, seed, db):
        """`update_group_version` assigns straight from the model with `setattr`,
        so what the model does NOT declare is the whole of the protection. An
        undeclared field must be dropped, not written."""
        gv_xid = _draft_group(client, author)
        response = client.patch(f"/api/v1/question-group-versions/{gv_xid}",
                                headers=author,
                                json={"instructions": {"en": "Complete each."},
                                      "status": "published", "version_no": 99})
        assert response.status_code == 200
        assert response.json()["instructions"] == {"en": "Complete each."}
        assert response.json()["status"] == "draft", "status was mass-assigned"
        assert response.json()["version_no"] == 1

    def test_the_word_limit_rule_round_trips(self, client, author):
        """The scorer enforces it, so it has to survive the editor exactly."""
        gv_xid = _draft_group(client, author)
        limit = {"max_words": 2, "allow_number": True, "hyphen_counts_as_one": True,
                 "on_violation": "mark_incorrect"}
        response = client.patch(f"/api/v1/question-group-versions/{gv_xid}",
                                headers=author, json={"word_limit": limit})
        assert response.json()["word_limit"] == limit

    def test_an_existing_question_can_be_pulled_into_a_group(self, client, author):
        """Composition by REFERENCE: reuse a bank item without copying it."""
        gv_xid = _draft_group(client, author)
        qv_xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep?"}}).json()["current_version"]["xid"]
        response = client.post(f"/api/v1/question-group-versions/{gv_xid}/items",
                               headers=author, json={"question_version_xid": qv_xid})
        assert response.status_code == 201

    def test_reading_a_group_version_back(self, client, author):
        gv_xid = _draft_group(client, author)
        response = client.get(f"/api/v1/question-group-versions/{gv_xid}",
                              headers=author)
        assert response.status_code == 200
        assert response.json()["status"] == "draft"

    def test_a_rival_cannot_read_one(self, client, author, db):
        gv_xid = _draft_group(client, author)
        assert client.get(f"/api/v1/question-group-versions/{gv_xid}",
                          headers=_rival(db)).status_code == 404


class TestTranscripts:
    """"Optional transcript upload, used for post-exam review, never exposed
    during the exam." """

    def test_an_author_uploads_and_reads_it_back(self, client, author, with_audio):
        segments = [{"start_ms": 0, "end_ms": 2000, "speaker": "M",
                     "text": "Good morning."}]
        put = client.put(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                         headers=author, json={"language": "en", "segments": segments})
        assert put.status_code == 200
        read = client.get(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                          headers=author)
        assert read.json()["segments"] == segments
        assert read.json()["source"] == "uploaded"

    def test_uploading_again_replaces_it(self, client, author, with_audio):
        for text_value in ("first", "second"):
            client.put(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                       headers=author,
                       json={"segments": [{"start_ms": 0, "end_ms": 900,
                                          "text": text_value}]})
        read = client.get(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                          headers=author)
        assert read.json()["segments"] == [{"start_ms": 0, "end_ms": 900,
                                            "text": "second"}]

    def test_a_track_with_no_transcript_is_a_404(self, client, author, with_audio):
        assert client.get(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
            headers=author).status_code == 404

    def test_the_track_reports_whether_it_has_one(self, client, author, with_audio):
        before = client.get(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}",
                            headers=author).json()
        assert before["has_transcript"] is False
        client.put(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                   headers=author, json={"segments": []})
        after = client.get(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}",
                           headers=author).json()
        assert after["has_transcript"] is True

    def test_it_is_denied_while_the_reader_has_a_live_attempt(
            self, client, author, with_audio, db, seed):
        """The transcript is the answer sheet. An author sitting their own paper
        to check the timing must not be able to open it mid-attempt."""
        client.put(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                   headers=author, json={"segments": [{"start_ms": 0, "end_ms": 900, "text": "x"}]})
        db.execute(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status)
            VALUES (:u, :tv, 'exam', 'in_progress')
        """).bindparams(u=seed["author"].id, tv=with_audio["test_version"].id))
        db.flush()
        refused = client.get(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript", headers=author)
        assert refused.status_code == 403
        assert refused.json()["code"] == "transcript_locked_during_attempt"

    def test_a_submitted_attempt_does_not_lock_it(self, client, author, with_audio,
                                                  db, seed):
        """"Post-exam review" is the whole purpose. Locking after submit would
        make the feature useless."""
        client.put(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                   headers=author, json={"segments": [{"start_ms": 0, "end_ms": 900, "text": "x"}]})
        db.execute(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status, submitted_at)
            VALUES (:u, :tv, 'exam', 'submitted', now())
        """).bindparams(u=seed["author"].id, tv=with_audio["test_version"].id))
        db.flush()
        assert client.get(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
            headers=author).status_code == 200

    def test_a_student_at_the_centre_cannot_read_it_before_sitting(
            self, client, student, author, with_audio):
        """**The leak.** `_owned` required only read-scope, and `scoped()` admits
        every member of the owning organization — so any student at the centre
        could read the transcript of any listening track it owns, which is every
        answer in the paper they are about to sit.

        The live-attempt check did not stop it: that only fires for an attempt
        already `in_progress`, so the bypass was to read the transcript first and
        start the exam second. Confirmed against a real database: 200, with the
        segments.
        """
        client.put(f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
                   headers=author,
                   json={"segments": [{"start_ms": 0, "end_ms": 900,
                                      "text": "Fourteen metres."}]})
        refused = client.get(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
            headers=student)
        assert refused.status_code == 403
        assert "Fourteen metres" not in refused.text

    def test_a_student_cannot_upload_one(self, client, student, with_audio):
        assert client.put(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
            headers=student, json={"segments": []}).status_code == 403


class TestSearchAndCaching:
    def test_passages_can_be_searched_by_title(self, client, author):
        client.post("/api/v1/passages", headers=author,
                    json={"title": "Zoroastrian Fire Temples", "blocks": []})
        client.post("/api/v1/passages", headers=author,
                    json={"title": "Glacial Retreat", "blocks": []})
        hits = client.get("/api/v1/passages?q=zoroast", headers=author).json()["items"]
        assert [p["title"] for p in hits] == ["Zoroastrian Fire Temples"]
        assert len(client.get("/api/v1/passages", headers=author).json()["items"]) > 1

    def test_the_bank_can_be_filtered_by_skill(self, client, author):
        client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "listening",
            "payload": {"text": "How deep?"}})
        hits = client.get("/api/v1/questions?skill=listening",
                          headers=author).json()["items"]
        assert hits and all(q["skill"] == "listening" for q in hits)

    def test_a_passage_version_carries_an_etag(self, client, author):
        """Authoring reads are polled by the editor; an ETag is what stops every
        keystroke costing a full passage over a 3G connection."""
        xid = _passage(client, author)
        response = client.get(f"/api/v1/passage-versions/{xid}", headers=author)
        assert response.headers["ETag"].startswith('"1-')

    def test_a_question_version_carries_one_too(self, client, author):
        xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep?"},
            "key": {"slots": {"s1": {"accept": ["14 m"]}}}}
        ).json()["current_version"]["xid"]
        response = client.get(f"/api/v1/question-versions/{xid}", headers=author)
        assert response.status_code == 200
        assert response.headers["ETag"].startswith('"1-')

    def test_the_points_on_a_draft_question_can_be_changed(self, client, author):
        xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep?"}, "points": 1}
        ).json()["current_version"]["xid"]
        response = client.patch(f"/api/v1/question-versions/{xid}", headers=author,
                                json={"points": 2})
        assert response.json()["points"] == 2

    def test_question_usage_reports_the_tests_that_carry_it(self, client, author,
                                                            db, published):
        qv = published["question_versions"][0]
        question_xid = db.scalar(text("SELECT xid FROM questions WHERE id = :q")
                                 .bindparams(q=qv.question_id))
        response = client.get(f"/api/v1/questions/{question_xid}/usage",
                              headers=author)
        assert response.status_code == 200
        assert [u["title"] for u in response.json()["references"]] == ["Mock 1 v1"]

    def test_platform_global_content_has_no_org_settings_to_read(self, db):
        """`org_settings(None)` is the platform-global case: content owned by no
        organization has no centre whose flags could apply."""
        from app.api.routers.assets import org_settings

        assert org_settings(db, None) == {}


# ── helpers ──────────────────────────────────────────────────────────

def _passage(client, headers) -> str:
    return client.post("/api/v1/passages", headers=headers,
                       json={"title": "Cartography", "blocks": PARAGRAPHS}
                       ).json()["current_version"]["xid"]


def _draft_group(client, headers) -> str:
    return client.post("/api/v1/question-groups", headers=headers,
                       json={"title": "Questions 1-3", "skill": "reading"}
                       ).json()["current_version"]["xid"]


def _set_org_setting(db, seed, **flags) -> None:
    """Through the ORM instance, not raw SQL.

    `seed` already loaded this Organization, so a raw UPDATE leaves the identity
    map holding the old `settings` and the handler reads stale values — a test
    artefact that looks exactly like the feature not working.
    """
    seed["org"].settings = {**(seed["org"].settings or {}), **flags}
    db.flush()


def _make_publisher(db, seed) -> None:
    """Turn the seeded teacher into someone who may publish, via the org setting
    rather than by inventing a new role — that is the documented route."""
    _set_org_setting(db, seed, teacher_can_publish=True)


def _colleague(db, seed) -> dict:
    """A second teacher at the same centre. Not the author of anything."""
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES ('+998907000002', 'Colleague', '1988-01-01', 'active')
        RETURNING id, xid
    """)).mappings().one()
    db.execute(text("""
        INSERT INTO org_memberships (org_id, user_id, role, status)
        VALUES (:o, :u, 'teacher', 'active')
    """).bindparams(o=seed["org"].id, u=row["id"]))
    db.flush()
    return {"Authorization": f"Bearer {issue_access_token(str(row['xid']))}"}


def _rival(db) -> dict:
    """A centre admin at a different organization."""
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES ('+998907000001', 'Rival', '1985-01-01', 'active') RETURNING id, xid
    """)).mappings().one()
    org = db.scalar(text("""
        INSERT INTO organizations (name, slug, status)
        VALUES ('Rival Centre', 'rival-assets', 'active') RETURNING id
    """))
    db.execute(text("""
        INSERT INTO org_memberships (org_id, user_id, role, status)
        VALUES (:o, :u, 'centre_admin', 'active')
    """).bindparams(o=org, u=row["id"]))
    db.flush()
    return {"Authorization": f"Bearer {issue_access_token(str(row['xid']))}"}


# ── the request bodies that were not request bodies ──────────────────

class TestATranscriptSegmentIsASpan:
    """`end_ms` was optional in the contract and mandatory in the only code that
    reads a transcript back.

    `exam.session._excerpt` takes the segments overlapping a question's window
    and skips any that is missing either bound. A contract-conforming upload with
    no `end_ms` therefore stored fine, read back fine through the authoring
    endpoint, and produced an empty review excerpt for every question on the
    track — with nothing anywhere reporting a problem.

    Every transcript fixture in this file was written that way, which is how it
    went unnoticed: the feature had no test that went all the way through.
    """

    def test_a_segment_with_no_end_is_refused(self, client, author, with_audio):
        assert client.put(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
            headers=author,
            json={"segments": [{"start_ms": 0, "text": "Good morning."}]}
        ).status_code == 422

    def test_a_segment_that_ends_before_it_starts_is_refused(self, client, author,
                                                             with_audio):
        """Not caught by requiring the field. `_excerpt` asks
        `start_ms < window_end and end_ms > window_start`, which a reversed span
        satisfies for windows it has nothing to do with — so it would attach the
        wrong words to the wrong question rather than none to any."""
        assert client.put(
            f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript",
            headers=author,
            json={"segments": [{"start_ms": 5000, "end_ms": 100, "text": "x"}]}
        ).status_code == 422

    def test_omitting_segments_entirely_no_longer_wipes_the_transcript(
            self, client, author, with_audio):
        """It was `body.get("segments", [])`, so a PUT with a truncated payload
        replaced a finished transcript with an empty array and answered 200. The
        upload is the only copy — nobody re-types a listening transcript."""
        url = f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript"
        client.put(url, headers=author, json={"segments": [
            {"start_ms": 0, "end_ms": 900, "text": "Fourteen metres."}]})
        assert client.put(url, headers=author, json={"language": "en"}
                          ).status_code == 422
        assert client.get(url, headers=author).json()["segments"]

    def test_clearing_it_deliberately_still_works(self, client, author, with_audio):
        """An explicit empty list is a different statement from an absent field,
        and an author withdrawing a bad transcript is a real action."""
        url = f"/api/v1/audio-tracks/{with_audio['audio_track'].xid}/transcript"
        client.put(url, headers=author, json={"segments": [
            {"start_ms": 0, "end_ms": 900, "text": "Fourteen metres."}]})
        assert client.put(url, headers=author, json={"segments": []}
                          ).status_code == 200
        assert client.get(url, headers=author).json()["segments"] == []


class TestPullingAnItemIntoAGroup:
    """`body["question_version_xid"]` was a bare subscript on an untyped dict."""

    def test_omitting_the_question_is_a_422_and_not_a_500(self, client, author):
        """`KeyError` inside a handler is a 500, and a 500 is what a client
        retries — so a malformed request became repeated load and an alert."""
        gv_xid = _draft_group(client, author)
        assert client.post(f"/api/v1/question-group-versions/{gv_xid}/items",
                           headers=author, json={}).status_code == 422

    def test_a_malformed_xid_is_a_422_and_not_a_500(self, client, author):
        """`uuid.UUID("banana")` raises `ValueError`, by the same route."""
        gv_xid = _draft_group(client, author)
        assert client.post(f"/api/v1/question-group-versions/{gv_xid}/items",
                           headers=author,
                           json={"question_version_xid": "banana"}).status_code == 422

    @pytest.mark.parametrize("position", [0, -1])
    def test_a_position_below_one_is_refused(self, client, author, position):
        """`minimum: 1` was in the contract and in nothing else. Position is the
        order a student answers in; a zero or a negative silently sorts ahead of
        every real item in a published group."""
        gv_xid = _draft_group(client, author)
        qv_xid = client.post("/api/v1/questions", headers=author, json={
            "type_key": "short_answer", "skill": "reading",
            "payload": {"text": "How deep?"}}).json()["current_version"]["xid"]
        assert client.post(
            f"/api/v1/question-group-versions/{gv_xid}/items", headers=author,
            json={"question_version_xid": qv_xid, "position": position}
        ).status_code == 422


class TestACueCardSetHasAnIdentity:
    """`title` and `body` are both `required` in the contract and both were read
    as `.get(field, default)`."""

    def test_an_untitled_set_is_refused(self, client, author):
        """The library lists by title and returns nothing else identifying, so an
        empty one produced a row its author could create and never find again."""
        assert client.post("/api/v1/cue-card-sets", headers=author,
                           json={"body": {"part1": ["Where do you live?"]}}
                           ).status_code == 422

    def test_and_so_is_a_blank_one(self, client, author):
        assert client.post("/api/v1/cue-card-sets", headers=author,
                           json={"title": "", "body": {"part1": ["x"]}}
                           ).status_code == 422

    def test_a_set_with_no_prompts_is_refused(self, client, author):
        """`body.get("body", {})` cast straight to jsonb, so `{}` stored fine and
        reached a speaking session as a prompt set with no prompts in it."""
        assert client.post("/api/v1/cue-card-sets", headers=author,
                           json={"title": "Hometown"}).status_code == 422

    def test_a_body_that_is_not_an_object_is_refused(self, client, author):
        """A list or a string also cast to valid jsonb."""
        assert client.post("/api/v1/cue-card-sets", headers=author,
                           json={"title": "Hometown", "body": ["part1"]}
                           ).status_code == 422

    def test_a_real_set_round_trips_through_all_three_parts(self, client, author):
        """The other half: a validator that refused everything would pass every
        test above. The three parts ARE an IELTS speaking test."""
        created = client.post("/api/v1/cue-card-sets", headers=author, json={
            "title": "Hometown", "tags": ["speaking"],
            "body": {"part1": ["Where do you live?"],
                     "part2": {"topic": "A place you visit",
                               "bullets": ["where it is", "why you go"]},
                     "part3": ["How has your city changed?"]}})
        assert created.status_code == 201
        assert created.json()["title"] == "Hometown"
        assert created.json()["current_version_xid"]
