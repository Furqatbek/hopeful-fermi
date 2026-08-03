"""The passage lifecycle, the usage guardrail, and the export, as the console
drives them.

Three screens share this file because they share one question: **what else does
this change?** A passage, an item and a group are REFERENCED by tests, never
copied into them — that reuse is what makes a bank a bank — so one edit reaches
material the author is not looking at. `GET /passage-versions/{xid}/usage` and
`GET /questions/{xid}/usage` exist to answer that before the edit, and every
sentence the console prints beside them is checked here rather than assumed.

The load-bearing one is `TestFrozenAtPublish`. Publishing a TEST materializes a
snapshot — `content_repo.publish` writes the whole document onto
`test_versions.snapshot` and `exam.session` serves exactly that row — so a
published paper cannot be changed by a later edit to its passage, and a draft
can. The console says both of those things; this proves them, and it also finds
where the claim stops: the EXPORT of a published version is rebuilt from live
content, not from the snapshot, so it can hand a centre a file that is not the
paper their students sat.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.deps import issue_access_token


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
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


@pytest.fixture
def admin(db, seed):
    """A centre admin. `Action.PUBLISH` excludes teachers by default, so this is
    who the publish half of these screens is for."""
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
def rival(db):
    """A teacher at a DIFFERENT centre. The contractual promise this suite guards:
    a centre's material must never reach a competitor."""
    from app.modules.identity.models import Organization, OrgMembership, User

    org = Organization(name="Samarkand Prep", slug=f"sp-{_uuid.uuid4().hex[:6]}",
                       status="active")
    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Kamola",
                date_of_birth=dt.date(1992, 6, 1))
    db.add_all([org, user])
    db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=user.id, role="teacher",
                         status="active"))
    db.flush()
    return auth(user.xid)


def question_of(db, question_version):
    from app.modules.content.models import Question

    return db.get(Question, question_version.question_id)


def new_passage(client, headers, title="The history of glass", paragraphs=("One.", "Two.")):
    """Exactly what `PassageLibrary`'s create form sends."""
    return _ok(client.post("/api/v1/passages", headers=headers, json={
        "title": title,
        "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": p}]}
                   for p in paragraphs],
        "attestation": {"claim": "original", "statement_version": "1"},
    }), 201)


def edit_version(client, headers, xid, body):
    """The read-then-write `If-Match` dance `useVersionEdit` performs."""
    read = client.get(f"/api/v1/passage-versions/{xid}", headers=headers)
    assert read.status_code == 200, read.text
    tag = read.headers.get("ETag")
    assert tag, "no ETag to lock against; the console refuses to write without one"
    return client.patch(f"/api/v1/passage-versions/{xid}",
                        headers={**headers, "If-Match": tag}, json=body)


class TestPublishingAPassage:
    def test_a_draft_publishes_and_is_then_refused_every_edit(self, client, admin):
        """Why no Edit control is drawn for a published version: the server
        answers `version_immutable`, and a button that can only 409 is worse than
        no button."""
        created = new_passage(client, admin)
        version = created["current_version"]["xid"]

        published = _ok(client.post(f"/api/v1/passage-versions/{version}/publish",
                                    headers=admin))
        assert published["status"] == "published"

        refused = edit_version(client, admin, version, {"title": "Second thoughts"})
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "version_immutable"

    def test_a_teacher_cannot_publish_until_the_centre_allows_it(
            self, client, db, seed):
        """`Action.PUBLISH` omits teachers on purpose — a centre's reputation
        rides on its published material — with one opt-in per organization. The
        console shows the control to everyone and prints the refusal, because
        which side of that setting a centre is on is not knowable from here."""
        teacher = auth(seed["author"].xid)
        created = new_passage(client, teacher, title="A teacher's passage")
        version = created["current_version"]["xid"]

        refused = client.post(f"/api/v1/passage-versions/{version}/publish",
                              headers=teacher)
        assert refused.status_code == 403
        assert refused.json()["code"] == "publish_not_permitted"

        seed["org"].settings = {"teacher_can_publish": True}
        db.flush()
        allowed = client.post(f"/api/v1/passage-versions/{version}/publish",
                              headers=teacher)
        assert allowed.status_code == 200, allowed.text

    def test_a_rival_centre_can_neither_read_nor_publish_it(
            self, client, admin, rival):
        """Both answers are 404, not 403: existence is itself information about a
        competitor's bank."""
        created = new_passage(client, admin)
        version = created["current_version"]["xid"]

        assert client.get(f"/api/v1/passage-versions/{version}",
                          headers=rival).status_code == 404
        assert client.post(f"/api/v1/passage-versions/{version}/publish",
                           headers=rival).status_code == 404

    def test_publishing_moves_no_test(self, client, db, seed, admin):
        """The sentence the screen prints. Publishing freezes the version and
        nothing else — a section points at ONE passage version, so a paper picks
        this up only when its composition is re-pointed at it."""
        passage_version = seed["passage_version"]
        passage_version.status = "draft"
        db.flush()
        before = _ok(client.get(
            f"/api/v1/passage-versions/{passage_version.xid}/usage", headers=admin))

        _ok(client.post(f"/api/v1/passage-versions/{passage_version.xid}/publish",
                        headers=admin))

        after = _ok(client.get(
            f"/api/v1/passage-versions/{passage_version.xid}/usage", headers=admin))
        assert after == before
        assert seed["section"].passage_version_id == passage_version.id


