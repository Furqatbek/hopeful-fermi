"""The two cohort analytics screens, driven the way the console drives them.

`test_analytics_endpoints.py` covers what the endpoints compute. This covers the
call SEQUENCE each screen issues — org list, class list, then the report — and
the three things about the responses that decide how they can honestly be drawn:

  * **every number on `/cohorts/{xid}/progress` arrives as a JSON string.** Each
    one comes from a PostgreSQL `numeric`, which is a `Decimal` in Python and a
    quoted string on the wire. The generated TypeScript says `number`, so
    `avg_band.toFixed(1)` compiles and throws;
  * **`late` is a subset of `completed`.** A late submission is counted in both,
    so an attendance bar has to subtract rather than stack;
  * **`first_band`/`latest_band` are `min`/`max`, not first and last.** A student
    who declined is reported as having improved by the size of their own fall,
    which is why the Cohort progress screen labels them lowest and highest and
    puts the only real direction on the cohort's weekly series.

The `late` and role-check assertions here have both been proved by sabotage.
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


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def cohort(db, seed):
    """A class at the seeded centre, with the seeded student in it."""
    cohort_id = db.scalar(text("""
        INSERT INTO cohorts (org_id, name, created_by)
        VALUES (:o, 'Evening IELTS', :u) RETURNING id
    """).bindparams(o=seed["org"].id, u=seed["author"].id))
    db.execute(text("""
        INSERT INTO cohort_members (cohort_id, user_id, status)
        VALUES (:c, :u, 'active')
    """).bindparams(c=cohort_id, u=seed["student"].id))
    db.flush()
    xid = db.scalar(text("SELECT xid FROM cohorts WHERE id = :c").bindparams(c=cohort_id))
    return {"id": cohort_id, "xid": xid}


def _pick(client, headers, cohort_name="Evening IELTS"):
    """The picker both screens open with: every org, then that org's classes.

    Exactly what `CohortProgress` and `Attendance` issue before they can ask for
    anything — the cohort xid is a path parameter, so there is no report at all
    until these two land.
    """
    orgs = _ok(client.get("/api/v1/orgs?limit=25", headers=headers))
    org_xid = orgs["items"][0]["xid"]
    classes = _ok(client.get(f"/api/v1/orgs/{org_xid}/cohorts", headers=headers))
    chosen = next(c for c in classes if c["name"] == cohort_name)
    return org_xid, chosen


def _scored(db, seed, published, *, band, when, mode="exam", org_context=True,
            user=None):
    """One scored attempt, dated so it lands in a particular week."""
    attempt_id = db.scalar(text("""
        INSERT INTO attempts (user_id, test_version_id, mode, status, submitted_at,
                              org_context_id)
        VALUES (:u, :tv, :m, 'scored', CAST(:w AS timestamptz), :o) RETURNING id
    """).bindparams(u=(user or seed["student"]).id,
                    tv=published["test_version"].id, m=mode,
                    w=f"{when} 12:00+05", o=seed["org"].id if org_context else None))
    db.execute(text("""
        INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                raw_score, max_raw, band, per_section, is_current)
        VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 2, 3, :b,
                CAST(:ps AS jsonb), true)
    """).bindparams(a=attempt_id, b=band,
                    ps='{"reading": {"band": %s}, "listening": {"band": %s}}'
                       % (band, band)))


def _refresh(db) -> None:
    """Plain, not CONCURRENTLY: the test session is inside a transaction and
    CONCURRENTLY cannot run in one."""
    db.flush()
    db.execute(text("REFRESH MATERIALIZED VIEW mv_cohort_progress"))


def _classmate(db, seed, cohort, name):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(2008, 3, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id, role="student",
                         status="active"))
    db.execute(text("INSERT INTO cohort_members (cohort_id, user_id, status) "
                    "VALUES (:c, :u, 'active')")
               .bindparams(c=cohort["id"], u=user.id))
    db.flush()
    return user


# ── cohort progress ──────────────────────────────────────────────────

class TestTheProgressScreenReachesItsData:
    def test_the_pickers_lead_to_the_report(self, client, seed, cohort):
        """The three calls in order. A teacher is the least-privileged actor who
        may open this, so it is the one the sequence is proved with."""
        headers = auth(seed["author"].xid)
        _, chosen = _pick(client, headers)
        assert chosen["xid"] == str(cohort["xid"])
        body = _ok(client.get(f"/api/v1/cohorts/{chosen['xid']}/progress",
                              headers=headers))
        assert body["cohort_xid"] == str(cohort["xid"])

    def test_an_archived_class_never_reaches_the_picker(self, client, db, seed,
                                                         cohort):
        """`GET /orgs/{xid}/cohorts` filters to `status = 'active'`, and it is the
        only listing there is — so last term's class has a report the console has
        no route to. The endpoint itself does not mind."""
        db.execute(text("UPDATE cohorts SET status = 'archived' WHERE id = :c")
                   .bindparams(c=cohort["id"]))
        db.flush()
        headers = auth(seed["author"].xid)
        org_xid = _ok(client.get("/api/v1/orgs?limit=25", headers=headers))["items"][0]["xid"]
        assert _ok(client.get(f"/api/v1/orgs/{org_xid}/cohorts", headers=headers)) == []
        assert client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                          headers=headers).status_code == 200

    def test_an_empty_class_renders_as_empty_rather_than_erroring(
            self, client, seed, cohort):
        body = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))
        assert body["weeks"] == []
        assert body["students"] == []


class TestTheNumbersArriveAsStrings:
    """The defect that decides how this screen may read a band.

    `sum(attempts)` over a bigint and `round(avg(...), 1)` are both PostgreSQL
    `numeric`; psycopg gives a `Decimal` and pydantic serializes one as a quoted
    string. `CohortProgress` declares them `number`, so every arithmetic operation
    the screen would naturally reach for is a `TypeError` in front of a teacher.
    """

    def test_every_weekly_figure_is_a_string(self, client, db, seed, cohort,
                                              published):
        _scored(db, seed, published, band=5.5, when="2026-07-08")
        _refresh(db)
        week = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))["weeks"][0]
        for field in ("attempts", "avg_band", "reading_band", "listening_band"):
            assert isinstance(week[field], str), (
                f"{field} is declared a number and arrived as {type(week[field])}")
        assert float(week["avg_band"]) == 5.5

    def test_the_per_student_bands_are_strings_too(self, client, db, seed, cohort,
                                                    published):
        _scored(db, seed, published, band=5.5, when="2026-07-08")
        _refresh(db)
        student = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                                 headers=auth(seed["author"].xid)))["students"][0]
        assert isinstance(student["first_band"], str)
        assert isinstance(student["latest_band"], str)
        # `delta` is computed in Python with `float()`, so it alone is a number.
        # Two types for five fields on one object is what makes a blanket cast
        # unsafe and a per-field coercion the only honest read.
        assert isinstance(student["delta"], float)

    def test_a_missing_band_is_null_and_not_the_string_none(
            self, client, db, seed, cohort, published):
        """A reading-only paper leaves `listening_band` empty. Null survives the
        coercion as "no band"; the string "None" would parse as nothing and could
        be rendered as a band."""
        attempt_id = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status,
                                  submitted_at, org_context_id)
            VALUES (:u, :tv, 'exam', 'scored',
                    CAST('2026-07-08 12:00+05' AS timestamptz), :o) RETURNING id
        """).bindparams(u=seed["student"].id, tv=published["test_version"].id,
                        o=seed["org"].id))
        db.execute(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, band, per_section, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 2, 3, 6.0,
                    '{"reading": {"band": 6.0}}'::jsonb, true)
        """).bindparams(a=attempt_id))
        _refresh(db)
        week = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))["weeks"][0]
        assert week["listening_band"] is None
        assert week["reading_band"] == "6.0"


class TestWhatTheStudentColumnsActuallyMean:
    def test_a_student_who_declined_is_reported_as_having_improved(
            self, client, db, seed, cohort, published):
        """`first_band` is `min(avg_band)` and `latest_band` is `max(best_band)`,
        so `delta` is best minus worst and cannot be negative.

        This student went 7.0 in the first week and 5.0 in the second. The API
        answers "first 5.0, latest 7.0, +2.0" — the exact opposite of what
        happened, on the one question a centre is asked by a parent. The screen
        therefore labels these lowest/highest/spread and takes the class's
        direction from the weekly series instead.
        """
        _scored(db, seed, published, band=7.0, when="2026-07-08")
        _scored(db, seed, published, band=5.0, when="2026-07-15")
        _refresh(db)
        body = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))
        student = body["students"][0]
        assert float(student["first_band"]) == 5.0
        assert float(student["latest_band"]) == 7.0
        assert student["delta"] == pytest.approx(2.0)
        # The weekly series, which the screen draws, has it the right way round.
        assert [float(w["avg_band"]) for w in body["weeks"]] == [7.0, 5.0]

    def test_a_students_attempts_field_counts_weeks_not_sittings(
            self, client, db, seed, cohort, published):
        """The per-student query is `count(*)` over a view with one row per
        (cohort, student, week). Three sittings across two weeks reports 2, while
        the weekly rows correctly sum to 3 — so the same word means two things on
        one response."""
        _scored(db, seed, published, band=5.0, when="2026-07-08")
        _scored(db, seed, published, band=6.0, when="2026-07-09")
        _scored(db, seed, published, band=7.0, when="2026-07-15")
        _refresh(db)
        body = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))
        assert sum(int(w["attempts"]) for w in body["weeks"]) == 3
        assert body["students"][0]["attempts"] == 2

    def test_weak_types_is_always_empty(self, client, db, seed, cohort, published):
        """Declared as a list of the question types a student loses marks on, and
        pinned to `[]` in the handler. `user_skill_progress.weak_types` holds the
        real thing and nothing joins to it, so the screen shows no column for it
        rather than an always-blank one."""
        _scored(db, seed, published, band=5.0, when="2026-07-08")
        _refresh(db)
        student = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                                 headers=auth(seed["author"].xid)))["students"][0]
        assert student["weak_types"] == []


class TestTheDeclaredDateFilterDoesNothing:
    def test_from_and_to_are_accepted_and_ignored(self, client, db, seed, cohort,
                                                   published):
        """The contract declares both; the handler's signature has neither, and
        the SQL has no date predicate. Sending them looks like it worked, which is
        worse than a 400 — so the screen does not offer a date range."""
        _scored(db, seed, published, band=5.0, when="2026-07-08")
        _scored(db, seed, published, band=7.0, when="2026-07-15")
        _refresh(db)
        headers = auth(seed["author"].xid)
        everything = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                                    headers=headers))
        narrowed = _ok(client.get(
            f"/api/v1/cohorts/{cohort['xid']}/progress?from=2026-07-14&to=2026-07-20",
            headers=headers))
        assert len(everything["weeks"]) == 2
        assert narrowed["weeks"] == everything["weeks"]


class TestOnlyWorkTheCentreSet:
    def test_a_students_own_practice_stays_out_of_their_schools_report(
            self, client, db, seed, cohort, published):
        """The contractual line, asserted through the call the screen makes. An
        attempt with no org context is the student's own use of the app."""
        _scored(db, seed, published, band=8.0, when="2026-07-08", org_context=False)
        _refresh(db)
        body = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))
        assert body["students"] == []
        assert body["weeks"] == []

    def test_an_author_preview_is_not_a_sitting(self, client, db, seed, cohort,
                                                 published):
        _scored(db, seed, published, band=8.0, when="2026-07-08", mode="preview")
        _refresh(db)
        assert _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid)))["weeks"] == []


