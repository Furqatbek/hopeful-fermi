"""The exam session lifecycle, against a real database.

This is the gap Deliverable 4 §6 flagged: the domain logic was proven in
isolation and the database invariants were proven separately, but nothing
exercised both at once. Everything here runs through the ORM against a schema
built by the real migrations, so a mapping that disagrees with the schema fails.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.modules.content import publish_gate
from app.modules.content import repo as content_repo
from app.modules.exam.models import (
    Attempt,
    AttemptAnswer,
    AttemptSection,
    ItemScore,
    Outbox,
    ScoreRun,
)
from app.modules.exam.session import AnswerDelta, ExamSession
from app.platform.errors import Conflict


def svc(db, scorer_svc, clock) -> ExamSession:
    return ExamSession(db, scorer_svc, clock, grace_seconds=30)


def deltas(published, answers: list[str], start_seq: int = 1) -> list[AnswerDelta]:
    """Answer the first `len(answers)` questions of the seeded paper.

    Driven by `answers`, not zipped with the question list: half these call sites
    pass one answer for a three-item paper on purpose, because a partial autosave
    is the thing under test. Written as a zip it was ambiguous about which list
    was allowed to be short, and indexing makes an answer list LONGER than the
    paper raise instead of silently dropping the tail.
    """
    return [
        AnswerDelta(question_version_xid=str(published["question_versions"][i].xid),
                    slot_key="s1", response=answer, client_seq=start_seq + i)
        for i, answer in enumerate(answers)
    ]


class TestPublishThenSit:
    def test_gate_passes_and_publish_materializes_a_snapshot(self, db, seed, scorer_svc, clock):
        composition = content_repo.load_composition(db, seed["test_version"].id)
        report = publish_gate.run(composition, scorer_svc._registry)
        assert report.passed, [f.message for f in report.errors]

        tv = content_repo.publish(db, seed["test_version"].id, seed["author"].id, clock.now())
        assert tv.status == "published"
        assert tv.total_questions == 3
        assert tv.snapshot["sections"][0]["groups"][0]["questions"][0]["number"] == 1

    def test_snapshot_contains_no_answer_keys(self, db, published):
        """It is the document that goes to the device, so nothing secret may be
        in it. Asserted on the serialized form, not by inspecting fields."""
        import json
        blob = json.dumps(published["test_version"].snapshot)
        for secret in ("bicycle", "library", "museum", "accept"):
            assert secret not in blob, f"{secret!r} leaked into the student payload"

    def test_full_lifecycle_issue_to_scored(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        assert attempt.status == "in_progress"
        assert attempt.expires_at == clock.now() + timedelta(seconds=3600)

        payload = exam.payload(attempt)
        assert payload["total_questions"] == 3

        result = exam.save_answers(attempt, deltas(published, ["bicycle", "library", "wrong"]))
        assert result.accepted == 3 and result.rejected == []
        assert result.seconds_remaining == 3600

        run = exam.submit(attempt)
        assert attempt.status == "scored"
        assert float(run.raw_score) == 2 and float(run.max_raw) == 3
        assert float(run.band) == 6.0
        assert run.engine_version == scorer_svc.engine_version

    def test_score_run_records_the_exact_key_versions(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle", "library", "museum"]))
        run = exam.submit(attempt)
        assert len(run.key_versions) == 3
        assert all(str(v).isdigit() for v in run.key_versions.values())

    def test_item_scores_carry_the_marking_explanation(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["BICYCLE ", "the library", "wrong"]))
        exam.submit(attempt)

        scores = db.scalars(select(ItemScore).order_by(ItemScore.id)).all()
        assert [s.verdict for s in scores] == ["correct", "correct", "incorrect"]
        assert scores[0].normalized_response == "bicycle"
        assert scores[1].explain["normalizers"]
        assert scores[0].matched_alternative == "bicycle"


class TestAutosave:
    def test_repeated_saves_upsert_rather_than_accumulate(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        for seq in range(1, 6):
            exam.save_answers(attempt, [AnswerDelta(
                str(published["question_versions"][0].xid), "s1", f"draft-{seq}", seq)])
        rows = db.scalars(select(AttemptAnswer).where(
            AttemptAnswer.attempt_id == attempt.id)).all()
        assert len(rows) == 1
        assert rows[0].response == "draft-5" and rows[0].revision == 5

    def test_a_stale_sequence_number_cannot_resurrect_an_older_answer(
            self, db, published, scorer_svc, clock):
        """Retries plus out-of-order delivery is the normal case on these
        networks, and without the sequence check it silently corrupts answers."""
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        qv = str(published["question_versions"][0].xid)

        exam.save_answers(attempt, [AnswerDelta(qv, "s1", "newest", client_seq=9)])
        result = exam.save_answers(attempt, [AnswerDelta(qv, "s1", "older", client_seq=4)])

        assert result.accepted == 0
        assert result.rejected == [{"slot_key": "s1", "reason": "stale_seq"}]
        row = db.scalars(select(AttemptAnswer).where(
            AttemptAnswer.attempt_id == attempt.id)).one()
        assert row.response == "newest"

    def test_one_bad_delta_does_not_cost_the_batch(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        batch = deltas(published, ["bicycle", "library", "museum"])
        batch.append(AnswerDelta("00000000-0000-0000-0000-000000000000", "s1", "x", 99))

        result = exam.save_answers(attempt, batch)
        assert result.accepted == 3
        assert result.rejected == [{"slot_key": "s1", "reason": "unknown_slot"}]

    def test_every_save_returns_the_authoritative_clock(self, db, published, scorer_svc, clock):
        """This is why exam timing needs no WebSocket: the sync rides along with
        traffic the client is already sending."""
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        clock.advance(seconds=600)
        result = exam.save_answers(attempt, deltas(published, ["bicycle"]))
        assert result.server_now == clock.now()
        assert result.seconds_remaining == 3000

    def test_answers_are_frozen_after_submit_by_the_database(
            self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle"]))
        exam.submit(attempt)

        with pytest.raises(Conflict):
            exam.save_answers(attempt, deltas(published, ["library"], start_seq=50))

        # And the trigger holds even if the service check is bypassed entirely.
        with pytest.raises(Exception) as exc:
            db.execute(text("UPDATE attempt_answers SET response = '\"x\"' "
                            "WHERE attempt_id = :a").bindparams(a=attempt.id))
            db.flush()
        assert "frozen" in str(exc.value).lower()
        db.rollback()


class TestDeadline:
    def test_submitting_slightly_late_is_accepted_and_recorded(
            self, db, published, scorer_svc, clock):
        """Four seconds late on a mobile network is a hiccup, not cheating."""
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle", "library", "museum"]))

        clock.advance(seconds=3604)
        run = exam.submit(attempt)
        assert float(run.raw_score) == 3
        assert 3_000 < attempt.late_by_ms < 5_000

    def test_saving_well_past_the_deadline_auto_submits_instead(
            self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle"]))

        clock.advance(seconds=4000)
        with pytest.raises(Conflict) as exc:
            exam.save_answers(attempt, deltas(published, ["library"], start_seq=50))
        assert exc.value.code == "attempt_expired"
        assert attempt.status == "scored"
        assert attempt.submitted_via == "auto_expiry"

    def test_the_sweeper_submits_abandoned_attempts(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle", "library"]))

        assert exam.auto_submit_expired() == []
        clock.advance(seconds=3700)
        assert exam.auto_submit_expired() == [attempt.id]
        assert attempt.status == "scored"
        assert float(db.scalars(select(ScoreRun.raw_score)).one()) == 2


class TestIdempotenceAndLimits:
    def test_restarting_an_assigned_attempt_returns_the_same_one(
            self, db, published, seed, scorer_svc, clock):
        from app.modules.exam.models import Assignment

        assignment = Assignment(
            org_id=seed["org"].id, test_version_id=published["test_version"].id,
            assigned_by=seed["author"].id, target_kind="users",
            opens_at=clock.now() - timedelta(hours=1),
            closes_at=clock.now() + timedelta(days=1), max_attempts=1)
        db.add(assignment)
        db.flush()

        exam = svc(db, scorer_svc, clock)
        first = exam.start(user_id=published["student"].id,
                           test_version_id=published["test_version"].id,
                           assignment_id=assignment.id)
        second = exam.start(user_id=published["student"].id,
                            test_version_id=published["test_version"].id,
                            assignment_id=assignment.id)
        assert first.id == second.id
        assert db.scalar(select(Attempt).where(Attempt.assignment_id == assignment.id)
                         .with_only_columns(Attempt.id).order_by(Attempt.id)) == first.id

    def test_submitting_twice_returns_the_same_score_run(
            self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle"]))
        first = exam.submit(attempt)
        second = exam.submit(attempt)
        assert first.id == second.id
        assert db.scalar(select(ScoreRun).where(ScoreRun.attempt_id == attempt.id)
                         .with_only_columns(ScoreRun.id).order_by(ScoreRun.id.desc())) == first.id

    def test_only_one_current_score_run_exists(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle"]))
        exam.submit(attempt)
        current = db.scalars(select(ScoreRun).where(ScoreRun.attempt_id == attempt.id,
                                                    ScoreRun.is_current.is_(True))).all()
        assert len(current) == 1

    def test_an_unpublished_version_cannot_be_sat(self, db, seed, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        with pytest.raises(Conflict) as exc:
            exam.start(user_id=seed["student"].id,
                       test_version_id=seed["test_version"].id)
        assert exc.value.code == "test_version_not_published"

    def test_preview_may_sit_an_unpublished_draft(self, db, seed, scorer_svc, clock):
        """An author must be able to rehearse before publishing, and preview
        attempts are excluded from reporting by a partial index."""
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=seed["author"].id,
                             test_version_id=seed["test_version"].id, mode="preview")
        assert attempt.mode == "preview"


class TestPlayOnce:
    def test_exam_mode_grants_audio_once(self, db, published, with_audio,
                                         scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        grant = exam.audio_grant(attempt, position=1)
        assert grant["grant"] and grant["plays_remaining"] == 0
        with pytest.raises(Conflict) as exc:
            exam.audio_grant(attempt, position=1)
        assert exc.value.code == "audio_already_played"

    def test_practice_mode_replays_freely(self, db, published, with_audio,
                                          scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id,
                             mode="practice")
        for _ in range(3):
            assert exam.audio_grant(attempt, position=1)["plays_remaining"] is None

    def test_a_section_with_no_audio_does_not_burn_the_play(
            self, db, published, scorer_svc, clock):
        """The reading section. A stray call must not cost a student their single
        play of a section that has nothing to play."""
        from app.platform.errors import NotFound

        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        with pytest.raises(NotFound) as exc:
            exam.audio_grant(attempt, position=1)
        assert exc.value.code == "section_has_no_audio"

        section = db.scalars(select(AttemptSection).where(
            AttemptSection.attempt_id == attempt.id)).first()
        assert section.audio_locked_at is None
        assert section.audio_play_count == 0


class TestOutboxAndReview:
    def test_domain_events_land_in_the_outbox_transactionally(
            self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bicycle"]))
        exam.submit(attempt)
        events = db.scalars(select(Outbox.event_type).order_by(Outbox.id)).all()
        assert events == ["attempt.started", "attempt.scored"]

    def test_review_explains_every_mark(self, db, published, scorer_svc, clock):
        exam = svc(db, scorer_svc, clock)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        exam.save_answers(attempt, deltas(published, ["bike", "library", "museum"]))
        exam.submit(attempt)

        review = exam.review(attempt)
        assert len(review) == 3
        wrong = next(r for r in review if r["verdict"] == "incorrect")
        assert wrong["raw_response"] == "bike"
        assert wrong["accepted_answers"] == ["bicycle"]
        assert wrong["explain"]["primitive"] == "text_per_slot"
