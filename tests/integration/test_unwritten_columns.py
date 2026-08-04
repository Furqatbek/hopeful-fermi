"""Five columns the query layer filtered on and no code path ever wrote.

`scripts/check_write_paths.py` asks one question of the schema — does anything
ever WRITE the column this query filters on — and these are what it found on the
runs after it started resolving attribute assignments per model:

    consents.revoked_at            a parent could grant `stranger_matching` and
                                   never take it back
    org_memberships.left_at        a centre could enrol somebody and never
                                   un-enrol them
    entitlements.revoked_at        a payment reversed at the bank left the
                                   feature switched on for ever
    users.deleted_at               a student who asked to be removed could only
                                   be suspended, which is a conduct verdict
    cue_card_sets.archived_at      the fifth archivable asset, missed when the
                                   other four got their endpoint

Every one passed every other gate in this repository, because every one is
locally correct: the column exists, the filter is valid SQL, the read works, the
type checks. What was wrong is the relationship between two places.

These tests assert the READ changes, not merely that a column moved. A test that
checks `revoked_at IS NOT NULL` and stops would pass against an endpoint that
writes the column and changes nothing — which is the same defect wearing the fix
as a disguise.
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


def auth(who) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(who.xid))}"}


def _user(db, org_id, *, role="student", name="Aziza", born=dt.date(1980, 1, 1)):
    from app.modules.identity.models import OrgMembership, User

    person = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
                  date_of_birth=born, status="active")
    db.add(person)
    db.flush()
    db.add(OrgMembership(org_id=org_id, user_id=person.id, role=role,
                         status="active"))
    db.flush()
    return person


def _assignment(seed, **extra) -> dict:
    """A valid `AssignmentCreate`. `opens_at`/`closes_at` are required, and a
    body missing them 422s before any authorization rule is reached — which is
    how a test meant to prove a refusal ends up proving the schema instead."""
    now = dt.datetime.now(dt.UTC)
    return {"test_version_xid": str(seed["test_version"].xid),
            "opens_at": now.isoformat(),
            "closes_at": (now + dt.timedelta(days=7)).isoformat(), **extra}


@pytest.fixture
def centre_admin(db, seed):
    return _user(db, seed["org"].id, role="centre_admin", name="Gulnora")


@pytest.fixture
def assignable(db, seed):
    """`POST /assignments` is gated on `org.assignments`, so without this the
    tests below get a 402 and never reach the targeting rule they are about."""
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                          feature="org.assignments", source_kind="order"))
    # And the sittings themselves. `expand_targets` refuses an assignment whose
    # targets are not covered, which is a second 402 on the way to the rule.
    db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                          feature="mock.unlimited", source_kind="order"))
    db.flush()


@pytest.fixture
def operator(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    who = _user(db, seed["org"].id, role="centre_admin", name="Operator")
    db.add(PlatformRoleGrant(user_id=who.id, role="platform_admin",
                             granted_by=who.id))
    db.flush()
    return who


class TestWithdrawingConsent:
    """`consents.revoked_at`.

    The read that matters is `speaking.book_slot`: a minor enters a PUBLIC
    speaking pool only while a parent's `stranger_matching` consent is live.
    Consent could be given and never taken back, so the tests here go through
    booking rather than through the column.
    """

    @pytest.fixture
    def minor(self, db, seed):
        born = dt.date.today() - dt.timedelta(days=365 * 15)
        return _user(db, seed["org"].id, name="Kamola", born=born)

    def _grant(self, client, who):
        return client.post("/api/v1/me/consents", headers=auth(who), json={
            "kind": "stranger_matching", "doc_version": "2026-01",
            "granted_by_kind": "parent", "parent_name": "Dilnoza",
            "parent_phone": "+998901234567"})

    def test_a_consent_can_be_withdrawn(self, client, minor):
        assert self._grant(client, minor).status_code == 201
        response = client.delete("/api/v1/me/consents/stranger_matching",
                                 headers=auth(minor))
        assert response.status_code == 204, response.text

    def test_withdrawing_closes_the_public_pool_again(self, client, db, seed, minor):
        """The whole point. Booking a public slot must go 200 → 403 across the
        withdrawal, or `revoked_at` is a column that moves and decides nothing.
        """
        self._grant(client, minor)
        slot = client.post("/api/v1/speaking/slots", headers=auth(seed["author"]),
                           json={"audience": "public", "skill": "speaking",
                                 # The age band is checked FIRST and would 403
                                 # before the consent check is ever reached, so
                                 # this has to be a slot a minor may enter at all.
                                 "age_band": "minor",
                                 "starts_at": (dt.datetime.now(dt.UTC)
                                               + dt.timedelta(days=1)).isoformat(),
                                 "duration_minutes": 15})
        assert slot.status_code in (200, 201), slot.text
        xid = slot.json()["xid"]

        before = client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(minor))
        assert before.status_code in (200, 201), before.text
        client.delete(f"/api/v1/speaking/slots/{xid}/book", headers=auth(minor))

        client.delete("/api/v1/me/consents/stranger_matching", headers=auth(minor))
        after = client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(minor))
        assert after.status_code == 403, after.text
        assert "parental_consent_required" in after.text or "consent" in after.text

    def test_the_row_is_kept_as_evidence(self, client, db, minor):
        """Revoked, never deleted: "was there parental consent on the day of
        this call" is answered by the interval, and a deleted row answers it
        with silence."""
        self._grant(client, minor)
        client.delete("/api/v1/me/consents/stranger_matching", headers=auth(minor))
        row = db.execute(text("""
            SELECT granted_at, revoked_at FROM consents WHERE user_id = :u
        """).bindparams(u=minor.id)).mappings().one()
        assert row["granted_at"] is not None and row["revoked_at"] is not None

    def test_withdrawing_twice_is_a_404(self, client, minor):
        self._grant(client, minor)
        client.delete("/api/v1/me/consents/stranger_matching", headers=auth(minor))
        again = client.delete("/api/v1/me/consents/stranger_matching",
                              headers=auth(minor))
        assert again.status_code == 404, again.text

    def test_a_fresh_grant_after_a_withdrawal_works(self, client, minor):
        """A new grant is a new row, so the parent can change their mind twice."""
        self._grant(client, minor)
        client.delete("/api/v1/me/consents/stranger_matching", headers=auth(minor))
        assert self._grant(client, minor).status_code == 201
        listed = client.get("/api/v1/me/consents", headers=auth(minor)).json()
        assert len(listed) == 2


class TestLeavingTheCentre:
    """`org_memberships.left_at`, and `status` beside it."""

    def _remove(self, client, actor, org_xid, who):
        return client.delete(f"/api/v1/orgs/{org_xid}/members/{who.xid}",
                             headers=auth(actor))

    def test_a_member_can_be_removed(self, client, db, seed, centre_admin):
        leaver = _user(db, seed["org"].id)
        assert self._remove(client, centre_admin, seed["org"].xid,
                            leaver).status_code == 204

        listed = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                            headers=auth(centre_admin)).json()
        assert str(leaver.xid) not in [m["user"]["xid"] for m in listed["items"]]

    def test_both_columns_move(self, client, db, seed, centre_admin):
        """Two queries disagree about which one means "still here": the roster
        filters `status`, the assignment expansion filters `left_at`. Writing
        one would take somebody off the roster while still setting them work."""
        leaver = _user(db, seed["org"].id)
        self._remove(client, centre_admin, seed["org"].xid, leaver)
        row = db.execute(text("""
            SELECT status, left_at FROM org_memberships WHERE user_id = :u
        """).bindparams(u=leaver.id)).mappings().one()
        assert row["status"] == "left" and row["left_at"] is not None

    def test_their_classes_end_too(self, client, db, seed, centre_admin):
        """Leaving the centre and leaving its classes are two tables, and the
        cohort expansion reads only the second. Removing the org row alone would
        take a departed student off the roster while their class kept delivering
        mocks to them."""
        from app.modules.identity.models import Cohort, CohortMember

        leaver = _user(db, seed["org"].id)
        cohort = Cohort(org_id=seed["org"].id, name="Evening")
        db.add(cohort)
        db.flush()
        db.add(CohortMember(cohort_id=cohort.id, user_id=leaver.id))
        db.flush()

        self._remove(client, centre_admin, seed["org"].xid, leaver)
        row = db.execute(text("""
            SELECT status, left_at FROM cohort_members WHERE user_id = :u
        """).bindparams(u=leaver.id)).mappings().one()
        assert row["status"] == "left" and row["left_at"] is not None

    def test_they_stop_being_an_assignment_target(self, client, db, seed, published,
                                                 assignable, centre_admin):
        """`expand_targets` refuses a `users` assignment naming somebody outside
        the actor's centre. That refusal is the reason `left_at` exists."""
        leaver = _user(db, seed["org"].id)
        body = _assignment(seed, target_kind="users", user_xids=[str(leaver.xid)])
        before = client.post("/api/v1/assignments", headers=auth(centre_admin),
                             json=body)
        # The refusal has to be EARNED. Asserting a 4xx without this line passes
        # against a body the endpoint rejects for its shape, which is a green
        # test that never reached the rule it names.
        assert before.status_code in (200, 201), before.text

        self._remove(client, centre_admin, seed["org"].xid, leaver)
        after = client.post("/api/v1/assignments", headers=auth(centre_admin),
                            json=body)
        assert after.status_code == 403, after.text
        assert "student_not_in_org" in after.text

    def test_their_class_stops_targeting_them(self, client, db, seed, published,
                                             assignable, centre_admin):
        """The read where `left_at` is the ONLY filter.

        `expand_targets` resolves a cohort assignment with
        `WHERE cohort_id = ... AND left_at IS NULL` and does not look at
        `status` at all — so this is what would still deliver mocks to a
        departed student if the removal wrote only the roster column. The
        `users` path checks both and would pass either way; this one cannot.
        """
        from app.modules.identity.models import Cohort, CohortMember

        leaver = _user(db, seed["org"].id)
        cohort = Cohort(org_id=seed["org"].id, name="Evening")
        db.add(cohort)
        db.flush()
        db.add(CohortMember(cohort_id=cohort.id, user_id=leaver.id))
        db.flush()
        self._remove(client, centre_admin, seed["org"].xid, leaver)

        response = client.post("/api/v1/assignments", headers=auth(centre_admin),
                               json=_assignment(seed, target_kind="cohort",
                                                cohort_xid=str(cohort.xid)))
        assert response.status_code in (200, 201), response.text
        targeted = db.scalar(text("""
            SELECT count(*) FROM assignment_targets t
            JOIN assignments a ON a.id = t.assignment_id
            WHERE t.user_id = :u
        """).bindparams(u=leaver.id))
        assert targeted == 0

    def test_the_last_centre_admin_is_refused(self, client, db, seed, centre_admin):
        """Otherwise an admin removes themselves and the organization has nobody
        who can add one back."""
        response = self._remove(client, centre_admin, seed["org"].xid, centre_admin)
        assert response.status_code == 409, response.text
        assert "last_centre_admin" in response.text

    def test_a_second_admin_makes_it_allowed(self, client, db, seed, centre_admin):
        _user(db, seed["org"].id, role="centre_admin", name="Sardor")
        assert self._remove(client, centre_admin, seed["org"].xid,
                            centre_admin).status_code == 204

    def test_a_rival_centre_cannot_remove(self, client, db, seed, centre_admin):
        from app.modules.identity.models import Organization

        rival_org = Organization(name="Rival", slug=f"rival-{uuid.uuid4().hex[:8]}")
        db.add(rival_org)
        db.flush()
        rival = _user(db, rival_org.id, role="centre_admin", name="Rival")
        leaver = _user(db, seed["org"].id)
        response = self._remove(client, rival, seed["org"].xid, leaver)
        assert response.status_code in (403, 404), response.text

    def test_a_teacher_cannot_remove(self, client, db, seed):
        """MANAGE_ORG, not a teaching permission."""
        leaver = _user(db, seed["org"].id)
        response = self._remove(client, seed["author"], seed["org"].xid, leaver)
        assert response.status_code == 403, response.text

    def test_the_seat_is_not_released(self, client, db, seed, centre_admin):
        """Deliberate. A seat is paid for and has its own endpoint and audit
        trail; handing one back as a side effect of a roster edit is how a centre
        discovers it has been billed for something it did not do."""
        from app.modules.billing.models import EntitlementRow, SeatAssignment

        leaver = _user(db, seed["org"].id)
        licence = EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                                 feature="mock.seat", quantity=10)
        db.add(licence)
        db.flush()
        db.add(SeatAssignment(entitlement_id=licence.id, user_id=leaver.id,
                              assigned_by=centre_admin.id))
        db.flush()

        self._remove(client, centre_admin, seed["org"].xid, leaver)
        assert db.scalar(text("""
            SELECT released_at FROM seat_assignments WHERE user_id = :u
        """).bindparams(u=leaver.id)) is None


