"""Ranking and the state machine. Pure, so both run in milliseconds.

A leaderboard is the most-screenshotted surface in the product and the one people
argue about. Two properties carry that weight:

  * a genuine tie shares a rank — inventing a winner between two identical papers
    is the one thing a contest must never do;
  * the tiebreak array is data, evaluated left to right, and an unknown rule
    raises rather than being ignored.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.modules.competitions.ranking import (
    Entry, UnknownTiebreak, podium_changes, rank,
)
from app.modules.competitions.schedule import (
    GRADING_PATIENCE, Timing, next_status, registration_open,
)

T0 = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC)


def entry(name: str, score: str, *, duration: int = 60_000,
          submitted: int = 0, band: str | None = None) -> Entry:
    return Entry(user_xid=name, attempt_xid=f"a-{name}", raw_score=Decimal(score),
                 band=Decimal(band) if band else None, duration_ms=duration,
                 submitted_at=T0 + dt.timedelta(seconds=submitted))


def order(ranked) -> list[tuple[str, int]]:
    return [(r.entry.user_xid, r.rank) for r in ranked]


class TestRanking:
    def test_highest_score_first(self):
        assert order(rank([entry("b", "30"), entry("a", "38"), entry("c", "22")])) == [
            ("a", 1), ("b", 2), ("c", 3)]

    def test_equal_scores_are_broken_by_speed(self):
        ranked = rank([entry("slow", "30", duration=900_000),
                       entry("fast", "30", duration=600_000)])
        assert order(ranked) == [("fast", 1), ("slow", 2)]

    def test_equal_score_and_speed_are_broken_by_submission_time(self):
        ranked = rank([entry("second", "30", submitted=10),
                       entry("first", "30", submitted=5)])
        assert order(ranked) == [("first", 1), ("second", 2)]

    def test_a_genuine_tie_shares_a_rank_and_the_next_one_skips(self):
        """1, 2, 2, 4 — standard competition ranking.

        Two identical papers separated by row id would give one of them second
        place for no reason they could be told, and they would be right to
        complain.
        """
        ranked = rank([entry("a", "38"), entry("b", "30"), entry("c", "30"),
                       entry("d", "22")])
        assert order(ranked) == [("a", 1), ("b", 2), ("c", 2), ("d", 4)]

    def test_three_way_tie(self):
        ranked = rank([entry(x, "30") for x in "abc"] + [entry("d", "20")])
        assert sorted(r.rank for r in ranked) == [1, 1, 1, 4]

    def test_a_custom_tiebreak_is_honoured(self):
        """A centre running "highest score, then whoever finished last" is odd,
        but it is their contest and it needs no deploy."""
        ranked = rank([entry("quick", "30", duration=100),
                       entry("thorough", "30", duration=900_000)],
                      ["raw_score_desc", "duration_desc"])
        assert order(ranked) == [("thorough", 1), ("quick", 2)]

    def test_an_unknown_rule_raises(self):
        with pytest.raises(UnknownTiebreak) as exc:
            rank([entry("a", "1")], ["raw_score_desc", "by_vibes"])
        assert "by_vibes" in str(exc.value)

    def test_the_default_applies_when_none_is_given(self):
        assert order(rank([entry("a", "10"), entry("b", "20")], None)) == [
            ("b", 1), ("a", 2)]

    def test_tiebreak_keys_are_materialized_and_sort_the_same_way(self):
        """The durable board renders from `tiebreak_key`, so it must order
        identically to the comparator that produced it — years later, without
        re-running any code."""
        ranked = rank([entry("a", "38"), entry("b", "30", duration=1000),
                       entry("c", "30", duration=2000)])
        by_key = sorted(ranked, key=lambda r: r.tiebreak_key)
        assert [r.entry.user_xid for r in by_key] == ["a", "b", "c"]

    def test_an_empty_board(self):
        assert rank([]) == []


class TestPodiumChanges:
    def test_no_movement_is_zero(self):
        board = rank([entry("a", "38"), entry("b", "30"), entry("c", "22")])
        assert podium_changes(board, board) == 0

    def test_a_swapped_first_and_second_counts_two(self):
        before = rank([entry("a", "38"), entry("b", "30"), entry("c", "22")])
        after = rank([entry("a", "30"), entry("b", "38"), entry("c", "22")])
        assert podium_changes(before, after) == 2

    def test_movement_outside_the_podium_does_not_count(self):
        """The number an admin needs: twelve ranks moving in the middle of the
        field is noise, one podium change is not."""
        before = rank([entry(x, s) for x, s in
                       [("a", "38"), ("b", "30"), ("c", "22"), ("d", "20"), ("e", "18")]])
        after = rank([entry(x, s) for x, s in
                      [("a", "38"), ("b", "30"), ("c", "22"), ("d", "10"), ("e", "18")]])
        assert podium_changes(before, after) == 0


class TestStateMachine:
    def timing(self, status: str, **kw) -> Timing:
        return Timing(status=status,
                      lobby_opens_at=kw.get("lobby", T0 - dt.timedelta(seconds=120)),
                      starts_at=kw.get("starts", T0),
                      ends_at=kw.get("ends", T0 + dt.timedelta(minutes=30)),
                      registration_closes_at=kw.get("reg_close"))

    def test_scheduled_opens_registration_immediately(self):
        assert next_status(self.timing("scheduled"), T0 - dt.timedelta(days=1)) \
            == "registration"

    def test_registration_waits_for_the_lobby(self):
        t = self.timing("registration")
        assert next_status(t, T0 - dt.timedelta(seconds=300)) is None
        assert next_status(t, T0 - dt.timedelta(seconds=119)) == "lobby"

    def test_the_lobby_waits_for_the_start(self):
        t = self.timing("lobby")
        assert next_status(t, T0 - dt.timedelta(seconds=1)) is None
        assert next_status(t, T0) == "live"

    def test_live_ends_at_the_deadline(self):
        t = self.timing("live")
        assert next_status(t, T0 + dt.timedelta(minutes=29)) is None
        assert next_status(t, T0 + dt.timedelta(minutes=30)) == "grading"

    def test_grading_waits_for_every_attempt_to_be_scored(self):
        t = self.timing("grading")
        moment = T0 + dt.timedelta(minutes=31)
        assert next_status(t, moment, entries_pending=3) is None
        assert next_status(t, moment, entries_pending=0) == "final"

    def test_grading_gives_up_on_stragglers_eventually(self):
        """A board that never appears is worse than one published without the
        two people whose attempts died."""
        t = self.timing("grading")
        late = T0 + dt.timedelta(minutes=30) + GRADING_PATIENCE
        assert next_status(t, late, entries_pending=3) == "final"

    def test_terminal_states_never_move(self):
        for status in ("final", "cancelled"):
            assert next_status(self.timing(status), T0 + dt.timedelta(days=9)) is None

    def test_the_machine_advances_one_step_per_tick(self):
        """A tick that has been down for an hour walks forward one state at a
        time, so every transition's side effects still run in order."""
        status = "scheduled"
        very_late = T0 + dt.timedelta(hours=1)
        seen = [status]
        for _ in range(10):
            nxt = next_status(self.timing(status, reg_close=None), very_late,
                              entries_pending=0)
            if nxt is None:
                break
            status = nxt
            seen.append(status)
        assert seen == ["scheduled", "registration", "lobby", "live", "grading", "final"]

    def test_registration_closes_at_its_deadline(self):
        t = self.timing("registration", reg_close=T0 - dt.timedelta(minutes=10))
        assert registration_open(t, T0 - dt.timedelta(minutes=20)) is True
        assert registration_open(t, T0 - dt.timedelta(minutes=5)) is False

    def test_registration_never_opens_after_the_start(self):
        assert registration_open(self.timing("registration"),
                                 T0 + dt.timedelta(seconds=1)) is False
