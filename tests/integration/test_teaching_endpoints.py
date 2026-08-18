"""Assignments and regrades — what a teacher does after the authoring is done.

Two defects in the untested 17%.

**A teacher could not assign to their own student.** `target_kind="users"` exists
for the ad-hoc case — otherwise you would use `target_kind="cohort"` — and its
"is this student at my centre?" check queried `cohort_members`:

    members = select(CohortMember.user_id)
        .join(Cohort, Cohort.id == CohortMember.cohort_id)
        .where(CohortMember.user_id.in_(ids), Cohort.org_id.in_(actor.org_ids))

Centre membership lives in `org_memberships`. A student enrolled at the centre but
not yet in any class was refused with "You can only assign to students in your own
centre" — about a student who is. `identity.add_cohort_members` already asks this
question the right way, one file over.

**A band-map regrade reported an impact of zero.** The whole flow is *stage a dry
run → read the impact → apply*, and `_affected_count` — "the number the author
actually wants first: how many students does this touch" — had no branch for
`band_map_version` and fell through to `return 0`. The planner scopes that subject
type perfectly well, so the job was real and the number in front of the human
deciding whether to run it was not.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token
from app.modules.billing.entitlements import SEAT_BUNDLE


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


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


@pytest.fixture
def teacher(db, seed):
    """The seeded author, who already holds `teacher` at the centre.

    TWO features, because they answer two different questions and the B2B product
    grants both — `products.features` is a list for exactly this reason.
    `org.assignments` is the capability the centre bought; `mock.unlimited` is what
    each student consumes by sitting the paper, and is what a seat licence covers.
    Site-wide here (`source_kind="order"`, no quantity), which is the
    subscription centre: everybody is covered.
    """
    from app.modules.billing.models import EntitlementRow

    for feature in ("org.assignments", "mock.unlimited"):
        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature=feature, source_kind="order",
                              starts_at=_now() - dt.timedelta(days=1)))
    db.flush()
    return auth(seed["author"].xid)


@pytest.fixture
def admin(db, seed, teacher):
    """The seeded author, promoted. Keeps `teacher`'s entitlement: a platform
    admin still sets assignments against a centre that holds seats."""
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return auth(seed["author"].xid)


def _user(db, name: str, *, org_id=None, role: str = "student", cohort_id=None):
    from app.modules.identity.models import CohortMember, OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
                family_name="Karimova", date_of_birth=dt.date(2000, 1, 1))
    db.add(user)
    db.flush()
    if org_id:
        db.add(OrgMembership(org_id=org_id, user_id=user.id, role=role,
                             status="active"))
    if cohort_id:
        db.add(CohortMember(cohort_id=cohort_id, user_id=user.id))
    db.flush()
    return user


@pytest.fixture
def cohort(db, seed):
    from app.modules.identity.models import Cohort

    row = Cohort(org_id=seed["org"].id, name="Evening group",
                 created_by=seed["author"].id)
    db.add(row)
    db.flush()
    return row


def _assign(client, headers, published, **overrides):
    body = {"test_version_xid": str(published["test_version"].xid),
            "target_kind": "cohort",
            "opens_at": (_now() - dt.timedelta(hours=1)).isoformat(),
            "closes_at": (_now() + dt.timedelta(days=7)).isoformat()}
    body.update(overrides)
    return client.post("/api/v1/assignments", json=body, headers=headers)


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


@pytest.fixture
def seated(db, seed):
    """A centre on a SEAT licence rather than a site one: two seats, and the
    capability to set work at all.

    Module-scope because two classes model the same centre now — the one that
    checks seats are counted, and the one that checks the bundle decides which
    licence counting is even done against.
    """
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                          feature="org.assignments", source_kind="manual_grant",
                          starts_at=_now() - dt.timedelta(days=1)))
    licence = EntitlementRow(
        subject_kind="org", subject_id=seed["org"].id, feature=SEAT_BUNDLE[0],
        source_kind="seat", quantity=2, starts_at=_now() - dt.timedelta(days=1))
    db.add(licence)
    db.flush()
    return licence


def _seat(db, licence, user):
    from app.modules.billing.models import SeatAssignment

    db.add(SeatAssignment(entitlement_id=licence.id, user_id=user.id))
    db.flush()


class TestSeatsCoverTheStudents:
    """"A seat licence only covers users who actually hold a seat. Without this,
    buying 10 seats would entitle a 400-student centre."

    That rule lives in `entitlements.check` and the assigned path never asked it
    about a student. The check ran against the TEACHER and stopped, so a centre
    with ten seats could assign to four hundred students and every one of them
    would sit the paper — and `POST /attempts` does not re-check on the assigned
    path, by design, so nothing downstream was going to catch it either.

    The contract has documented the 402 as "the organization has no seat **or
    entitlement** covering these students" since it was drafted.
    """

    def _seat(self, db, licence, user):
        _seat(db, licence, user)

    def _three(self, db, seed, cohort):
        return [_user(db, name, org_id=seed["org"].id, cohort_id=cohort.id)
                for name in ("Aziza", "Bekzod", "Charos")]

    def test_a_ten_seat_centre_cannot_assign_to_everybody(
            self, client, db, seed, cohort, published, seated):
        students = self._three(db, seed, cohort)
        self._seat(db, seated, students[0])
        self._seat(db, seated, students[1])

        refused = _assign(client, auth(seed["author"].xid), published,
                          cohort_xid=str(cohort.xid))
        assert refused.status_code == 402, refused.text
        body = refused.json()
        assert body["uncovered_count"] == 1
        assert body["uncovered_user_xids"] == [str(students[2].xid)]
        assert body["reason"] == "no_seat"

    def test_nothing_is_created_when_it_is_refused(self, client, db, seed, cohort,
                                                   published, seated):
        """Refused whole, not applied partly. An assignment quietly missing the
        students who had no seat is discovered at results."""
        self._three(db, seed, cohort)
        assert _assign(client, auth(seed["author"].xid), published,
                       cohort_xid=str(cohort.xid)).status_code == 402
        db.rollback()
        assert db.scalar(text("SELECT count(*) FROM assignments")) == 0
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 0

    def test_a_fully_seated_cohort_is_assigned(self, client, db, seed, cohort,
                                               published, seated):
        """Two seats, two students. A gate that refuses a centre that HAS paid is
        worse than no gate — it gets switched off by a refund."""
        for name in ("Aziza", "Bekzod"):
            self._seat(db, seated,
                       _user(db, name, org_id=seed["org"].id, cohort_id=cohort.id))
        assert _assign(client, auth(seed["author"].xid), published,
                       cohort_xid=str(cohort.xid)).status_code == 201

    def test_a_released_seat_stops_covering(self, client, db, seed, cohort,
                                            published, seated):
        """A student who left the centre had their seat released for someone
        else. Counting it would let a centre assign to twice its licence by
        cycling students through."""
        student = _user(db, "Dilnoza", org_id=seed["org"].id, cohort_id=cohort.id)
        self._seat(db, seated, student)
        db.execute(text("UPDATE seat_assignments SET released_at = now()"))
        db.flush()
        assert _assign(client, auth(seed["author"].xid), published,
                       cohort_xid=str(cohort.xid)).status_code == 402

    def test_a_student_with_their_own_subscription_needs_no_seat(
            self, client, db, seed, cohort, published, seated):
        """Resolution order, most specific first. A student who pays for the
        platform themselves does not consume their school's seat."""
        from app.modules.billing.models import EntitlementRow

        student = _user(db, "Eldor", org_id=seed["org"].id, cohort_id=cohort.id)
        db.add(EntitlementRow(subject_kind="user", subject_id=student.id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=_now() - dt.timedelta(days=1)))
        db.flush()
        assert _assign(client, auth(seed["author"].xid), published,
                       cohort_xid=str(cohort.xid)).status_code == 201

    def test_a_site_licence_covers_everyone(self, client, db, seed, cohort,
                                            published, teacher):
        """The subscription centre, and the regression guard for it: a centre
        whose licence is not seat-based must notice no difference at all."""
        self._three(db, seed, cohort)
        assert _assign(client, teacher, published,
                       cohort_xid=str(cohort.xid)).status_code == 201

    def test_an_expired_site_licence_stops_covering(self, client, db, seed, cohort,
                                                    published, teacher):
        self._three(db, seed, cohort)
        db.execute(text("UPDATE entitlements SET expires_at = now() - interval '1 day' "
                        "WHERE feature = :f").bindparams(f=SEAT_BUNDLE[0]))
        db.flush()
        refused = _assign(client, teacher, published, cohort_xid=str(cohort.xid))
        assert refused.status_code == 402
        assert refused.json()["uncovered_count"] == 3
        # This said `no_seat`, always, whatever had happened. The centre's fault
        # here is a lapsed subscription; `no_seat` sends a centre admin to buy
        # seats, which for a site licence changes nothing and costs money.
        assert refused.json()["reason"] == "expired"

    def test_named_students_are_checked_too(self, client, db, seed, published,
                                            seated):
        """`target_kind="users"` is the other way in, and it takes the audience
        straight from the request body."""
        students = [_user(db, n, org_id=seed["org"].id) for n in ("Farrux", "Gulnoz")]
        refused = _assign(client, auth(seed["author"].xid), published,
                          target_kind="users",
                          user_xids=[str(s.xid) for s in students])
        assert refused.status_code == 402
        assert refused.json()["uncovered_count"] == 2

    def test_an_empty_cohort_is_still_assignable(self, client, db, seed, cohort,
                                                 published, seated):
        """Nobody to cover. A centre setting work for a class it has not enrolled
        yet is doing something odd, not something unpaid."""
        assert _assign(client, auth(seed["author"].xid), published,
                       cohort_xid=str(cohort.xid)).status_code == 201


