"""Profiles, consents, devices, organizations, cohorts and invites.

The last of the large routers at 70%. Two defects in the gap, and the first one
is the same mistake in the sibling of an endpoint that was already fixed:

  * **`GET /orgs/{xid}/members` returned every phone number to any member.**
    `list_cohort_members` was corrected to hand classmates names only; this one
    was not, and it is worse — the whole centre rather than one class, and it
    includes `is_minor`, which turns a directory into a targeting list.
  * **Accepting an invite you are already a member of returned 500 forever.**
    `org_memberships` is UNIQUE on `(org_id, user_id)`; the insert was
    unconditional, and because the failed transaction rolled back, the invite
    was never marked accepted, so every retry produced the same 500.

Everything else here was already right — notably parental consent, which is the
one place the brief's minor-safety rule is written down in this module.
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


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _user(db, phone: str, name: str, *, dob: str = "1995-01-01"):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, :n, CAST(:d AS date), 'active') RETURNING id, xid
    """).bindparams(p=phone, n=name, d=dob)).mappings().one()
    db.flush()
    return row


def _member(db, org_id: int, row, role: str = "student"):
    db.execute(text("""
        INSERT INTO org_memberships (org_id, user_id, role, status)
        VALUES (:o, :u, :r, 'active')
    """).bindparams(o=org_id, u=row["id"], r=role))
    db.flush()
    return row


@pytest.fixture
def centre_admin(db, seed):
    row = _user(db, "+998908000001", "Centre Admin", dob="1985-01-01")
    return _member(db, seed["org"].id, row, "centre_admin")


@pytest.fixture
def platform_admin(db):
    row = _user(db, "+998908000002", "Platform Admin", dob="1980-01-01")
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return row


@pytest.fixture
def minor(db):
    fifteen = (dt.date.today() - dt.timedelta(days=365 * 15)).isoformat()
    return _user(db, "+998908000003", "Aziza", dob=fifteen)


# ── the roster leak ──────────────────────────────────────────────────

class TestTheOrgRosterIsNotADirectory:
    """`list_cohort_members` was fixed to hand classmates names only. Its sibling
    was not, and it covers the whole centre."""

    def test_a_student_does_not_get_phone_numbers(self, client, seed):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert response.status_code == 200
        assert response.json()["items"]
        assert all(i["user"]["phone"] is None for i in response.json()["items"])

    def test_nor_telegram_usernames(self, client, seed):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert all(i["user"]["telegram_username"] is None
                   for i in response.json()["items"])

    def test_nor_who_is_a_minor(self, client, db, seed, minor):
        """A roster of names is a directory. A roster of names, phone numbers and
        `is_minor` is a targeting list."""
        _member(db, seed["org"].id, minor)
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert all(i["user"]["is_minor"] is None for i in response.json()["items"])

    def test_the_actual_phone_number_is_nowhere_in_the_body(self, client, seed):
        """Belt and braces: assert on the response TEXT, so a future field that
        happens to carry the number is caught too."""
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert seed["author"].phone not in response.text

    def test_a_student_still_sees_who_is_in_the_centre(self, client, seed):
        """Names are the point; removing the endpoint would be the wrong fix."""
        names = [i["user"]["given_name"]
                 for i in client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                                     headers=auth(seed["student"].xid)).json()["items"]]
        assert seed["author"].given_name in names

    def test_a_teacher_gets_the_full_record(self, client, seed):
        """They administer the roster and need to phone a parent."""
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["author"].xid))
        assert any(i["user"]["phone"] for i in response.json()["items"])

    def test_a_centre_admin_does_too(self, client, seed, centre_admin):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(centre_admin["xid"]))
        assert any(i["user"]["phone"] for i in response.json()["items"])

    def test_it_can_be_filtered_by_role(self, client, seed):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members?role=teacher",
                              headers=auth(seed["author"].xid))
        assert [i["role"] for i in response.json()["items"]] == ["teacher"]

    def test_a_rival_centre_gets_a_404(self, client, db, seed):
        outsider = _user(db, "+998908000009", "Outsider")
        assert client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                          headers=auth(outsider["xid"])).status_code == 404


# ── invites ──────────────────────────────────────────────────────────