class TestWhoMayOpenTheProgressScreen:
    def test_a_student_in_the_class_may_not(self, client, db, seed, cohort,
                                             published):
        """404, not 403: this carries every classmate's bands, and confirming the
        class exists is itself information. A student in the same organization is
        precisely the person who must not see it, so membership is the wrong test
        and the role is the right one."""
        _scored(db, seed, published, band=8.0, when="2026-07-08")
        _refresh(db)
        refused = client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 404, refused.text
        assert "8.0" not in refused.text

    def test_a_rival_centre_may_not(self, client, db, seed, cohort, published,
                                    rival):
        _scored(db, seed, published, band=8.0, when="2026-07-08")
        _refresh(db)
        refused = client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                             headers=rival)
        assert refused.status_code == 404, refused.text


# ── attendance ───────────────────────────────────────────────────────

def _assignment(db, seed, cohort, published, opens, closes):
    row = db.scalar(text("""
        INSERT INTO assignments (org_id, cohort_id, test_version_id, assigned_by,
                                 target_kind, opens_at, closes_at, mode,
                                 allow_review_after, max_attempts, status)
        VALUES (:o, :c, :tv, :u, 'cohort', CAST(:op AS timestamptz),
                CAST(:cl AS timestamptz), 'exam', 'close', 1, 'active')
        RETURNING id
    """).bindparams(o=seed["org"].id, c=cohort["id"],
                    tv=published["test_version"].id, u=seed["author"].id,
                    op=opens, cl=closes))
    return row