class TestTheBundleIsWhatCovers:
    """The gate names a BUNDLE, not a string in a router.

    `products.features` is jsonb — "adding a plan is a row, not a code change" —
    and the coverage check was one hardcoded `"mock.unlimited"`. So a plan is data
    and the thing that redeems it was not, which is a mismatch that only shows up
    once somebody sells a plan nobody wrote code for.

    The bundle also has to REFUSE the wrong members, and that half matters more.
    `org.assignments` is org-held with a non-seat source, so `check()` grants it to
    every member of the org: put it in the bundle and every student at every centre
    that can set work at all is covered. That is not a widened gate, it is a
    deleted one, and it is the exact shape of the hole this check was added to
    close.
    """

    def test_the_capability_the_teacher_holds_does_not_cover_the_students(
            self, client, db, seed, cohort, published):
        """The one-line change that would look like a fix and be a hole.

        This centre holds `org.assignments` and nothing else — the plan shape that
        motivated a bundle in the first place. It must still be refused, because
        the alternative is a centre buying the right to SET work and getting every
        student's SITTING free.
        """
        from app.modules.billing.models import EntitlementRow

        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature="org.assignments", source_kind="order",
                              starts_at=_now() - dt.timedelta(days=1)))
        db.flush()
        for name in ("Aziza", "Bekzod"):
            _user(db, name, org_id=seed["org"].id, cohort_id=cohort.id)

        refused = _assign(client, auth(seed["author"].xid), published,
                          cohort_xid=str(cohort.xid))
        assert refused.status_code == 402, refused.text
        assert "org.assignments" not in SEAT_BUNDLE
        # Not `no_seat`: this centre has no seat licence to be outside of. The
        # answer is a product row, not an "assign seats" button.
        assert refused.json()["reason"] == "no_entitlement"
        assert "does not cover mock sittings" in refused.json()["title"]

    @pytest.mark.parametrize("granted", ["mock.pack", "mock.unlimited"])
    def test_any_member_of_the_bundle_covers(self, client, db, seed, cohort,
                                             published, monkeypatch, granted):
        """A second feature added to the tuple must work with no other change.

        Patched rather than invented, because shipping a feature string no product
        grants is how the `mock_exams` seat licence next door came to exist. What
        is being pinned is that `SEAT_BUNDLE` is the only place that decides.

        **Both positions, because one position proves nothing.** Written first
        with the granted feature at index 0, this passed while `check_any` was
        sabotaged to try only `features[:1]` — the loop it exists to run was never
        entered. A bundle test that only ever grants the first member is a test
        for a constant.
        """
        from app.api.routers import teaching
        from app.modules.billing.models import EntitlementRow

        monkeypatch.setattr(teaching, "SEAT_BUNDLE", ("mock.pack", "mock.unlimited"))
        for feature in ("org.assignments", granted):
            db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                                  feature=feature, source_kind="order",
                                  starts_at=_now() - dt.timedelta(days=1)))
        db.flush()
        for name in ("Aziza", "Bekzod", "Charos"):
            _user(db, name, org_id=seed["org"].id, cohort_id=cohort.id)
        assert _assign(client, auth(seed["author"].xid), published,
                       cohort_xid=str(cohort.xid)).status_code == 201

    def test_the_most_informative_denial_across_the_class_is_the_one_reported(
            self, client, db, seed, cohort, published, seated):
        """Thirty students, one sentence.

        One student is uncovered because the centre never seated them; another
        because their own subscription lapsed. `expired` outranks `no_seat` — the
        specific fault is the one worth acting on — and which one is reported must
        not depend on the order the class happens to come back in.
        """
        from app.modules.billing.models import EntitlementRow

        unseated = _user(db, "Aziza", org_id=seed["org"].id, cohort_id=cohort.id)
        lapsed = _user(db, "Bekzod", org_id=seed["org"].id, cohort_id=cohort.id)
        db.add(EntitlementRow(subject_kind="user", subject_id=lapsed.id,
                              feature=SEAT_BUNDLE[0], source_kind="order",
                              starts_at=_now() - dt.timedelta(days=30),
                              expires_at=_now() - dt.timedelta(days=1)))
        db.flush()
        refused = _assign(client, auth(seed["author"].xid), published,
                          cohort_xid=str(cohort.xid))
        assert refused.status_code == 402, refused.text
        assert refused.json()["uncovered_count"] == 2
        assert refused.json()["reason"] == "expired"
        assert {str(unseated.xid), str(lapsed.xid)} == set(
            refused.json()["uncovered_user_xids"])

    def test_the_402_names_what_to_buy(self, client, db, seed, cohort, published,
                                       seated):
        """The client renders `feature`, and a bundle still has to answer "which
        one". The first entry is the one that is actually sold."""
        _user(db, "Aziza", org_id=seed["org"].id, cohort_id=cohort.id)
        refused = _assign(client, auth(seed["author"].xid), published,
                          cohort_xid=str(cohort.xid))
        assert refused.json()["feature"] == SEAT_BUNDLE[0] == "mock.unlimited"