class TestInvites:
    def test_a_centre_admin_creates_one(self, client, seed, centre_admin):
        response = client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                               headers=auth(centre_admin["xid"]),
                               json={"phone": "+998909000001", "role": "student"})
        assert response.status_code == 201
        assert response.json()["token"]

    def test_a_teacher_cannot(self, client, seed):
        """Inviting sets who is at the centre, which is org administration."""
        assert client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                           headers=auth(seed["author"].xid),
                           json={"phone": "+998909000002",
                                 "role": "teacher"}).status_code == 403

    def test_only_the_hash_is_stored(self, client, db, seed, centre_admin):
        """A database dump must not be a pile of usable invite links."""
        token = _invite(client, seed, centre_admin)
        stored = db.scalar(text("SELECT token_hash FROM org_invites"))
        assert stored != token and len(stored) == 64

    def test_accepting_joins_the_organization(self, client, db, seed, centre_admin):
        token = _invite(client, seed, centre_admin)
        newcomer = _user(db, "+998909000003", "Newcomer")
        response = client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                               json={"token": token})
        assert response.status_code == 200
        assert response.json()["role"] == "student"

    def test_accepting_also_joins_the_named_cohort(self, client, db, seed,
                                                   centre_admin):
        cohort_xid = client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                 headers=auth(centre_admin["xid"]),
                                 json={"name": "Evening IELTS"}).json()["xid"]
        token = _invite(client, seed, centre_admin, cohort_xid=cohort_xid)
        newcomer = _user(db, "+998909000004", "Newcomer")
        client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                    json={"token": token})
        assert db.scalar(text("SELECT count(*) FROM cohort_members WHERE user_id = :u")
                         .bindparams(u=newcomer["id"])) == 1

    def test_an_unknown_token_is_a_404(self, client, db):
        newcomer = _user(db, "+998909000005", "Newcomer")
        assert client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                           json={"token": "not-a-real-token"}).status_code == 404

    def test_a_token_cannot_be_used_twice(self, client, db, seed, centre_admin):
        token = _invite(client, seed, centre_admin)
        first = _user(db, "+998909000006", "First")
        second = _user(db, "+998909000007", "Second")
        assert client.post("/api/v1/invites/accept", headers=auth(first["xid"]),
                           json={"token": token}).status_code == 200
        reused = client.post("/api/v1/invites/accept", headers=auth(second["xid"]),
                             json={"token": token})
        assert reused.status_code == 409
        assert reused.json()["code"] == "invite_unusable"

    def test_an_expired_invite_is_refused(self, client, db, seed, centre_admin):
        token = _invite(client, seed, centre_admin)
        db.execute(text("UPDATE org_invites SET expires_at = now() - interval '1 day'"))
        db.flush()
        newcomer = _user(db, "+998909000008", "Newcomer")
        assert client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                           json={"token": token}).status_code == 409


class TestAcceptingAnInviteTwiceOver:
    """The 500. `org_memberships` is UNIQUE on `(org_id, user_id)` and the insert
    was unconditional, so an existing member clicking a new link got a 500 — and
    because the transaction rolled back, the invite stayed unaccepted and every
    retry produced the same 500. A student already enrolled, sent a link for a
    second cohort, hit exactly that."""

    def test_an_existing_member_can_accept(self, client, seed, centre_admin):
        token = _invite(client, seed, centre_admin)
        response = client.post("/api/v1/invites/accept",
                               headers=auth(seed["student"].xid), json={"token": token})
        assert response.status_code == 200

    def test_and_does_not_gain_a_second_membership(self, client, db, seed,
                                                   centre_admin):
        token = _invite(client, seed, centre_admin)
        client.post("/api/v1/invites/accept", headers=auth(seed["student"].xid),
                    json={"token": token})
        db.expire_all()
        assert db.scalar(text("""
            SELECT count(*) FROM org_memberships WHERE org_id = :o AND user_id = :u
        """).bindparams(o=seed["org"].id, u=seed["student"].id)) == 1

    def test_it_joins_them_to_the_new_cohort(self, client, db, seed, centre_admin):
        """The realistic case: already at the centre, invited to a second class."""
        cohort_xid = client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                 headers=auth(centre_admin["xid"]),
                                 json={"name": "Saturday intensive"}).json()["xid"]
        token = _invite(client, seed, centre_admin, cohort_xid=cohort_xid)
        client.post("/api/v1/invites/accept", headers=auth(seed["student"].xid),
                    json={"token": token})
        assert db.scalar(text("""
            SELECT count(*) FROM cohort_members WHERE user_id = :u
        """).bindparams(u=seed["student"].id)) == 1

    def test_a_higher_role_is_granted(self, client, db, seed, centre_admin):
        """The invite names a role and its author holds MANAGE_ORG, so a
        promotion is the intent."""
        token = _invite(client, seed, centre_admin, role="teacher")
        response = client.post("/api/v1/invites/accept",
                               headers=auth(seed["student"].xid), json={"token": token})
        assert response.json()["role"] == "teacher"

    def test_a_lower_role_is_not_applied(self, client, db, seed, centre_admin):
        """A centre admin pasting a `student` link into a group chat must not
        demote the teacher who clicks it."""
        token = _invite(client, seed, centre_admin, role="student")
        response = client.post("/api/v1/invites/accept",
                               headers=auth(seed["author"].xid), json={"token": token})
        assert response.json()["role"] == "teacher"

    def test_accepting_twice_still_burns_the_token(self, client, seed, centre_admin):
        token = _invite(client, seed, centre_admin)
        client.post("/api/v1/invites/accept", headers=auth(seed["student"].xid),
                    json={"token": token})
        again = client.post("/api/v1/invites/accept",
                            headers=auth(seed["student"].xid), json={"token": token})
        assert again.status_code == 409


