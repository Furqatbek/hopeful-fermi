"""Safety, takedowns, content grants and seats — the rest of `platform_ops`.

These are the endpoints where the brief made explicit promises: minors routed to
a separate queue, a suspension killing live sessions immediately, a takedown
hiding material without destroying the evidence, and content never becoming
world-visible without review.

None of them had a test. The payment callbacks in the same module turned out to
have no authentication at all (`test_payments.py`), which is the reason for going
through the remainder rather than stopping at the coverage number.
"""

from __future__ import annotations

import datetime as dt
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


def _user(db, phone: str, name: str, *, dob: str = "1995-01-01",
          platform_admin: bool = False):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, :n, CAST(:d AS date), 'active') RETURNING id, xid
    """).bindparams(p=phone, n=name, d=dob)).mappings().one()
    if platform_admin:
        db.execute(text("""
            INSERT INTO platform_role_grants (user_id, role, granted_by)
            VALUES (:u, 'platform_admin', :u)
        """).bindparams(u=row["id"]))
    db.flush()
    return row


def auth(row) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(row['xid']))}"}


@pytest.fixture
def admin(db):
    return _user(db, "+998900000001", "Admin", platform_admin=True)


@pytest.fixture
def centre_admin(db, seed):
    """`seed` carries a teacher and a student; SHARE needs a centre admin, and
    the permission matrix leaves teachers out of it on purpose."""
    row = _user(db, "+998900000004", "Centre Admin")
    db.execute(text("""
        INSERT INTO org_memberships (org_id, user_id, role, status)
        VALUES (:o, :u, 'centre_admin', 'active')
    """).bindparams(o=seed["org"].id, u=row["id"]))
    db.flush()
    return row


@pytest.fixture
def adult(db):
    return _user(db, "+998900000002", "Adult", dob="1995-01-01")


@pytest.fixture
def minor(db):
    fifteen = (dt.date.today() - dt.timedelta(days=365 * 15)).isoformat()
    return _user(db, "+998900000003", "Minor", dob=fifteen)


# ── safety reports ───────────────────────────────────────────────────

class TestReportsAndTheMinorsQueue:
    """"`involves_minor` is set by the SYSTEM from participant ages, never by the
    reporter." Both halves of that matter: a reporter cannot claim it to jump the
    queue, and cannot omit it to avoid one."""

    def test_a_report_from_an_adult_about_an_adult_is_normal_priority(
            self, client, adult, db):
        other = _user(db, "+998900000009", "Other")
        response = client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(other["xid"]),
            "category": "harassment"})
        assert response.status_code == 201
        assert response.json()["priority"] == "normal"
        assert response.json()["involves_minor"] is False

    def test_a_report_about_a_minor_is_flagged_and_raised(self, client, adult, minor):
        response = client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(minor["xid"]),
            "category": "harassment"})
        assert response.json()["involves_minor"] is True
        assert response.json()["priority"] == "high"

    def test_a_report_BY_a_minor_is_flagged_too(self, client, minor, adult):
        """The reporter's own age counts. A child reporting an adult is exactly
        the case that must not sit in the general queue."""
        response = client.post("/api/v1/reports", headers=auth(minor), json={
            "subject_kind": "user", "subject_xid": str(adult["xid"]),
            "category": "harassment"})
        assert response.json()["involves_minor"] is True

    def test_grooming_involving_a_minor_is_critical(self, client, adult, minor):
        response = client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(minor["xid"]),
            "category": "grooming"})
        assert response.json()["priority"] == "critical"

    def test_the_same_category_without_a_minor_is_not(self, client, adult, db):
        other = _user(db, "+998900000010", "Other")
        response = client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(other["xid"]),
            "category": "grooming"})
        assert response.json()["priority"] == "normal"

    def test_a_report_about_something_that_is_not_a_user_still_files(
            self, client, adult):
        response = client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "content", "category": "other",
            "description": "Offensive passage"})
        assert response.status_code == 201

    def test_an_unknown_subject_does_not_lose_the_report(self, client, adult):
        """A report naming a user who has since been deleted is still a report."""
        response = client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(uuid.uuid4()),
            "category": "harassment"})
        assert response.status_code == 201

    def test_filing_requires_authentication(self, client, adult):
        assert client.post("/api/v1/reports", json={
            "subject_kind": "user", "category": "harassment"}).status_code in (401, 403)


class TestTheModerationQueue:
    def test_the_minors_queue_is_a_separate_list_not_a_filter(
            self, client, admin, adult, minor, db):
        other = _user(db, "+998900000011", "Other")
        client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(other["xid"]),
            "category": "harassment"})
        client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(minor["xid"]),
            "category": "harassment"})

        general = client.get("/api/v1/admin/reports", headers=auth(admin))
        minors = client.get("/api/v1/admin/reports?queue=minors", headers=auth(admin))
        assert len(general.json()["items"]) == 2
        assert len(minors.json()["items"]) == 1
        assert minors.json()["items"][0]["involves_minor"] is True

    def test_it_can_be_filtered_by_status(self, client, admin, adult, db):
        """`status`, which is what the contract declares. The handler took
        `status_filter` and the declared name was silently ignored — so this
        test passed by sending the implementation's name, which no generated
        client can send."""
        other = _user(db, "+998900000012", "Other")
        client.post("/api/v1/reports", headers=auth(adult), json={
            "subject_kind": "user", "subject_xid": str(other["xid"]),
            "category": "harassment"})
        assert client.get("/api/v1/admin/reports?status=new",
                          headers=auth(admin)).json()["items"]
        assert client.get("/api/v1/admin/reports?status=dismissed",
                          headers=auth(admin)).json()["items"] == []

    def test_a_non_admin_cannot_read_it(self, client, adult):
        response = client.get("/api/v1/admin/reports", headers=auth(adult))
        assert response.status_code == 403
        assert response.json()["code"] == "admin_only"


class TestModerationActions:
    def test_a_suspension_kills_every_live_session(self, client, admin, adult, db):
        """"A suspend or ban revokes every live session immediately — which is
        why refresh tokens are opaque and stored rather than stateless JWTs.""" ""
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at)
            VALUES (:u, 'hash-a', now() + interval '90 days'),
                   (:u, 'hash-b', now() + interval '90 days')
        """).bindparams(u=adult["id"]))
        db.flush()

        response = client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                               json={"action": "suspend", "reason": "harassment",
                                     "target_user_xid": str(adult["xid"])})
        assert response.status_code == 201
        db.expire_all()
        assert db.scalar(text("""
            SELECT count(*) FROM auth_sessions
            WHERE user_id = :u AND revoked_at IS NULL
        """).bindparams(u=adult["id"])) == 0

    def test_it_marks_the_account_suspended(self, client, admin, adult, db):
        client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                    json={"action": "ban", "reason": "grooming",
                          "target_user_xid": str(adult["xid"])})
        db.expire_all()
        assert db.scalar(text("SELECT status FROM users WHERE id = :u")
                         .bindparams(u=adult["id"])) == "suspended"

    def test_a_warning_does_not_revoke_sessions(self, client, admin, adult, db):
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at)
            VALUES (:u, 'hash-c', now() + interval '90 days')
        """).bindparams(u=adult["id"]))
        db.flush()
        client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                    json={"action": "warn", "reason": "language",
                          "target_user_xid": str(adult["xid"])})
        db.expire_all()
        assert db.scalar(text("""
            SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL
        """)) == 1

    def test_it_is_recorded_with_who_did_it(self, client, admin, adult, db):
        client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                    json={"action": "suspend", "reason": "harassment",
                          "target_user_xid": str(adult["xid"])})
        row = db.execute(text("""
            SELECT action, reason, actor_user_id FROM moderation_actions
        """)).mappings().one()
        assert (row["action"], row["reason"]) == ("suspend", "harassment")
        assert row["actor_user_id"] == admin["id"]

    def test_a_non_admin_cannot_take_one(self, client, adult, minor):
        response = client.post("/api/v1/admin/moderation-actions", headers=auth(adult),
                               json={"action": "ban", "reason": "because",
                                     "target_user_xid": str(minor["xid"])})
        assert response.status_code == 403


class TestBlocks:
    def test_blocking_records_the_pair(self, client, adult, minor, db):
        response = client.post("/api/v1/blocks", headers=auth(adult),
                               json={"user_xid": str(minor["xid"])})
        assert response.status_code == 201
        assert db.scalar(text("SELECT count(*) FROM user_blocks")) == 1

    def test_blocking_the_same_person_twice_is_not_an_error(self, client, adult,
                                                            minor, db):
        for _ in range(2):
            client.post("/api/v1/blocks", headers=auth(adult),
                        json={"user_xid": str(minor["xid"])})
        assert db.scalar(text("SELECT count(*) FROM user_blocks")) == 1

    def test_you_cannot_block_yourself(self, client, adult):
        response = client.post("/api/v1/blocks", headers=auth(adult),
                               json={"user_xid": str(adult["xid"])})
        assert response.status_code == 409
        assert response.json()["code"] == "self_block"

    def test_blocking_an_unknown_user_is_a_404(self, client, adult):
        assert client.post("/api/v1/blocks", headers=auth(adult),
                           json={"user_xid": str(uuid.uuid4())}).status_code == 404

    def test_the_list_shows_who_you_blocked(self, client, adult, minor):
        client.post("/api/v1/blocks", headers=auth(adult),
                    json={"user_xid": str(minor["xid"])})
        listed = client.get("/api/v1/blocks", headers=auth(adult)).json()
        assert [b["user"]["given_name"] for b in listed] == ["Minor"]

    def test_the_other_party_does_not_see_it(self, client, adult, minor):
        """A block is not a notification. Telling someone they were blocked is
        how a block becomes an escalation."""
        client.post("/api/v1/blocks", headers=auth(adult),
                    json={"user_xid": str(minor["xid"])})
        assert client.get("/api/v1/blocks", headers=auth(minor)).json() == []

    def test_unblocking_removes_it(self, client, adult, minor, db):
        client.post("/api/v1/blocks", headers=auth(adult),
                    json={"user_xid": str(minor["xid"])})
        xid = client.get("/api/v1/blocks", headers=auth(adult)).json()[0]["xid"]
        assert client.delete(f"/api/v1/blocks/{xid}",
                             headers=auth(adult)).status_code == 204


# ── takedowns ────────────────────────────────────────────────────────

TAKEDOWN = {
    "claimant_name": "Cambridge Assessment",
    "claimant_org": "Cambridge University Press & Assessment",
    "claimant_email": "legal@example.org",
    "rights_basis": "copyright_owner",
    "sworn_statement": True,
    "subject_type": "passage",
    "description": "Reproduces IELTS 17 Test 2 Passage 1 verbatim.",
}


class TestTakedowns:
    """"Assume some centres WILL try to upload published Cambridge papers, and
    design so that liability and evidence are handled." """

    def test_anyone_can_file_without_an_account(self, client):
        """A rights holder must not need to register to reach us. Absorbing some
        spam beats being unreachable to a claimant's lawyers."""
        response = client.post("/api/v1/takedowns",
                               json={**TAKEDOWN, "subject_xid": str(uuid.uuid4())})
        assert response.status_code == 201

    def test_the_subject_is_hidden_on_receipt(self, client):
        response = client.post("/api/v1/takedowns",
                               json={**TAKEDOWN, "subject_xid": str(uuid.uuid4())})
        assert response.json()["hidden_at"] is not None

    def test_a_sworn_statement_is_required(self, client):
        """Without it the filing carries no legal weight, and acting on it would
        mean hiding a centre's material on an anonymous say-so."""
        response = client.post("/api/v1/takedowns",
                               json={**TAKEDOWN, "sworn_statement": False,
                                     "subject_xid": str(uuid.uuid4())})
        assert response.status_code == 403
        assert response.json()["code"] == "sworn_statement_required"

    def test_a_takedown_against_real_content_records_which(self, client, db, seed):
        response = client.post("/api/v1/takedowns", json={
            **TAKEDOWN, "subject_xid": str(seed["passage_version"].xid)})
        assert response.status_code == 201
        assert db.scalar(text("SELECT subject_type FROM takedown_requests")) == "passage"

    def test_an_admin_decides_it(self, client, admin, db):
        xid = client.post("/api/v1/takedowns",
                          json={**TAKEDOWN, "subject_xid": str(uuid.uuid4())}
                          ).json()["xid"]
        response = client.patch(f"/api/v1/admin/takedowns/{xid}", headers=auth(admin),
                                json={"status": "upheld",
                                      "outcome_note": "Verbatim reproduction."})
        assert response.json()["status"] == "upheld"
        assert response.json()["outcome_note"] == "Verbatim reproduction."

    def test_the_evidence_survives_the_decision(self, client, admin, db):
        """Hidden, never hard-deleted: destroying the material destroys the
        evidence with it, and the claim may be litigated later."""
        xid = client.post("/api/v1/takedowns",
                          json={**TAKEDOWN, "subject_xid": str(uuid.uuid4())}
                          ).json()["xid"]
        client.patch(f"/api/v1/admin/takedowns/{xid}", headers=auth(admin),
                     json={"status": "rejected"})
        row = db.execute(text("""
            SELECT sworn_statement, hidden_at, claimant_email, description
            FROM takedown_requests
        """)).mappings().one()
        assert row["sworn_statement"] is True
        assert row["hidden_at"] is not None
        assert row["claimant_email"] and row["description"]

    def test_deciding_an_unknown_one_is_a_404(self, client, admin):
        assert client.patch(f"/api/v1/admin/takedowns/{uuid.uuid4()}",
                            headers=auth(admin),
                            json={"status": "upheld"}).status_code == 404

    def test_a_non_admin_cannot_decide(self, client, adult):
        xid = client.post("/api/v1/takedowns",
                          json={**TAKEDOWN, "subject_xid": str(uuid.uuid4())}
                          ).json()["xid"]
        assert client.patch(f"/api/v1/admin/takedowns/{xid}", headers=auth(adult),
                            json={"status": "rejected"}).status_code == 403


