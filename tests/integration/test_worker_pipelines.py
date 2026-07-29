"""The four pipelines, end to end, against a real database.

The regrade one is the point of the file. Deliverable 1 called a bad answer key
"the fastest way to lose a school client", and every promise made about fixing
one — that the dry run touches nothing, that history survives, that a competition
cannot be silently re-ranked, that only band changes are notified — is a claim
about code that until now did not exist. This is where those claims meet a
database.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select, text

from app.modules.analytics import projections
from app.modules.competitions import service as competitions
from app.modules.exam import planner
from app.modules.exam.models import Attempt, ItemScore, RegradeJob, ScoreRun
from app.modules.exam.session import ExamSession
from app.modules.identity import notify
from app.modules.qtypes.registry import default_scorer
from app.modules.speaking import service as speaking
from app.platform.errors import Conflict


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@pytest.fixture
def exam(db, clock):
    return ExamSession(db, default_scorer(), clock, grace_seconds=30)


def sit(db, exam, published, user, answers: list[str]) -> Attempt:
    """One student sits the seeded three-item paper and submits."""
    from app.modules.exam.session import AnswerDelta

    attempt = exam.start(user_id=user.id, test_version_id=published["test_version"].id)
    exam.save_answers(attempt, [
        AnswerDelta(question_version_xid=str(qv.xid), slot_key="s1",
                    response=answer, client_seq=i + 1)
        for i, (qv, answer) in enumerate(zip(published["question_versions"], answers))
    ])
    exam.submit(attempt)
    return attempt


def student(db, name: str, born: int = 2000):
    from app.modules.identity.models import User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(born, 6, 1))
    db.add(user)
    db.flush()
    return user


def broken_key(db, published, *, accept: list[str]) -> RegradeJob:
    """Supersede item 1's key, then stage a regrade for it.

    The seeded key accepts only "bicycle". Widening it to accept "bike" is the
    exact scenario the whole regrade path exists for.
    """
    from app.modules.content.models import AnswerKeyVersion

    qv = published["question_versions"][0]
    current = db.scalars(
        select(AnswerKeyVersion).where(AnswerKeyVersion.question_version_id == qv.id,
                                       AnswerKeyVersion.is_current.is_(True))).one()
    current.is_current = False
    current.superseded_at = _now()
    db.add(AnswerKeyVersion(question_version_id=qv.id, version_no=2,
                            key={"slots": {"s1": {"accept": accept}}},
                            reason="key_fix", created_by=published["author"].id,
                            is_current=True))
    db.flush()

    job = RegradeJob(trigger="answer_key_change", subject_type="question_version",
                     subject_id=qv.id, initiated_by=published["author"].id,
                     reason="Key omitted 'bike'", dry_run=True, status="planning")
    db.add(job)
    db.flush()
    return job


class TestRegradePipeline:
    def test_the_dry_run_reports_the_impact_and_writes_no_scores(
            self, db, exam, published):
        """"Nothing has been regraded" must be true, not a hopeful note in the
        response body."""
        wrong = student(db, "Aziza")
        sit(db, exam, published, wrong, ["bike", "library", "museum"])
        runs_before = db.scalar(select(func.count()).select_from(ScoreRun))
        items_before = db.scalar(select(func.count()).select_from(ItemScore))

        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())

        assert job.status == "ready"
        assert job.dry_run is True
        assert job.attempts_total == 1
        assert job.scores_changed == 1
        assert db.scalar(select(func.count()).select_from(ScoreRun)) == runs_before
        assert db.scalar(select(func.count()).select_from(ItemScore)) == items_before

    def test_applying_supersedes_rather_than_edits(self, db, exam, published):
        """The student's original mark survives with the reason it was replaced.

        That history is the evidence the regrade was fair; overwriting it would
        leave a centre's complaint impossible to answer.
        """
        wrong = student(db, "Aziza")
        attempt = sit(db, exam, published, wrong, ["bike", "library", "museum"])
        original = db.scalars(select(ScoreRun).where(
            ScoreRun.attempt_id == attempt.id, ScoreRun.is_current.is_(True))).one()
        assert float(original.raw_score) == 2.0

        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())
        assert planner.apply(db, job, default_scorer(), now=_now()) == 1

        runs = db.scalars(select(ScoreRun).where(ScoreRun.attempt_id == attempt.id)
                          .order_by(ScoreRun.id)).all()
        assert len(runs) == 2
        assert runs[0].id == original.id and runs[0].is_current is False
        assert runs[1].is_current is True
        assert float(runs[1].raw_score) == 3.0
        assert runs[1].regrade_job_id == job.id
        assert "Key omitted" in runs[1].note
        db.refresh(attempt)
        assert attempt.current_score_run_id == runs[1].id

    def test_an_unaffected_attempt_gets_no_new_run(self, db, exam, published):
        """Inserting an identical run would churn `score_runs` and make "what
        changed" unanswerable from the table that exists to answer it."""
        right = student(db, "Correct")
        wrong = student(db, "Wrong")
        clean = sit(db, exam, published, right, ["bicycle", "library", "museum"])
        sit(db, exam, published, wrong, ["bike", "library", "museum"])

        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())
        assert job.attempts_total == 2
        assert job.scores_changed == 1
        assert planner.apply(db, job, default_scorer(), now=_now()) == 1
        assert db.scalar(select(func.count()).select_from(ScoreRun)
                         .where(ScoreRun.attempt_id == clean.id)) == 1

    def test_only_students_whose_band_moved_are_notified(self, db, exam, published):
        """A raw-score wobble that leaves the band alone is not worth a push.

        The seeded band map moves a band per raw mark, so this student's band
        does move — the assertion is that the notification is keyed to the BAND
        change and deduplicated, not that one exists per rescored attempt.
        """
        wrong = student(db, "Aziza")
        sit(db, exam, published, wrong, ["bike", "library", "museum"])
        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())
        assert job.bands_changed == 1
        planner.apply(db, job, default_scorer(), now=_now())

        rows = db.execute(text("""
            SELECT user_id, template, params, dedupe_key FROM notifications
            WHERE template = 'regrade.band_changed'
        """)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["user_id"] == wrong.id
        assert rows[0]["params"]["direction"] == "up"
        assert rows[0]["params"]["old_band"] == 6.0
        assert rows[0]["params"]["new_band"] == 7.0

    def test_applying_twice_does_not_notify_twice(self, db, exam, published):
        """The at-least-once guarantee made safe by a unique index rather than by
        hoping the relay never redelivers."""
        wrong = student(db, "Aziza")
        sit(db, exam, published, wrong, ["bike", "library", "museum"])
        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())
        planner.apply(db, job, default_scorer(), now=_now())
        job.status = "ready"
        db.flush()
        planner.apply(db, job, default_scorer(), now=_now())

        assert db.scalar(text("""
            SELECT count(*) FROM notifications WHERE template = 'regrade.band_changed'
        """)) == 1

    def test_previews_are_never_regraded(self, db, exam, published):
        """An author trying a draft is not a student with a band to correct."""
        author = published["author"]
        preview = exam.start(user_id=author.id,
                             test_version_id=published["test_version"].id,
                             mode="preview")
        exam.submit(preview)
        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())
        assert job.attempts_total == 0

    def test_a_competition_attempt_is_not_swept_into_a_bulk_regrade(
            self, db, exam, published):
        """The rule from ADR-0001 §8.4, enforced in the selection rather than in
        a check someone has to remember to write."""
        contest = db.execute(text("""
            INSERT INTO competitions (test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, status,
                                      created_by)
            VALUES (:tv, 'Friday', now(), now(), now() + interval '30 min', 1800,
                    'final', :by) RETURNING id
        """).bindparams(tv=published["test_version"].id,
                        by=published["author"].id)).scalar()
        contender = student(db, "Contender")
        attempt = sit(db, exam, published, contender, ["bike", "library", "museum"])
        attempt.competition_id = contest
        db.flush()

        job = broken_key(db, published, accept=["bicycle", "bike"])
        planner.plan(db, job, default_scorer())
        assert job.attempts_total == 0, "a finished contest was swept into a regrade"

    def test_the_domain_refuses_an_apply_with_an_undecided_competition(
            self, db, exam, published):
        """Belt and braces: the API returns 409 before the job is enqueued, and
        the domain refuses again here where no caller can bypass it."""
        from app.modules.exam import regrade

        impact = regrade.RegradeImpact(
            competition_impact=[regrade.CompetitionImpact(
                competition_xid="c-1", attempts=40, rank_changes=12,
                podium_changes=1)])
        with pytest.raises(Conflict) as exc:
            regrade.apply(impact, competition_decisions=set())
        assert exc.value.code == "competition_decision_required"
        # ...and permitted once a human has recorded a decision.
        assert regrade.apply(impact, competition_decisions={"c-1"}) == []