class TestRevokingAnEntitlement:
    """`entitlements.revoked_at`."""

    @pytest.fixture
    def held(self, db, seed):
        from app.modules.billing.models import EntitlementRow

        row = EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                             feature="mock.unlimited")
        db.add(row)
        db.flush()
        return row

    def test_the_operator_can_see_what_a_centre_holds(self, client, seed, operator,
                                                      held):
        """The listing is half the fix: before it there was no way to reach the
        id the revoke takes."""
        response = client.get(f"/api/v1/admin/orgs/{seed['org'].xid}/entitlements",
                              headers=auth(operator))
        assert response.status_code == 200, response.text
        assert str(held.xid) in [row["xid"] for row in response.json()]

    def test_a_centre_admin_cannot_read_it(self, client, seed, centre_admin, held):
        response = client.get(f"/api/v1/admin/orgs/{seed['org'].xid}/entitlements",
                              headers=auth(centre_admin))
        assert response.status_code == 403, response.text

    def test_revoking_removes_it_from_what_the_centre_holds(self, client, seed,
                                                            centre_admin, operator,
                                                            held):
        """`GET /me/entitlements` filters `revoked_at IS NULL`. That read going
        from present to absent is the assertion; the column is incidental."""
        mine = client.get("/api/v1/me/entitlements", headers=auth(centre_admin)).json()
        assert "mock.unlimited" in [row["feature"] for row in mine]

        response = client.post(f"/api/v1/admin/entitlements/{held.xid}/revoke",
                               headers=auth(operator),
                               json={"reason": "chargeback 4471"})
        assert response.status_code == 200, response.text

        after = client.get("/api/v1/me/entitlements", headers=auth(centre_admin)).json()
        assert "mock.unlimited" not in [row["feature"] for row in after]

    def test_the_reason_is_kept(self, client, seed, operator, held):
        client.post(f"/api/v1/admin/entitlements/{held.xid}/revoke", headers=auth(operator),
                    json={"reason": "chargeback 4471"})
        listed = client.get(f"/api/v1/admin/orgs/{seed['org'].xid}/entitlements",
                            headers=auth(operator)).json()
        row = next(r for r in listed if r["xid"] == str(held.xid))
        assert row["revoked_reason"] == "chargeback 4471"
        assert row["revoked_at"] is not None

    def test_a_reason_is_required(self, client, operator, held):
        """This is the table a billing dispute is argued from, and "revoked, no
        reason given" is not an answer to give a centre that has just lost
        access it paid for."""
        response = client.post(f"/api/v1/admin/entitlements/{held.xid}/revoke",
                               headers=auth(operator), json={"reason": ""})
        assert response.status_code == 422, response.text

    def test_a_centre_admin_cannot_revoke(self, client, centre_admin, held):
        response = client.post(f"/api/v1/admin/entitlements/{held.xid}/revoke",
                               headers=auth(centre_admin),
                               json={"reason": "we changed our mind"})
        assert response.status_code == 403, response.text

    def test_revoking_twice_is_a_404(self, client, operator, held):
        client.post(f"/api/v1/admin/entitlements/{held.xid}/revoke", headers=auth(operator),
                    json={"reason": "first"})
        again = client.post(f"/api/v1/admin/entitlements/{held.xid}/revoke",
                            headers=auth(operator), json={"reason": "second"})
        assert again.status_code == 404, again.text