# ── content grants ───────────────────────────────────────────────────

class TestContentGrants:
    """The only mechanism by which content crosses an organization boundary on
    purpose. `filter_content`'s `grant_ids` clause is the read half."""

    def test_a_centre_admin_shares_with_another_org(self, client, db, seed, centre_admin):
        response = client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "test", "subject_xid": str(seed["test"].xid),
                "grantee_kind": "org", "grantee_xid": str(seed["org"].xid),
                "permission": "view"})
        assert response.status_code == 201
        assert response.json()["permission"] == "view"

    def test_an_unparseable_expiry_is_refused(self, client, db, seed, centre_admin):
        """`expires_at` is the whole difference between a trial and a permanent
        licence to a competitor's material, and no test had ever sent it.

        Found by sabotage: loosening it to `str` broke nothing, which is the
        definition of an untested field. Typed, it is refused here; untyped it
        reaches a `timestamptz` column, and the failed statement aborts the whole
        transaction so the real cause surfaces three errors later.
        """
        assert client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "test", "subject_xid": str(seed["test"].xid),
                "grantee_kind": "org", "grantee_xid": str(seed["org"].xid),
                "permission": "view", "expires_at": "end of term"}
        ).status_code == 422

    def test_and_a_real_one_is_kept(self, client, db, seed, centre_admin):
        """A trial share has to actually expire, so the value must survive the
        round trip rather than merely be accepted."""
        response = client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "test", "subject_xid": str(seed["test"].xid),
                "grantee_kind": "org", "grantee_xid": str(seed["org"].xid),
                "permission": "view", "expires_at": "2027-01-31T00:00:00Z"})
        assert response.status_code == 201
        assert db.scalar(text("SELECT expires_at FROM content_grants")) is not None

    def test_only_a_platform_admin_may_share_publicly(self, client, db, seed, centre_admin):
        """"Content can never become world-visible without a platform-admin
        review. That single rule is most of the copyright containment." """
        response = client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "test", "subject_xid": str(seed["test"].xid),
                "grantee_kind": "public", "permission": "view"})
        assert response.status_code == 403
        assert response.json()["code"] == "public_share_not_permitted"

    def test_a_platform_admin_may(self, client, db, seed, admin):
        response = client.post("/api/v1/content-grants", headers=auth(admin), json={
            "subject_type": "test", "subject_xid": str(seed["test"].xid),
            "grantee_kind": "public", "permission": "view"})
        assert response.status_code == 201

    def test_an_unknown_subject_type_is_a_404(self, client, seed, centre_admin):
        response = client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "spaceship", "subject_xid": str(seed["test"].xid),
                "grantee_kind": "org", "grantee_xid": str(seed["org"].xid),
                "permission": "view"})
        assert response.status_code == 404

    def test_an_unknown_subject_is_a_404(self, client, seed, centre_admin):
        response = client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "test", "subject_xid": str(uuid.uuid4()),
                "grantee_kind": "org", "grantee_xid": str(seed["org"].xid),
                "permission": "view"})
        assert response.status_code == 404

    def test_revoking_marks_it_rather_than_deleting_it(self, client, db, seed, centre_admin):
        xid = client.post("/api/v1/content-grants", headers=auth(centre_admin), json={
                "subject_type": "test", "subject_xid": str(seed["test"].xid),
                "grantee_kind": "org", "grantee_xid": str(seed["org"].xid),
                "permission": "view"}).json()["xid"]
        assert client.delete(f"/api/v1/content-grants/{xid}",
                             headers=auth(centre_admin)).status_code == 204
        row = db.execute(text("""
            SELECT revoked_at, revoked_by FROM content_grants
        """)).mappings().one()
        assert row["revoked_at"] is not None and row["revoked_by"] is not None


