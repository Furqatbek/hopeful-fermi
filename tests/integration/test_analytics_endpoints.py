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

import json
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
    """The endpoint an author opens when a paper looks wrong.

    It used to be a SECOND implementation of `analytics.stats`, aggregating
    `item_scores` in SQL and pinning `discrimination`, `mean_time_ms` and
    `option_distribution` to constants — while `stats.analyse()` computed all of
    them, correctly, for the projection `flagged-items` reads. The endpoint an
    author actually opens was the poorer of the two.
    """

    def _items(self, client, seed, published, *, who=None, scope=None):
        query = f"?org_scope={scope}" if scope else ""
        body = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            f"/item-analysis{query}",
            headers=auth(who or seed["author"].xid))
        assert body.status_code == 200, body.text
        return body.json()

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
        item = self._items(client, seed, published)["items"][0]
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
        item = self._items(client, seed, published)["items"][0]
        assert item["common_wrong"][0] == {"value": "zebra", "count": 5}

    def test_preview_attempts_are_excluded(self, client, db, seed, published):
        """An author checking their own paper is not evidence about it. Counting
        previews would make a freshly written test look sat."""
        _sit(db, seed, published, [("bike", "correct")], mode="preview")
        assert self._items(client, seed, published)["items"] == []

    def test_n_attempts_counts_attempts(self, client, db, seed, published):
        """It was `len(rows)` over a query grouped by QUESTION, so a 40-question
        paper sat by three students reported `n_attempts: 40`."""
        _sit(db, seed, published, [("bike", "correct")] * 3)
        _sit(db, seed, published, [("bike", "correct")] * 3, item=1)
        body = self._items(client, seed, published)
        assert len(body["items"]) == 2
        assert body["n_attempts"] == 6


class TestFlaggingOnNoise:
    """`MIN_RESPONSES = 20`: "below this many responses the statistics are noise,
    and flagging on noise trains authors to ignore the flags."

    The endpoint had its own threshold — `p_value < 0.05`, no minimum — so one
    student answering one item wrong raised the alarm on it. The suite asserted
    that behaviour, which is how it survived.
    """

    def test_three_students_getting_it_wrong_is_not_yet_a_flag(
            self, client, db, seed, published):
        _sit(db, seed, published, [("bicycle", "incorrect")] * 3)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["p_value"] == 0.0
        assert item["flagged"] is False, "three responses is noise, not evidence"

    def test_twenty_students_getting_it_wrong_is(self, client, db, seed, published):
        """`near_zero_p` is the automatic "your key is wrong" signal. Nobody
        answering correctly is almost never the students' fault."""
        _sit(db, seed, published, [("bicycle", "incorrect")] * 20)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["flagged"] is True
        assert "near_zero_p" in item["flag_reasons"]

    def test_an_item_everyone_gets_right_is_flagged_as_too_easy(
            self, client, db, seed, published):
        """`near_one_p` — an item that discriminates nothing is wasting a slot on
        the paper. The endpoint could not emit this reason at all."""
        _sit(db, seed, published, [("bike", "correct")] * 20)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["p_value"] == 1.0
        assert "near_one_p" in item["flag_reasons"]

    def test_a_common_wrong_answer_is_a_flag_reason(self, client, db, seed,
                                                    published):
        """The reason that names a missing key alternative directly, and the one
        this endpoint most needed. Ten of twenty-five students typing the same
        thing is not twenty-five careless students."""
        _sit(db, seed, published,
             [("bike", "correct")] * 15 + [("bicycle", "incorrect")] * 10)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert "common_wrong_answer" in item["flag_reasons"]


