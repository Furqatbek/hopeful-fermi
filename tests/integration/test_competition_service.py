"""Driving a contest through its states, and publishing its board.

`test_competition_endpoints.py` covers the HTTP surface. This covers the module
underneath it, which is where the two acts that a competitor actually experiences
live: the tick that walks `scheduled → registration → lobby → live → grading →
final`, and `republish` — the only path by which a published ranking ever changes.

`republish` had never been executed. Nor had the `grading` transition, nor the
board's handling of an entry that is not a legitimate competitor.

**A disqualified entry stayed on the leaderboard.** `materialize` selects on the
ATTEMPT's status — `a.status = 'scored'` — and never looks at the entry's. But
`competition_entries.status` has a `disqualified` value, `disqualified_reason` and
`disqualified_by` columns exist for the investigation that produced it, and
`release_key` already refuses to hand a paper to a disqualified entry. Every part
of the mechanism was in place except the one that matters: the cheat kept their
rank, and everybody below them stayed one place down.

**An attempt with no recorded duration won every duration tiebreak.**
`_duration_ms` returns `0` when `started_at` or `submitted_at` is null, and
`duration_asc` sorts ascending — so "we do not know how long they took" ranked
ahead of a competitor who genuinely finished fastest.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app.modules.competitions import service


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@pytest.fixture
def contest(db, published):
    """A contest that has ended, with its window in the past."""
    return db.execute(text("""
        INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                  starts_at, ends_at, duration_seconds, status,
                                  visibility, created_by)
        VALUES (:o, :tv, 'Friday', now() - interval '3 hours',
                now() - interval '3 hours', now() - interval '1 hour', 1800,
                'live', 'org', :by)
        RETURNING id, xid
    """).bindparams(o=published["org"].id, tv=published["test_version"].id,
                    by=published["author"].id)).mappings().one()


def _competitor(db, published, contest, name: str, *, raw, band=7.0,
                duration_ms=900_000, status="submitted", scored=True,
                started=True, submitted=True):
    """A registered entry with a scored attempt, at a chosen duration."""
    user = db.execute(text("""
        INSERT INTO users (phone, given_name, family_name, date_of_birth, status)
        VALUES (:p, :n, 'Karimova', CAST('2000-01-01' AS date), 'active')
        RETURNING id, xid
    """).bindparams(p=f"+99890{abs(hash(name)) % 10**7:07d}", n=name)).mappings().one()
    start = _now() - dt.timedelta(hours=2)
    attempt = db.scalar(text("""
        INSERT INTO attempts (user_id, test_version_id, competition_id, mode, status,
                              started_at, submitted_at)
        VALUES (:u, :tv, :c, 'exam', :st,
                CASE WHEN :started THEN :start END,
                CASE WHEN :submitted THEN :start + make_interval(secs => :secs) END)
        RETURNING id
    """).bindparams(u=user["id"], tv=published["test_version"].id, c=contest["id"],
                    st="scored" if scored else "in_progress", start=start,
                    started=started, submitted=submitted,
                    secs=duration_ms / 1000))
    if scored:
        db.execute(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, band, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, :raw, 40, :band, true)
        """).bindparams(a=attempt, raw=raw, band=band))
    db.execute(text("""
        INSERT INTO competition_entries (competition_id, user_id, attempt_id, status)
        VALUES (:c, :u, :a, :s)
    """).bindparams(c=contest["id"], u=user["id"], a=attempt, s=status))
    db.flush()
    return {**user, "attempt_id": attempt}


def _board(db, contest) -> list[tuple]:
    return db.execute(text("""
        SELECT u.given_name, r.rank, r.is_provisional FROM competition_results r
        JOIN users u ON u.id = r.user_id
        WHERE r.competition_id = :c ORDER BY r.rank, u.given_name
    """).bindparams(c=contest["id"])).all()


# ── the board ────────────────────────────────────────────────────────