class TestAssignedWorkIsNeverThePupilsBill:
    """The other half of the rule, and the reason the check above belongs at
    creation rather than at attempt time: `POST /attempts` does not charge the
    student for assigned work.

    "A school pays per seat and its students never see a paywall for work the
    school set." Checking at creation is what lets that stay true — the coverage
    question is asked once, of a human who can act on the answer, and never of a
    child halfway through a timed mock.
    """

    @pytest.fixture
    def lapsed(self, client, db, seed, cohort, published, teacher):
        """Work set while the licence was live, then the licence expires.

        The realistic sequence and the one that decides where the check goes: a
        centre forgets to renew mid-term, and thirty students have a mock on
        Friday.
        """
        student = _user(db, "Hilola", org_id=seed["org"].id, cohort_id=cohort.id)
        created = _ok(_assign(client, teacher, published,
                              cohort_xid=str(cohort.xid)), 201)
        db.execute(text("UPDATE entitlements SET expires_at = now() - interval '1 day'"))
        db.flush()
        return student, created

    def test_the_student_still_sits_the_work_that_was_set(self, client, db, lapsed):
        student, created = lapsed
        assert db.scalar(text("""
            SELECT count(*) FROM entitlements WHERE subject_kind = 'user'
              AND subject_id = :u
        """).bindparams(u=student.id)) == 0
        started = client.post("/api/v1/attempts", headers=auth(student.xid),
                              json={"assignment_xid": created["xid"]})
        assert started.status_code == 201, started.text

    def test_the_same_student_is_refused_self_serve_practice(self, client, published,
                                                             lapsed):
        """The distinction that makes the model work. In-flight school work
        completes; a personal practice run against a lapsed licence does not."""
        student, _ = lapsed
        refused = client.post(
            "/api/v1/attempts", headers=auth(student.xid),
            json={"test_version_xid": str(published["test_version"].xid),
                  "mode": "practice"})
        assert refused.status_code == 402, refused.text

    def test_a_new_assignment_cannot_be_set_against_the_lapsed_licence(
            self, client, cohort, published, teacher, lapsed):
        """Where the centre DOES meet the wall — on the teacher's screen, with a
        renewal one click away, rather than on a student's."""
        assert _assign(client, teacher, published,
                       cohort_xid=str(cohort.xid)).status_code == 402


# ── the defect: assigning to your own students ───────────────────────

class TestAssigningToNamedStudents:
    """`target_kind="users"` is the ad-hoc path. Its centre check asked
    `cohort_members` — but a centre's roster is `org_memberships`, so a student
    enrolled at the centre and not yet in any class was refused as an outsider."""

    def test_a_student_at_my_centre_with_no_cohort_can_be_assigned(
            self, client, teacher, db, seed, published):
        """The whole reason this target kind exists: a student who is not in a
        class yet. Under the old check, every one of them was 'not in your own
        centre'."""
        student = _user(db, "Nodira", org_id=seed["org"].id)
        response = _assign(client, teacher, published, target_kind="users",
                           user_xids=[str(student.xid)])
        assert response.status_code == 201, response.text
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 1

    def test_a_student_at_my_centre_who_is_in_a_cohort_can_too(
            self, client, teacher, db, seed, published, cohort):
        student = _user(db, "Kamola", org_id=seed["org"].id, cohort_id=cohort.id)
        assert _assign(client, teacher, published, target_kind="users",
                       user_xids=[str(student.xid)]).status_code == 201

    def test_a_student_at_another_centre_is_still_refused(self, client, teacher, db,
                                                          published):
        """The half that is not negotiable. Setting work for a competitor's
        students would put this centre's paper in front of them."""
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        outsider = _user(db, "Sardor", org_id=rival.id)
        refused = _assign(client, teacher, published, target_kind="users",
                          user_xids=[str(outsider.xid)])
        assert refused.status_code == 403
        assert refused.json()["code"] == "student_not_in_org"

    def test_a_user_with_no_centre_at_all_is_refused(self, client, teacher, db,
                                                     published):
        loner = _user(db, "Anon")
        assert _assign(client, teacher, published, target_kind="users",
                       user_xids=[str(loner.xid)]).status_code == 403

    def test_a_former_member_is_refused_even_with_a_stale_cohort_row(
            self, client, teacher, db, seed, published, cohort):
        """`org_memberships` is the authority on who is at the centre, and
        `left_at` is what makes a roster a CURRENT roster.

        Deliberately left in the cohort. A student who left in June commonly still
        has the class row from last term, and under the old `cohort_members` check
        that stale row was enough to keep setting them work in September.
        """
        student = _user(db, "Gone", org_id=seed["org"].id, cohort_id=cohort.id)
        db.execute(text("UPDATE org_memberships SET status = 'left', left_at = now() "
                        "WHERE user_id = :u").bindparams(u=student.id))
        db.flush()
        refused = _assign(client, teacher, published, target_kind="users",
                          user_xids=[str(student.xid)])
        assert refused.status_code == 403
        assert refused.json()["code"] == "student_not_in_org"

    def test_an_unknown_user_xid_is_a_404(self, client, teacher, published):
        assert _assign(client, teacher, published, target_kind="users",
                       user_xids=[str(uuid.uuid4())]).status_code == 404

    def test_a_platform_admin_is_not_bound_by_the_centre_check(
            self, client, admin, db, published):
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        anyone = _user(db, "Sardor", org_id=rival.id)
        assert _assign(client, admin, published, target_kind="users",
                       user_xids=[str(anyone.xid)]).status_code == 201

    def test_several_students_at_once(self, client, teacher, db, seed, published):
        people = [_user(db, n, org_id=seed["org"].id) for n in ("A", "B", "C")]
        _ok(_assign(client, teacher, published, target_kind="users",
                    user_xids=[str(p.xid) for p in people]), 201)
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 3

    def test_one_outsider_spoils_the_batch(self, client, teacher, db, seed,
                                           published):
        """Partial success would leave the teacher unsure who was set the work."""
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        mine = _user(db, "Mine", org_id=seed["org"].id)
        theirs = _user(db, "Theirs", org_id=rival.id)
        assert _assign(client, teacher, published, target_kind="users",
                       user_xids=[str(mine.xid), str(theirs.xid)]).status_code == 403
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 0