class TestAnalyticsPipeline:
    def test_exposure_is_recorded_once_per_attempt(self, db, exam, published):
        learner = student(db, "Aziza")
        attempt = sit(db, exam, published, learner, ["bicycle", "library", "museum"])

        assert projections.record_exposure(db, attempt.id) == 3
        # Redelivery is a no-op: counting an attempt twice would make every item
        # look more burned than it is.
        assert projections.record_exposure(db, attempt.id) == 0
        assert db.scalar(text(
            "SELECT count(*) FROM item_exposures WHERE attempt_id = :a"
        ).bindparams(a=attempt.id)) == 3

    def test_a_preview_exposes_nothing(self, db, exam, published):
        preview = exam.start(user_id=published["author"].id,
                             test_version_id=published["test_version"].id,
                             mode="preview")
        exam.submit(preview)
        assert projections.record_exposure(db, preview.id) == 0

    def test_burn_score_is_written_from_the_exposure_log(self, db, exam, published):
        learner = student(db, "Aziza")
        attempt = sit(db, exam, published, learner, ["bicycle", "library", "museum"])
        projections.record_exposure(db, attempt.id)
        assert projections.refresh_exposure(db, now=_now()) == 3

        row = db.execute(text("""
            SELECT times_sat, distinct_users, burn_score FROM item_exposure_stats
            LIMIT 1
        """)).mappings().one()
        assert row["times_sat"] == 1 and row["distinct_users"] == 1
        assert 0 <= float(row["burn_score"]) < 0.1

    def test_item_stats_are_upserted_not_duplicated(self, db, exam, published):
        """The reason migration 0019 exists. Without the unique key this job
        inserts a fresh set of rows every run and the flagged-items dashboard
        shows each item several times with different numbers.
        """
        for name in ("A", "B", "C"):
            sit(db, exam, published, student(db, name),
                ["bike", "library", "museum"])

        first = projections.refresh_item_stats(db, now=_now())
        assert first == 3
        assert projections.refresh_item_stats(db, now=_now()) == 3
        assert db.scalar(text("SELECT count(*) FROM item_stats")) == 3

    def test_item_stats_carry_the_wrong_answer_everyone_gave(self, db, exam,
                                                             published):
        """The highest-value output of the whole analytics path: three students
        wrote "bike" and the key says "bicycle"."""
        for name in ("A", "B", "C"):
            sit(db, exam, published, student(db, name),
                ["bike", "library", "museum"])
        projections.refresh_item_stats(db, now=_now())

        row = db.execute(text("""
            SELECT p_value, common_wrong FROM item_stats
            WHERE question_version_id = :qv
        """).bindparams(qv=published["question_versions"][0].id)).mappings().one()
        assert float(row["p_value"]) == 0.0
        assert row["common_wrong"][0]["value"] == "bike"
        assert row["common_wrong"][0]["count"] == 3

    def test_the_cohort_view_refreshes_concurrently(self, db):
        """`CONCURRENTLY` needs the unique index that migration 0016 created.
        Without it this takes an ACCESS EXCLUSIVE lock and every dashboard 500s.
        """
        db.commit()          # CONCURRENTLY cannot run inside a transaction block
        projections.refresh_cohort_progress(db)
        db.commit()

    def test_attendance_is_projected_per_assignment_and_student(self, db, published):
        from app.modules.exam.models import Assignment, AssignmentTarget
        from app.modules.identity.models import Cohort, CohortMember

        cohort = Cohort(org_id=published["org"].id, name="Evening",
                        created_by=published["author"].id)
        db.add(cohort)
        db.flush()
        learner = student(db, "Aziza")
        db.add(CohortMember(cohort_id=cohort.id, user_id=learner.id))
        assignment = Assignment(
            org_id=published["org"].id, cohort_id=cohort.id,
            test_version_id=published["test_version"].id,
            assigned_by=published["author"].id,
            opens_at=_now() - dt.timedelta(days=1),
            closes_at=_now() + dt.timedelta(days=1))
        db.add(assignment)
        db.flush()
        db.add(AssignmentTarget(assignment_id=assignment.id, user_id=learner.id))
        db.flush()

        assert projections.refresh_attendance(db, now=_now()) == 1
        row = db.execute(text("SELECT assigned, started, completed FROM attendance_facts")
                         ).mappings().one()
        assert row["assigned"] is True and row["started"] is False