class TestWhoAppearsOnTheBoard:
    """`materialize` filtered on the ATTEMPT's status and never on the ENTRY's."""

    def test_a_disqualified_entry_is_not_ranked(self, db, published, contest):
        """The whole point of disqualifying someone. `release_key` already refuses
        them a paper; leaving them on the board means the investigation changed
        nothing a competitor can see."""
        _competitor(db, published, contest, "Honest", raw=30)
        _competitor(db, published, contest, "Cheat", raw=39, status="disqualified")
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        assert [(n, r) for n, r, _ in _board(db, contest)] == [("Honest", 1)]

    def test_removing_a_cheat_promotes_everyone_below_them(self, db, published,
                                                           contest):
        """Not just absent from the list — the ranks have to close up, or second
        place stays second while standing on an empty step."""
        _competitor(db, published, contest, "Cheat", raw=39, status="disqualified")
        _competitor(db, published, contest, "Bekzod", raw=30)
        _competitor(db, published, contest, "Aziza", raw=25)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        assert [(n, r) for n, r, _ in _board(db, contest)] \
            == [("Bekzod", 1), ("Aziza", 2)]

    def test_a_disqualified_competitor_is_removed_from_a_board_they_were_on(
            self, db, published, contest):
        """The realistic sequence: the board is published, the cheating is found
        afterwards, the entry is disqualified, the board is rewritten."""
        _competitor(db, published, contest, "Cheat", raw=39)
        _competitor(db, published, contest, "Bekzod", raw=30)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        assert [n for n, _r, _p in _board(db, contest)] == ["Cheat", "Bekzod"]

        db.execute(text("""
            UPDATE competition_entries SET status = 'disqualified',
                   disqualified_reason = 'second device'
            WHERE user_id = (SELECT id FROM users WHERE given_name = 'Cheat')
        """))
        db.flush()
        service.republish(db, contest["id"], now=_now(),
                          public_notice="An entry was disqualified.")
        assert [(n, r) for n, r, _ in _board(db, contest)] == [("Bekzod", 1)]

    def test_an_unscored_attempt_is_not_ranked(self, db, published, contest):
        _competitor(db, published, contest, "Finished", raw=30)
        _competitor(db, published, contest, "Still going", raw=0, scored=False,
                    status="started")
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        assert [n for n, _r, _p in _board(db, contest)] == ["Finished"]

    def test_a_contest_nobody_finished_writes_nothing(self, db, contest):
        assert service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                                   provisional=False) == 0
        assert _board(db, contest) == []


class TestRanking:
    def test_the_provisional_flag_is_recorded(self, db, published, contest):
        """"A rank shown while people are still submitting is a snapshot, and
        saying so is what stops it being read as a result." """
        _competitor(db, published, contest, "Aziza", raw=30)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=True)
        assert _board(db, contest) == [("Aziza", 1, True)]

    def test_a_later_pass_overwrites_rather_than_duplicating(self, db, published,
                                                             contest):
        """Upserted on `(competition_id, user_id)`, so the live board can refresh
        repeatedly and the final pass overwrites it."""
        _competitor(db, published, contest, "Aziza", raw=30)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=True)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        assert _board(db, contest) == [("Aziza", 1, False)]

    def test_a_genuine_tie_shares_a_rank(self, db, published, contest):
        """"Inventing a winner between two identical papers is the one thing a
        leaderboard must never do." """
        _competitor(db, published, contest, "Aziza", raw=30, duration_ms=900_000)
        _competitor(db, published, contest, "Bekzod", raw=30, duration_ms=900_000)
        service.materialize(db, contest["id"],
                            tiebreak=["raw_score_desc", "duration_asc"], now=_now(),
                            provisional=False)
        assert [r for _n, r, _p in _board(db, contest)] == [1, 1]

    def test_the_contests_own_tiebreak_is_used(self, db, published, contest):
        """`band_desc` has no raw-score term, so the lower raw score wins when its
        band is higher — which is the whole reason the rules are data."""
        _competitor(db, published, contest, "HighBand", raw=20, band=8.5)
        _competitor(db, published, contest, "HighRaw", raw=38, band=6.0)
        service.materialize(db, contest["id"], tiebreak=["band_desc"], now=_now(),
                            provisional=False)
        assert [(n, r) for n, r, _ in _board(db, contest)] \
            == [("HighBand", 1), ("HighRaw", 2)]