# ── creating an assignment ───────────────────────────────────────────

class TestCreatingAnAssignment:
    def test_a_cohort_assignment_targets_its_members(self, client, teacher, db,
                                                     seed, published, cohort):
        for name in ("A", "B"):
            _user(db, name, org_id=seed["org"].id, cohort_id=cohort.id)
        body = _ok(_assign(client, teacher, published, target_kind="cohort",
                           cohort_xid=str(cohort.xid)), 201)
        assert body["cohort"]["member_count"] == 2
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 2

    def test_the_audience_is_materialized_at_creation(self, client, teacher, db,
                                                      seed, published, cohort):
        """"A cohort's membership changes; the assignment's audience does not."
        A student who joins next week must not be silently late for work set
        before they arrived."""
        _user(db, "Early", org_id=seed["org"].id, cohort_id=cohort.id)
        _ok(_assign(client, teacher, published, target_kind="cohort",
                    cohort_xid=str(cohort.xid)), 201)
        _user(db, "Late", org_id=seed["org"].id, cohort_id=cohort.id)
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 1

    def test_a_cohort_assignment_without_a_cohort_is_refused(self, client, teacher,
                                                             published):
        refused = _assign(client, teacher, published, target_kind="cohort")
        assert refused.status_code == 409
        assert refused.json()["code"] == "cohort_required"

    def test_a_self_serve_assignment_has_no_targets(self, client, teacher, db,
                                                    published):
        _ok(_assign(client, teacher, published, target_kind="self_serve"), 201)
        assert db.scalar(text("SELECT count(*) FROM assignment_targets")) == 0

    def test_a_window_that_closes_before_it_opens_is_refused(self, client, teacher,
                                                             published):
        refused = _assign(client, teacher, published, target_kind="self_serve",
                          opens_at=(_now() + dt.timedelta(days=2)).isoformat(),
                          closes_at=(_now() + dt.timedelta(days=1)).isoformat())
        assert refused.status_code == 409
        assert refused.json()["code"] == "invalid_window"

    def test_an_unpublished_version_cannot_be_assigned(self, client, teacher, seed):
        refused = _assign(client, teacher, seed, target_kind="self_serve")
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_not_published"

    def test_an_unknown_test_version_is_a_404(self, client, teacher, published):
        assert _assign(client, teacher, published, target_kind="self_serve",
                       test_version_xid=str(uuid.uuid4())).status_code == 404

    def test_a_student_cannot_set_one(self, client, db, seed, published):
        student = _user(db, "Aziza", org_id=seed["org"].id)
        refused = _assign(client, auth(student.xid), published,
                          target_kind="self_serve")
        assert refused.status_code == 403
        assert refused.json()["code"] == "not_a_teacher"

    def test_another_centres_cohort_is_a_404(self, client, teacher, db, seed,
                                             published):
        from app.modules.identity.models import Cohort, Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        theirs = Cohort(org_id=rival.id, name="Theirs",
                        created_by=seed["author"].id)
        db.add(theirs)
        db.flush()
        assert _assign(client, teacher, published, target_kind="cohort",
                       cohort_xid=str(theirs.xid)).status_code == 404

    def test_a_retry_with_the_same_key_returns_the_same_assignment(
            self, client, teacher, db, published):
        """The body must be byte-identical — `_assign` stamps `_now()` per call,
        and a same-key-different-body replay is deliberately a 409."""
        headers = {**teacher, "Idempotency-Key": "set-it-once"}
        body = {"test_version_xid": str(published["test_version"].xid),
                "target_kind": "self_serve",
                "opens_at": _now().isoformat(),
                "closes_at": (_now() + dt.timedelta(days=7)).isoformat()}
        first = _ok(client.post("/api/v1/assignments", json=body, headers=headers), 201)
        again = _ok(client.post("/api/v1/assignments", json=body, headers=headers), 201)
        assert first["xid"] == again["xid"]
        assert db.scalar(text("SELECT count(*) FROM assignments")) == 1

    def test_the_same_key_with_a_different_body_is_a_409(self, client, teacher,
                                                         published):
        headers = {**teacher, "Idempotency-Key": "set-it-once"}
        _ok(_assign(client, headers, published, target_kind="self_serve"), 201)
        clash = _assign(client, headers, published, target_kind="self_serve",
                        max_attempts=3)
        assert clash.status_code == 409
        assert clash.json()["code"] == "idempotency_key_reused"

    def test_creation_emits_the_outbox_event(self, client, teacher, db, published,
                                             cohort):
        _user(db, "A", org_id=cohort.org_id, cohort_id=cohort.id)
        _ok(_assign(client, teacher, published, target_kind="cohort",
                    cohort_xid=str(cohort.xid)), 201)
        row = db.execute(text("""
            SELECT payload FROM outbox WHERE event_type = 'assignment.created'
        """)).mappings().one()
        assert row["payload"]["targets"] == 1