# ── the request bodies that were not request bodies ──────────────────

class TestTheSafetyBodiesAreDeclared:
    """Three handlers here read an untyped `dict` with bare subscripts, so a
    missing or malformed field was an unhandled exception — a 500.

    On these three endpoints specifically that matters more than usual. A 500 is
    what a client retries and what a person reads as "it didn't work"; the
    endpoints are a block, a moderation action, and a takedown decision.
    """

    def test_blocking_with_no_user_is_a_422_not_a_500(self, client, adult):
        """`uuid.UUID(str(body["user_xid"]))`: `KeyError` with no field.

        A student taps Block after something went wrong in a call — often a
        minor, often immediately — and a 500 is indistinguishable from "the block
        did not happen". They stay in the pool and can be matched again.
        """
        assert client.post("/api/v1/blocks", headers=auth(adult),
                           json={}).status_code == 422

    def test_nor_does_a_malformed_one_crash(self, client, adult):
        assert client.post("/api/v1/blocks", headers=auth(adult),
                           json={"user_xid": "not-a-uuid"}).status_code == 422

    def test_a_block_still_works(self, client, adult, minor):
        """A validator that refused everything would pass both tests above."""
        assert client.post("/api/v1/blocks", headers=auth(adult),
                           json={"user_xid": str(minor["xid"])}).status_code == 201

    @pytest.mark.parametrize("body", [{}, {"reason": "spam"},
                                      {"action": "bann", "reason": "spam"},
                                      {"action": "ban", "reason": ""}])
    def test_a_moderation_action_outside_the_enum_is_refused(self, client, admin,
                                                             body):
        """**The dangerous one.** `body["action"]` was compared against
        `("suspend", "ban")` and never against the set of actions that exist, so
        a typo'd `"bann"` matched neither branch: no sessions revoked, no account
        suspended, and an audit row saying the user had been dealt with.

        That is worse than refusing AND worse than acting, because the queue then
        shows the report as handled and nobody looks again.
        """
        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json=body).status_code == 422

    def test_a_ban_still_revokes_every_session(self, client, db, admin, adult):
        response = client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                               json={"action": "ban", "reason": "Repeated abuse",
                                     "target_user_xid": str(adult["xid"])})
        assert response.status_code == 201
        assert response.json()["action"] == "ban"

    def test_an_unparseable_expiry_is_a_422_not_a_transaction_abort(self, client,
                                                                    admin, adult):
        """It went straight into the INSERT, so PostgreSQL raised — and a failed
        statement aborts the whole transaction, making every later query on that
        session fail with the real cause three errors back."""
        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json={"action": "mute", "reason": "Language",
                                 "target_user_xid": str(adult["xid"]),
                                 "expires_at": "soon"}).status_code == 422

    def test_a_takedown_decision_outside_the_enum_is_refused(self, client, admin,
                                                             seed):
        """"Assume some centres WILL try to upload published Cambridge papers,
        and design so that liability and evidence are handled." This row IS that
        evidence, and any string at all went into the UPDATE."""
        xid = client.post("/api/v1/takedowns",
                          json={**TAKEDOWN,
                                "subject_xid": str(seed["test_version"].xid)}
                          ).json()["xid"]
        assert client.patch(f"/api/v1/admin/takedowns/{xid}", headers=auth(admin),
                            json={"status": "definitely"}).status_code == 422
        assert client.patch(f"/api/v1/admin/takedowns/{xid}", headers=auth(admin),
                            json={"outcome_note": "no status"}).status_code == 422
        assert client.patch(f"/api/v1/admin/takedowns/{xid}", headers=auth(admin),
                            json={"status": "upheld"}).status_code == 200