# ── consent ──────────────────────────────────────────────────────────

class TestConsent:
    """"Parental-consent flag on minor accounts." Consent is evidence: the
    document version and its hash are stored with the grant."""

    def test_an_adult_consents_for_themselves(self, client, seed):
        response = client.post("/api/v1/me/consents", headers=auth(seed["student"].xid),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07"})
        assert response.status_code == 201
        assert response.json()["granted_by_kind"] == "self"

    def test_a_minor_cannot_self_consent_to_stranger_matching(self, client, minor):
        """"A general terms acceptance does not cover voice calls with strangers,
        and a regulator will not read it that way." """
        response = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07"})
        assert response.status_code == 403
        assert response.json()["code"] == "parental_consent_required"

    def test_a_parent_without_contact_details_is_not_enough(self, client, minor):
        """"With contact details" is the operative part: an unreachable parent is
        an unverifiable claim."""
        response = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07",
                                     "granted_by_kind": "parent",
                                     "parent_name": "Nodira"})
        assert response.status_code == 403

    def test_a_parent_with_contact_details_is(self, client, minor):
        response = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07",
                                     "granted_by_kind": "parent",
                                     "parent_name": "Nodira",
                                     "parent_phone": "+998901234567"})
        assert response.status_code == 201
        assert response.json()["granted_by_kind"] == "parent"

    def test_a_minor_may_still_accept_ordinary_terms_alone(self, client, minor):
        """The rule is about voice calls with strangers, not about everything."""
        assert client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                           json={"kind": "terms",
                                 "doc_version": "terms-2026-07"}).status_code == 201

    def test_the_document_is_hashed_with_the_grant(self, client, db, seed):
        """"Consent is evidence, not a boolean." "They agreed" is not a defence;
        "they agreed to THIS text, whose hash is X" is."""
        client.post("/api/v1/me/consents", headers=auth(seed["student"].xid),
                    json={"kind": "terms", "doc_version": "terms-2026-07"})
        row = db.execute(text("""
            SELECT doc_version, doc_hash FROM consents
        """)).mappings().one()
        assert row["doc_version"] == "terms-2026-07"
        assert len(row["doc_hash"]) == 64

    def test_the_parents_details_are_stored(self, client, db, minor):
        client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                    json={"kind": "stranger_matching", "doc_version": "t",
                          "granted_by_kind": "parent", "parent_name": "Nodira",
                          "parent_phone": "+998901234567"})
        row = db.execute(text("""
            SELECT parent_name, parent_phone FROM consents
        """)).mappings().one()
        assert (row["parent_name"], row["parent_phone"]) == ("Nodira", "+998901234567")

    def test_they_are_listed_back(self, client, seed):
        client.post("/api/v1/me/consents", headers=auth(seed["student"].xid),
                    json={"kind": "terms", "doc_version": "terms-2026-07"})
        listed = client.get("/api/v1/me/consents",
                            headers=auth(seed["student"].xid)).json()
        assert [c["kind"] for c in listed] == ["terms"]