# ── the list ─────────────────────────────────────────────────────────

class TestListingAssignments:
    """"Two different queries behind one path, chosen from the actor's role rather
    than from a client-supplied flag." """

    @pytest.fixture
    def three(self, client, teacher, db, published, cohort):
        _user(db, "A", org_id=cohort.org_id, cohort_id=cohort.id)
        _ok(_assign(client, teacher, published, target_kind="cohort",
                    cohort_xid=str(cohort.xid),
                    opens_at=(_now() - dt.timedelta(days=2)).isoformat(),
                    closes_at=(_now() + dt.timedelta(days=2)).isoformat()), 201)
        _ok(_assign(client, teacher, published, target_kind="self_serve",
                    opens_at=(_now() + dt.timedelta(days=1)).isoformat(),
                    closes_at=(_now() + dt.timedelta(days=5)).isoformat()), 201)
        _ok(_assign(client, teacher, published, target_kind="self_serve",
                    opens_at=(_now() - dt.timedelta(days=9)).isoformat(),
                    closes_at=(_now() - dt.timedelta(days=1)).isoformat()), 201)
        return cohort

    def test_a_teacher_sees_the_centres_assignments(self, client, teacher, three):
        body = _ok(client.get("/api/v1/assignments", headers=teacher))
        assert len(body["items"]) == 3

    def test_the_open_filter(self, client, teacher, three):
        body = _ok(client.get("/api/v1/assignments?state=open", headers=teacher))
        assert len(body["items"]) == 1

    def test_the_upcoming_filter(self, client, teacher, three):
        body = _ok(client.get("/api/v1/assignments?state=upcoming", headers=teacher))
        assert len(body["items"]) == 1

    def test_the_closed_filter(self, client, teacher, three):
        body = _ok(client.get("/api/v1/assignments?state=closed", headers=teacher))
        assert len(body["items"]) == 1

    def test_the_cohort_filter(self, client, teacher, three):
        body = _ok(client.get(f"/api/v1/assignments?cohort_xid={three.xid}",
                              headers=teacher))
        assert len(body["items"]) == 1
        assert body["items"][0]["cohort"]["name"] == "Evening group"

    def test_a_cohort_at_another_centre_is_a_404(self, client, teacher, db, seed,
                                                 three):
        from app.modules.identity.models import Cohort, Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        theirs = Cohort(org_id=rival.id, name="Theirs",
                        created_by=seed["author"].id)
        db.add(theirs)
        db.flush()
        assert client.get(f"/api/v1/assignments?cohort_xid={theirs.xid}",
                          headers=teacher).status_code == 404

    def test_a_student_sees_only_their_own(self, client, teacher, db, seed, three):
        """A student passing any filter gets their own assignments, not the
        centre's — the scope comes from their role, not from the request."""
        member = db.scalar(text("""
            SELECT u.xid FROM users u JOIN cohort_members m ON m.user_id = u.id
            WHERE m.cohort_id = :c
        """).bindparams(c=three.id))
        body = _ok(client.get("/api/v1/assignments", headers=auth(member)))
        assert len(body["items"]) == 1

    def test_a_student_at_the_centre_with_no_assignment_sees_nothing(
            self, client, db, seed, three):
        bystander = _user(db, "Nobody", org_id=seed["org"].id)
        body = _ok(client.get("/api/v1/assignments", headers=auth(bystander.xid)))
        assert body["items"] == []

    def test_a_directly_targeted_student_sees_it(self, client, teacher, db, seed,
                                                 published):
        student = _user(db, "Named", org_id=seed["org"].id)
        _ok(_assign(client, teacher, published, target_kind="users",
                    user_xids=[str(student.xid)]), 201)
        body = _ok(client.get("/api/v1/assignments", headers=auth(student.xid)))
        assert len(body["items"]) == 1
        assert body["items"][0]["my_attempts_used"] == 0


# ── progress ─────────────────────────────────────────────────────────