class TestStartingANewVersion:
    def test_the_new_draft_copies_the_text_and_is_used_by_nothing(
            self, client, db, seed, admin):
        """The other half of the same sentence, and the reason the console sends
        an author to the composition screen afterwards.

        Usage is per VERSION — `TestVersionSection.passage_version_id` names one —
        so a new version starts referenced by nothing at all. An author who
        expected "fix the passage, every paper follows" gets the opposite, and
        this is where they find that out.
        """
        from app.modules.content.models import Passage

        passage = db.get(Passage, seed["passage_version"].passage_id)
        # `create_passage` sets this pointer; the seed fixture does not, and
        # neither does the importer — see `test_a_new_version_of_an_imported_
        # passage_comes_back_empty`. Set it here so this test is about the
        # versioning move rather than about that defect.
        passage.current_version_id = seed["passage_version"].id
        db.flush()
        passage_xid = passage.xid

        used_before = _ok(client.get(
            f"/api/v1/passage-versions/{seed['passage_version'].xid}/usage",
            headers=admin))
        assert used_before["references"], "the seeded paper does use this version"

        draft = _ok(client.post(f"/api/v1/passages/{passage_xid}/versions",
                                headers=admin), 201)
        assert draft["version_no"] == 2
        assert draft["status"] == "draft"
        assert draft["blocks"] == seed["passage_version"].blocks

        assert _ok(client.get(f"/api/v1/passage-versions/{draft['xid']}/usage",
                              headers=admin)) == {
            "published_count": 0, "draft_count": 0, "references": []}