# ── profile and devices ──────────────────────────────────────────────

class TestProfile:
    def test_the_date_of_birth_never_appears(self, client, seed):
        """"Collected for the 18 boundary and nothing else, and never appears in
        a response, a leaderboard or a B2B export." """
        response = client.get("/api/v1/me", headers=auth(seed["student"].xid))
        assert "date_of_birth" not in response.text
        assert "is_minor" in response.json()

    def test_a_profile_can_be_updated(self, client, seed):
        response = client.patch("/api/v1/me", headers=auth(seed["student"].xid),
                                json={"given_name": "Azizaxon", "target_band": 7.5})
        assert response.json()["given_name"] == "Azizaxon"
        assert response.json()["target_band"] == 7.5

    def test_the_date_of_birth_is_not_editable(self, client, db, seed):
        """"Changing it moves a user across the minor/adult boundary and silently
        alters which speaking pools they can enter." """
        before = db.scalar(text("SELECT date_of_birth FROM users WHERE id = :u")
                           .bindparams(u=seed["student"].id))
        client.patch("/api/v1/me", headers=auth(seed["student"].xid),
                     json={"date_of_birth": "2015-01-01"})
        db.expire_all()
        assert db.scalar(text("SELECT date_of_birth FROM users WHERE id = :u")
                         .bindparams(u=seed["student"].id)) == before

    def test_an_impossible_target_band_is_rejected(self, client, seed):
        for band in (0.5, 9.5, -1):
            assert client.patch("/api/v1/me", headers=auth(seed["student"].xid),
                                json={"target_band": band}).status_code == 422