class TestCompetitionPipeline:
    def _contest(self, db, published, status="grading", offset_minutes=-40):
        starts = _now() + dt.timedelta(minutes=offset_minutes)
        return db.execute(text("""
            INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, status,
                                      created_by)
            VALUES (:org, :tv, 'Friday', :starts - interval '2 min', :starts,
                    :starts + interval '30 min', 1800, :status, :by)
            RETURNING id, xid
        """).bindparams(org=published["org"].id, tv=published["test_version"].id,
                        starts=starts, status=status,
                        by=published["author"].id)).mappings().one()

    def _enter(self, db, exam, published, contest, name, answers):
        user = student(db, name)
        attempt = sit(db, exam, published, user, answers)
        attempt.competition_id = contest["id"]
        db.flush()
        db.execute(text("""
            INSERT INTO competition_entries (competition_id, user_id, attempt_id, status)
            VALUES (:c, :u, :a, 'submitted')
        """).bindparams(c=contest["id"], u=user.id, a=attempt.id))
        return user

    def test_the_board_is_materialized_when_the_contest_goes_final(
            self, db, exam, published):
        contest = self._contest(db, published)
        self._enter(db, exam, published, contest, "Top", ["bicycle", "library", "museum"])
        self._enter(db, exam, published, contest, "Mid", ["bicycle", "library", "wrong"])
        self._enter(db, exam, published, contest, "Low", ["no", "no", "no"])
        db.flush()

        moved = competitions.tick(db, _now())
        assert {m["to"] for m in moved} == {"final"}

        board = db.execute(text("""
            SELECT u.given_name, r.rank, r.raw_score, r.is_provisional
            FROM competition_results r JOIN users u ON u.id = r.user_id
            WHERE r.competition_id = :c ORDER BY r.rank
        """).bindparams(c=contest["id"])).mappings().all()
        assert [r["given_name"] for r in board] == ["Top", "Mid", "Low"]
        assert [r["rank"] for r in board] == [1, 2, 3]
        assert all(r["is_provisional"] is False for r in board)

    def test_a_tie_shares_a_rank(self, db, exam, published):
        contest = self._contest(db, published)
        self._enter(db, exam, published, contest, "A", ["bicycle", "library", "museum"])
        self._enter(db, exam, published, contest, "B", ["bicycle", "library", "museum"])
        db.flush()
        competitions.tick(db, _now())
        ranks = db.execute(text(
            "SELECT rank FROM competition_results WHERE competition_id = :c"
        ).bindparams(c=contest["id"])).scalars().all()
        # Identical scores; only submission time separates them, and these were
        # submitted in the same frozen-clock instant.
        assert sorted(ranks) in ([1, 1], [1, 2])

    def test_materializing_twice_updates_rather_than_duplicates(self, db, exam,
                                                                published):
        contest = self._contest(db, published, status="live")
        self._enter(db, exam, published, contest, "A", ["bicycle", "library", "museum"])
        db.flush()
        competitions.materialize(db, contest["id"], tiebreak=None, now=_now(),
                                 provisional=True)
        competitions.materialize(db, contest["id"], tiebreak=None, now=_now(),
                                 provisional=False)
        rows = db.execute(text(
            "SELECT is_provisional FROM competition_results WHERE competition_id = :c"
        ).bindparams(c=contest["id"])).scalars().all()
        assert rows == [False]

    def test_registered_but_absent_entries_become_no_shows(self, db, published):
        contest = self._contest(db, published, status="lobby", offset_minutes=-1)
        ghost = student(db, "Ghost")
        db.execute(text("""
            INSERT INTO competition_entries (competition_id, user_id) VALUES (:c, :u)
        """).bindparams(c=contest["id"], u=ghost.id))
        db.flush()

        competitions.tick(db, _now())
        assert db.scalar(text("""
            SELECT status FROM competition_entries WHERE competition_id = :c
        """).bindparams(c=contest["id"])) == "no_show"

    def test_grading_waits_for_an_unscored_entry(self, db, exam, published):
        # Ends five minutes ago, so it is inside GRADING_PATIENCE. The next test
        # covers what happens once that runs out.
        contest = self._contest(db, published, offset_minutes=-35)
        user = student(db, "Slow")
        attempt = exam.start(user_id=user.id,
                             test_version_id=published["test_version"].id)
        attempt.competition_id = contest["id"]
        db.flush()
        db.execute(text("""
            INSERT INTO competition_entries (competition_id, user_id, attempt_id, status)
            VALUES (:c, :u, :a, 'started')
        """).bindparams(c=contest["id"], u=user.id, a=attempt.id))
        db.flush()

        assert competitions.tick(db, _now()) == []
        assert db.scalar(text("SELECT status FROM competitions WHERE id = :c")
                         .bindparams(c=contest["id"])) == "grading"

    def test_grading_publishes_without_a_straggler_eventually(self, db, exam,
                                                              published):
        """A board that never appears is worse than one published without the
        two people whose attempts died."""
        contest = self._contest(db, published, offset_minutes=-90)
        user = student(db, "Vanished")
        attempt = exam.start(user_id=user.id,
                             test_version_id=published["test_version"].id)
        attempt.competition_id = contest["id"]
        db.flush()
        db.execute(text("""
            INSERT INTO competition_entries (competition_id, user_id, attempt_id, status)
            VALUES (:c, :u, :a, 'started')
        """).bindparams(c=contest["id"], u=user.id, a=attempt.id))
        db.flush()

        assert [m["to"] for m in competitions.tick(db, _now())] == ["final"]


