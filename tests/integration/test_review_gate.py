"""The review gate on publish — and the two other doors publish shared.

`content_reviews` has existed since migration 0008. `submit-review` runs the
publish gate first, `review` records a decision, ADR-0007 §3.7 argues carefully
that approval must not itself publish. And `POST /test-versions/{xid}/publish`
read none of it, so the entire workflow was decoration:

  * a draft nobody had looked at published;
  * a version a reviewer had explicitly rejected published, unchanged;
  * the submitter approved their own request and the row named them as reviewer.

The gate now runs off `settings.require_review`, and an approval binds to a
FINGERPRINT of the content, not to a version id — because a version stays
editable while `in_review`, so approve → change a key → publish was a sequence
one person could run with a real approval on the record.

Two more, found on the way in and both on the helper publish shares with
`validate`:

  * **`POST /test-versions/{xid}/validate` had no authorization at all.** Not
    scoped, no policy call. It returns the publish gate's findings, and those
    quote the material — `KEY_EXCEEDS_WORD_LIMIT` names the accepted answer
    verbatim. A rival centre could read a competitor's section titles and, on a
    test with a key problem, the key; a STUDENT could read the answers to the
    paper they were about to sit. It also wrote a `test_version_validations` row
    against someone else's test.
  * **Publish wrote no audit record**, though the OpenAPI description has said
    "an audit record is written" since the contract was drafted, and "immutable
    audit log for ... all authoring actions" is a stated constraint.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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
    assert response.status_code in (expected or (200, 201, 202)), response.text
    return response.json()


@pytest.fixture
def author(seed):
    """The seeded author — a `teacher`, and `test_versions.created_by`."""
    return auth(seed["author"].xid)


@pytest.fixture
def student(seed):
    return auth(seed["student"].xid)


@pytest.fixture
def centre_admin(db, seed):
    """The centre_admin who publishes. Not the author."""
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Gulnora",
                date_of_birth=dt.date(1980, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def reviewer(db, seed):
    """A second centre_admin. Neither the author nor, in these tests, the
    submitter — the other pair of eyes a review is supposed to be."""
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Kamola",
                date_of_birth=dt.date(1982, 6, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def rival(db, seed):
    """A centre_admin at a DIFFERENT organization, with no relationship to the
    seeded test. The competitor the confidentiality promise is about."""
    from app.modules.identity.models import Organization, OrgMembership, User

    org = Organization(name="Rival Centre", slug=f"rv-{uuid.uuid4().hex[:6]}",
                       status="active")
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=dt.date(1985, 1, 1))
    db.add_all([org, user])
    db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=user.id, role="centre_admin",
                         status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def strict(db, seed):
    """A centre that has turned the requirement on."""
    seed["org"].settings = {"require_review": True}
    db.flush()
    return seed["org"]


def _publish(client, headers, seed):
    return client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                       headers=headers)


def _submit(client, headers, seed, **body):
    return client.post(
        f"/api/v1/test-versions/{seed['test_version'].xid}/submit-review",
        json=body, headers=headers)


def _decide(client, headers, seed, decision, **body):
    return client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/review",
                       json={"decision": decision, **body}, headers=headers)


# ── the gate ─────────────────────────────────────────────────────────

class TestPublishRequiresAnApprovedReview:
    def test_an_unreviewed_version_is_refused(self, client, db, seed, strict,
                                              centre_admin):
        refused = _publish(client, centre_admin, seed)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "review_required"
        db.expire_all()
        assert db.scalar(text("SELECT status FROM test_versions WHERE id = :v")
                         .bindparams(v=seed["test_version"].id)) == "draft"

    def test_a_rejected_version_is_refused(self, client, db, seed, strict,
                                           author, reviewer):
        """The reviewer said no. Before this, publish did not look.

        This is the sharp case: a human read the paper, found a bad key, wrote it
        down — and the author could put it in front of students anyway, with the
        rejection sitting in `content_reviews` next to it.
        """
        _ok(_submit(client, author, seed, notes="ready"))
        _ok(_decide(client, reviewer, seed, "changes_requested",
                    notes="the key for Q2 is wrong"))
        refused = _publish(client, reviewer, seed)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "review_required"

    def test_an_approved_version_publishes(self, client, db, seed, strict,
                                           author, reviewer):
        _ok(_submit(client, author, seed))
        _ok(_decide(client, reviewer, seed, "approved"))
        db.expire_all()
        assert _ok(_publish(client, reviewer, seed))["status"] == "published"

    def test_a_centre_that_does_not_require_review_still_publishes(
            self, client, seed, centre_admin):
        """The default, and the reason it is the default.

        Most centres here are one or two people. Requiring review universally,
        with nobody permitted to approve their own submission, is a one-teacher
        centre that cannot publish at all.
        """
        assert _ok(_publish(client, centre_admin, seed))["status"] == "published"


class TestApprovalIsOverContentNotOverARow:
    """The gate, as opposed to the checkbox.

    A version is editable while it is `in_review` — `_mutable()` refuses
    `published` and `archived` and nothing else. So approval → edit → publish was
    available to a single actor, and the answer key is the field most worth
    changing once somebody else has signed off.
    """

    def _approved(self, client, seed, author, reviewer):
        _ok(_submit(client, author, seed))
        return _ok(_decide(client, reviewer, seed, "approved"))

    def test_an_edit_after_approval_invalidates_it(self, client, db, seed, strict,
                                                   author, reviewer):
        self._approved(client, seed, author, reviewer)
        db.execute(text("""
            UPDATE answer_key_versions
            SET key = '{"slots": {"s1": {"accept": ["tram"]}}}'::jsonb
            WHERE is_current AND question_version_id = :q
        """).bindparams(q=seed["question_versions"][0].id))
        db.flush()
        db.expire_all()
        refused = _publish(client, reviewer, seed)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "review_stale"

    def test_a_key_change_is_what_the_snapshot_checksum_would_miss(
            self, client, db, seed, strict, author, reviewer):
        """`test_versions.checksum` is over `build_snapshot()`, which excludes
        answer keys by design — it is the student-facing document. Reusing it here
        would have been the obvious move and blind to exactly this edit."""
        from app.modules.content import repo as content_repo

        before = content_repo.build_snapshot(
            content_repo.load_composition(db, seed["test_version"].id))
        db.execute(text("""
            UPDATE answer_key_versions
            SET key = '{"slots": {"s1": {"accept": ["tram"]}}}'::jsonb
            WHERE is_current AND question_version_id = :q
        """).bindparams(q=seed["question_versions"][0].id))
        db.flush()
        after = content_repo.build_snapshot(
            content_repo.load_composition(db, seed["test_version"].id))
        assert before == after, "the snapshot cannot see a key change; the gate must"

    def test_a_structural_edit_after_approval_invalidates_it(
            self, client, db, seed, strict, author, reviewer):
        self._approved(client, seed, author, reviewer)
        db.execute(text("UPDATE test_version_sections SET title = 'Passage 1 (rev)' "
                        "WHERE id = :s").bindparams(s=seed["section"].id))
        db.flush()
        db.expire_all()
        assert _publish(client, reviewer, seed).json()["code"] == "review_stale"

    def test_reverting_the_edit_restores_the_approval(self, client, db, seed, strict,
                                                      author, reviewer):
        """The fingerprint is over content, so it is content that decides — not
        an edit counter. Putting it back is putting it back."""
        self._approved(client, seed, author, reviewer)
        db.execute(text("UPDATE test_version_sections SET title = 'Passage 1 (rev)' "
                        "WHERE id = :s").bindparams(s=seed["section"].id))
        db.flush()
        db.expire_all()
        assert _publish(client, reviewer, seed).status_code == 409
        db.execute(text("UPDATE test_version_sections SET title = 'Passage 1' "
                        "WHERE id = :s").bindparams(s=seed["section"].id))
        db.flush()
        db.expire_all()
        assert _ok(_publish(client, reviewer, seed))["status"] == "published"

    def test_an_approval_recording_no_fingerprint_does_not_carry(
            self, client, db, seed, strict, author, reviewer):
        """Rows decided before migration 0022 have `content_checksum` NULL.

        Backfilling one would manufacture the claim the column exists to make
        honest, so they are treated as stale: the approval cannot demonstrate what
        it was over.
        """
        self._approved(client, seed, author, reviewer)
        db.execute(text("UPDATE content_reviews SET content_checksum = NULL"))
        db.flush()
        db.expire_all()
        assert _publish(client, reviewer, seed).json()["code"] == "review_stale"

    def test_the_checksum_is_recorded_only_on_approval(self, client, db, seed,
                                                       author, reviewer):
        _ok(_submit(client, author, seed))
        _ok(_decide(client, reviewer, seed, "changes_requested", notes="no"))
        assert db.scalar(text("SELECT content_checksum FROM content_reviews "
                              "ORDER BY id DESC LIMIT 1")) is None


class TestNobodyApprovesTheirOwnWork:
    """"Otherwise a teacher who cannot publish approves their own work and a
    centre_admin rubber-stamps it — the review becomes theatre."

    `decide_review` said that and checked one rank down: it required publish
    authority and then let whoever held it approve their own submission.
    """

    def test_the_submitter_cannot_approve(self, client, seed, centre_admin):
        _ok(_submit(client, centre_admin, seed))
        refused = _decide(client, centre_admin, seed, "approved")
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "self_approval"

    def test_the_author_cannot_approve_even_when_someone_else_submitted(
            self, client, db, seed, centre_admin, reviewer):
        """Submitter and author are not always the same person, and either one
        approving produces the same worthless record."""
        author_id = db.scalar(text("SELECT user_id FROM org_memberships "
                                   "WHERE role = 'centre_admin' ORDER BY id LIMIT 1"))
        db.execute(text("UPDATE test_versions SET created_by = :u WHERE id = :v")
                   .bindparams(u=author_id, v=seed["test_version"].id))
        db.flush()
        db.expire_all()
        _ok(_submit(client, reviewer, seed))
        refused = _decide(client, centre_admin, seed, "approved")
        assert refused.status_code == 403, refused.text
        assert refused.json()["reason"] == "author"

    def test_requesting_changes_on_your_own_submission_is_fine(self, client, db,
                                                               seed, centre_admin):
        """Only approval is barred. Withdrawing your own work by asking for
        changes harms nobody and is the natural way to take it back."""
        _ok(_submit(client, centre_admin, seed))
        _ok(_decide(client, centre_admin, seed, "changes_requested", notes="oops"))
        db.expire_all()
        assert db.scalar(text("SELECT status FROM test_versions WHERE id = :v")
                         .bindparams(v=seed["test_version"].id)) == "draft"

    def test_a_second_reviewer_can_approve(self, client, db, seed, author, reviewer):
        _ok(_submit(client, author, seed))
        body = _ok(_decide(client, reviewer, seed, "approved"))
        assert body["state"] == "approved"
        assert db.scalar(text("SELECT content_checksum FROM content_reviews "
                              "ORDER BY id DESC LIMIT 1"))


# ── the audit record ─────────────────────────────────────────────────

class TestPublishIsAudited:
    """"On success ... an audit record is written." The contract has said so from
    the start and nothing wrote one."""

    def _rows(self, db, action):
        return db.execute(text("SELECT actor_user_id, org_id, subject_id, after "
                               "FROM audit_log WHERE action = :a")
                          .bindparams(a=action)).mappings().all()

    def test_publishing_writes_one(self, client, db, seed, centre_admin):
        _ok(_publish(client, centre_admin, seed))
        rows = self._rows(db, "content.published")
        assert len(rows) == 1
        assert rows[0]["subject_id"] == str(seed["test_version"].xid)
        assert rows[0]["org_id"] == seed["org"].id
        assert rows[0]["after"]["version_no"] == 1

    def test_it_names_the_reviewer_who_approved(self, client, db, seed, strict,
                                                author, reviewer):
        _ok(_submit(client, author, seed))
        approved = _ok(_decide(client, reviewer, seed, "approved"))
        db.expire_all()
        _ok(_publish(client, reviewer, seed))
        after = self._rows(db, "content.published")[0]["after"]
        assert after["reviewed_by"] == db.scalar(
            text("SELECT reviewer_id FROM content_reviews WHERE id = :i")
            .bindparams(i=uuid.UUID(approved["xid"]).int))
        assert after["review_checksum"]

    def test_a_centre_that_does_not_require_review_says_so(self, client, db, seed,
                                                           centre_admin):
        """`reviewed_by: null` is a real answer to "who signed off", not a gap in
        the record: this centre does not require review."""
        _ok(_publish(client, centre_admin, seed))
        assert self._rows(db, "content.published")[0]["after"]["reviewed_by"] is None

    def test_the_review_decision_is_audited(self, client, db, seed, author, reviewer):
        _ok(_submit(client, author, seed))
        _ok(_decide(client, reviewer, seed, "changes_requested", notes="Q2 key"))
        rows = self._rows(db, "content.review_decided")
        assert len(rows) == 1
        assert rows[0]["after"]["decision"] == "changes_requested"
        assert rows[0]["after"]["requested_by"] == seed["author"].id


# ── the two doors publish shared with validate ───────────────────────

class TestValidateWasUnauthorized:
    """The gate's findings quote the material. `_test_version()` resolved any xid
    for anybody and neither endpoint on it called the policy."""

    @pytest.fixture
    def leaky(self, db, seed):
        """A key that breaks its group's own word limit, so the gate emits a
        finding naming the accepted answer."""
        db.execute(text("""
            UPDATE answer_key_versions
            SET key = '{"slots": {"s1": {"accept": ["the enormous velvet curtain"]}}}'::jsonb
            WHERE is_current AND question_version_id = :q
        """).bindparams(q=seed["question_versions"][0].id))
        db.flush()
        db.expire_all()
        return seed

    def _validate(self, client, headers, seed):
        return client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/validate",
            headers=headers)

    def test_a_competitor_centre_gets_a_404(self, client, leaky, rival):
        """"A centre's material must never leak to competitor centres. This is a
        contractual promise, treat it as one."

        404 rather than 403 on purpose: whether a competitor's test exists is
        itself theirs to know.
        """
        refused = self._validate(client, rival, leaky)
        assert refused.status_code == 404, refused.text
        assert "velvet" not in refused.text

    def test_a_student_at_the_centre_gets_a_403(self, client, leaky, student):
        """The worst version of it. `Action.READ` covers every student at the
        centre for `org_private` content, so a read-level check would have kept
        this open — they would have read the answers to the paper they were about
        to sit, out of a validation report."""
        refused = self._validate(client, student, leaky)
        assert refused.status_code == 403, refused.text
        assert "velvet" not in refused.text

    def test_no_validation_row_is_written_for_a_refused_caller(self, client, db,
                                                               leaky, rival):
        """It was a write as well as a read: a row against someone else's test,
        stamped with the caller's user id."""
        self._validate(client, rival, leaky)
        assert db.scalar(text("SELECT count(*) FROM test_version_validations "
                              "WHERE test_version_id = :v")
                         .bindparams(v=leaky["test_version"].id)) == 0

    def test_the_author_still_gets_the_report(self, client, leaky, author):
        body = _ok(self._validate(client, author, leaky))
        assert any(f["code"] == "KEY_EXCEEDS_WORD_LIMIT" for f in body["findings"])


class TestPublishResolvesScoped:
    def test_a_competitor_centre_gets_a_404_not_a_403(self, client, seed, rival):
        """It was 403 `publish_not_permitted` — refused, but an existence oracle
        for any xid a competitor can guess or harvest."""
        refused = _publish(client, rival, seed)
        assert refused.status_code == 404, refused.text

    def test_a_teacher_at_the_centre_still_gets_the_403(self, client, seed, author):
        """The check `_may_publish()` used to do, now from the central matrix.
        Same code, so a client written against it is unaffected."""
        refused = _publish(client, author, seed)
        assert refused.status_code == 403
        assert refused.json()["code"] == "publish_not_permitted"

    def test_the_teacher_can_publish_setting_still_works(self, client, db, seed,
                                                         author):
        """`_may_publish` reimplemented `Action.PUBLISH` and its one escape hatch.
        Deleting a duplicate is only safe if the survivor does the same thing."""
        seed["org"].settings = {"teacher_can_publish": True}
        db.flush()
        assert _ok(_publish(client, author, seed))["status"] == "published"