class TestDevices:
    def test_live_sessions_are_listed(self, client, db, seed):
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, device_label)
            VALUES (:u, 'h1', now() + interval '90 days', 'Redmi Note 12')
        """).bindparams(u=seed["student"].id))
        db.flush()
        listed = client.get("/api/v1/me/devices",
                            headers=auth(seed["student"].xid)).json()
        assert [d["label"] for d in listed] == ["Redmi Note 12"]

    def test_forgetting_one_revokes_its_session(self, client, db, seed):
        """A shared computer in a lab is the case this exists for."""
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, device_label)
            VALUES (:u, 'h2', now() + interval '90 days', 'Lab PC')
        """).bindparams(u=seed["student"].id))
        db.flush()
        xid = client.get("/api/v1/me/devices",
                         headers=auth(seed["student"].xid)).json()[0]["xid"]
        assert client.delete(f"/api/v1/me/devices/{xid}",
                             headers=auth(seed["student"].xid)).status_code == 204
        db.expire_all()
        assert db.scalar(text(
            "SELECT revoked_reason FROM auth_sessions")) == "user_forgot_device"

    def test_you_cannot_forget_someone_elses_device(self, client, db, seed):
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, device_label)
            VALUES (:u, 'h3', now() + interval '90 days', 'Teacher laptop')
        """).bindparams(u=seed["author"].id))
        db.flush()
        xid = client.get("/api/v1/me/devices",
                         headers=auth(seed["author"].xid)).json()[0]["xid"]
        assert client.delete(f"/api/v1/me/devices/{xid}",
                             headers=auth(seed["student"].xid)).status_code == 404

    def test_forgetting_an_unknown_device_is_a_404(self, client, seed):
        assert client.delete(f"/api/v1/me/devices/{uuid.uuid4()}",
                             headers=auth(seed["student"].xid)).status_code == 404


# ── organizations and cohorts ────────────────────────────────────────

class TestOrganizations:
    def test_only_a_platform_admin_creates_one(self, client, seed, platform_admin):
        assert client.post("/api/v1/orgs", headers=auth(seed["author"].xid),
                           json={"name": "New Centre",
                                 "slug": "new-centre"}).status_code == 403
        assert client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                           json={"name": "New Centre",
                                 "slug": "new-centre"}).status_code == 201

    def test_a_malformed_slug_is_rejected(self, client, platform_admin):
        for slug in ("ab", "Has Capitals", "under_scores", "x" * 41):
            assert client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                               json={"name": "N", "slug": slug}).status_code == 422

    def test_settings_are_merged_not_replaced(self, client, seed, centre_admin):
        """Two flags set in two requests must both survive."""
        client.patch(f"/api/v1/orgs/{seed['org'].xid}", headers=auth(centre_admin["xid"]),
                     json={"settings": {"teacher_can_publish": True}})
        response = client.patch(f"/api/v1/orgs/{seed['org'].xid}",
                                headers=auth(centre_admin["xid"]),
                                json={"settings": {"content_edit_others": True}})
        assert response.json()["settings"]["teacher_can_publish"] is True
        assert response.json()["settings"]["content_edit_others"] is True

    def test_the_plain_fields_update_too(self, client, seed, centre_admin):
        response = client.patch(f"/api/v1/orgs/{seed['org'].xid}",
                                headers=auth(centre_admin["xid"]),
                                json={"name": "Tashkent Prep (Chilonzor)",
                                      "contact_phone": "+998712000000"})
        assert response.json()["name"] == "Tashkent Prep (Chilonzor)"

    def test_a_teacher_cannot_change_the_settings(self, client, seed):
        """Both flags widen who can affect published material."""
        assert client.patch(f"/api/v1/orgs/{seed['org'].xid}",
                            headers=auth(seed["author"].xid),
                            json={"settings": {"teacher_can_publish": True}}
                            ).status_code == 403

    def test_listing_is_scoped_to_memberships(self, client, db, seed,
                                              platform_admin):
        client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                    json={"name": "Other Centre", "slug": "other-centre"})
        mine = client.get("/api/v1/orgs", headers=auth(seed["student"].xid)).json()
        assert [o["xid"] for o in mine["items"]] == [str(seed["org"].xid)]

    def test_a_platform_admin_sees_them_all(self, client, platform_admin, seed):
        client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                    json={"name": "Other Centre", "slug": "other-centre"})
        listed = client.get("/api/v1/orgs", headers=auth(platform_admin["xid"])).json()
        assert len(listed["items"]) >= 2

    def test_an_unknown_org_is_a_404(self, client, seed):
        assert client.get(f"/api/v1/orgs/{uuid.uuid4()}",
                          headers=auth(seed["student"].xid)).status_code == 404


class TestCohorts:
    def test_a_centre_admin_creates_and_lists_them(self, client, seed, centre_admin):
        created = client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                              headers=auth(centre_admin["xid"]),
                              json={"name": "Evening IELTS",
                                    "academic_year": "2026"})
        assert created.status_code == 201
        listed = client.get(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                            headers=auth(centre_admin["xid"])).json()
        assert [c["name"] for c in listed] == ["Evening IELTS"]

    def test_a_teacher_cannot_create_one(self, client, seed):
        assert client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                           headers=auth(seed["author"].xid),
                           json={"name": "Mine"}).status_code == 403

    def test_a_member_of_the_org_can_be_added(self, client, seed, centre_admin):
        cohort_xid = _cohort(client, seed, centre_admin)
        response = client.post(f"/api/v1/cohorts/{cohort_xid}/members",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(seed["student"].xid)]})
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_someone_from_outside_the_org_cannot_be(self, client, db, seed,
                                                    centre_admin):
        """"A centre could otherwise add anyone's account to its reporting." """
        outsider = _user(db, "+998909100001", "Outsider")
        cohort_xid = _cohort(client, seed, centre_admin)
        response = client.post(f"/api/v1/cohorts/{cohort_xid}/members",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(outsider["xid"])]})
        assert response.status_code == 409
        assert response.json()["code"] == "not_an_org_member"

    def test_adding_the_same_student_twice_is_not_an_error(self, client, seed,
                                                           centre_admin):
        cohort_xid = _cohort(client, seed, centre_admin)
        for _ in range(2):
            response = client.post(f"/api/v1/cohorts/{cohort_xid}/members",
                                   headers=auth(centre_admin["xid"]),
                                   json={"user_xids": [str(seed["student"].xid)]})
        assert response.status_code == 200
        assert len(response.json()) == 1


# ── helpers ──────────────────────────────────────────────────────────

def _invite(client, seed, admin, *, role: str = "student", cohort_xid=None) -> str:
    body = {"phone": "+998909900001", "role": role}
    if cohort_xid:
        body["cohort_xid"] = str(cohort_xid)
    return client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                       headers=auth(admin["xid"]), json=body).json()["token"]


def _cohort(client, seed, admin) -> str:
    return client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                       headers=auth(admin["xid"]),
                       json={"name": "Evening IELTS"}).json()["xid"]