class TestDurationForTheTiebreak:
    """`duration_asc` sorts ascending, so a zero is the fastest possible time."""

    def test_a_real_duration_is_recorded_in_milliseconds(self, db, published,
                                                         contest):
        _competitor(db, published, contest, "Aziza", raw=30, duration_ms=600_000)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        assert db.scalar(text("SELECT duration_ms FROM competition_results")) \
            == 600_000

    def test_the_faster_of_two_equal_scores_wins(self, db, published, contest):
        _competitor(db, published, contest, "Slow", raw=30, duration_ms=1_700_000)
        _competitor(db, published, contest, "Fast", raw=30, duration_ms=600_000)
        service.materialize(db, contest["id"],
                            tiebreak=["raw_score_desc", "duration_asc"], now=_now(),
                            provisional=False)
        assert [(n, r) for n, r, _ in _board(db, contest)] \
            == [("Fast", 1), ("Slow", 2)]

    def test_an_unknown_duration_does_not_win_the_tiebreak(self, db, published,
                                                           contest):
        """A missing timestamp used to become `0` — the best possible value — so a
        competitor whose start was never recorded beat everyone who actually
        finished quickly. Unknown must sort LAST, not first."""
        _competitor(db, published, contest, "Fast", raw=30, duration_ms=600_000)
        _competitor(db, published, contest, "Unknown", raw=30, started=False)
        service.materialize(db, contest["id"],
                            tiebreak=["raw_score_desc", "duration_asc"], now=_now(),
                            provisional=False)
        assert [(n, r) for n, r, _ in _board(db, contest)] \
            == [("Fast", 1), ("Unknown", 2)]

    def test_a_missing_submission_time_takes_nobody_else_down(self, db, published,
                                                              contest):
        """`competition_results.submitted_at` is NOT NULL and the insert passed
        whatever the attempt had, so one scored attempt with no submission time
        raised a NotNullViolation that killed the WHOLE board write. `materialize`
        runs inside `tick`, so that contest could never reach `final` — one
        malformed row, and nobody got a result.

        Such a row cannot become a result, so it is excluded rather than
        fabricated. Everyone else still gets ranked.
        """
        _competitor(db, published, contest, "Fast", raw=30, duration_ms=600_000)
        _competitor(db, published, contest, "Slow", raw=25)
        _competitor(db, published, contest, "Broken", raw=39, submitted=False)
        written = service.materialize(db, contest["id"],
                                      tiebreak=["raw_score_desc", "duration_asc"],
                                      now=_now(), provisional=False)
        assert written == 2
        assert [(n, r) for n, r, _ in _board(db, contest)] \
            == [("Fast", 1), ("Slow", 2)]


# ── republish ────────────────────────────────────────────────────────

