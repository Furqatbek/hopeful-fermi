"""The review round trip as the console performs it.

`require_review` is a switch the Roster screen offers, and until this the console
had `submit-review` and `publish` and no way to APPROVE — so turning the setting
on produced a centre that could submit a version and never publish it again.
Every request below is one a screen makes.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

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
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def _member(db, seed, name, role):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id, role=role,
                         status="active"))
    db.flush()
    return user


@pytest.fixture
def centre_requires_review(db, seed):
    """Through the ORM, not a raw UPDATE: `_settings` reads the org with
    `session.get`, which serves the identity map — a raw statement changes the
    row and leaves the object the handler sees untouched."""
    seed["org"].settings = {"require_review": True}
    db.flush()


@pytest.fixture
def ready(client, db, seed, published):
    """A draft that passes the gate, submitted for review by its author."""
    source = published["test_version"]
    version = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions",
                              headers=auth(seed["author"].xid), json={}), 201)
    # Seeded from the published version, so it already passes the gate.
    _ok(client.post(f"/api/v1/test-versions/{version['xid']}/submit-review",
                    headers=auth(seed["author"].xid), json={"notes": "Ready for a look"}))
    assert source is not None
    return version["xid"]


class TestTheReviewStateTheConsoleRenders:
    def test_a_fresh_version_reports_no_request(self, client, seed):
        version = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions",
                                  headers=auth(seed["author"].xid), json={}), 201)
        state = _ok(client.get(f"/api/v1/test-versions/{version['xid']}",
                               headers=auth(seed["author"].xid)))["review"]
        # Not "no review" — no review REQUEST. The console needs the difference
        # between "nobody has asked" and "asked and waiting".
        assert state["state"] is None
        assert state["can_decide"] is False

    def test_a_submitted_version_names_who_asked(self, client, seed, ready):
        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(seed["author"].xid)))["review"]
        assert state["state"] == "requested"
        assert state["requested_by"]["xid"] == str(seed["author"].xid)
        assert state["requested_at"]

    def test_the_author_is_not_offered_the_button(self, client, seed, ready):
        """Offering the control anyway would make it a button that always fails.

        The seeded author is a teacher, so the PUBLISH check refuses first and
        the caller never reaches the self-approval rule — two reasons, and the
        code names the one that actually fired rather than the more interesting
        one. `can_decide` folds both, which is exactly why the client should not
        try to work it out.
        """
        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(seed["author"].xid)))["review"]
        assert state["can_decide"] is False
        refused = client.post(f"/api/v1/test-versions/{ready}/review",
                              headers=auth(seed["author"].xid),
                              json={"decision": "approved"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "publish_not_permitted"

    def test_a_reviewer_cannot_approve_their_own_submission(self, client, db, seed):
        """The self-approval rule proper: publish authority is not enough when
        the work is yours. "Approved by the person who wrote it" answers "who
        signed off?" with a name that means nothing."""
        admin = _member(db, seed, "Rustam", "centre_admin")
        version = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions",
                                  headers=auth(admin.xid), json={}), 201)
        _ok(client.post(f"/api/v1/test-versions/{version['xid']}/submit-review",
                        headers=auth(admin.xid), json={}))

        state = _ok(client.get(f"/api/v1/test-versions/{version['xid']}",
                               headers=auth(admin.xid)))["review"]
        assert state["can_decide"] is False

        refused = client.post(f"/api/v1/test-versions/{version['xid']}/review",
                              headers=auth(admin.xid), json={"decision": "approved"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "self_approval"

    def test_a_centre_admin_who_did_not_write_it_is(self, client, db, seed, ready):
        admin = _member(db, seed, "Rustam", "centre_admin")
        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(admin.xid)))["review"]
        assert state["can_decide"] is True

    def test_a_teacher_without_publish_authority_is_not(self, client, db, seed, ready):
        """Approving is publish-adjacent. A teacher who cannot publish must not
        be able to wave content through to somebody who can."""
        other = _member(db, seed, "Malika", "teacher")
        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(other.xid)))["review"]
        assert state["can_decide"] is False


class TestApprovingUnblocksPublishing:
    def test_the_round_trip(self, client, db, seed, ready, centre_requires_review):
        admin = _member(db, seed, "Rustam", "centre_admin")

        # Before approval, publishing is refused — the whole point of the switch.
        refused = client.post(f"/api/v1/test-versions/{ready}/publish",
                              headers=auth(admin.xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "review_required"

        decided = _ok(client.post(f"/api/v1/test-versions/{ready}/review",
                                  headers=auth(admin.xid),
                                  json={"decision": "approved", "notes": "Looks right"}))
        assert decided["state"] == "approved"

        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(admin.xid)))["review"]
        assert state["state"] == "approved"
        assert state["is_stale"] is False
        # Approval is not a deploy: the version is still `in_review` and somebody
        # has to publish it deliberately.
        assert _ok(client.get(f"/api/v1/test-versions/{ready}",
                              headers=auth(admin.xid)))["status"] == "in_review"

        published = _ok(client.post(f"/api/v1/test-versions/{ready}/publish",
                                    headers=auth(admin.xid)))
        assert published["status"] == "published"

    def test_changes_requested_sends_it_back_with_the_reason(
            self, client, db, seed, ready):
        admin = _member(db, seed, "Rustam", "centre_admin")
        _ok(client.post(f"/api/v1/test-versions/{ready}/review",
                        headers=auth(admin.xid),
                        json={"decision": "changes_requested",
                              "notes": "Question 4's key accepts a wrong spelling"}))
        body = _ok(client.get(f"/api/v1/test-versions/{ready}",
                              headers=auth(seed["author"].xid)))
        # Back to draft so the author can actually fix it.
        assert body["status"] == "draft"
        assert body["review"]["state"] == "changes_requested"
        # The verdict alone is useless; the author needs the reason.
        assert "wrong spelling" in body["review"]["notes"]
        assert body["review"]["reviewer"]["xid"] == str(admin.xid)


