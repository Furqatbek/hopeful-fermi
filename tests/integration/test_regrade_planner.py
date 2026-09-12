"""The regrade planner's paging and provenance, against a real database.

`test_worker_pipelines.py` proves the promises the regrade makes to a school —
dry run touches nothing, history survives, contests are not silently re-ranked.
This file is about the mechanics underneath those promises, each of which was
wrong in a way one student's regrade could not show:

  * `CHUNK = 5_000` was a `LIMIT`, not a page size. A subject with more sat
    attempts rescored the lowest 5,000 ids, reported 5,000 as the total and
    marked the job completed — unresumable, and silent.
  * Every applied run recorded `reason = 'regrade_key'`, whatever the trigger.
    The CHECK on `score_runs.reason` has carried `regrade_band_map` since the
    first exam migration and nothing wrote it.
  * `_persist` looked up question ids per attempt, so a paper sat by five
    hundred students issued the same `question_versions` SELECT five hundred
    times inside one transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import event, func, select, text

from app.modules.exam import planner
from app.modules.exam.models import Attempt, RegradeJob, ScoreRun
from app.modules.exam.session import AnswerDelta, ExamSession
from app.modules.qtypes.registry import default_scorer


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@pytest.fixture
def exam(db, clock):
    return ExamSession(db, default_scorer(), clock, grace_seconds=30)


def _student(db, name: str):
    from app.modules.identity.models import User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(2000, 6, 1))
    db.add(user)
    db.flush()
    return user


def _sit(db, exam, published, user, answers: list[str]) -> Attempt:
    attempt = exam.start(user_id=user.id, test_version_id=published["test_version"].id)
    exam.save_answers(attempt, [
        AnswerDelta(question_version_xid=str(published["question_versions"][i].xid),
                    slot_key="s1", response=answer, client_seq=i + 1)
        for i, answer in enumerate(answers)
    ])
    exam.submit(attempt)
    return attempt


def _widen_key(db, published, *, accept: list[str]) -> None:
    """Supersede item 1's key so that "bike" is now accepted. The seeded key
    takes only "bicycle", so every student who wrote "bike" gains a mark."""
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


def _job(db, published, *, trigger: str, subject_type: str, subject_id: int) -> RegradeJob:
    job = RegradeJob(trigger=trigger, subject_type=subject_type, subject_id=subject_id,
                     initiated_by=published["author"].id, reason="test",
                     dry_run=True, status="planning")
    db.add(job)
    db.flush()
    return job


def _key_job(db, published) -> RegradeJob:
    _widen_key(db, published, accept=["bicycle", "bike"])
    return _job(db, published, trigger="answer_key_change",
                subject_type="question_version",
                subject_id=published["question_versions"][0].id)


def _five_wrong(db, exam, published) -> list[Attempt]:
    return [_sit(db, exam, published, _student(db, f"S{i}"),
                 ["bike", "library", "museum"]) for i in range(5)]


def _current_reasons(db) -> set[str]:
    return set(db.scalars(select(ScoreRun.reason).where(ScoreRun.is_current.is_(True))))


class TestTheJobIsChunked:
    """Five attempts, a page size of two: three pages, and every one of the
    five is planned, rescored and counted."""

    @pytest.fixture(autouse=True)
    def _small_pages(self, monkeypatch):
        monkeypatch.setattr(planner, "CHUNK", 2)

    def test_selection_yields_every_page(self, db, exam, published):
        _five_wrong(db, exam, published)
        job = _key_job(db, published)
        pages = list(planner._affected(db, job))
        assert [len(p) for p in pages] == [2, 2, 1]
        ids = [a.id for page in pages for a in page]
        assert ids == sorted(ids), "keyset paging must walk ids in order"
        assert len(set(ids)) == 5

    def test_the_plan_counts_past_the_first_page(self, db, exam, published):
        _five_wrong(db, exam, published)
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        assert job.status == "ready"
        assert job.attempts_total == 5
        assert job.scores_changed == 5
        assert job.bands_changed == 5
        # The report names every band change, not only the first page's.
        assert len(job.report["changes"]) == 5

    def test_apply_rescores_past_the_first_page(self, db, exam, published):
        attempts = _five_wrong(db, exam, published)
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        assert planner.apply(db, job, default_scorer(), now=_now()) == 5

        assert job.status == "completed"
        assert job.attempts_processed == 5
        assert job.scores_changed == 5
        new_runs = db.scalars(
            select(ScoreRun).where(ScoreRun.is_current.is_(True),
                                   ScoreRun.regrade_job_id == job.id)).all()
        assert {r.attempt_id for r in new_runs} == {a.id for a in attempts}
        assert {float(r.raw_score) for r in new_runs} == {3.0}
        # Every student whose band moved is told, once each.
        assert db.scalar(text(
            "SELECT count(*) FROM notifications WHERE template = 'regrade.band_changed'"
        )) == 5

    def test_the_callers_attempt_sees_the_new_run(self, db, exam, published):
        """`apply` expires the session between pages so the core UPDATE that
        points an attempt at its new run is visible on the instance the caller
        holds — an `expunge` would have left it pointing at the old one."""
        [attempt] = [_sit(db, exam, published, _student(db, "One"),
                          ["bike", "library", "museum"])]
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        planner.apply(db, job, default_scorer(), now=_now())
        current = db.scalar(select(ScoreRun.id).where(
            ScoreRun.attempt_id == attempt.id, ScoreRun.is_current.is_(True)))
        assert attempt.current_score_run_id == current

    def test_an_empty_selection_still_finishes_the_plan(self, db, published):
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        assert job.status == "ready"
        assert job.attempts_total == 0
        assert job.report["changes"] == []


class TestTheRunSaysWhyItReplacedItsPredecessor:
    """`score_runs.reason` is the provenance column. The CHECK allows
    `regrade_band_map`; the planner wrote `regrade_key` for everything."""

    def test_a_key_change_records_regrade_key(self, db, exam, published):
        _sit(db, exam, published, _student(db, "Aziza"), ["bike", "library", "museum"])
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        planner.apply(db, job, default_scorer(), now=_now())
        assert _current_reasons(db) == {"regrade_key"}

    def test_a_band_map_change_records_regrade_band_map(self, db, exam, published):
        """Staged against the band-map version, as the console stages it.

        The key is widened too, so the raw score moves: `regrade.apply` writes a
        run only for a raw change, which a band-map retune on its own does not
        produce. That is a separate gap in the pure module; here the question
        is what the run SAYS once one is written.
        """
        _sit(db, exam, published, _student(db, "Aziza"), ["bike", "library", "museum"])
        _widen_key(db, published, accept=["bicycle", "bike"])
        job = _job(db, published, trigger="band_map_change",
                   subject_type="band_map_version",
                   subject_id=published["band_map_version"].id)
        planner.plan(db, job, default_scorer())
        assert job.attempts_total == 1
        planner.apply(db, job, default_scorer(), now=_now())
        assert _current_reasons(db) == {"regrade_band_map"}

    @pytest.mark.parametrize("trigger", ["engine_fix", "manual"])
    def test_a_recompute_records_recompute(self, db, exam, published, trigger):
        """Not `manual_override`, which the CHECK reserves for a hand-entered
        score that bypasses the engine. An admin pressing "recompute" is still
        the engine marking the paper."""
        _sit(db, exam, published, _student(db, "Aziza"), ["bike", "library", "museum"])
        _widen_key(db, published, accept=["bicycle", "bike"])
        job = _job(db, published, trigger=trigger, subject_type="test_version",
                   subject_id=published["test_version"].id)
        planner.plan(db, job, default_scorer())
        planner.apply(db, job, default_scorer(), now=_now())
        assert _current_reasons(db) == {"recompute"}

    def test_the_dry_run_persists_no_reason_at_all(self, db, exam, published):
        """`regrade_dry_run` is outside the CHECK on purpose: a plan that reached
        `score_runs` would fail the constraint rather than pass as a regrade."""
        _sit(db, exam, published, _student(db, "Aziza"), ["bike", "library", "museum"])
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        assert _current_reasons(db) == {"initial"}


class TestQuestionIdsAreResolvedOncePerPage:
    """The write-side twin of `_recompute`'s "once per test version, not once
    per attempt". Five students on one paper is one lookup, not five."""

    @staticmethod
    def _question_version_lookups(statements: list[str]) -> int:
        # `_question_ids` is a bare `SELECT ... FROM question_versions WHERE id
        # IN (...)`. The scoring-input walk also touches the table, through a
        # JOIN, and is counted elsewhere.
        return sum(1 for s in statements
                   if "FROM question_versions" in s and "JOIN" not in s)

    @pytest.fixture
    def recorder(self, engine):
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        yield statements
        event.remove(engine, "before_cursor_execute", record)

    def test_one_page_is_one_lookup(self, db, exam, published, recorder):
        _five_wrong(db, exam, published)
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        recorder.clear()
        assert planner.apply(db, job, default_scorer(), now=_now()) == 5
        assert self._question_version_lookups(recorder) == 1

    def test_three_pages_are_three_lookups(self, db, exam, published, recorder,
                                           monkeypatch):
        monkeypatch.setattr(planner, "CHUNK", 2)
        _five_wrong(db, exam, published)
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        recorder.clear()
        assert planner.apply(db, job, default_scorer(), now=_now()) == 5
        assert self._question_version_lookups(recorder) == 3

    def test_item_scores_still_carry_their_question(self, db, exam, published):
        """Hoisting must not lose the id: `question_id` on every item score is
        what the analytics projections join on."""
        _five_wrong(db, exam, published)
        job = _key_job(db, published)
        planner.plan(db, job, default_scorer())
        planner.apply(db, job, default_scorer(), now=_now())
        unlinked = db.scalar(text("""
            SELECT count(*) FROM item_scores i
            JOIN score_runs r ON r.id = i.score_run_id
            WHERE r.regrade_job_id = :j AND i.question_id = 0
        """).bindparams(j=job.id))
        assert unlinked == 0
        assert db.scalar(select(func.count()).select_from(ScoreRun)
                         .where(ScoreRun.regrade_job_id == job.id)) == 5
