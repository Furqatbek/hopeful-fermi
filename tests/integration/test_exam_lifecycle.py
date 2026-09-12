"""The exam session lifecycle, against a real database.

This is the gap Deliverable 4 §6 flagged: the domain logic was proven in
isolation and the database invariants were proven separately, but nothing
exercised both at once. Everything here runs through the ORM against a schema
built by the real migrations, so a mapping that disagrees with the schema fails.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.deps import issue_access_token
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
        # The rejection names its QUESTION as well as its slot: every question in
        # a paper has an `s1`, so the slot alone identifies nothing.
        assert result.rejected == [{"question_version_xid": qv, "slot_key": "s1",
                                    "reason": "stale_seq"}]
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
        assert result.rejected == [
            {"question_version_xid": "00000000-0000-0000-0000-000000000000",
             "slot_key": "s1", "reason": "unknown_slot"}]

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


class TestSubmitTakesTheLastFlushWithIt:
    """`POST /attempts/{xid}/submit` declares `final_answers` — "last outbox
    flush, applied before freezing" — and the handler took no body at all.

    The student runner flushes just before submit, and that flush fails
    silently by design. When it did, the submit went ahead, the database froze
    the answers, and the client dropped the undelivered rows. The last thing
    typed before Finish was gone from both ends. This is the server half: the
    body reaches the attempt before it freezes, and a body-less submit is what
    it always was.
    """

    @pytest.fixture
    def client(self, engine, db):
        from app.api import deps
        from app.api.main import create_app

        app = create_app()
        app.dependency_overrides[deps.db] = lambda: db
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    @pytest.fixture
    def live(self, client, db, published):
        from datetime import UTC, datetime

        from app.modules.billing.models import EntitlementRow

        db.add(EntitlementRow(subject_kind="user", subject_id=published["student"].id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=datetime.now(UTC) - timedelta(days=1)))
        db.flush()
        response = client.post("/api/v1/attempts", headers=self._auth(published),
                               json={"test_version_xid":
                                     str(published["test_version"].xid)})
        assert response.status_code == 201, response.text
        return response.json()

    @staticmethod
    def _auth(published, **extra) -> dict:
        return {"Authorization":
                f"Bearer {issue_access_token(str(published['student'].xid))}", **extra}

    @staticmethod
    def _delta(published, index: int, answer: str, seq: int = 1) -> dict:
        return {"question_version_xid": str(published["question_versions"][index].xid),
                "slot_key": "s1", "response": answer, "client_seq": seq}

    def test_the_final_flush_is_stored_and_marked(self, client, db, published, live):
        """Two answers saved the ordinary way, the third only in the submit
        body. The score says whether the third was marked."""
        head = self._auth(published)
        saved = client.post(f"/api/v1/attempts/{live['xid']}/answers", headers=head,
                            json={"deltas": [self._delta(published, 0, "bicycle"),
                                             self._delta(published, 1, "library")]})
        assert saved.json()["accepted"] == 2, saved.text

        result = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                             json={"final_answers": [self._delta(published, 2, "museum")]})
        assert result.status_code == 200, result.text
        assert result.json()["raw_score"] == 3.0
        stored = db.scalars(select(AttemptAnswer.response).where(
            AttemptAnswer.attempt_id == db.scalar(
                select(Attempt.id).where(Attempt.xid == live["xid"])))).all()
        assert sorted(stored) == ["bicycle", "library", "museum"]

    def test_the_flush_respects_the_sequence_rule(self, client, published, live):
        """The same rule as autosave: a stale delta in the final flush cannot
        resurrect an older answer over the one already stored."""
        head = self._auth(published)
        client.post(f"/api/v1/attempts/{live['xid']}/answers", headers=head,
                    json={"deltas": [self._delta(published, 0, "bicycle", seq=5)]})
        result = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                             json={"final_answers": [self._delta(published, 0, "wrong",
                                                                 seq=2)]})
        assert result.status_code == 200, result.text
        assert result.json()["raw_score"] == 1.0

    def test_a_bodyless_submit_is_unchanged(self, client, published, live):
        head = self._auth(published)
        client.post(f"/api/v1/attempts/{live['xid']}/answers", headers=head,
                    json={"deltas": [self._delta(published, 0, "bicycle")]})
        result = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head)
        assert result.status_code == 200, result.text
        assert result.json()["raw_score"] == 1.0
        assert result.json()["status"] == "scored"

    def test_an_empty_body_is_the_same_request(self, client, published, live):
        head = self._auth(published)
        for body in ({}, {"final_answers": []}, {"final_answers": None}):
            result = client.post(f"/api/v1/attempts/{live['xid']}/submit",
                                 headers=head, json=body)
            assert result.status_code == 200, (body, result.text)

    def test_a_replay_with_the_same_flush_returns_the_stored_result(
            self, client, published, live):
        """The idempotency fingerprint covers the flush. A retry after a lost
        response carries the same rows and gets the same answer."""
        head = self._auth(published, **{"Idempotency-Key": "finish-1"})
        body = {"final_answers": [self._delta(published, 0, "bicycle")]}
        first = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                            json=body)
        again = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                            json=body)
        assert first.status_code == again.status_code == 200
        assert first.json() == again.json()
        assert first.json()["raw_score"] == 1.0

    def test_a_replay_with_a_different_flush_is_refused(self, client, published, live):
        """Same key, different rows, is the misuse the key exists to catch."""
        head = self._auth(published, **{"Idempotency-Key": "finish-2"})
        client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                    json={"final_answers": [self._delta(published, 0, "bicycle")]})
        other = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                            json={"final_answers": [self._delta(published, 1, "library")]})
        assert other.status_code == 409, other.text

    def test_a_retry_against_a_frozen_attempt_still_returns_the_run(
            self, client, published, live):
        """The response was lost and the client retries WITHOUT the key it
        should have kept, rows still attached. The attempt is frozen, so the
        flush is refused — and the submit must still answer with the run rather
        than a 409 the runner would show as a failed finish."""
        head = self._auth(published)
        body = {"final_answers": [self._delta(published, 0, "bicycle")]}
        first = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                            json=body)
        again = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                            json=body)
        assert again.status_code == 200, again.text
        assert again.json()["score_run_xid"] == first.json()["score_run_xid"]

    def test_the_flush_past_the_deadline_does_not_score_twice(
            self, client, db, published, live):
        """Past `expires_at` plus grace, `save_answers` auto-submits and raises.
        `ExamSession.submit(final_answers=...)` swallows that and scores AGAIN,
        stamping `submitted_via = 'user'` over `auto_expiry`; the handler applies
        the flush first and lets `submit` find the attempt already scored."""
        head = self._auth(published)
        client.post(f"/api/v1/attempts/{live['xid']}/answers", headers=head,
                    json={"deltas": [self._delta(published, 0, "bicycle")]})
        db.execute(text("UPDATE attempts SET expires_at = now() - interval '10 minutes' "
                        "WHERE xid = CAST(:x AS uuid)").bindparams(x=live["xid"]))
        db.flush()
        db.expire_all()
        result = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                             json={"final_answers": [self._delta(published, 1, "library")]})
        assert result.status_code == 200, result.text
        # The late row was refused, so only the first answer is marked.
        assert result.json()["raw_score"] == 1.0
        runs = db.scalars(select(ScoreRun).where(ScoreRun.attempt_id == db.scalar(
            select(Attempt.id).where(Attempt.xid == live["xid"])))).all()
        assert len(runs) == 1, "the attempt was scored twice"
        assert db.scalar(text("SELECT submitted_via FROM attempts WHERE xid = CAST(:x AS uuid)")
                         .bindparams(x=live["xid"])) == "auto_expiry"

    def test_more_than_a_batch_is_refused_up_front(self, client, published, live):
        """Same cap as `AnswerBatch`. This is one flush, not a backlog, and a
        422 from the model lands before the attempt is touched."""
        head = self._auth(published)
        result = client.post(f"/api/v1/attempts/{live['xid']}/submit", headers=head,
                             json={"final_answers":
                                   [self._delta(published, 0, "x", seq=i)
                                    for i in range(201)]})
        assert result.status_code == 422, result.text


class TestThePaperIsLoadedOnlyWhereItIsServed:
    """`TestVersion.snapshot` is the whole published paper as JSONB, and it was
    a plain column: every `session.get(TestVersion)` that wanted a scalar — the
    status at start, the band map at submit, the title once per row of the
    assignment listing — pulled the paper with it. A 25-row home screen moved
    25 papers out of Postgres to emit 25 titles; a submit moved one to read an
    integer. The column is deferred now, and the two serving paths undefer it
    on the same SELECT, so the paper is read exactly where the docstrings
    promise "one row read" and nowhere else.

    Asserted on the statements the engine emits, the way the listing tests
    count queries: a mapping change that quietly went back to loading the
    column would pass every functional test here.
    """

    @pytest.fixture
    def client(self, engine, db):
        from app.api import deps
        from app.api.main import create_app

        app = create_app()
        app.dependency_overrides[deps.db] = lambda: db
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    @pytest.fixture
    def live(self, client, db, published):
        from datetime import UTC, datetime

        from app.modules.billing.models import EntitlementRow

        db.add(EntitlementRow(subject_kind="user", subject_id=published["student"].id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=datetime.now(UTC) - timedelta(days=1)))
        db.flush()
        response = client.post("/api/v1/attempts", headers=self._auth(published),
                               json={"test_version_xid":
                                     str(published["test_version"].xid)})
        assert response.status_code == 201, response.text
        return response.json()

    @pytest.fixture
    def assigned(self, db, published):
        """One assignment addressed to the student, so `GET /assignments` has a
        row whose title comes off a TestVersion."""
        from datetime import UTC, datetime

        from app.modules.exam.models import Assignment, AssignmentTarget

        row = Assignment(
            org_id=published["org"].id, test_version_id=published["test_version"].id,
            assigned_by=published["author"].id, target_kind="users",
            opens_at=datetime.now(UTC) - timedelta(hours=1),
            closes_at=datetime.now(UTC) + timedelta(days=7),
            max_attempts=1, mode="exam", allow_review_after="close")
        db.add(row)
        db.flush()
        db.add(AssignmentTarget(assignment_id=row.id, user_id=published["student"].id))
        db.flush()
        return row

    @staticmethod
    def _auth(published) -> dict:
        return {"Authorization":
                f"Bearer {issue_access_token(str(published['student'].xid))}"}

    @staticmethod
    def _statements(db, call) -> list[str]:
        """Every statement the engine ran while `call()` ran.

        The identity map is EMPTIED first, not expired: a request in production
        starts with a fresh session, and an expired-but-present row behaves
        differently — `Session.get` refreshes it without loader options and the
        deferred column then arrives on a second SELECT, which is an artefact of
        the shared test session rather than of the code under test.
        """
        from sqlalchemy import event

        seen: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:
            seen.append(statement)

        engine = db.get_bind()
        db.expunge_all()
        event.listen(engine, "before_cursor_execute", record)
        try:
            call()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return seen

    @staticmethod
    def _reads_the_paper(statement: str) -> bool:
        import re

        # `\b` after `snapshot` so `test_versions.snapshot_bytes` — the size,
        # a scalar every reader may have — does not count as the paper.
        return re.search(r"\btest_versions\.snapshot\b", statement) is not None

    def test_submitting_does_not_read_the_paper(self, client, db, published, live):
        """Scoring wants `band_map_version_id` and the item join, not the
        snapshot; a submit used to move the whole paper to read one integer."""
        head = self._auth(published)
        statements = self._statements(
            db, lambda: client.post(f"/api/v1/attempts/{live['xid']}/submit",
                                    headers=head))
        assert any("test_versions" in s for s in statements), \
            "the submit path no longer touches test_versions at all — rewrite this test"
        assert not [s for s in statements if self._reads_the_paper(s)]

    def test_the_assignment_listing_does_not_read_the_paper(
            self, client, db, published, assigned):
        """The student home screen: one title per row, and it used to be one
        paper per row — `assignment_dto` called `session.get(TestVersion)` for
        each. `_PageContext` already selects the two columns it needs, so this
        one is a guard rather than a repair: a DTO that goes back to loading
        the row would pass every functional test and fail here."""
        head = self._auth(published)

        def call() -> None:
            response = client.get("/api/v1/assignments", headers=head)
            assert response.status_code == 200, response.text
            assert [a["test_version_xid"] for a in response.json()["items"]] \
                == [str(published["test_version"].xid)]

        statements = self._statements(db, call)
        assert any("test_versions" in s for s in statements)
        assert not [s for s in statements if self._reads_the_paper(s)]

    def test_the_payload_is_still_one_row_read(self, client, db, published, live):
        """The other half of the change: deferring the column must not turn the
        serving path into two statements. `payload()` undefers on its own
        SELECT, and the handler's second `get` hits the identity map."""
        head = self._auth(published)

        def call() -> None:
            response = client.get(f"/api/v1/attempts/{live['xid']}/payload",
                                  headers=head)
            assert response.status_code == 200, response.text
            assert response.json()["sections"]

        statements = self._statements(db, call)
        version_reads = [s for s in statements
                         if s.lstrip().upper().startswith("SELECT")
                         and "FROM test_versions" in s]
        assert len(version_reads) == 1, version_reads
        assert self._reads_the_paper(version_reads[0])