class TestProgress:
    """"Org MEMBERSHIP is not enough. This response carries every classmate's live
    progress and band, so it needs a teaching role at the centre that set it."" """

    @pytest.fixture
    def watched(self, client, teacher, db, seed, published, cohort):
        student = _user(db, "Aziza", org_id=seed["org"].id, cohort_id=cohort.id)
        body = _ok(_assign(client, teacher, published, target_kind="cohort",
                           cohort_xid=str(cohort.xid)), 201)
        return {"assignment_xid": body["xid"], "student": student,
                "cohort": cohort}

    def _attempt(self, db, watched, *, status: str, band=None, answered=0,
                 blanks: tuple[str, ...] = (), scored: bool = False):
        attempt = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, assignment_id, mode,
                                  status, started_at, expires_at)
            VALUES (:u, (SELECT test_version_id FROM assignments WHERE xid = CAST(:a AS uuid)),
                    (SELECT id FROM assignments WHERE xid = CAST(:a AS uuid)),
                    'exam', :s, now(),
                    now() + interval '1 hour')
            RETURNING id
        """).bindparams(u=watched["student"].id, a=watched["assignment_xid"],
                        s=status))
        for i in range(answered):
            db.execute(text("""
                INSERT INTO attempt_answers (attempt_id, question_version_id,
                                             slot_key, response)
                VALUES (:a, (SELECT min(id) FROM question_versions), :k,
                        '{"v": "x"}'::jsonb)
            """).bindparams(a=attempt, k=f"s{i}"))
        # Responses a student typed and then emptied. Stored as jsonb strings,
        # which is what the autosave endpoint writes and is NOT SQL NULL.
        for i, blank in enumerate(blanks):
            db.execute(text("""
                INSERT INTO attempt_answers (attempt_id, question_version_id,
                                             slot_key, response)
                VALUES (:a, (SELECT min(id) FROM question_versions), :k,
                        to_jsonb(CAST(:v AS text)))
            """).bindparams(a=attempt, k=f"b{i}", v=blank))
        if band is not None or scored:
            db.execute(text("""
                INSERT INTO score_runs (attempt_id, reason, engine_version,
                                        key_versions, raw_score, max_raw, band,
                                        is_current)
                VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 30, 40, :b, true)
            """).bindparams(a=attempt, b=band))
        db.flush()
        return attempt

    def test_a_student_who_has_not_started(self, client, teacher, watched):
        body = _ok(client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=teacher))
        assert body["summary"] == {"assigned": 1, "not_started": 1,
                                   "in_progress": 0, "submitted": 0}
        assert body["students"][0]["status"] == "not_started"

    def test_a_student_mid_paper(self, client, teacher, db, watched):
        self._attempt(db, watched, status="in_progress", answered=2)
        body = _ok(client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=teacher))
        assert body["students"][0]["status"] == "in_progress"
        assert body["students"][0]["answered"] == 2
        assert body["summary"]["in_progress"] == 1

    def test_a_submitted_but_unscored_paper(self, client, teacher, db, watched):
        self._attempt(db, watched, status="submitted")
        body = _ok(client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=teacher))
        assert body["students"][0]["status"] == "submitted"
        assert body["students"][0]["band"] is None

    def test_a_scored_paper_carries_its_band(self, client, teacher, db, watched):
        self._attempt(db, watched, status="scored", band=7.5)
        body = _ok(client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=teacher))
        assert body["students"][0]["status"] == "scored"
        assert body["students"][0]["band"] == 7.5
        assert body["summary"]["submitted"] == 1

    def test_a_cleared_answer_is_not_counted_as_answered(
            self, client, teacher, db, watched):
        """The screen and the marking used to disagree about the same student.

        `response IS NOT NULL` counted a box that was typed into and emptied,
        because a cleared input stores an empty JSON string rather than SQL NULL
        — while the scorer reads it as unanswered. A teacher walking the room saw
        a student five questions further on than the marking would ever agree
        they were.
        """
        self._attempt(db, watched, status="in_progress", answered=2,
                      blanks=("", "   ", "\n"))
        body = _ok(client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=teacher))
        assert body["students"][0]["answered"] == 2

    def test_a_scored_paper_with_no_band_still_reads_as_scored(
            self, client, teacher, db, watched):
        """A raw the band map does not cover scores with `band = null`. The run
        is real and the marking is real; only the number is missing.

        Reading the band reported that attempt as "submitted" for ever, so the
        Marking button never appeared for exactly the cohort a teacher most needs
        to look at — the one whose band map has a hole in it.
        """
        self._attempt(db, watched, status="scored", band=None, scored=True)
        body = _ok(client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=teacher))
        assert body["students"][0]["status"] == "scored"
        assert body["students"][0]["band"] is None

    def test_a_classmate_cannot_watch_the_room(self, client, db, seed, watched):
        """The one that matters. A student in the same org is exactly who must
        not see every classmate's live progress, band and phone number."""
        classmate = _user(db, "Nosy", org_id=seed["org"].id,
                          cohort_id=watched["cohort"].id)
        assert client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=auth(classmate.xid)).status_code == 404

    def test_a_teacher_at_another_centre_cannot(self, client, db, watched):
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        stranger = _user(db, "Rival teacher", org_id=rival.id, role="teacher")
        assert client.get(
            f"/api/v1/assignments/{watched['assignment_xid']}/progress",
            headers=auth(stranger.xid)).status_code == 404

    def test_an_unknown_assignment_is_a_404(self, client, teacher):
        assert client.get(f"/api/v1/assignments/{uuid.uuid4()}/progress",
                          headers=teacher).status_code == 404


# ── regrades ─────────────────────────────────────────────────────────