class TestClosingAnAccount:
    """`users.deleted_at`."""

    def test_the_account_can_be_closed(self, client, db, seed, operator):
        leaver = _user(db, seed["org"].id)
        response = client.post(f"/api/v1/admin/users/{leaver.xid}/close",
                               headers=auth(operator),
                               json={"reason": "asked to be removed"})
        assert response.status_code == 200, response.text
        assert response.json()["deleted_at"] is not None

    def test_their_access_token_stops_working(self, client, db, seed, operator):
        """`resolve_principal` refuses a user whose `status` is not `active`, so
        the token already in a closed account's phone dies on its next request
        rather than fifteen minutes later. That read going 200 → 401 is the
        assertion; the column moving is not.

        NOT tested through `POST /auth/otp/request`, which answers 202 for every
        number on purpose — it must not become a phone-number oracle, so it
        cannot tell this test anything either.
        """
        leaver = _user(db, seed["org"].id)
        assert client.get("/api/v1/me", headers=auth(leaver)).status_code == 200

        client.post(f"/api/v1/admin/users/{leaver.xid}/close", headers=auth(operator),
                    json={"reason": "asked to be removed"})
        after = client.get("/api/v1/me", headers=auth(leaver))
        assert after.status_code in (401, 403), after.text

    def test_live_sessions_end(self, client, db, seed, operator):
        from app.modules.identity.models import AuthSession

        leaver = _user(db, seed["org"].id)
        db.add(AuthSession(user_id=leaver.id, token_hash=uuid.uuid4().hex,
                           device_label="phone",
                           expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=90)))
        db.flush()
        body = client.post(f"/api/v1/admin/users/{leaver.xid}/close",
                           headers=auth(operator),
                           json={"reason": "asked to be removed"}).json()
        assert body["sessions_revoked"] >= 1

    def test_they_leave_every_roster(self, client, db, seed, centre_admin, operator):
        leaver = _user(db, seed["org"].id)
        client.post(f"/api/v1/admin/users/{leaver.xid}/close", headers=auth(operator),
                    json={"reason": "asked to be removed"})
        listed = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                            headers=auth(centre_admin)).json()
        assert str(leaver.xid) not in [m["user"]["xid"] for m in listed["items"]]

    def test_their_work_is_kept(self, client, db, seed, operator):
        """Closure, not erasure. Attempts, results and audit rows are evidence
        in a copyright or safety investigation and a centre's exam records
        besides — the endpoint's docstring says so and this is what says it in
        code."""
        leaver = _user(db, seed["org"].id)
        client.post(f"/api/v1/admin/users/{leaver.xid}/close", headers=auth(operator),
                    json={"reason": "asked to be removed"})
        row = db.execute(text("""
            SELECT status, deleted_at, phone, given_name FROM users WHERE id = :u
        """).bindparams(u=leaver.id)).mappings().one()
        assert row["deleted_at"] is not None and row["status"] == "deleted"
        # Not scrubbed. Erasure is a larger job than this endpoint and must not
        # be mistaken for it because the verb is DELETE.
        assert row["given_name"] == "Aziza"

    def test_they_stop_being_an_assignment_target(self, client, db, seed, published,
                                                 assignable, operator):
        """The read where `deleted_at` is the ONLY filter.

        `expand_targets` resolves `target_kind: users` with
        `WHERE xid IN (...) AND deleted_at IS NULL`, and the org-membership
        check beside it is skipped for a platform admin — so for this actor
        `deleted_at` is the whole of the refusal. `status` cannot stand in.
        """
        leaver = _user(db, seed["org"].id)
        body = _assignment(seed, target_kind="users", user_xids=[str(leaver.xid)])
        client.post(f"/api/v1/admin/users/{leaver.xid}/close", headers=auth(operator),
                    json={"reason": "asked to be removed"})
        response = client.post("/api/v1/assignments", headers=auth(operator), json=body)
        assert response.status_code == 404, response.text

    def test_a_centre_admin_cannot_close_an_account(self, client, db, seed,
                                                    centre_admin):
        """A centre admin can remove somebody from their own roster; closing the
        person's account is a different power."""
        leaver = _user(db, seed["org"].id)
        response = client.post(f"/api/v1/admin/users/{leaver.xid}/close",
                               headers=auth(centre_admin),
                               json={"reason": "troublesome"})
        assert response.status_code == 403, response.text

    def test_closing_twice_is_a_404(self, client, db, seed, operator):
        leaver = _user(db, seed["org"].id)
        client.post(f"/api/v1/admin/users/{leaver.xid}/close", headers=auth(operator),
                    json={"reason": "first"})
        again = client.post(f"/api/v1/admin/users/{leaver.xid}/close",
                            headers=auth(operator), json={"reason": "second"})
        assert again.status_code == 404, again.text


