"""The analytics read models: item analysis, exposure, cohort progress.

The last of `platform_ops`, and the endpoints a centre actually looks at. Two of
them decide things:

  * **`item-analysis` is how a broken answer key is found.** `common_wrong`
    surfaces what students typed and had marked wrong; spelling and number
    variants never reach it because the tolerance lexicon absorbs them, so what
    is left is a genuine missing alternative — which is the trigger for a
    regrade.
  * **`exposure` decides when an item is retired.** `burn_score` rises with
    circulation, and a question that has crossed too many centres is one whose
    answers are on Telegram.

`test_authz_leaks.py` already covers who may *reach* the cohort endpoints (a
teaching role at the centre, not mere membership). What was never executed is
what they return.
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


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


@pytest.fixture
def cohort(db, seed):
    """A cohort at the seeded centre, with the seeded student in it."""
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


class TestCohortProgress:
    def test_an_empty_cohort_reports_no_weeks_and_no_students(self, client, seed,
                                                              cohort):
        response = client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 200
        assert response.json() == {"cohort_xid": str(cohort["xid"]),
                                   "weeks": [], "students": []}

    def test_it_aggregates_by_week_and_by_student(self, client, db, seed, cohort,
                                                  published):
        """`mv_cohort_progress` is a materialized view over real attempts, so the
        rows are made by sitting the paper and refreshing it — which exercises
        the projection as well as the endpoint."""
        _scored(db, seed, published, band=5.5, when="2026-07-08")
        _scored(db, seed, published, band=6.5, when="2026-07-15")
        _refresh(db)
        body = client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                          headers=auth(seed["author"].xid)).json()
        assert len(body["weeks"]) == 2
        assert len(body["students"]) == 1

    def test_the_delta_is_first_to_latest(self, client, db, seed, cohort, published):
        """What a centre shows a parent: did this child improve."""
        _scored(db, seed, published, band=5.0, when="2026-07-08")
        _scored(db, seed, published, band=7.0, when="2026-07-15")
        _refresh(db)
        student = client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                             headers=auth(seed["author"].xid)).json()["students"][0]
        assert float(student["first_band"]) == 5.0
        assert float(student["latest_band"]) == 7.0
        assert student["delta"] == pytest.approx(2.0)

    def test_private_practice_is_excluded(self, client, db, seed, cohort, published):
        """"The centre sees the work it set, not what a student did at 1 a.m. on
        their own account." An attempt with no `org_context_id` is the student's
        own, and it must not appear in their school's dashboard."""
        _scored(db, seed, published, band=8.0, when="2026-07-08", org_context=False)
        _refresh(db)
        body = client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                          headers=auth(seed["author"].xid)).json()
        assert body["students"] == []

    def test_preview_attempts_are_excluded_too(self, client, db, seed, cohort,
                                               published):
        _scored(db, seed, published, band=8.0, when="2026-07-08", mode="preview")
        _refresh(db)
        assert client.get(f"/api/v1/cohorts/{cohort['xid']}/progress",
                          headers=auth(seed["author"].xid)).json()["students"] == []

    def test_an_unknown_cohort_is_a_404(self, client, seed):
        assert client.get(f"/api/v1/cohorts/{uuid.uuid4()}/progress",
                          headers=auth(seed["author"].xid)).status_code == 404