class TestStagingARegrade:
    """"Nothing is recomputed on the way in and nothing is applied without a human
    seeing the numbers first." The numbers are the point."""

    def _stage(self, client, headers, **overrides):
        body = {"trigger": "answer_key_change", "subject_type": "question_version",
                "subject_xid": None, "reason": "Question 12 key was wrong"}
        body.update(overrides)
        return client.post("/api/v1/regrades", json=body, headers=headers)

    def test_staging_against_a_question_version(self, client, admin, db, seed):
        qv = seed["question_versions"][0]
        body = _ok(self._stage(client, admin, subject_xid=str(qv.xid)), 201)
        assert body["dry_run"] is True
        assert body["status"] == "planning"

    def test_the_impact_counts_the_attempts_that_scored_that_item(
            self, client, admin, db, seed, published):
        """"How many students does this touch" is the first thing the author
        wants, and it is computed inline because a count is cheap."""
        qv = seed["question_versions"][0]
        for _ in range(2):
            attempt = db.scalar(text("""
                INSERT INTO attempts (user_id, test_version_id, mode, status,
                                      started_at, submitted_at)
                VALUES (:u, :tv, 'exam', 'scored', now(), now()) RETURNING id
            """).bindparams(u=seed["student"].id, tv=seed["test_version"].id))
            run = db.scalar(text("""
                INSERT INTO score_runs (attempt_id, reason, engine_version,
                                        key_versions, raw_score, max_raw, is_current)
                VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 3, 3, true) RETURNING id
            """).bindparams(a=attempt))
            db.execute(text("""
                INSERT INTO item_scores (score_run_id, question_id,
                                     question_version_id, slot_key, awarded,
                                     max_points, verdict)
                VALUES (:r, (SELECT question_id FROM question_versions WHERE id = :q),
                        :q, 's1', 1, 1, 'correct')
            """).bindparams(r=run, q=qv.id))
        db.flush()
        body = _ok(self._stage(client, admin, subject_xid=str(qv.xid)), 201)
        assert body["impact"]["attempts_total"] == 2

    def test_a_preview_attempt_is_not_counted(self, client, admin, db, seed):
        """An author's own preview run is not a student whose band moves."""
        qv = seed["question_versions"][0]
        attempt = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status,
                                  started_at, submitted_at)
            VALUES (:u, :tv, 'preview', 'scored', now(), now()) RETURNING id
        """).bindparams(u=seed["author"].id, tv=seed["test_version"].id))
        run = db.scalar(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 3, 3, true) RETURNING id
        """).bindparams(a=attempt))
        db.execute(text("""
            INSERT INTO item_scores (score_run_id, question_id, question_version_id,
                                     slot_key, awarded, max_points, verdict)
            VALUES (:r, (SELECT question_id FROM question_versions WHERE id = :q),
                    :q, 's1', 1, 1, 'correct')
        """).bindparams(r=run, q=qv.id))
        db.flush()
        body = _ok(self._stage(client, admin, subject_xid=str(qv.xid)), 201)
        assert body["impact"]["attempts_total"] == 0

    def test_staging_against_a_test_version_counts_its_sat_attempts(
            self, client, admin, db, seed):
        for status in ("scored", "submitted", "in_progress"):
            db.execute(text("""
                INSERT INTO attempts (user_id, test_version_id, mode, status,
                                      started_at)
                VALUES (:u, :tv, 'exam', :s, now())
            """).bindparams(u=seed["student"].id, tv=seed["test_version"].id,
                            s=status))
        db.flush()
        body = _ok(self._stage(client, admin, subject_type="test_version",
                               subject_xid=str(seed["test_version"].xid),
                               trigger="engine_fix"), 201)
        # in_progress is not a score anyone has been told, so it is not impact.
        assert body["impact"]["attempts_total"] == 2

    def test_a_band_map_regrade_reports_a_real_impact(self, client, admin, db, seed):
        """The defect. `_affected_count` had no `band_map_version` branch and fell
        through to `return 0`, so the number in front of the human deciding whether
        to run the job was zero — for a job the planner scopes perfectly well.

        A band map is a curve every test version using it is scored against, so
        retuning it is the single widest-reaching regrade in the system.
        """
        for status in ("scored", "submitted"):
            db.execute(text("""
                INSERT INTO attempts (user_id, test_version_id, mode, status,
                                      started_at)
                VALUES (:u, :tv, 'exam', :s, now())
            """).bindparams(u=seed["student"].id, tv=seed["test_version"].id,
                            s=status))
        db.flush()
        body = _ok(self._stage(client, admin, subject_type="band_map_version",
                               subject_xid=str(seed["band_map_version"].xid),
                               trigger="band_map_change"), 201)
        assert body["impact"]["attempts_total"] == 2

    def test_a_band_map_regrade_ignores_versions_on_another_curve(
            self, client, admin, db, seed):
        """Scoped exactly as the planner scopes it — attempts on test versions
        that use THIS band map."""
        from app.modules.content.models import BandMap, BandMapVersion

        other_map = BandMap(org_id=seed["org"].id, name="Other", skill="reading",
                            created_by=seed["author"].id)
        db.add(other_map)
        db.flush()
        other = BandMapVersion(band_map_id=other_map.id, mapping=[], max_raw=40,
                               status="published", created_by=seed["author"].id)
        db.add(other)
        db.flush()
        db.execute(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status, started_at)
            VALUES (:u, :tv, 'exam', 'scored', now())
        """).bindparams(u=seed["student"].id, tv=seed["test_version"].id))
        db.flush()
        body = _ok(self._stage(client, admin, subject_type="band_map_version",
                               subject_xid=str(other.xid),
                               trigger="band_map_change"), 201)
        assert body["impact"]["attempts_total"] == 0

    def test_staging_against_a_single_attempt(self, client, admin, db, seed):
        attempt_xid = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status, started_at)
            VALUES (:u, :tv, 'exam', 'scored', now()) RETURNING xid
        """).bindparams(u=seed["student"].id, tv=seed["test_version"].id))
        db.flush()
        body = _ok(self._stage(client, admin, subject_type="attempt",
                               subject_xid=str(attempt_xid), trigger="manual"), 201)
        assert body["impact"]["attempts_total"] == 1

    @pytest.mark.parametrize("subject_type", ["question_version", "test_version",
                                              "band_map_version", "attempt"])
    def test_an_unknown_subject_is_a_404(self, client, admin, subject_type):
        assert self._stage(client, admin, subject_type=subject_type,
                           subject_xid=str(uuid.uuid4()),
                           trigger="manual").status_code == 404

    def test_include_competitions_is_accepted_and_dropped(self, client, admin, db,
                                                          seed):
        """"A finished contest is never swept into a bulk regrade, it gets its own
        recorded decision." Accepting the flag and ignoring it is deliberate; the
        stored scope must not carry it."""
        qv = seed["question_versions"][0]
        _ok(self._stage(client, admin, subject_xid=str(qv.xid),
                        scope={"include_competitions": True, "from_date": "2026-01-01"}),
            201)
        stored = db.scalar(text("SELECT scope FROM regrade_jobs"))
        assert stored == {"from_date": "2026-01-01"}

    def test_a_student_cannot_stage_one(self, client, db, seed):
        student = _user(db, "Aziza", org_id=seed["org"].id)
        assert self._stage(client, auth(student.xid),
                           subject_xid=str(seed["question_versions"][0].xid)) \
            .status_code == 403

    def test_a_retry_returns_the_same_job(self, client, admin, db, seed):
        headers = {**admin, "Idempotency-Key": "plan-once"}
        qv = seed["question_versions"][0]
        first = _ok(self._stage(client, headers, subject_xid=str(qv.xid)), 201)
        again = _ok(self._stage(client, headers, subject_xid=str(qv.xid)), 201)
        assert first["xid"] == again["xid"]
        assert db.scalar(text("SELECT count(*) FROM regrade_jobs")) == 1

    def test_staging_emits_the_plan_request(self, client, admin, db, seed):
        _ok(self._stage(client, admin,
                        subject_xid=str(seed["question_versions"][0].xid)), 201)
        assert db.scalar(text("""
            SELECT count(*) FROM outbox WHERE event_type = 'regrade.plan_requested'
        """)) == 1