def _target(db, assignment_id, user):
    db.execute(text("INSERT INTO assignment_targets (assignment_id, user_id) "
                    "VALUES (:a, :u)").bindparams(a=assignment_id, u=user.id))


def _sat(db, seed, published, assignment_id, user, when):
    db.execute(text("""
        INSERT INTO attempts (user_id, test_version_id, assignment_id, mode, status,
                              submitted_at, org_context_id, attempt_no)
        VALUES (:u, :tv, :a, 'exam', 'scored', CAST(:w AS timestamptz), :o, 1)
    """).bindparams(u=user.id, tv=published["test_version"].id, a=assignment_id,
                    w=when, o=seed["org"].id))


@pytest.fixture
def a_term(db, seed, cohort, published):
    """Three pieces of work set to the class: one handed in on time, one handed
    in after it closed, one never opened.

    Built through `refresh_attendance` rather than by writing `attendance_facts`
    by hand, because the question this file has to answer is what the projection
    produces — a hand-written row would let the screen agree with an assumption
    instead of with the job.
    """
    from app.modules.analytics import projections

    late = _assignment(db, seed, cohort, published,
                       "2026-07-06 09:00+05", "2026-07-10 09:00+05")
    on_time = _assignment(db, seed, cohort, published,
                          "2026-07-06 09:00+05", "2026-07-14 09:00+05")
    untouched = _assignment(db, seed, cohort, published,
                            "2026-07-06 09:00+05", "2026-07-30 09:00+05")
    for assignment in (late, on_time, untouched):
        _target(db, assignment, seed["student"])
    _sat(db, seed, published, late, seed["student"], "2026-07-12 09:00+05")
    _sat(db, seed, published, on_time, seed["student"], "2026-07-12 09:00+05")
    db.flush()
    projections.refresh_attendance(db, now=dt.datetime(2026, 7, 20, tzinfo=dt.UTC))
    db.flush()
    return {"late": late, "on_time": on_time, "untouched": untouched}