class TestDiscrimination:
    """"The single most useful automated signal we have for finding the mistake
    that loses a school client" — and the endpoint returned `None` for it.

    `raw_score` is each student's total on the paper, which is what the
    point-biserial correlates against.
    """

    def test_a_bad_key_shows_as_negative_discrimination(self, client, db, seed,
                                                        published):
        """The strong students get it WRONG. That is what a wrong key looks like
        from the outside: the people who knew the material wrote the answer the
        key does not accept."""
        # Strong students (total 3) answer "bicycle" and are marked wrong; weak
        # students (total 0) guess "bike" and are marked right.
        _sit(db, seed, published, [("bicycle", "incorrect")] * 12,
             raw_score=lambda n, v: 3)
        _sit(db, seed, published, [("bike", "correct")] * 12,
             raw_score=lambda n, v: 0)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["discrimination"] is not None
        assert item["discrimination"] < 0
        assert "negative_discrimination" in item["flag_reasons"]

    def test_a_sound_item_discriminates_positively(self, client, db, seed,
                                                   published):
        _sit(db, seed, published, [("bike", "correct")] * 12,
             raw_score=lambda n, v: 3)
        _sit(db, seed, published, [("bicycle", "incorrect")] * 12,
             raw_score=lambda n, v: 0)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["discrimination"] > 0
        assert "negative_discrimination" not in item["flag_reasons"]

    def test_no_variance_in_total_score_reports_null_not_zero(
            self, client, db, seed, published):
        """"A confident 0.0 and 'cannot be computed' are different answers and an
        author acts on them differently." Everyone scoring the same means the
        correlation is undefined, not absent."""
        _sit(db, seed, published,
             [("bike", "correct")] * 3 + [("bicycle", "incorrect")] * 3,
             raw_score=lambda n, v: 2)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["discrimination"] is None


class TestMeanTime:
    def test_it_averages_the_time_students_spent(self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")] * 2, time_ms=30_000)
        _sit(db, seed, published, [("bike", "correct")] * 2, time_ms=10_000)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["mean_time_ms"] == 20_000

    def test_no_answer_rows_at_all_is_null(self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")] * 3)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["mean_time_ms"] is None

    def test_a_reported_zero_is_null_not_zero(self, client, db, seed, published):
        """`attempt_answers.time_spent_ms` is NOT NULL DEFAULT 0 and CLIENT-
        reported, so a client that sends no timing writes rows full of zeroes.
        Averaging those reports every item as answered instantly — a confident
        number that is simply false, about a field the client controls.

        The distinction this test exists for: the row IS there, and its value is
        0. With no row at all the sum is already NULL and the guard is a no-op,
        which is how a version of this test that omitted the answers entirely
        passed against an implementation that had no guard.
        """
        _sit(db, seed, published, [("bike", "correct")] * 3, time_ms=0)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["mean_time_ms"] is None

    def test_one_student_reporting_nothing_does_not_drag_the_mean_to_zero(
            self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")], time_ms=40_000)
        _sit(db, seed, published, [("bike", "correct")], time_ms=0)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["mean_time_ms"] == 40_000


class TestOneResponsePerStudentNotPerBlank:
    """A three-blank sentence completion is ONE item a student either got right
    or did not.

    `item_scores` holds a row per SLOT, so grouping by slot counts one student as
    three — which makes `n_responses` wrong, and every p-value with it.
    """

    def test_a_multi_slot_item_counts_students(self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")] * 4, slots=("s1", "s2", "s3"))
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["n_responses"] == 4, "four students, not twelve blanks"

    def test_one_blank_wrong_makes_the_item_wrong(self, client, db, seed, published):
        """`bool_and`. Partial credit is the scoring engine's business; item
        difficulty is "did they get it", and an item two-thirds right is not a
        two-thirds student."""
        _sit(db, seed, published, [("bike", "correct")] * 3, slots=("s1", "s2"),
             wrong_slot="s2")
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["n_responses"] == 3
        assert item["p_value"] == 0.0


class TestOptionDistribution:
    def test_it_reports_what_students_actually_chose(self, client, db, seed,
                                                     published):
        """Pinned to `{}`. It is the distractor analysis — which wrong option
        pulled the most students — and it covers RIGHT answers too, which
        `common_wrong` by construction cannot."""
        _sit(db, seed, published,
             [("bike", "correct")] * 3 + [("bicycle", "incorrect")] * 2)
        item = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"][0]
        assert item["option_distribution"] == {"bike": 3, "bicycle": 2}


class TestItemNumbering:
    def test_items_carry_their_number_on_the_paper(self, client, db, seed,
                                                   published):
        """It was `enumerate()` over an unordered `GROUP BY` — neither the paper's
        numbering nor stable between two calls. "Question 7 is flagged" has to
        mean question 7."""
        _sit(db, seed, published, [("bike", "correct")], item=2)
        _sit(db, seed, published, [("bike", "correct")], item=0)
        items = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"]
        assert [i["number"] for i in items] == [1, 3]

    def test_the_numbers_match_the_published_snapshot(self, client, db, seed,
                                                      published):
        """The same rule `build_snapshot` numbers with, which is the numbering the
        student saw. Two sources for one sequence is how they drift."""
        for i in range(3):
            _sit(db, seed, published, [("bike", "correct")], item=i)
        items = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()["items"]
        snapshot = db.scalar(text("SELECT snapshot FROM test_versions WHERE id = :v")
                             .bindparams(v=published["test_version"].id))
        expected = {q["question_version_xid"]: q["number"]
                    for s in snapshot["sections"] for g in s["groups"]
                    for q in g["questions"]}
        assert sorted(i["number"] for i in items) == sorted(expected.values())


class TestOrgScope:
    """`org_scope` was a documented query parameter the handler accepted and never
    read, so `global` and `mine` returned the same thing."""

    def test_mine_counts_only_this_centres_sittings(self, client, db, seed,
                                                    published):
        _sit(db, seed, published, [("bike", "correct")] * 2)
        _sit(db, seed, published, [("bicycle", "incorrect")] * 4, org=None)
        body = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            "/item-analysis?org_scope=mine",
            headers=auth(seed["author"].xid)).json()
        assert body["items"][0]["n_responses"] == 2
        assert body["items"][0]["p_value"] == 1.0

    def test_global_counts_every_sitting(self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")] * 2)
        _sit(db, seed, published, [("bicycle", "incorrect")] * 4, org=None)
        body = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            "/item-analysis?org_scope=global",
            headers=auth(seed["author"].xid)).json()
        assert body["items"][0]["n_responses"] == 6
        assert body["items"][0]["p_value"] == pytest.approx(1 / 3, abs=1e-4)

    def test_mine_is_the_default(self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")] * 2)
        _sit(db, seed, published, [("bicycle", "incorrect")] * 4, org=None)
        body = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).json()
        assert body["items"][0]["n_responses"] == 2


class TestWhoMayRead:
    """It resolved a bare `WHERE xid = :xid` with no scope and no policy call."""

    def test_a_student_at_the_centre_cannot(self, client, db, seed, published):
        """A p-value is a map of which questions to spend time on, and
        `common_wrong` is a list of what other students typed."""
        _sit(db, seed, published, [("bike", "correct")])
        refused = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["student"].xid))
        assert refused.status_code == 403, refused.text
        assert "bike" not in refused.text

    def test_a_competitor_centre_gets_a_404(self, client, db, seed, published,
                                            rival):
        """"A centre's material must never leak to competitor centres." 404, not
        403: whether a rival's test exists is theirs to know."""
        _sit(db, seed, published, [("bike", "correct")])
        refused = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=rival)
        assert refused.status_code == 404, refused.text
        assert "bike" not in refused.text

    def test_the_author_still_reads_it(self, client, db, seed, published):
        _sit(db, seed, published, [("bike", "correct")])
        assert client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/item-analysis",
            headers=auth(seed["author"].xid)).status_code == 200