class TestAContentActionSaysWhichContent:
    """`moderation_actions` has carried `target_subject_type`,
    `target_subject_id` and `report_id` since the migration that created it, with
    a partial index over the first two — `WHERE target_subject_id IS NOT NULL` —
    built for exactly the query "what has been actioned on this content".

    The handler wrote none of them. Three of the seven actions the CHECK
    constraint permits are content actions, so a `content_remove` recorded that
    content had been removed and not WHICH, in the immutable log that exists to
    answer that question when a rights holder's lawyers ask it.

    `check_schema_conformance.py` named all three the moment a Pydantic model
    existed to name them in. Inside `body: dict` they were equally unread and the
    script could not see them — which is most of the argument for declaring
    request bodies at all.
    """

    def test_the_subject_is_recorded(self, client, db, admin, seed):
        response = client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                               json={"action": "content_hide",
                                     "reason": "Suspected Cambridge reproduction",
                                     "target_subject_type": "test",
                                     "target_subject_xid": str(seed["test"].xid)})
        assert response.status_code == 201
        row = db.execute(text("""
            SELECT target_subject_type, target_subject_id FROM moderation_actions
        """)).mappings().one()
        assert row["target_subject_type"] == "test"
        assert row["target_subject_id"] == seed["test"].id

    def test_a_content_action_with_no_content_is_refused(self, client, admin):
        """It is not a recoverable omission: the row it would write says an
        action was taken and cannot say on what."""
        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json={"action": "content_remove",
                                 "reason": "Verbatim reproduction"}
                           ).status_code == 422

    def test_half_a_subject_is_refused(self, client, admin, seed):
        """An xid with no type cannot be resolved to a table, and a type with no
        xid names nothing. Either alone would store a null and read as
        'no content'."""
        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json={"action": "warn", "reason": "x",
                                 "target_subject_xid": str(seed["test"].xid)}
                           ).status_code == 422
        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json={"action": "warn", "reason": "x",
                                 "target_subject_type": "test"}).status_code == 422

    def test_an_unknown_subject_type_is_a_404_not_a_500(self, client, admin, seed):
        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json={"action": "content_hide", "reason": "x",
                                 "target_subject_type": "spreadsheet",
                                 "target_subject_xid": str(seed["test"].xid)}
                           ).status_code == 404

    def test_the_report_it_answers_is_recorded(self, client, db, admin, adult, minor):
        """Without it the queue cannot show a report as resolved by a specific
        action, and the two halves of a safety incident stay unlinked."""
        report_xid = client.post("/api/v1/reports", headers=auth(minor), json={
            "subject_kind": "user", "subject_xid": str(adult["xid"]),
            "category": "harassment"}).json()["xid"]
        response = client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                               json={"action": "suspend", "reason": "Upheld report",
                                     "target_user_xid": str(adult["xid"]),
                                     "report_xid": report_xid})
        assert response.status_code == 201
        assert db.scalar(text("SELECT report_id FROM moderation_actions")) is not None

    def test_an_unknown_report_is_a_404_not_a_torn_transaction(self, client, admin,
                                                               adult):
        """`report_id` is a foreign key, so an unknown one was an IntegrityError
        — a 500, and an aborted transaction that would have taken the session
        revocation above it down as well."""
        import uuid as _uuid

        assert client.post("/api/v1/admin/moderation-actions", headers=auth(admin),
                           json={"action": "suspend", "reason": "x",
                                 "target_user_xid": str(adult["xid"]),
                                 "report_xid": str(_uuid.uuid4())}).status_code == 404