class TestTheAttendanceScreenReachesItsData:
    def test_the_pickers_lead_to_the_grid(self, client, seed, cohort):
        headers = auth(seed["author"].xid)
        _, chosen = _pick(client, headers)
        body = _ok(client.get(f"/api/v1/cohorts/{chosen['xid']}/attendance",
                              headers=headers))
        assert body["cohort_xid"] == str(cohort["xid"])

    def test_a_class_that_has_been_set_nothing_is_empty_not_an_error(
            self, client, seed, cohort):
        assert _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                              headers=auth(seed["author"].xid)))["rows"] == []

    def test_the_counts_are_numbers_unlike_the_progress_screen(
            self, client, seed, cohort, a_term):
        """Every field here is an `int` or a Python `float`, so this screen's
        arithmetic is safe where the other screen's is not. Asserted so the two
        do not get treated as one problem."""
        row = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                             headers=auth(seed["author"].xid)))["rows"][0]
        for field in ("assigned", "started", "completed", "late"):
            assert isinstance(row[field], int), field
        assert isinstance(row["completion_rate"], float)


class TestLateIsNotTheSameAsNotStarted:
    """The distinction the page exists to draw, and the only one a parent asks
    about twice."""

    def test_a_late_submission_counts_as_completed_as_well_as_late(
            self, client, seed, cohort, a_term):
        """`completed` comes from the attempt's status, `late` from
        `submitted_at > closes_at` — two columns off the same submitted attempt.
        Adding them would report four pieces of work where three were set, and
        showing late work as not done would tell a parent their child skipped it.
        """
        row = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                             headers=auth(seed["author"].xid)))["rows"][0]
        assert (row["assigned"], row["started"], row["completed"], row["late"]) \
            == (3, 2, 2, 1)
        # What the bar draws: on time, late, unfinished, never started.
        on_time = row["completed"] - row["late"]
        never_started = row["assigned"] - row["started"]
        assert (on_time, row["late"], never_started) == (1, 1, 1)
        assert on_time + row["late"] + never_started == row["assigned"]

    def test_a_class_that_has_started_nothing_reads_as_not_started(
            self, client, db, seed, cohort, published):
        """The state a class is in the day after its first mock is set. Every
        segment of every bar has to be the not-started one — never blank, which
        would look like a page that failed to load."""
        from app.modules.analytics import projections

        assignment = _assignment(db, seed, cohort, published,
                                 "2026-07-06 09:00+05", "2026-07-30 09:00+05")
        for student in (seed["student"], _classmate(db, seed, cohort, "Bek")):
            _target(db, assignment, student)
        db.flush()
        projections.refresh_attendance(db, now=dt.datetime(2026, 7, 20, tzinfo=dt.UTC))
        db.flush()
        rows = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                              headers=auth(seed["author"].xid)))["rows"]
        assert len(rows) == 2
        for row in rows:
            assert (row["assigned"], row["started"], row["completed"], row["late"]) \
                == (1, 0, 0, 0)
            assert row["completion_rate"] == 0.0

    def test_the_servers_completion_rate_agrees_with_the_counts(
            self, client, seed, cohort, a_term):
        """The screen computes the percentage from the counts it prints, so both
        come from one source. This pins the server's own figure to the same
        answer, so a divergence shows up here rather than as two different
        numbers on one row."""
        row = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                             headers=auth(seed["author"].xid)))["rows"][0]
        assert round(row["completion_rate"] * 100) == round(
            row["completed"] / row["assigned"] * 100)