class TestCohortAttendance:
    def test_it_counts_assigned_started_completed_and_late(self, client, db, seed,
                                                           cohort):
        db.execute(text("""
            INSERT INTO attendance_facts (org_id, cohort_id, user_id, assignment_id,
                                          day, assigned, started, completed, late)
            VALUES (:o, :c, :u, 1, DATE '2026-07-08', true, true, true, false),
                   (:o, :c, :u, 2, DATE '2026-07-09', true, true, false, true),
                   (:o, :c, :u, 3, DATE '2026-07-10', true, false, false, false)
        """).bindparams(o=seed["org"].id, c=cohort["id"], u=seed["student"].id))
        db.flush()
        row = client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                         headers=auth(seed["author"].xid)).json()["rows"][0]
        assert (row["assigned"], row["started"], row["completed"], row["late"]) == \
               (3, 2, 1, 1)

    def test_an_empty_cohort_reports_no_rows(self, client, seed, cohort):
        response = client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                              headers=auth(seed["author"].xid))
        assert response.json()["rows"] == []

    def test_a_student_cannot_read_it(self, client, seed, cohort):
        """404 rather than 403: these return every classmate's attendance, and
        confirming the cohort exists is itself information."""
        assert client.get(f"/api/v1/cohorts/{cohort['xid']}/attendance",
                          headers=auth(seed["student"].xid)).status_code == 404


class TestItemAnalysis:
    def test_a_test_nobody_has_sat_reports_no_items(self, client, seed):
        response = client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid))
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_an_unknown_test_version_is_a_404(self, client, seed):
        assert client.get(f"/api/v1/test-versions/{uuid.uuid4()}/item-analysis",
                          headers=auth(seed["author"].xid)).status_code == 404

    def test_it_reports_p_value_and_the_wrong_answers_students_typed(
            self, client, db, seed, published):
        """`common_wrong` is the mechanism: three students all typing `bicycle`
        against a key that only accepts `bike` is a missing alternative, not
        three careless students."""
        _sit(db, seed, published, [("bike", "correct"), ("bicycle", "incorrect"),
                                   ("bicycle", "incorrect")])
        body = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()
        item = body["items"][0]
        assert item["n_responses"] == 3
        assert item["p_value"] == pytest.approx(1 / 3, abs=1e-4)
        assert item["common_wrong"] == [{"value": "bicycle", "count": 2}]

    def test_common_wrong_is_ordered_by_frequency_not_alphabet(
            self, client, db, seed, published):
        """It was `sorted(set(wrong))[:5]` over a DISTINCT array — the five
        alphabetically-first answers, each with a count of exactly 1. The answer
        an author opens this page to find could be absent because it starts with
        a late letter, and the counts said nothing at all."""
        _sit(db, seed, published,
             [("aardvark", "incorrect")] + [("zebra", "incorrect")] * 5)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["common_wrong"][0] == {"value": "zebra", "count": 5}

    def test_an_item_nobody_gets_right_is_flagged(self, client, db, seed, published):
        """`near_zero_p` is the automatic "your key is wrong" signal. Nobody
        answering correctly is almost never the students' fault."""
        _sit(db, seed, published, [("bicycle", "incorrect")] * 3)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["p_value"] == 0.0
        assert item["flagged"] is True
        assert "near_zero_p" in item["flag_reasons"]

    def test_an_item_everyone_gets_right_is_not_flagged(self, client, db, seed,
                                                        published):
        _sit(db, seed, published, [("bike", "correct")] * 3)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["p_value"] == 1.0
        assert item["flagged"] is False

    def test_preview_attempts_are_excluded(self, client, db, seed, published):
        """An author checking their own paper is not evidence about it. Counting
        previews would make a freshly written test look sat."""
        _sit(db, seed, published, [("bike", "correct")], mode="preview")
        assert client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"] == []