class TestAnApprovalDoesNotSurviveAnEdit:
    def test_editing_after_approval_is_reported_before_publish_refuses(
            self, client, db, seed, ready, centre_requires_review):
        """A version stays editable while `in_review`, so approve → change the
        answer key → publish is a sequence one person could otherwise run. The
        publish gate catches it; this reports it on the screen where the editing
        happened, rather than refusing at the last step with nothing said about
        what moved."""
        admin = _member(db, seed, "Rustam", "centre_admin")
        _ok(client.post(f"/api/v1/test-versions/{ready}/review",
                        headers=auth(admin.xid), json={"decision": "approved"}))
        assert _ok(client.get(f"/api/v1/test-versions/{ready}",
                              headers=auth(admin.xid)))["review"]["is_stale"] is False

        # The author changes a key on the approved content.
        qv = db.execute(text("""
            SELECT qv.xid FROM question_versions qv
            JOIN question_group_items i ON i.question_version_id = qv.id
            LIMIT 1""")).scalar()
        _ok(client.post(f"/api/v1/question-versions/{qv}/keys",
                        headers=auth(seed["author"].xid),
                        json={"key": {"slots": {"s1": {"accept": ["anything"]}}},
                              "reason": "key_fix"}), 201)

        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(admin.xid)))["review"]
        assert state["state"] == "approved"
        assert state["is_stale"] is True

        refused = client.post(f"/api/v1/test-versions/{ready}/publish",
                              headers=auth(admin.xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "review_stale"


class TestResubmittingAfterAnEdit:
    """A stale approval must not be a dead end. The version cannot be published
    and cannot be edited back into approval by itself — the only way forward is
    to ask again, and the screen has to be able to see that it worked."""

    def test_the_new_request_is_what_the_screen_reports(
            self, client, db, seed, ready, centre_requires_review):
        admin = _member(db, seed, "Rustam", "centre_admin")
        _ok(client.post(f"/api/v1/test-versions/{ready}/review",
                        headers=auth(admin.xid), json={"decision": "approved"}))
        qv = db.execute(text("""
            SELECT qv.xid FROM question_versions qv
            JOIN question_group_items i ON i.question_version_id = qv.id
            LIMIT 1""")).scalar()
        _ok(client.post(f"/api/v1/question-versions/{qv}/keys",
                        headers=auth(seed["author"].xid),
                        json={"key": {"slots": {"s1": {"accept": ["changed"]}}},
                              "reason": "key_fix"}), 201)
        assert _ok(client.get(f"/api/v1/test-versions/{ready}",
                              headers=auth(admin.xid)))["review"]["is_stale"] is True

        _ok(client.post(f"/api/v1/test-versions/{ready}/submit-review",
                        headers=auth(seed["author"].xid), json={}))

        # **The old `approved` row and the new `requested` row are stamped in the
        # same transaction**, so `ORDER BY created_at DESC` chose between them
        # arbitrarily and the screen went on reading "approved": the reviewer was
        # offered nothing to decide and the request sat there invisible.
        state = _ok(client.get(f"/api/v1/test-versions/{ready}",
                               headers=auth(admin.xid)))["review"]
        assert state["state"] == "requested"
        assert state["can_decide"] is True
        assert state["is_stale"] is False

    def test_approving_the_new_request_publishes(
            self, client, db, seed, ready, centre_requires_review):
        admin = _member(db, seed, "Rustam", "centre_admin")
        _ok(client.post(f"/api/v1/test-versions/{ready}/review",
                        headers=auth(admin.xid), json={"decision": "approved"}))
        qv = db.execute(text("""
            SELECT qv.xid FROM question_versions qv
            JOIN question_group_items i ON i.question_version_id = qv.id
            LIMIT 1""")).scalar()
        _ok(client.post(f"/api/v1/question-versions/{qv}/keys",
                        headers=auth(seed["author"].xid),
                        json={"key": {"slots": {"s1": {"accept": ["changed"]}}},
                              "reason": "key_fix"}), 201)
        _ok(client.post(f"/api/v1/test-versions/{ready}/submit-review",
                        headers=auth(seed["author"].xid), json={}))
        _ok(client.post(f"/api/v1/test-versions/{ready}/review",
                        headers=auth(admin.xid), json={"decision": "approved"}))
        # Approved over the CHANGED content this time, so the fingerprint matches
        # and the gate opens.
        published = _ok(client.post(f"/api/v1/test-versions/{ready}/publish",
                                    headers=auth(admin.xid)))
        assert published["status"] == "published"