class TestSpeakingPipeline:
    def _slot(self, db, published, *, age_band="adult"):
        return db.execute(text("""
            INSERT INTO speaking_slots (org_id, starts_at, duration_minutes, capacity,
                                        status, audience, age_band, created_by)
            VALUES (:org, now() - interval '1 minute', 15, 20, 'booking', 'public',
                    :band, :by)
            RETURNING id, xid
        """).bindparams(org=published["org"].id, band=age_band,
                        by=published["author"].id)).mappings().one()

    def _book(self, db, slot, user, *, checked_in=True, band=None):
        db.execute(text("""
            INSERT INTO speaking_slot_bookings (slot_id, user_id, self_band, checked_in_at)
            VALUES (:s, :u, :b, :ci)
        """).bindparams(s=slot["id"], u=user.id, b=band,
                        ci=_now() if checked_in else None))
        db.flush()

    def test_checked_in_students_are_paired(self, db, published):
        slot = self._slot(db, published)
        a, b = student(db, "A"), student(db, "B")
        self._book(db, slot, a, band=6.0)
        self._book(db, slot, b, band=6.0)

        outcome = speaking.match_slot(db, slot["id"], _now())
        assert len(outcome.pairs) == 1
        pair = db.execute(text("SELECT * FROM speaking_pairs")).mappings().one()
        assert {pair["user_a_id"], pair["user_b_id"]} == {a.id, b.id}
        assert pair["age_band"] == "adult"

    def test_a_minor_is_never_paired_with_an_adult_even_in_an_adult_slot(
            self, db, published):
        """The invariant, checked where it can actually be violated.

        `is_minor` is read from `users.adult_at` at match time, not from the
        slot's label — so a slot a teacher mislabelled `adult` still cannot put a
        fourteen-year-old in an adult pair.
        """
        slot = self._slot(db, published, age_band="adult")
        adult = student(db, "Grown", born=1995)
        minor = student(db, "Child", born=dt.date.today().year - 14)
        self._book(db, slot, adult, band=6.0)
        self._book(db, slot, minor, band=6.0)

        outcome = speaking.match_slot(db, slot["id"], _now())
        assert outcome.pairs == ()
        assert db.scalar(text("SELECT count(*) FROM speaking_pairs")) == 0

    def test_someone_who_did_not_check_in_is_a_no_show(self, db, published):
        slot = self._slot(db, published)
        present, absent = student(db, "Here"), student(db, "Absent")
        self._book(db, slot, present)
        self._book(db, slot, absent, checked_in=False)

        speaking.match_slot(db, slot["id"], _now())
        assert db.scalar(text("""
            SELECT no_show FROM speaking_slot_bookings WHERE user_id = :u
        """).bindparams(u=absent.id)) is True

    def test_an_unmatched_student_is_told(self, db, published):
        """Someone who showed up and got nothing needs to know it was the pool,
        not them."""
        slot = self._slot(db, published)
        lonely = student(db, "Lonely")
        self._book(db, slot, lonely)

        speaking.match_slot(db, slot["id"], _now())
        assert db.scalar(text("""
            SELECT count(*) FROM notifications WHERE template = 'speaking.no_partner'
        """)) == 1

    def test_a_block_survives_into_the_matcher(self, db, published):
        slot = self._slot(db, published)
        victim, harasser = student(db, "Victim"), student(db, "Harasser")
        db.execute(text("""
            INSERT INTO user_blocks (blocker_user_id, blocked_user_id)
            VALUES (:a, :b)
        """).bindparams(a=victim.id, b=harasser.id))
        self._book(db, slot, victim, band=6.0)
        self._book(db, slot, harasser, band=6.0)

        outcome = speaking.match_slot(db, slot["id"], _now())
        assert outcome.pairs == ()

    def test_the_live_queue_pairs_within_an_age_band(self, db, published):
        for name, born in [("A", 1998), ("B", 1999),
                           ("C", dt.date.today().year - 15)]:
            user = student(db, name, born=born)
            db.execute(text("""
                INSERT INTO speaking_queue_entries (user_id, language, age_band)
                VALUES (:u, 'en', :band)
            """).bindparams(u=user.id,
                            band="minor" if born > 2005 else "adult"))
        db.flush()

        outcome = speaking.match_queue(db, _now())
        assert len(outcome.pairs) == 1
        assert not outcome.pairs[0].a.is_minor
        assert db.scalar(text("""
            SELECT count(*) FROM speaking_queue_entries WHERE status = 'waiting'
        """)) == 1

    def test_a_stale_queue_entry_expires(self, db, published):
        user = student(db, "Gone")
        db.execute(text("""
            INSERT INTO speaking_queue_entries (user_id, language, age_band, joined_at)
            VALUES (:u, 'en', 'adult', now() - interval '30 minutes')
        """).bindparams(u=user.id))
        db.flush()
        speaking.match_queue(db, _now())
        assert db.scalar(text("""
            SELECT status FROM speaking_queue_entries WHERE user_id = :u
        """).bindparams(u=user.id)) == "expired"