class TestWhatTheListingDoesNotCarry:
    """Two defects the console has to work around. Both are reported, neither is
    fixed here — the contract is frozen — and each justifies a piece of UI that
    should be deleted when the backend catches up."""

    def test_the_passage_listing_returns_no_version_for_any_row(
            self, client, admin, seed):
        """`list_passages` builds every item with `passage_dto(p)` and never
        passes the version, so `current_version` is null on every row of a
        response whose schema declares it.

        Nothing else in the API addresses a passage version: there is no
        `GET /passages/{xid}` and no version listing. So the console can only
        reach the versions that a create or a new-version response handed it,
        which is why `PassageLibrary` keeps a map of them — and why the word count
        and version columns are blank. It is also why the composition screen's
        passage picker sends an empty xid. Delete the map when this is fixed.
        """
        new_passage(client, admin)
        listing = _ok(client.get("/api/v1/passages?limit=100", headers=admin))
        assert listing["items"], "the passages are listed"
        assert all(row["current_version"] is None for row in listing["items"])

    def test_a_new_version_arrives_with_no_word_count_and_does_not_become_current(
            self, client, db, seed, admin):
        """`new_passage_version` copies `blocks` and `paragraph_labels` and
        neither recomputes `word_count` nor moves `passage.current_version_id`.

        The second one is the trap the screen warns about: the copy is taken from
        `current_version_id`, so starting a SECOND new version copies the original
        again rather than the draft in front of the author, and their work is left
        behind on a version the listing cannot even name.
        """
        from app.modules.content.models import Passage, PassageVersion

        created = new_passage(client, admin, paragraphs=("Four words go here.",))
        assert created["current_version"]["word_count"] == 4
        passage_xid = created["xid"]

        second = _ok(client.post(f"/api/v1/passages/{passage_xid}/versions",
                                 headers=admin), 201)
        assert second["blocks"] == created["current_version"]["blocks"]
        assert second["word_count"] == 0, "copied text, uncounted words"

        passage = db.scalars(select(Passage).where(Passage.xid == passage_xid)).one()
        db.refresh(passage)
        current = db.get(PassageVersion, passage.current_version_id)
        assert current.version_no == 1, "the pointer stayed on v1"

        third = _ok(client.post(f"/api/v1/passages/{passage_xid}/versions",
                                headers=admin), 201)
        assert third["version_no"] == 3
        assert third["blocks"] == created["current_version"]["blocks"]

    def test_a_new_version_of_an_imported_passage_comes_back_empty(
            self, client, db, seed, admin):
        """The worst of the three, because it loses text rather than a number.

        `new_passage_version` copies from `passage.current_version_id`, and
        `content.importer` creates a `Passage` and a `PassageVersion` without ever
        setting that pointer. So for every passage a centre imported — which is
        the whole route by which a centre arrives with forty papers — "start a new
        version" copies from nothing and answers with a draft containing no text
        at all. SQLAlchemy warns (`fully NULL primary key identity`) and the
        endpoint carries on.

        The console cannot repair it, so it reports it: the new draft is opened
        and, when it comes back with no blocks, the screen says why rather than
        showing an author an empty passage they will assume they broke.
        """
        from sqlalchemy.exc import SAWarning

        from app.modules.content.models import Passage

        passage = db.get(Passage, seed["passage_version"].passage_id)
        assert passage.current_version_id is None, "as the importer leaves it"

        # Recorded rather than raised: `filterwarnings = ["error"]` turns this
        # into a 500 under test, and the point is what a centre gets in
        # production, where the same call answers 201 with an empty passage.
        with pytest.warns(SAWarning):
            created = client.post(f"/api/v1/passages/{passage.xid}/versions",
                                  headers=admin)
        body = _ok(created, 201)
        assert body["blocks"] == []
        assert body["paragraph_labels"] == []

    def test_a_passage_attestation_is_accepted_and_stored_nowhere(
            self, client, db, admin):
        """The console asks where a passage came from and sends the claim, because
        asking is worth doing — but `PassageCreate` has no `attestation` field, so
        pydantic drops it and no evidence row is written.

        Audio does write one, through `media.record_attestation`. "Copyright
        attestation is logged with every upload" is therefore true of the audio
        route and not of the passage route, and no copy on this screen may say
        otherwise.
        """
        created = new_passage(client, admin)
        rows = db.execute(text(
            "SELECT count(*) FROM content_attestations WHERE subject_type = 'passage'"
        )).scalar()
        assert created["title"]
        assert rows == 0


class TestUsageIsTheGuardrail:
    def test_a_passage_names_the_papers_that_use_it(self, client, seed, admin):
        report = _ok(client.get(
            f"/api/v1/passage-versions/{seed['passage_version'].xid}/usage",
            headers=admin))
        assert report["draft_count"] == 1 and report["published_count"] == 0
        assert report["references"] == [{
            "kind": "test_version", "xid": str(seed["test_version"].xid),
            "title": seed["test_version"].title, "status": "draft"}]

    def test_an_item_names_them_too(self, client, db, seed, admin):
        question = question_of(db, seed["question_versions"][0])
        report = _ok(client.get(f"/api/v1/questions/{question.xid}/usage",
                                headers=admin))
        assert [r["xid"] for r in report["references"]] == [str(seed["test_version"].xid)]

    def test_the_item_report_never_mentions_a_group(self, client, db, seed, admin):
        """`UsageReport.kind` declares `question_group_version` and the summary
        promises "which groups and tests use this item". `_usage()` only ever
        builds `test_version` rows, so the group half is unimplemented — which is
        why the panel links a reference only when it is a test version rather than
        routing a kind it has never seen.
        """
        question = question_of(db, seed["question_versions"][0])
        report = _ok(client.get(f"/api/v1/questions/{question.xid}/usage",
                                headers=admin))
        assert {r["kind"] for r in report["references"]} == {"test_version"}

    def test_an_archived_paper_is_counted_as_a_draft(self, client, seed, admin,
                                                     published):
        """Why the panel recounts the rows instead of printing `draft_count`.

        `_usage` partitions on `status == "published"` and calls the remainder
        drafts, so a retired paper lands in the draft column. "One draft uses
        this" sends an author looking for a paper still being written; "one
        archived" is a different decision entirely.
        """
        before = _ok(client.get(
            f"/api/v1/passage-versions/{seed['passage_version'].xid}/usage",
            headers=admin))
        assert (before["published_count"], before["draft_count"]) == (1, 0)

        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/archive",
                        headers=admin))

        after = _ok(client.get(
            f"/api/v1/passage-versions/{seed['passage_version'].xid}/usage",
            headers=admin))
        assert (after["published_count"], after["draft_count"]) == (0, 1)
        assert after["references"][0]["status"] == "archived"

    def test_usage_does_not_report_a_rival_centres_paper(
            self, client, db, seed, rival, admin):
        """The listing behind this report goes through `filter_content` like every
        other content query. Without it, "where is this used" would be a read of
        another centre's library — the exact leak the policy engine exists to stop,
        arriving through a screen whose whole purpose is to be reassuring.
        """
        refused = client.get(
            f"/api/v1/passage-versions/{seed['passage_version'].xid}/usage",
            headers=rival)
        assert refused.status_code == 404, refused.text