class TestRepublish:
    """"The only path by which a published ranking changes. Every affected
    competitor is notified — if a podium moved, the people on it are told, and
    they are told the same words the admin wrote down when they decided." """

    @pytest.fixture
    def published_board(self, db, published, contest):
        first = _competitor(db, published, contest, "Aziza", raw=38)
        second = _competitor(db, published, contest, "Bekzod", raw=30)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        return {"first": first, "second": second}

    def _regrade(self, db, user_id: int, raw: float) -> None:
        """A key fix landed: a new current score run for this competitor."""
        db.execute(text("""
            UPDATE score_runs SET is_current = false
            WHERE attempt_id IN (SELECT id FROM attempts WHERE user_id = :u)
        """).bindparams(u=user_id))
        db.execute(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, band, is_current)
            SELECT id, 'regrade_key', '1.0.0', '{}'::jsonb, :raw, 40, 7.0, true
            FROM attempts WHERE user_id = :u
        """).bindparams(u=user_id, raw=raw))
        db.flush()

    def test_the_board_is_rewritten_from_the_new_runs(self, db, contest,
                                                      published_board):
        self._regrade(db, published_board["second"]["id"], raw=40)
        service.republish(db, contest["id"], now=_now(),
                          public_notice="Question 12 was re-marked.")
        assert [(n, r) for n, r, _ in _board(db, contest)] \
            == [("Bekzod", 1), ("Aziza", 2)]

    def test_everyone_whose_rank_moved_is_notified(self, db, contest,
                                                   published_board):
        self._regrade(db, published_board["second"]["id"], raw=40)
        service.republish(db, contest["id"], now=_now(),
                          public_notice="Question 12 was re-marked.")
        assert db.scalar(text("""
            SELECT count(*) FROM notifications
            WHERE template = 'competition.rank_changed'
        """)) == 2

    def test_a_competitor_whose_rank_did_not_move_is_left_alone(self, db, published,
                                                               contest,
                                                               published_board):
        """"Notifying on every raw-score wobble trains students to ignore the
        channel you need for what matters." A third place that stays third has
        nothing to be told."""
        third = _competitor(db, published, contest, "Nodira", raw=20)
        service.materialize(db, contest["id"], tiebreak=None, now=_now(),
                            provisional=False)
        self._regrade(db, published_board["second"]["id"], raw=40)
        service.republish(db, contest["id"], now=_now(), public_notice="Re-marked.")
        assert not db.scalar(text("""
            SELECT count(*) FROM notifications WHERE user_id = :u
        """).bindparams(u=third["id"]))

    def test_the_notice_the_admin_wrote_is_what_the_student_reads(
            self, db, contest, published_board):
        self._regrade(db, published_board["second"]["id"], raw=40)
        service.republish(db, contest["id"], now=_now(),
                          public_notice="Question 12 accepted 'bicycle'.")
        params = db.execute(text("""
            SELECT params FROM notifications WHERE template = 'competition.rank_changed'
            ORDER BY id LIMIT 1
        """)).scalar()
        assert params["notice"] == "Question 12 accepted 'bicycle'."

    def test_the_notification_carries_both_ranks(self, db, contest, published_board):
        """"You were 2nd, you are now 1st" is the message. Either half alone is
        not."""
        self._regrade(db, published_board["second"]["id"], raw=40)
        service.republish(db, contest["id"], now=_now(), public_notice="Re-marked.")
        moved = db.execute(text("""
            SELECT params FROM notifications
            WHERE user_id = :u AND template = 'competition.rank_changed'
        """).bindparams(u=published_board["second"]["id"])).scalar()
        assert moved["old_rank"] == 2
        assert moved["new_rank"] == 1

    def test_republishing_twice_does_not_notify_twice(self, db, contest,
                                                      published_board):
        """"Stops a retried regrade job from notifying the same student twice."
        The actor has `max_retries=1`, so this runs again on any transient
        failure."""
        self._regrade(db, published_board["second"]["id"], raw=40)
        for _ in range(2):
            service.republish(db, contest["id"], now=_now(),
                              public_notice="Re-marked.")
        assert db.scalar(text("""
            SELECT count(*) FROM notifications
            WHERE template = 'competition.rank_changed'
        """)) == 2

    def test_it_returns_how_many_rows_it_wrote(self, db, contest, published_board):
        assert service.republish(db, contest["id"], now=_now(),
                                 public_notice="Re-marked.") == 2

    def test_the_republished_board_is_not_provisional(self, db, contest,
                                                      published_board):
        self._regrade(db, published_board["second"]["id"], raw=40)
        service.republish(db, contest["id"], now=_now(), public_notice="Re-marked.")
        assert all(not provisional for _n, _r, provisional in _board(db, contest))


# ── the state machine, driven ────────────────────────────────────────

class TestTick:
    def _contest(self, db, published, *, status, starts_in, ends_in):
        return db.execute(text("""
            INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, status,
                                      visibility, created_by)
            VALUES (:o, :tv, 'Friday',
                    now() + make_interval(secs => :starts) - interval '120 seconds',
                    now() + make_interval(secs => :starts),
                    now() + make_interval(secs => :ends), 1800, :st, 'org', :by)
            RETURNING id, xid
        """).bindparams(o=published["org"].id, tv=published["test_version"].id,
                        starts=starts_in, ends=ends_in, st=status,
                        by=published["author"].id)).mappings().one()

    def _status(self, db, contest) -> str:
        return db.scalar(text("SELECT status FROM competitions WHERE id = :c")
                         .bindparams(c=contest["id"]))

    def test_one_step_per_pass(self, db, published):
        """"A tick that has been down for an hour walks the machine forward one
        state per pass rather than jumping to the end, so every transition's side
        effects still run in order." """
        contest = self._contest(db, published, status="scheduled",
                                starts_in=-7200, ends_in=-3600)
        for expected in ("registration", "lobby", "live", "grading", "final"):
            service.tick(db, _now())
            db.flush()
            assert self._status(db, contest) == expected

    def test_the_move_is_reported_for_the_log(self, db, published):
        contest = self._contest(db, published, status="scheduled", starts_in=3600,
                                ends_in=7200)
        moved = service.tick(db, _now())
        assert {"competition_xid": str(contest["xid"]), "from": "scheduled",
                "to": "registration"} in moved

    def test_a_contest_where_it_should_be_does_not_move(self, db, published):
        self._contest(db, published, status="registration", starts_in=3600,
                      ends_in=7200)
        assert service.tick(db, _now()) == []

    def test_going_live_marks_the_no_shows(self, db, published):
        """"Registered, never prefetched, contest started. Recorded rather than
        deleted: a centre asking 'who missed the mock' needs the row." """
        contest = self._contest(db, published, status="lobby", starts_in=-60,
                                ends_in=3600)
        absent = _competitor(db, published, contest, "Absent", raw=0, scored=False,
                             status="registered")
        service.tick(db, _now())
        db.flush()
        assert db.scalar(text("""
            SELECT status FROM competition_entries WHERE user_id = :u
        """).bindparams(u=absent["id"])) == "no_show"

    def test_a_prefetched_entry_is_not_a_no_show(self, db, published):
        contest = self._contest(db, published, status="lobby", starts_in=-60,
                                ends_in=3600)
        present = _competitor(db, published, contest, "Present", raw=0, scored=False,
                              status="prefetched")
        service.tick(db, _now())
        db.flush()
        assert db.scalar(text("""
            SELECT status FROM competition_entries WHERE user_id = :u
        """).bindparams(u=present["id"])) == "prefetched"

    def test_grading_closes_the_entries_that_finished(self, db, published):
        contest = self._contest(db, published, status="live", starts_in=-7200,
                                ends_in=-60)
        done = _competitor(db, published, contest, "Done", raw=30, status="started")
        service.tick(db, _now())
        db.flush()
        assert self._status(db, contest) == "grading"
        assert db.scalar(text("""
            SELECT status FROM competition_entries WHERE user_id = :u
        """).bindparams(u=done["id"])) == "submitted"

    def test_grading_waits_for_a_straggler(self, db, published):
        """"Publishing a leaderboard that is still missing rows is worse than
        publishing it a minute late." """
        contest = self._contest(db, published, status="grading", starts_in=-7200,
                                ends_in=-60)
        _competitor(db, published, contest, "Straggler", raw=0, scored=False,
                    status="started")
        service.tick(db, _now())
        db.flush()
        assert self._status(db, contest) == "grading"

    def test_grading_gives_up_after_the_patience_window(self, db, published):
        """"An attempt that is not scored 10 minutes after the contest ended is
        not coming." A board that never appears is the worse failure."""
        contest = self._contest(db, published, status="grading", starts_in=-7200,
                                ends_in=-1800)
        _competitor(db, published, contest, "Straggler", raw=0, scored=False,
                    status="started")
        service.tick(db, _now())
        db.flush()
        assert self._status(db, contest) == "final"

    def test_going_final_publishes_the_board(self, db, published):
        contest = self._contest(db, published, status="grading", starts_in=-7200,
                                ends_in=-60)
        _competitor(db, published, contest, "Aziza", raw=30, status="submitted")
        service.tick(db, _now())
        db.flush()
        assert self._status(db, contest) == "final"
        assert _board(db, contest) == [("Aziza", 1, False)]

    def test_a_final_contest_is_left_alone(self, db, published):
        self._contest(db, published, status="final", starts_in=-7200, ends_in=-3600)
        assert service.tick(db, _now()) == []

    def test_a_cancelled_contest_is_left_alone(self, db, published):
        self._contest(db, published, status="cancelled", starts_in=-7200,
                      ends_in=-3600)
        assert service.tick(db, _now()) == []