class TestListingAndReadingRegrades:
    @pytest.fixture
    def job(self, db, seed):
        return db.execute(text("""
            INSERT INTO regrade_jobs (trigger, subject_type, subject_id,
                                      initiated_by, reason, dry_run, status)
            VALUES ('answer_key_change', 'question_version', :q, :u,
                    'key was wrong', true, 'ready')
            RETURNING id, xid
        """).bindparams(q=seed["question_versions"][0].id,
                        u=seed["author"].id)).mappings().one()

    def test_a_job_is_listed_to_the_person_who_started_it(self, client, admin, job):
        body = _ok(client.get("/api/v1/regrades", headers=admin))
        assert [j["xid"] for j in body] == [str(job["xid"])]

    def test_the_status_filter(self, client, admin, job):
        """`status`, the declared name. The handler took `status_filter`, so the
        console's filter did nothing and this test passed only by sending the
        name the generated client cannot produce."""
        assert _ok(client.get("/api/v1/regrades?status=running",
                              headers=admin)) == []
        assert len(_ok(client.get("/api/v1/regrades?status=ready",
                                  headers=admin))) == 1

    def test_someone_elses_job_is_not_listed(self, client, db, seed, job):
        """"A centre sees the jobs it started, not the platform's." """
        other = _user(db, "Other teacher", org_id=seed["org"].id, role="teacher")
        assert _ok(client.get("/api/v1/regrades", headers=auth(other.xid))) == []

    def test_reading_someone_elses_job_is_a_404(self, client, db, seed, job):
        other = _user(db, "Other teacher", org_id=seed["org"].id, role="teacher")
        assert client.get(f"/api/v1/regrades/{job['xid']}",
                          headers=auth(other.xid)).status_code == 404

    def test_reading_your_own(self, client, admin, job):
        body = _ok(client.get(f"/api/v1/regrades/{job['xid']}", headers=admin))
        assert body["status"] == "ready"

    def test_an_unknown_job_is_a_404(self, client, admin):
        assert client.get(f"/api/v1/regrades/{uuid.uuid4()}",
                          headers=admin).status_code == 404


class TestApplyingARegrade:
    """"`apply` refuses outright while a finished competition's ranking would
    move, because a leaderboard that changes by itself looks like fraud." """

    @pytest.fixture
    def ready(self, db, seed):
        return db.execute(text("""
            INSERT INTO regrade_jobs (trigger, subject_type, subject_id,
                                      initiated_by, reason, dry_run, status)
            VALUES ('answer_key_change', 'question_version', :q, :u,
                    'key was wrong', true, 'ready')
            RETURNING id, xid
        """).bindparams(q=seed["question_versions"][0].id,
                        u=seed["author"].id)).mappings().one()

    def test_applying_a_ready_job_starts_it(self, client, admin, db, ready):
        body = _ok(client.post(f"/api/v1/regrades/{ready['xid']}/apply",
                               headers=admin), 202)
        assert body["status"] == "running"
        assert body["dry_run"] is False
        assert db.scalar(text("""
            SELECT count(*) FROM outbox WHERE event_type = 'regrade.apply_requested'
        """)) == 1

    def test_a_job_still_planning_cannot_be_applied(self, client, admin, db, ready):
        db.execute(text("UPDATE regrade_jobs SET status = 'planning' WHERE id = :j")
                   .bindparams(j=ready["id"]))
        db.flush()
        refused = client.post(f"/api/v1/regrades/{ready['xid']}/apply", headers=admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "regrade_not_ready"

    def test_an_undecided_competition_blocks_it(self, client, admin, db, seed,
                                                published, ready):
        """The 409 names the contests, so the admin knows what to go and decide
        rather than being told 'no'."""
        contest_xid = db.scalar(text("""
            INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, status,
                                      visibility, created_by)
            VALUES (:o, :tv, 'Friday', now() - interval '2 hours',
                    now() - interval '2 hours', now() - interval '1 hour', 1800,
                    'final', 'org', :by)
            RETURNING xid
        """).bindparams(o=seed["org"].id, tv=seed["test_version"].id,
                        by=seed["author"].id))
        db.execute(text("""
            UPDATE regrade_jobs SET competition_impact =
                CAST(:impact AS jsonb) WHERE id = :j
        """).bindparams(j=ready["id"], impact=__import__("json").dumps(
            [{"competition_xid": str(contest_xid), "decision_required": True,
              "rank_changes": 3}])))
        db.flush()
        refused = client.post(f"/api/v1/regrades/{ready['xid']}/apply", headers=admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "competition_decision_required"
        assert refused.json()["competitions"] == [str(contest_xid)]

    def test_a_decided_competition_does_not(self, client, admin, db, seed, ready):
        contest = db.execute(text("""
            INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, status,
                                      visibility, created_by)
            VALUES (:o, :tv, 'Friday', now() - interval '2 hours',
                    now() - interval '2 hours', now() - interval '1 hour', 1800,
                    'final', 'org', :by)
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, tv=seed["test_version"].id,
                        by=seed["author"].id)).mappings().one()
        db.execute(text("""
            UPDATE regrade_jobs SET competition_impact = CAST(:impact AS jsonb)
            WHERE id = :j
        """).bindparams(j=ready["id"], impact=__import__("json").dumps(
            [{"competition_xid": str(contest["xid"]), "decision_required": True}])))
        db.execute(text("""
            INSERT INTO competition_regrade_decisions
                (competition_id, regrade_job_id, impact, decision, decided_by,
                 decided_at, rationale)
            VALUES (:c, :j, '{}'::jsonb, 'leave_as_is', :u, now(), 'no rank change')
        """).bindparams(c=contest["id"], j=ready["id"], u=seed["author"].id))
        db.flush()
        assert client.post(f"/api/v1/regrades/{ready['xid']}/apply",
                           headers=admin).status_code == 202

    def test_an_impact_needing_no_decision_does_not_block(self, client, admin, db,
                                                          ready):
        db.execute(text("""
            UPDATE regrade_jobs SET competition_impact = CAST(:impact AS jsonb)
            WHERE id = :j
        """).bindparams(j=ready["id"], impact=__import__("json").dumps(
            [{"competition_xid": str(uuid.uuid4()), "decision_required": False}])))
        db.flush()
        assert client.post(f"/api/v1/regrades/{ready['xid']}/apply",
                           headers=admin).status_code == 202

    def test_a_retry_returns_the_same_response(self, client, admin, db, ready):
        headers = {**admin, "Idempotency-Key": "apply-once"}
        first = _ok(client.post(f"/api/v1/regrades/{ready['xid']}/apply",
                                headers=headers), 202)
        again = _ok(client.post(f"/api/v1/regrades/{ready['xid']}/apply",
                                headers=headers), 202)
        assert first == again
        assert db.scalar(text("""
            SELECT count(*) FROM outbox WHERE event_type = 'regrade.apply_requested'
        """)) == 1

    def test_someone_elses_job_cannot_be_applied(self, client, db, seed, ready):
        other = _user(db, "Other teacher", org_id=seed["org"].id, role="teacher")
        assert client.post(f"/api/v1/regrades/{ready['xid']}/apply",
                           headers=auth(other.xid)).status_code == 404

    def test_an_unknown_job_is_a_404(self, client, admin):
        assert client.post(f"/api/v1/regrades/{uuid.uuid4()}/apply",
                           headers=admin).status_code == 404