class TestFrozenAtPublish:
    """What a later edit does and does not reach, which is the sentence the panel
    prints and the one thing it must not get wrong."""

    def test_a_published_paper_keeps_the_text_and_letters_it_was_published_with(
            self, client, db, seed, admin):
        # The seeded passage version is published; a passage created through the
        # console is not, and nothing in the publish gate requires it to be. So a
        # published test referencing an editable passage is the ORDINARY state,
        # not a contrived one.
        seed["passage_version"].status = "draft"
        db.flush()
        version_xid = seed["test_version"].xid
        _ok(client.post(f"/api/v1/test-versions/{version_xid}/publish", headers=admin))

        edited = edit_version(client, admin, seed["passage_version"].xid, {"blocks": [
            {"type": "paragraph", "runs": [{"t": "text", "v": "A new first paragraph."}]},
            {"type": "paragraph", "runs": [{"t": "text", "v": "Maps are old."}]}]})
        assert edited.status_code == 200, edited.text
        # The letters moved: what used to be A is now B, and a matching-headings
        # key pointing at A now points at the inserted paragraph.
        assert edited.json()["paragraph_labels"] == ["A", "B"]

        db.expire_all()
        snapshot = seed["test_version"].snapshot["sections"][0]["passage"]
        assert snapshot["blocks"][0]["runs"][0]["v"] == "Maps are old."
        assert snapshot["paragraph_labels"] == ["A", "B", "C"]

    def test_a_draft_takes_the_material_as_it_stands_when_it_is_published(
            self, client, db, seed, admin):
        """The other half of the panel's sentence, and the half that bites.

        A draft holds no copy. `content_repo.publish` builds the snapshot from
        live content at the moment of publishing, so today's edit is in the paper
        that goes out next week — which is why a draft reference in the usage list
        is not the harmless kind.
        """
        seed["passage_version"].status = "draft"
        db.flush()
        _ok(edit_version(client, admin, seed["passage_version"].xid, {"blocks": [
            {"type": "paragraph", "runs": [{"t": "text", "v": "Edited before publish."}]}]}))

        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=admin))

        db.expire_all()
        snapshot = seed["test_version"].snapshot["sections"][0]["passage"]
        assert snapshot["blocks"][0]["runs"][0]["v"] == "Edited before publish."
        assert snapshot["paragraph_labels"] == ["A"]

    def test_the_export_of_a_published_paper_is_not_the_published_paper(
            self, client, db, seed, admin):
        """A gap worth reporting rather than papering over.

        `export_version` calls `load_composition` and rebuilds the document, where
        every other reader of a published version — `exam.session`, the
        competition entry pack — reads `test_versions.snapshot`. So the file a
        centre downloads for their records is the material as it stands today, and
        after the edit above it is not what anybody sat. The export panel says so.
        """
        seed["passage_version"].status = "draft"
        db.flush()
        version_xid = seed["test_version"].xid
        _ok(client.post(f"/api/v1/test-versions/{version_xid}/publish", headers=admin))
        _ok(edit_version(client, admin, seed["passage_version"].xid, {"blocks": [
            {"type": "paragraph", "runs": [{"t": "text", "v": "A new first paragraph."}]},
            {"type": "paragraph", "runs": [{"t": "text", "v": "Maps are old."}]}]}))

        exported = _ok(client.get(
            f"/api/v1/test-versions/{version_xid}/export?format=json&include_keys=false",
            headers=admin))
        passage = exported["sections"][0]["passage"]
        assert passage["blocks"][0]["runs"][0]["v"] == "A new first paragraph."

        db.expire_all()
        snapshot = seed["test_version"].snapshot["sections"][0]["passage"]
        assert passage != snapshot, (
            "the export and the sat paper have diverged, and only the export moved")