def _sit(db, seed, published, answers, *, mode="exam"):
    """Write score rows directly. The scoring engine has its own tests; what is
    under test here is the aggregation over what it produced."""
    qv = published["question_versions"][0]
    for n, (response, verdict) in enumerate(answers):
        user_id = db.scalar(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES (:p, 'Sitter', '2004-01-01', 'active') RETURNING id
        """).bindparams(p=f"+99890555{n:04d}"))
        attempt_id = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status, submitted_at)
            VALUES (:u, :tv, :m, 'scored', now()) RETURNING id
        """).bindparams(u=user_id, tv=published["test_version"].id, m=mode))
        run_id = db.scalar(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 1, 3, true) RETURNING id
        """).bindparams(a=attempt_id))
        db.execute(text("""
            INSERT INTO item_scores (score_run_id, question_id, question_version_id,
                                     answer_key_version_id, slot_key, awarded,
                                     max_points, verdict, raw_response)
            VALUES (:r, :q, :qv, NULL, 's1', :aw, 1, :v, :resp)
        """).bindparams(r=run_id, q=qv.question_id, qv=qv.id,
                        aw=1 if verdict == "correct" else 0, v=verdict, resp=response))
    db.flush()


class TestQuestionExposure:
    def test_an_unseen_question_reads_as_fresh(self, client, db, seed, published):
        qv = published["question_versions"][0]
        xid = _question_xid(db, qv)
        body = client.get(f"/api/v1/questions/{xid}/exposure",
                          headers=auth(seed["author"].xid)).json()
        assert body["times_sat"] == 0
        assert body["recommendation"] == "fresh"

    @pytest.mark.parametrize("burn,expected", [
        (0.1, "fresh"), (0.5, "watch"), (0.9, "retire"),
    ])
    def test_the_recommendation_follows_the_burn_score(self, client, db, seed,
                                                       published, burn, expected):
        """Items burn once they circulate. Past 0.7 the answers are on Telegram
        and the item is worth less than the effort of writing a new one."""
        qv = published["question_versions"][0]
        db.execute(text("""
            INSERT INTO item_exposure_stats (question_id, times_sat, distinct_users,
                                             distinct_orgs, burn_score)
            VALUES (:q, 400, 380, 12, :b)
        """).bindparams(q=qv.question_id, b=burn))
        db.flush()
        xid = _question_xid(db, qv)
        body = client.get(f"/api/v1/questions/{xid}/exposure",
                          headers=auth(seed["author"].xid)).json()
        assert body["burn_score"] == pytest.approx(burn)
        assert body["recommendation"] == expected

    def test_an_unknown_question_is_a_404(self, client, seed):
        assert client.get(f"/api/v1/questions/{uuid.uuid4()}/exposure",
                          headers=auth(seed["author"].xid)).status_code == 404

    def test_a_rival_centre_gets_a_404(self, client, db, seed, published):
        """Exposure is commercial intelligence: it says how heavily a competitor
        uses an item. `filter_content` is what keeps it in."""
        rival = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998906000001', 'Rival', '1985-01-01', 'active')
            RETURNING id, xid
        """)).mappings().one()
        org = db.scalar(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival', 'rival-x', 'active') RETURNING id
        """))
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, 'centre_admin', 'active')
        """).bindparams(o=org, u=rival["id"]))
        db.flush()
        qv = published["question_versions"][0]
        xid = _question_xid(db, qv)
        assert client.get(f"/api/v1/questions/{xid}/exposure",
                          headers=auth(rival["xid"])).status_code == 404


def _refresh(db) -> None:
    db.flush()
    db.execute(text("REFRESH MATERIALIZED VIEW mv_cohort_progress"))


def _scored(db, seed, published, *, band: float, when: str, mode: str = "exam",
            org_context: bool = True) -> None:
    """One scored attempt by the seeded student, dated so it lands in a
    particular week."""
    attempt_id = db.scalar(text("""
        INSERT INTO attempts (user_id, test_version_id, mode, status, submitted_at,
                              org_context_id)
        VALUES (:u, :tv, :m, 'scored', CAST(:w AS timestamptz), :o) RETURNING id
    """).bindparams(u=seed["student"].id, tv=published["test_version"].id, m=mode,
                    w=f"{when} 12:00+05", o=seed["org"].id if org_context else None))
    db.execute(text("""
        INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                raw_score, max_raw, band, per_section, is_current)
        VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 2, 3, :b,
                CAST(:ps AS jsonb), true)
    """).bindparams(a=attempt_id, b=band,
                    ps='{"reading": {"band": %s}}' % band))


def _question_xid(db, qv):
    return db.scalar(text("SELECT xid FROM questions WHERE id = :q")
                     .bindparams(q=qv.question_id))
