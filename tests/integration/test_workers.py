"""The workers against a real database.

The relay tests are the ones that matter most. Everything asynchronous in this
system rests on one claim — that an event written in the same transaction as its
cause is delivered at least once — and this is where that claim is checked, in
particular the awkward half of it: that a crash between sending and marking
produces a DUPLICATE rather than a loss.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select, text

from app.modules.exam.models import Outbox
from app.workers import relay, sweeper


@pytest.fixture
def recorder():
    """A dispatcher that records instead of enqueueing, so the relay's contract
    is testable without a broker."""
    sent: list[tuple] = []

    def dispatch(event_type, payload, aggregate_type, aggregate_id):
        sent.append((event_type, payload, aggregate_type, aggregate_id))

    dispatch.sent = sent                                            # type: ignore
    return dispatch


def event(db, event_type: str = "attempt.scored", **payload) -> Outbox:
    row = Outbox(aggregate_type="attempt", aggregate_id=str(uuid.uuid4()),
                 event_type=event_type, payload=payload or {"attempt_id": 1})
    db.add(row)
    db.flush()
    return row


class TestRelay:
    def test_an_undispatched_event_is_sent_and_marked(self, db, recorder):
        row = event(db, "attempt.scored", attempt_id=7)
        assert relay.drain(db, recorder) == 1
        assert recorder.sent == [("attempt.scored", {"attempt_id": 7}, "attempt",
                                 row.aggregate_id)]
        db.refresh(row)
        assert row.dispatched_at is not None

    def test_a_dispatched_event_is_not_sent_again(self, db, recorder):
        event(db)
        relay.drain(db, recorder)
        assert relay.drain(db, recorder) == 0

    def test_a_failed_dispatch_leaves_the_row_undispatched(self, db):
        """The half of at-least-once that people get wrong.

        The row must still be pending after a failure, and must come back — a
        marked-then-failed row is an event that silently never happened.
        """
        row = event(db)

        def explode(*_a):
            raise RuntimeError("broker down")

        assert relay.drain(db, explode) == 0
        db.refresh(row)
        assert row.dispatched_at is None
        assert row.attempts == 1
        assert "broker down" in row.last_error

    def test_a_failed_row_backs_off_before_retrying(self, db, recorder):
        row = event(db)

        def explode(*_a):
            raise RuntimeError("nope")

        moment = dt.datetime.now(dt.UTC)
        relay.drain(db, explode, now=moment)
        db.refresh(row)
        assert row.available_at > moment

        # Not picked up until the backoff has elapsed...
        assert relay.drain(db, recorder, now=moment) == 0
        # ...and then it is.
        assert relay.drain(db, recorder, now=moment + dt.timedelta(minutes=1)) == 1

    def test_a_row_that_has_exhausted_its_attempts_stops_being_retried(self, db,
                                                                       recorder):
        """Not deleted and not marked dispatched. A dead letter you cannot see is
        a dead letter you will not fix."""
        row = event(db)
        row.attempts = relay.MAX_ATTEMPTS
        db.flush()
        assert relay.drain(db, recorder) == 0
        assert relay.stuck(db) == 1
        db.refresh(row)
        assert row.dispatched_at is None

    def test_a_send_that_succeeds_but_whose_mark_is_lost_redelivers(self, db):
        """The crash window, simulated.

        The relay sends and then marks. If the process dies between them, the row
        is still pending and the event is sent twice. That is the correct failure
        direction and this test pins it — a change that marks first would make
        this test pass by losing the event, so it asserts the duplicate.
        """
        row = event(db, "attempt.scored", attempt_id=9)
        seen: list[dict] = []

        def send_then_crash(event_type, payload, *_a):
            seen.append(payload)
            raise KeyboardInterrupt("power cut after send")

        # A savepoint, so the rollback undoes the RELAY's work and not the event
        # itself — which is what a process dying mid-batch actually does.
        savepoint = db.begin_nested()
        with pytest.raises(KeyboardInterrupt):
            relay.drain(db, send_then_crash)
        savepoint.rollback()

        delivered: list[dict] = []
        relay.drain(db, lambda et, p, *a: delivered.append(p))
        assert seen == delivered == [{"attempt_id": 9}]
        # Read the row back with SQL, not through the identity map: the savepoint
        # rollback expired the ORM object and re-reading it would prove nothing
        # about what is actually committed.
        assert db.scalar(text("SELECT dispatched_at FROM outbox WHERE id = :i")
                         .bindparams(i=row.id)) is not None

    def test_events_are_dispatched_oldest_first(self, db, recorder):
        for i in range(5):
            event(db, "attempt.scored", attempt_id=i)
        relay.drain(db, recorder)
        assert [p["attempt_id"] for _, p, _, _ in recorder.sent] == [0, 1, 2, 3, 4]

    def test_the_batch_size_is_honoured(self, db, recorder):
        for i in range(10):
            event(db, "attempt.scored", attempt_id=i)
        assert relay.drain(db, recorder, batch=4) == 4
        assert relay.drain(db, recorder, batch=4) == 4
        assert relay.drain(db, recorder, batch=4) == 2

    def test_lag_is_zero_when_the_queue_is_empty(self, db):
        assert relay.lag_seconds(db) == 0

    def test_lag_measures_the_oldest_pending_row(self, db, recorder):
        """The single number Deliverable 5 §5 says to alert on."""
        row = event(db)
        row.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=90)
        db.flush()
        assert relay.lag_seconds(db) >= 89
        relay.drain(db, recorder)
        assert relay.lag_seconds(db) == 0

    def test_dispatched_rows_are_purged_but_pending_ones_survive(self, db, recorder):
        old = event(db)
        relay.drain(db, recorder)
        old.dispatched_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=60)
        pending = event(db)
        db.flush()

        assert relay.purge_dispatched(db, older_than=dt.timedelta(days=30)) == 1
        assert db.get(Outbox, pending.id) is not None


class TestRouting:
    def test_every_emitted_event_type_has_a_route(self):
        """A typo in an event name is a job that silently never runs.

        The set of event types the application EMITS is discovered from the
        source, so an event added next month without a route fails here rather
        than in production.
        """
        import pathlib
        import re

        from app.workers.actors import ROUTES

        root = pathlib.Path(__file__).resolve().parents[2] / "app"
        patterns = (
            r'event_type\s*=\s*"([a-z_]+\.[a-z_]+)"',      # Outbox(...) kwarg
            r'self\._emit\([^,]+,\s*"([a-z_]+\.[a-z_]+)"',  # ExamSession._emit
            r'\bemit\(\s*session\s*,\s*"([a-z_]+\.[a-z_]+)"',  # module-level emit()
        )
        emitted = set()
        for path in root.rglob("*.py"):
            body = path.read_text()
            for pattern in patterns:
                emitted |= set(re.findall(pattern, body))

        # A floor, so the scan silently matching nothing cannot pass this test.
        # It has to rise whenever a new emitter shape appears, which is the
        # moment to check the pattern list still covers everything.
        assert len(emitted) >= 6, f"the scan found only {sorted(emitted)}"
        assert "media.uploaded" in emitted, "the emit() pattern stopped matching"
        assert emitted <= set(ROUTES), f"unrouted: {sorted(emitted - set(ROUTES))}"

    def test_an_unknown_event_raises_rather_than_being_dropped(self):
        from app.workers.actors import dispatch

        with pytest.raises(KeyError):
            dispatch("nonsense.happened", {}, "attempt", "x")


class TestSweeper:
    def test_expired_idempotency_keys_are_deleted(self, db, seed):
        from app.modules.exam.models import IdempotencyKey

        db.add(IdempotencyKey(scope="s", key="k", user_id=seed["author"].id,
                              request_hash="h",
                              expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2)))
        db.add(IdempotencyKey(scope="s", key="live", user_id=seed["author"].id,
                              request_hash="h",
                              expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1)))
        db.flush()
        assert sweeper.expire_idempotency(db, dt.datetime.now(dt.UTC)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyKey)) == 1

    def test_next_months_partitions_are_created(self, db):
        """The failure this prevents is silent: there is a DEFAULT partition, so
        running out does not error — the table just stops being partitioned."""
        far = dt.datetime(2027, 3, 15, tzinfo=dt.UTC)
        assert sweeper.ensure_partitions(db, far) > 0
        for table in sweeper.PARTITIONED:
            assert db.scalar(text("SELECT to_regclass(:n) IS NOT NULL")
                             .bindparams(n=f"{table}_2027_03"))

    def test_creating_partitions_twice_is_a_no_op(self, db):
        far = dt.datetime(2027, 6, 1, tzinfo=dt.UTC)
        sweeper.ensure_partitions(db, far)
        assert sweeper.ensure_partitions(db, far) == 0

    def test_the_month_rollover_wraps_the_year(self, db):
        assert sweeper._month_start(dt.datetime(2026, 12, 3, tzinfo=dt.UTC), 1) \
            == dt.date(2027, 1, 1)
        assert sweeper._month_start(dt.datetime(2026, 11, 3, tzinfo=dt.UTC), 3) \
            == dt.date(2027, 2, 1)

    def test_an_abandoned_upload_is_marked(self, db, seed):
        asset = db.scalar(text("""
            INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                      content_type, bytes, checksum_sha256, status)
            VALUES (:u, 'audio', 'media', 'k', 'audio/mpeg', 0, 'x', 'uploading')
            RETURNING id
        """).bindparams(u=seed["author"].id))
        db.execute(text("""
            INSERT INTO media_uploads (media_asset_id, provider_upload_id, created_by,
                                       part_size, expected_bytes, status, expires_at)
            VALUES (:a, 'p-1', :u, 5242880, 100, 'open', now() - interval '2 days')
        """).bindparams(a=asset, u=seed["author"].id))
        db.flush()
        assert sweeper.abandon_uploads(db, dt.datetime.now(dt.UTC)) == 1

    def test_old_otp_challenges_are_deleted(self, db):
        db.execute(text("""
            INSERT INTO otp_challenges (phone, purpose, code_hash, channel,
                                        expires_at, created_at)
            VALUES ('+998900000000', 'login', 'x', 'sms', now(),
                    now() - interval '2 days')
        """))
        db.flush()
        assert sweeper.expire_otp(db) == 1

    def test_health_reports_the_numbers_a_monitor_reads(self, db):
        event(db)
        health = sweeper.health(db)
        assert set(health) == {"outbox_lag_seconds", "outbox_stuck",
                               "notifications_queued", "notifications_failed",
                               "attempts_overdue", "sms_cost_minor_this_month"}
        assert health["outbox_stuck"] == 0
        assert health["outbox_lag_seconds"] >= 0

    def test_the_api_and_the_workers_read_the_same_health_numbers(self, db):
        """They cannot import each other, so both read `app.platform.health`.

        Two implementations of "is the queue healthy" would eventually disagree,
        and the one on the dashboard would be the wrong one.
        """
        from app.platform import health as kernel

        assert sweeper.health(db) == kernel.snapshot(db)

    def test_an_expired_attempt_is_auto_submitted_and_scored(self, db, published,
                                                             clock):
        """The absence of this job is a student stuck `in_progress` forever, never
        scored, showing as "not started" on their teacher's dashboard."""
        from app.modules.exam.models import Attempt, ScoreRun
        from app.modules.exam.session import ExamSession
        from app.modules.qtypes.registry import default_scorer

        exam = ExamSession(db, default_scorer(), clock, grace_seconds=30)
        attempt = exam.start(user_id=published["student"].id,
                             test_version_id=published["test_version"].id)
        attempt.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
        db.flush()

        assert sweeper.auto_submit(db, dt.datetime.now(dt.UTC)) == [attempt.id]
        db.refresh(attempt)
        assert attempt.status == "scored"
        assert attempt.submitted_via == "auto_expiry"
        assert db.scalars(select(ScoreRun).where(
            ScoreRun.attempt_id == attempt.id)).one().is_current
        assert db.get(Attempt, attempt.id).submitted_at is not None