class TestExport:
    def test_json_carries_the_keys_only_when_they_are_asked_for(
            self, client, seed, admin, published):
        """The contract says "JSON export includes answer keys"; the handler adds
        `answer_keys` only under `include_keys`. The code is the safer of the two
        and the checkbox is therefore real, so the console follows the code."""
        base = f"/api/v1/test-versions/{seed['test_version'].xid}/export?format=json"
        plain = _ok(client.get(f"{base}&include_keys=false", headers=admin))
        assert "answer_keys" not in plain
        assert plain["export"]["includes_keys"] is False

        keyed = _ok(client.get(f"{base}&include_keys=true", headers=admin))
        assert keyed["export"]["includes_keys"] is True
        accepted = [slot["accept"] for entry in keyed["answer_keys"].values()
                    for slot in entry["key"]["slots"].values()]
        assert ["bicycle"] in accepted

    def test_csv_is_the_columns_the_importer_reads(self, client, seed, admin,
                                                   published):
        """The round trip only exists if export and import speak one format."""
        base = f"/api/v1/test-versions/{seed['test_version'].xid}/export?format=csv"
        plain = client.get(f"{base}&include_keys=false", headers=admin)
        assert plain.headers["content-type"].startswith("text/csv")
        assert "attachment" in plain.headers["content-disposition"]

        rows = list(csv.DictReader(io.StringIO(plain.text)))
        assert list(rows[0]) == ["test_title", "section", "skill", "group",
                                 "instructions", "word_limit", "type_key", "text",
                                 "answer"]
        assert all(row["answer"] == "" for row in rows), "no keys unless asked"

        keyed = list(csv.DictReader(io.StringIO(
            client.get(f"{base}&include_keys=true", headers=admin).text)))
        assert [row["answer"] for row in keyed] == ["bicycle", "library", "museum"]

    def test_asking_for_word_answers_with_the_json_file(self, client, seed, admin,
                                                        published):
        """Why the format select offers two options and not the contract's three.

        `format` is an enum of `[json, csv, docx]` and `export_version` has no
        docx branch, so the request falls through to JSON and returns
        `application/json` named `.json`. `/imports/template` refuses the same
        request with an explanation; this one does not. A console option named
        Word would download a file Word cannot open.
        """
        response = client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}/export?format=docx",
            headers=admin)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["content-disposition"].endswith('.json"')

    def test_a_student_may_not_export_a_paper(self, client, seed, published):
        """`Action.EXPORT` is staff-only. A student is a member of this centre, so
        the version is visible to them in the app — and must not be walkable out
        of it as a file."""
        refused = client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}/export?format=json",
            headers=auth(seed["student"].xid))
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "export_not_permitted"

    def test_a_rival_centre_cannot_export_it_at_all(self, client, seed, rival,
                                                    published):
        refused = client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}/export?format=json",
            headers=rival)
        assert refused.status_code == 404

    def test_a_teacher_may_export_the_keys(self, client, seed, published):
        """Not an oversight to work around in the UI: `Action.EXPORT` and
        `Action.VIEW_EXPOSURE` carry the same three roles, so the second check
        refuses nobody the first one admitted. The checkbox is offered to whoever
        can export, and the copy claims no more than that."""
        keyed = _ok(client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}"
            "/export?format=json&include_keys=true",
            headers=auth(seed["author"].xid)))
        assert "answer_keys" in keyed

    def test_exporting_the_keys_writes_no_audit_row(self, client, db, seed, admin,
                                                    published):
        """Checked because the console must not imply otherwise.

        Every authoring and regrade action lands in the immutable audit log;
        walking out with a paper and its answers does not. The export panel
        therefore says the file contains every answer, and does not say the
        download is recorded — because it is not.
        """
        before = db.execute(text("SELECT count(*) FROM audit_log")).scalar()
        _ok(client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}"
            "/export?format=json&include_keys=true", headers=admin))
        after = db.execute(text("SELECT count(*) FROM audit_log")).scalar()
        assert after == before