class TestRetiringACueCardSet:
    """`cue_card_sets.archived_at` — the fifth archivable asset.

    Found by `check_write_paths.py` on its first run, catching a gap made an
    hour earlier: four assets got an archive endpoint and this one did not.
    """

    @pytest.fixture
    def cue_cards(self, client, seed):
        return client.post("/api/v1/cue-card-sets", headers=auth(seed["author"]),
                           json={"title": "Hometown", "body": {"cards": []}}).json()

    def test_a_retired_set_leaves_the_listing(self, client, centre_admin, cue_cards):
        response = client.post(f"/api/v1/cue-card-sets/{cue_cards['xid']}/archive",
                               headers=auth(centre_admin))
        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is not None

        listed = client.get("/api/v1/cue-card-sets", headers=auth(centre_admin)).json()
        assert cue_cards["xid"] not in [s["xid"] for s in listed]

    def test_and_can_be_put_back(self, client, centre_admin, cue_cards):
        client.post(f"/api/v1/cue-card-sets/{cue_cards['xid']}/archive",
                    headers=auth(centre_admin))
        restored = client.delete(f"/api/v1/cue-card-sets/{cue_cards['xid']}/archive",
                                 headers=auth(centre_admin))
        assert restored.status_code == 200, restored.text
        assert restored.json()["archived_at"] is None

        listed = client.get("/api/v1/cue-card-sets", headers=auth(centre_admin)).json()
        assert cue_cards["xid"] in [s["xid"] for s in listed]

    def test_a_teacher_may_not_retire(self, client, seed, cue_cards):
        """ARCHIVE is centre-admin and above, the same as the other four."""
        response = client.post(f"/api/v1/cue-card-sets/{cue_cards['xid']}/archive",
                               headers=auth(seed["author"]))
        assert response.status_code == 403, response.text

    def test_the_row_is_kept(self, client, db, centre_admin, cue_cards):
        client.post(f"/api/v1/cue-card-sets/{cue_cards['xid']}/archive",
                    headers=auth(centre_admin))
        assert db.scalar(text("""
            SELECT archived_at FROM cue_card_sets WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=cue_cards["xid"])) is not None