class TestFlaggedItems:
    """The cross-test dashboard. Three of its columns were constants.

    `suggested_action` was the literal `"review_key"` on every row — so the column
    that tells an author what to DO said the same thing about an item nobody could
    answer as about one everybody could. `test_title` was `""` and `number` was
    `0`, so the row said an item was broken and not where to find it.
    """

    def _flag(self, db, published, *, reasons, item=0, **cols):
        qv = published["question_versions"][item]
        db.execute(text("""
            INSERT INTO item_stats (question_id, question_version_id, org_id,
                                    window_start, window_end, n_responses, n_correct,
                                    p_value, discrimination, mean_time_ms,
                                    option_distribution, common_wrong, flagged,
                                    flag_reasons)
            VALUES (:q, :qv, NULL, DATE '2026-05-01', DATE '2026-07-30', 30, :nc,
                    :p, :d, :t, CAST(:opts AS jsonb), '[]'::jsonb, true,
                    CAST(:reasons AS text[]))
        """).bindparams(q=qv.question_id, qv=qv.id, nc=cols.get("n_correct", 0),
                        p=cols.get("p_value", 0.0), d=cols.get("discrimination"),
                        t=cols.get("mean_time_ms"),
                        opts=json.dumps(cols.get("option_distribution", {})),
                        reasons=reasons))
        db.flush()

    def _rows(self, client, seed):
        response = client.get("/api/v1/content/flagged-items",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 200, response.text
        return response.json()

    @pytest.mark.parametrize("reasons,action", [
        (["negative_discrimination"], "review_key"),
        (["common_wrong_answer"], "review_key"),
        (["near_zero_p"], "review_item"),
        (["near_one_p"], "retire_too_easy"),
        # Ordered by how strongly the reason implicates the KEY: a negative
        # point-biserial outranks "hard", and both outrank "too easy".
        (["near_one_p", "negative_discrimination"], "review_key"),
    ])
    def test_the_action_follows_the_reason(self, client, db, seed, published,
                                           reasons, action):
        self._flag(db, published, reasons=reasons)
        assert self._rows(client, seed)[0]["suggested_action"] == action

    def test_it_names_the_test_and_the_question_number(self, client, db, seed,
                                                       published):
        """"Mock 1, question 2" is a place an author can go. `""` and `0` are not."""
        self._flag(db, published, reasons=["near_zero_p"], item=1)
        row = self._rows(client, seed)[0]
        assert row["test_title"] == "Mock 1"
        assert row["number"] == 2

    def test_the_statistics_come_through(self, client, db, seed, published):
        self._flag(db, published, reasons=["negative_discrimination"],
                   discrimination=-0.42, mean_time_ms=45_000,
                   option_distribution={"bike": 12, "bicycle": 18})
        row = self._rows(client, seed)[0]
        assert row["discrimination"] == pytest.approx(-0.42)
        assert row["mean_time_ms"] == 45_000
        assert row["option_distribution"] == {"bike": 12, "bicycle": 18}

    def test_an_item_in_no_test_still_lists(self, client, db, seed, published):
        """The join is LEFT for a reason: a question written and flagged from
        practice use, never placed on a paper, must not vanish from the dashboard
        that exists to find broken questions."""
        db.execute(text("DELETE FROM test_version_groups"))
        db.flush()
        self._flag(db, published, reasons=["near_zero_p"])
        row = self._rows(client, seed)[0]
        assert row["test_title"] == ""
        assert row["number"] == 0


@pytest.fixture
def rival(db, seed):
    """A centre_admin at a different organization."""
    import datetime as dt

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


def _sit(db, seed, published, answers, *, mode="exam", org=..., raw_score=None,
         time_ms=None, item=0, slots=("s1",), wrong_slot=None):
    """Write score rows directly. The scoring engine has its own tests; what is
    under test here is the aggregation over what it produced.

    `org_context_id` is the seeded centre by default, because that is what a real
    sitting looks like: `exam.py` sets it from the assignment and `competitions.py`
    from the contest. An attempt with no org context is self-serve practice, which
    is a different question and now has `org=None` to say so.
    """
    qv = published["question_versions"][item]
    org_id = seed["org"].id if org is ... else org
    for n, (response, verdict) in enumerate(answers):
        user_id = db.scalar(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES (:p, 'Sitter', '2004-01-01', 'active') RETURNING id
        """).bindparams(p=f"+9989{uuid.uuid4().int % 10**8:08d}"))
        # In progress first: `attempt_answers_frozen` refuses a write once the
        # attempt is scored, which is the whole point of it.
        attempt_id = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status,
                                  org_context_id)
            VALUES (:u, :tv, :m, 'in_progress', :org) RETURNING id
        """).bindparams(u=user_id, tv=published["test_version"].id, m=mode,
                        org=org_id))
        if time_ms is not None:
            db.execute(text("""
                INSERT INTO attempt_answers (attempt_id, question_version_id,
                                             slot_key, response, time_spent_ms)
                VALUES (:a, :qv, 's1', '{}'::jsonb, :t)
            """).bindparams(a=attempt_id, qv=qv.id, t=time_ms))
        db.execute(text("UPDATE attempts SET status = 'scored', submitted_at = now() "
                        "WHERE id = :a").bindparams(a=attempt_id))
        run_id = db.scalar(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, :raw, 3, true) RETURNING id
        """).bindparams(a=attempt_id,
                        raw=1 if raw_score is None else raw_score(n, verdict)))
        for slot in slots:
            # `item_scores` holds a row per BLANK. `wrong_slot` marks one of them
            # wrong so a multi-blank item can be scored the way a real one is:
            # mostly right, and still not a mark.
            slot_verdict = "incorrect" if slot == wrong_slot else verdict
            db.execute(text("""
                INSERT INTO item_scores (score_run_id, question_id,
                                         question_version_id, answer_key_version_id,
                                         slot_key, awarded, max_points, verdict,
                                         raw_response)
                VALUES (:r, :q, :qv, NULL, :slot, :aw, 1, :v, :resp)
            """).bindparams(r=run_id, q=qv.question_id, qv=qv.id, slot=slot,
                            aw=1 if slot_verdict == "correct" else 0,
                            v=slot_verdict, resp=response))
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