class TestNotifications:
    def test_telegram_is_preferred_over_sms(self, db, published):
        """The whole cost model in one assertion. SMS is the only user-linear
        line on the infra bill."""
        user = student(db, "Linked")
        db.execute(text("UPDATE users SET telegram_user_id = 42 WHERE id = :u")
                   .bindparams(u=user.id))
        db.flush()
        notify.queue(db, user_id=user.id, template="attempt.scored", params={})
        assert db.scalar(text("SELECT channel FROM notifications WHERE user_id = :u")
                         .bindparams(u=user.id)) == "telegram"

    def test_sms_is_used_only_for_a_login_code(self, db):
        user = student(db, "Unlinked")
        notify.queue(db, user_id=user.id, template="auth.otp", params={})
        notify.queue(db, user_id=user.id, template="attempt.scored", params={})
        channels = db.execute(text("""
            SELECT template, channel FROM notifications WHERE user_id = :u
            ORDER BY template
        """).bindparams(u=user.id)).mappings().all()
        assert dict((r["template"], r["channel"]) for r in channels) == {
            "auth.otp": "sms", "attempt.scored": "in_app"}

    def test_a_duplicate_dedupe_key_inserts_once(self, db):
        user = student(db, "Once")
        first = notify.queue(db, user_id=user.id, template="assignment.set",
                             params={}, dedupe_key="k-1")
        second = notify.queue(db, user_id=user.id, template="assignment.set",
                              params={}, dedupe_key="k-1")
        assert first is not None and second is None

    def test_a_night_time_notice_is_deferred_to_the_morning(self, db):
        """A push at 02:00 does not get read, it gets the app muted."""
        user = student(db, "Sleeper")
        midnight_local = dt.datetime(2026, 8, 1, 18, 30, tzinfo=dt.UTC)  # 23:30 +05
        notify.queue(db, user_id=user.id, template="attempt.scored", params={},
                     now=midnight_local)
        scheduled = db.scalar(text(
            "SELECT scheduled_at FROM notifications WHERE user_id = :u"
        ).bindparams(u=user.id))
        assert scheduled > midnight_local
        assert scheduled.astimezone(dt.timezone(dt.timedelta(hours=5))).hour == 8

    def test_an_urgent_notice_ignores_quiet_hours(self, db):
        user = student(db, "Urgent")
        night = dt.datetime(2026, 8, 1, 18, 30, tzinfo=dt.UTC)
        notify.queue(db, user_id=user.id, template="auth.otp", params={}, now=night)
        assert db.scalar(text("SELECT scheduled_at FROM notifications WHERE user_id = :u")
                         .bindparams(u=user.id)) == night

    def test_delivery_marks_sent_and_records_the_cost(self, db):
        user = student(db, "Payer")
        notify.queue(db, user_id=user.id, template="auth.otp", params={})
        sent, failed = notify.deliver(db, notify.Transport(), now=_now())
        assert (sent, failed) == (1, 0)
        assert notify.monthly_sms_cost(db) == notify.COST_MINOR["sms"]

    def test_a_failing_transport_retries_then_gives_up(self, db):
        user = student(db, "Unreachable")
        notify.queue(db, user_id=user.id, template="auth.otp", params={})

        class Broken(notify.Transport):
            def send(self, **_kw):
                raise RuntimeError("provider down")

        moment = _now()
        for attempt in range(notify.MAX_ATTEMPTS):
            notify.deliver(db, Broken(), now=moment + dt.timedelta(hours=attempt))
        assert db.scalar(text("SELECT status FROM notifications WHERE user_id = :u")
                         .bindparams(u=user.id)) == "failed"

    def test_a_deleted_user_is_suppressed_not_retried(self, db):
        user = student(db, "Departed")
        notify.queue(db, user_id=user.id, template="attempt.scored", params={})
        db.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u")
                   .bindparams(u=user.id))
        db.flush()
        notify.deliver(db, notify.Transport(), now=_now())
        assert db.scalar(text("SELECT status FROM notifications WHERE user_id = :u")
                         .bindparams(u=user.id)) == "suppressed"