class TestAStudentWithNoWorkSetIsInvisible:
    def test_they_are_absent_from_the_grid_entirely(self, client, db, seed,
                                                     cohort, a_term):
        """`attendance_facts` has a row per (assignment, student), so a student
        nobody has set work to has no row and does not appear at all — not as
        zeroes. On a page shown to a parent, a missing child reads as a child who
        is not in the class."""
        _classmate(db, seed, cohort, "Nodir")
        headers = auth(seed["author"].xid)
        rows = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                              headers=headers))["rows"]
        assert len(rows) == 1
        assert rows[0]["user"]["given_name"] == "Aziza"

    def test_the_class_list_is_what_names_them(self, client, db, seed, cohort,
                                                a_term):
        """The extra call the Attendance screen makes for exactly this. It needs
        no permission the page did not already have — both are teacher-and-above
        at this centre."""
        _classmate(db, seed, cohort, "Nodir")
        headers = auth(seed["author"].xid)
        members = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/members",
                                 headers=headers))
        rows = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                              headers=headers))["rows"]
        listed = {row["user"]["xid"] for row in rows}
        missing = [m["user"]["given_name"] for m in members
                   if m["user"]["xid"] not in listed]
        assert missing == ["Nodir"]


class TestTheUserObjectIsNotTheDeclaredUser:
    def test_it_carries_four_fields_where_the_schema_promises_more(
            self, client, seed, cohort, a_term):
        """Both endpoints declare `User`, whose `phone` and `timezone` are
        required — and send neither. The generated types therefore type
        `row.user.phone` as `string` when it is `undefined` at runtime.

        Reading only the four that are really there is the whole of the fix on
        this side, and it is the right shape anyway: a class register does not
        need a phone number, and half the people on it are fifteen.
        """
        user = _ok(client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                              headers=auth(seed["author"].xid)))["rows"][0]["user"]
        assert set(user) == {"xid", "given_name", "family_name", "locale"}


class TestWhoMayOpenTheAttendanceScreen:
    def test_a_student_in_the_class_may_not(self, client, seed, cohort, a_term):
        """404 for the same reason as progress: this is every classmate's record,
        and a classmate is exactly who must not read it."""
        refused = client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 404, refused.text
        assert "Aziza" not in refused.text

    def test_a_rival_centre_may_not(self, client, seed, cohort, a_term, rival):
        """A competitor learning which of a centre's students are falling behind
        is commercial intelligence about that centre."""
        refused = client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                             headers=rival)
        assert refused.status_code == 404, refused.text
        assert "Aziza" not in refused.text

    def test_a_centre_admin_may(self, client, db, seed, cohort, a_term):
        from app.modules.identity.models import OrgMembership, User

        boss = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                    date_of_birth=dt.date(1990, 1, 1))
        db.add(boss)
        db.flush()
        db.add(OrgMembership(org_id=seed["org"].id, user_id=boss.id,
                             role="centre_admin", status="active"))
        db.flush()
        assert client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                          headers=auth(boss.xid)).status_code == 200

    def test_an_unknown_class_is_a_404_on_both(self, client, seed):
        headers = auth(seed["author"].xid)
        unknown = uuid.uuid4()
        assert client.get(f"/api/v1/cohorts/{unknown}/attendance",
                          headers=headers).status_code == 404
        assert client.get(f"/api/v1/cohorts/{unknown}/progress",
                          headers=headers).status_code == 404


@pytest.fixture
def rival(db, seed):
    """A centre_admin at a different organization."""
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
