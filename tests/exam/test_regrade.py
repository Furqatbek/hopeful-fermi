"""The regrade path.

The scenario this exists for: a teacher's key said `bicycle`, thirty-eight
students wrote `bike`, and the mistake is found in June for a test sat in March. Fixing it must be safe, previewable, auditable — and must not silently
re-rank a competition that already awarded prizes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.exam import regrade
from app.modules.exam.scoring import AttemptInput, BandMap, ItemInput, KeyVersion, score_attempt
from app.modules.qtypes.schemas import GroupRules, WordLimit
from app.platform.errors import Conflict

BAND_MAP = BandMap(
    xid="bm-1", max_raw=3,
    rows=((0, 0, Decimal("4.0")), (1, 1, Decimal("5.0")),
          (2, 2, Decimal("6.0")), (3, 3, Decimal("7.0"))),
)
GROUP = GroupRules(word_limit=WordLimit(max_words=2, allow_number=True))


def item(n: int) -> ItemInput:
    return ItemInput(
        question_xid=f"q-{n}", question_version_xid=f"qv-{n}",
        type_key="sentence_completion", type_version=1,
        payload={"text": f"Answer {n} is {{{{s1}}}}.", "slots": ["s1"]},
        slot_keys=("s1",), skill="reading", group=GROUP,
    )


ITEMS = (item(1), item(2), item(3))

# The wrong key: a legitimate synonym is missing. Deliberately NOT a spelling or
# number variant — the tolerance layer already handles those, so a key that only
# accepted `carpark` would never need regrading (see TestToleranceCatchesItFirst).
OLD_KEYS = {
    "qv-1": KeyVersion(xid="k1-v1", key={"slots": {"s1": {"accept": ["bicycle"]}}}),
    "qv-2": KeyVersion(xid="k2-v1", key={"slots": {"s1": {"accept": ["library"]}}}),
    "qv-3": KeyVersion(xid="k3-v1", key={"slots": {"s1": {"accept": ["museum"]}}}),
}
# The fix: the synonyms students actually wrote are added.
NEW_KEYS = {
    **OLD_KEYS,
    "qv-1": KeyVersion(xid="k1-v2",
                       key={"slots": {"s1": {"accept": ["bicycle", "bike", "cycle"]}}}),
}


def attempt(xid: str, answers: dict[str, str], *, competition=None) -> AttemptInput:
    return AttemptInput(
        attempt_xid=xid, user_xid=f"u-{xid}", items=ITEMS,
        responses={qv: {"slots": {"s1": value}} for qv, value in answers.items()},
        competition_xid=competition,
    )


def run(scorer, a: AttemptInput, keys):
    return score_attempt(a, keys, scorer, BAND_MAP)


class TestScoreIsAPureFunctionOfItsInputs:
    def test_same_inputs_reproduce_the_same_run(self, scorer):
        a = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "museum"})
        first, second = run(scorer, a, OLD_KEYS), run(scorer, a, OLD_KEYS)
        assert (first.raw_score, first.band) == (second.raw_score, second.band)

    def test_run_records_exactly_which_key_versions_produced_it(self, scorer):
        """Without this a score cannot be reproduced or explained years later."""
        a = attempt("a1", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"})
        assert run(scorer, a, OLD_KEYS).key_versions == {
            "qv-1": "k1-v1", "qv-2": "k2-v1", "qv-3": "k3-v1"}

    def test_run_records_the_engine_version(self, scorer):
        a = attempt("a1", {"qv-2": "library"})
        assert run(scorer, a, OLD_KEYS).engine_version == scorer.engine_version

    def test_a_missing_key_voids_the_item_rather_than_failing_the_student(self, scorer):
        a = attempt("a1", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"})
        partial = {k: v for k, v in OLD_KEYS.items() if k != "qv-3"}
        result = run(scorer, a, partial)
        assert result.raw_score == 2 and result.max_raw == 3


class TestDryRunChangesNothing:
    def test_plan_reports_impact_without_touching_scores(self, scorer):
        attempts = [attempt(f"a{i}", {"qv-1": "bike", "qv-2": "library",
                                      "qv-3": "museum"}) for i in range(5)]
        previous = {a.attempt_xid: run(scorer, a, OLD_KEYS) for a in attempts}
        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)

        assert impact.attempts_total == 5
        assert impact.scores_changed == 5
        assert impact.improved == 5 and impact.worsened == 0
        # The stored runs are untouched: a plan is a report, not a write.
        assert all(r.raw_score == 2 for r in previous.values())

    def test_unaffected_attempts_are_counted_but_not_changed(self, scorer):
        """A student who wrote `bicycle` was always right and stays right."""
        a = attempt("a1", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"})
        previous = {"a1": run(scorer, a, OLD_KEYS)}
        impact = regrade.plan([a], previous, NEW_KEYS, scorer, BAND_MAP)
        assert impact.attempts_total == 1 and impact.scores_changed == 0

    def test_band_changes_are_counted_separately_from_score_changes(self, scorer):
        """Some mark changes cross a band boundary and some do not; only the
        first kind is worth telling a student about."""
        narrow = BandMap(xid="bm-2", max_raw=3,
                         rows=((0, 1, Decimal("5.0")), (2, 3, Decimal("6.0"))))
        a = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "wrong"})
        previous = {"a1": score_attempt(a, OLD_KEYS, scorer, narrow)}
        impact = regrade.plan([a], previous, NEW_KEYS, scorer, narrow)
        assert impact.scores_changed == 1     # 1 -> 2
        assert impact.bands_changed == 1      # 5.0 -> 6.0

        a2 = attempt("a2", {"qv-1": "bike", "qv-2": "wrong", "qv-3": "wrong"})
        previous2 = {"a2": score_attempt(a2, OLD_KEYS, scorer, narrow)}
        impact2 = regrade.plan([a2], previous2, NEW_KEYS, scorer, narrow)
        assert impact2.scores_changed == 1    # 0 -> 1
        assert impact2.bands_changed == 0     # 5.0 -> 5.0, nothing to announce


class TestApply:
    def test_apply_produces_a_new_run_per_changed_attempt(self, scorer):
        attempts = [attempt(f"a{i}", {"qv-1": "bike"}) for i in range(3)]
        previous = {a.attempt_xid: run(scorer, a, OLD_KEYS) for a in attempts}
        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)
        runs = regrade.apply(impact)
        assert len(runs) == 3
        assert all(r.reason == "regrade_key" for r in runs)
        assert all(r.key_versions["qv-1"] == "k1-v2" for r in runs)

    def test_previous_runs_survive_so_score_history_is_preserved(self, scorer):
        a = attempt("a1", {"qv-1": "bike"})
        original = run(scorer, a, OLD_KEYS)
        impact = regrade.plan([a], {"a1": original}, NEW_KEYS, scorer, BAND_MAP)
        new_run = regrade.apply(impact)[0]
        assert original.raw_score == 0 and new_run.raw_score == 1
        assert original.key_versions["qv-1"] == "k1-v1"

    def test_unchanged_attempts_do_not_get_a_pointless_new_run(self, scorer):
        a = attempt("a1", {"qv-1": "bicycle"})
        impact = regrade.plan([a], {"a1": run(scorer, a, OLD_KEYS)}, NEW_KEYS, scorer, BAND_MAP)
        assert regrade.apply(impact) == []


class TestCompetitionGovernance:
    """ADR-0001 §8.4. A finished leaderboard does not re-rank itself."""

    def _contest(self, scorer):
        attempts = [
            attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "museum"},
                    competition="c-1"),   # 2 -> 3, would take first place
            attempt("a2", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"},
                    competition="c-1"),   # 3, already first
            attempt("a3", {"qv-1": "wrong", "qv-2": "library", "qv-3": "wrong"},
                    competition="c-1"),   # 1, unaffected
        ]
        previous = {a.attempt_xid: run(scorer, a, OLD_KEYS) for a in attempts}
        return attempts, previous

    def test_impact_reports_rank_and_podium_movement(self, scorer):
        attempts, previous = self._contest(scorer)
        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)
        assert len(impact.competition_impact) == 1
        c = impact.competition_impact[0]
        assert c.competition_xid == "c-1"
        assert c.attempts == 3
        assert c.rank_changes > 0
        assert c.decision_required is True

    def test_apply_is_refused_until_an_admin_decides(self, scorer):
        attempts, previous = self._contest(scorer)
        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)
        assert impact.blocked_on_decision is True
        with pytest.raises(Conflict) as exc:
            regrade.apply(impact)
        assert exc.value.extra["competitions"] == ["c-1"]
        assert exc.value.status == 409

    def test_apply_proceeds_once_the_decision_is_recorded(self, scorer):
        attempts, previous = self._contest(scorer)
        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)
        runs = regrade.apply(impact, competition_decisions={"c-1"})
        assert len(runs) == 1

    def test_practice_attempts_regrade_without_any_decision(self, scorer):
        """Only competitions need governance. Ordinary practice just gets fixed."""
        a = attempt("a1", {"qv-1": "bike"})
        impact = regrade.plan([a], {"a1": run(scorer, a, OLD_KEYS)}, NEW_KEYS, scorer, BAND_MAP)
        assert impact.blocked_on_decision is False
        assert len(regrade.apply(impact)) == 1

    def test_a_key_fix_that_moves_nobody_needs_no_decision(self, scorer):
        attempts = [attempt("a1", {"qv-2": "library"}, competition="c-1")]
        previous = {"a1": run(scorer, attempts[0], OLD_KEYS)}
        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)
        assert impact.blocked_on_decision is False


class TestNotifications:
    def test_only_band_changes_are_notified(self, scorer):
        narrow = BandMap(xid="bm-2", max_raw=3,
                         rows=((0, 1, Decimal("5.0")), (2, 3, Decimal("6.0"))))
        moved = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "wrong"})
        still = attempt("a2", {"qv-1": "bike", "qv-2": "wrong", "qv-3": "wrong"})
        previous = {a.attempt_xid: score_attempt(a, OLD_KEYS, scorer, narrow)
                    for a in (moved, still)}
        impact = regrade.plan([moved, still], previous, NEW_KEYS, scorer, narrow)
        notices = regrade.notifications_for(impact)
        assert [n["user_xid"] for n in notices] == ["u-a1"]
        assert notices[0]["params"]["direction"] == "up"

    def test_notifications_carry_a_dedupe_key(self, scorer):
        """A retried regrade job must not message the same student twice."""
        a = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "museum"})
        previous = {"a1": run(scorer, a, OLD_KEYS)}
        impact = regrade.plan([a], previous, NEW_KEYS, scorer, BAND_MAP)
        keys = [n["dedupe_key"] for n in regrade.notifications_for(impact)]
        assert keys and len(keys) == len(set(keys))

    def test_a_band_map_only_regrade_reports_the_band_direction(self, scorer):
        """The key is unchanged and the curve is retuned upward, so every raw
        stays put and every band rises. `direction` read the RAW score — which
        did not move — so each of these students was told their band went
        down. The notice is about the band, and its direction reads the band."""
        generous = BandMap(xid="bm-3", max_raw=3,
                           rows=((0, 0, Decimal("5.0")), (1, 1, Decimal("6.0")),
                                 (2, 2, Decimal("7.0")), (3, 3, Decimal("8.0"))))
        a = attempt("a1", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "wrong"})
        previous = {"a1": score_attempt(a, OLD_KEYS, scorer, BAND_MAP)}   # 2 -> 6.0
        impact = regrade.plan([a], previous, OLD_KEYS, scorer, generous)  # 2 -> 7.0
        assert impact.scores_changed == 0 and impact.bands_changed == 1
        notices = regrade.notifications_for(impact)
        assert notices[0]["params"]["old_band"] == 6.0
        assert notices[0]["params"]["new_band"] == 7.0
        assert notices[0]["params"]["direction"] == "up"

    def test_a_band_map_that_lowers_the_band_says_so(self, scorer):
        stingy = BandMap(xid="bm-4", max_raw=3,
                         rows=((0, 1, Decimal("4.0")), (2, 3, Decimal("5.0"))))
        a = attempt("a1", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "wrong"})
        previous = {"a1": score_attempt(a, OLD_KEYS, scorer, BAND_MAP)}   # 2 -> 6.0
        impact = regrade.plan([a], previous, OLD_KEYS, scorer, stingy)    # 2 -> 5.0
        assert regrade.notifications_for(impact)[0]["params"]["direction"] == "down"


class TestTheReportNamesTheStudents:
    """The per-attempt deltas were computed by `plan` and dropped by `as_dict`,
    so the persisted report — and the console's confirm dialog — could say how
    MANY bands change and never whose. An admin deciding whether to apply a
    regrade to a class wants the names."""

    def test_only_the_attempts_whose_band_moves_are_listed(self, scorer):
        narrow = BandMap(xid="bm-2", max_raw=3,
                         rows=((0, 1, Decimal("5.0")), (2, 3, Decimal("6.0"))))
        moved = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "wrong"})
        still = attempt("a2", {"qv-1": "bike", "qv-2": "wrong", "qv-3": "wrong"})
        previous = {a.attempt_xid: score_attempt(a, OLD_KEYS, scorer, narrow)
                    for a in (moved, still)}
        report = regrade.plan([moved, still], previous, NEW_KEYS, scorer, narrow).as_dict()
        # `still` gains a mark (0 -> 1) and stays at 5.0: a score change, not a
        # band change, and the list is band changes only.
        assert report["scores_changed"] == 2 and report["bands_changed"] == 1
        assert report["changes"] == [{
            "attempt_xid": "a1", "user_id": "u-a1",
            "old_raw": 1.0, "new_raw": 2.0, "old_band": 5.0, "new_band": 6.0,
        }]
        assert report["changes_truncated"] is False

    def test_the_list_is_bounded(self, scorer, monkeypatch):
        """Bounded by band changes, and a job past the bound says so rather
        than silently listing the first N as if they were all of them."""
        monkeypatch.setattr(regrade.RegradeImpact, "CHANGES_LIMIT", 2)
        attempts = [attempt(f"a{i}", {"qv-1": "bike", "qv-2": "library",
                                      "qv-3": "museum"}) for i in range(3)]
        previous = {a.attempt_xid: run(scorer, a, OLD_KEYS) for a in attempts}
        report = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP).as_dict()
        assert report["bands_changed"] == 3
        assert len(report["changes"]) == 2
        assert report["changes_truncated"] is True

    def test_the_report_survives_json(self, scorer):
        """`job.report` is JSONB. A `Decimal` anywhere in here fails at flush,
        inside the worker, after the plan was computed."""
        import json

        a = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "museum"})
        impact = regrade.plan([a], {"a1": run(scorer, a, OLD_KEYS)}, NEW_KEYS,
                              scorer, BAND_MAP)
        json.dumps(impact.as_dict())


class TestTheRealisticScenario:
    def test_thirty_eight_students_wrote_bike(self, scorer):
        """End to end, at the scale the failure actually happens."""
        wrote_bike = [
            attempt(f"bk-{i}", {"qv-1": "bike", "qv-2": "library", "qv-3": "museum"})
            for i in range(38)
        ]
        wrote_bicycle = [
            attempt(f"ok-{i}", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"})
            for i in range(12)
        ]
        attempts = wrote_bike + wrote_bicycle
        previous = {a.attempt_xid: run(scorer, a, OLD_KEYS) for a in attempts}

        # Under the bad key the 38 lost a mark they had earned.
        assert previous["bk-0"].raw_score == 2
        assert previous["ok-0"].raw_score == 3

        impact = regrade.plan(attempts, previous, NEW_KEYS, scorer, BAND_MAP)
        assert impact.attempts_total == 50
        assert impact.scores_changed == 38
        assert impact.worsened == 0            # a key fix must never cost a student marks
        assert impact.bands_changed == 38
        assert impact.students_to_notify == 38

        runs = regrade.apply(impact)
        assert len(runs) == 38
        assert all(r.raw_score == 3 and r.band == Decimal("7.0") for r in runs)


class TestABandMapThatDoesNotCoverTheScore:
    """`band_for` returns None when a raw score falls outside every row.

    Found by the coverage gate: it was the last unexecuted line in
    `scoring.py`, and it is not a defensive `else`. A band map is authored
    content — a centre-admin fills in a table — and a table with a gap in it, or
    one whose `max_raw` no longer matches a paper that has since grown a
    question, produces a student with a raw score and no band. What the engine
    does then is a product decision, so it should be pinned rather than
    discovered.
    """

    GAPPED = BandMap(xid="bm-gap", max_raw=3,
                     rows=((0, 0, Decimal("4.0")), (3, 3, Decimal("7.0"))))

    def test_a_score_in_the_gap_gets_no_band(self, scorer):
        assert self.GAPPED.band_for(Decimal(1)) is None
        assert self.GAPPED.band_for(Decimal(2)) is None

    def test_a_score_above_the_table_gets_no_band(self):
        assert BAND_MAP.band_for(Decimal(4)) is None

    def test_a_negative_score_gets_no_band(self):
        assert BAND_MAP.band_for(Decimal(-1)) is None

    def test_the_run_still_scores_and_simply_carries_no_band(self, scorer):
        """It must not raise, and it did.

        `per_section` computed `float(band_map.band_for(...)) if band_map else
        None` — a guard against a MISSING band map, not against one that returns
        None. So `float(None)` raised TypeError inside scoring and the student's
        submission failed outright. A raw score with no band is recoverable: fix
        the table, regrade, and the engine is pure so the mark is reproducible.
        A crash at submit is not.
        """
        a = attempt("a1", {"qv-1": "bike", "qv-2": "wrong", "qv-3": "wrong"})
        run = score_attempt(a, NEW_KEYS, scorer, self.GAPPED)
        assert run.raw_score == 1
        assert run.band is None
        assert run.per_section["reading"]["raw"] == 1.0
        assert run.per_section["reading"]["band"] is None

    def test_rounding_is_half_up_at_the_boundary(self, scorer):
        """`band_for` rounds to a whole mark before looking up. Half a mark is
        reachable — a two-slot item can award 0.5 — and rounding down at .5
        would cost a band at every boundary in the table."""
        assert BAND_MAP.band_for(Decimal("1.5")) == Decimal("6.0")
        assert BAND_MAP.band_for(Decimal("1.4")) == Decimal("5.0")

    def test_an_attempt_with_no_band_map_scores_raw_marks_only(self, scorer):
        """A test can legitimately have no band map: a practice set, a
        single-section drill, or a paper whose centre has not authored one yet.
        Raw marks are still authoritative; there is simply nothing to convert
        them into, per section or overall."""
        a = attempt("a1", {"qv-1": "bike", "qv-2": "library", "qv-3": "museum"})
        run = score_attempt(a, NEW_KEYS, scorer, None)
        assert run.raw_score == 3
        assert run.band is None
        assert run.band_map_xid is None
        assert run.per_section["reading"]["raw"] == 3.0
        assert run.per_section["reading"]["band"] is None


class TestASectionBandIsOnlyTheWholePapersBand:
    """One band map per test version, calibrated on the whole paper's `max_raw`.

    `per_section` applied it to each section's PARTIAL raw, so on a
    Reading + Listening paper a student with every Reading mark was shown the
    band for "3 of 6" under Reading, beside a correct headline band. A partial
    raw looked up on the whole-paper table is a wrong band, not a rough one,
    and the student result screen rendered it. Until a per-skill map exists a
    multi-skill paper shows no section band — the screen already renders `—`
    for null — and a single-skill paper's one section is the paper.
    """

    @staticmethod
    def _listening(n: int) -> ItemInput:
        return ItemInput(
            question_xid=f"lq-{n}", question_version_xid=f"lqv-{n}",
            type_key="sentence_completion", type_version=1,
            payload={"text": f"Heard {n} is {{{{s1}}}}.", "slots": ["s1"]},
            slot_keys=("s1",), skill="listening", group=GROUP,
        )

    def test_a_two_skill_paper_has_a_headline_band_and_no_section_bands(self, scorer):
        listening = tuple(self._listening(n) for n in (1, 2, 3))
        keys = {**OLD_KEYS, **{f"lqv-{n}": KeyVersion(
            xid=f"lk{n}", key={"slots": {"s1": {"accept": ["yes"]}}}) for n in (1, 2, 3)}}
        six = BandMap(xid="bm-6", max_raw=6,
                      rows=((0, 2, Decimal("4.0")), (3, 4, Decimal("5.0")),
                            (5, 6, Decimal("6.0"))))
        a = AttemptInput(
            attempt_xid="two", user_xid="u-two", items=ITEMS + listening,
            responses={"qv-1": {"slots": {"s1": "bicycle"}},
                       "qv-2": {"slots": {"s1": "library"}},
                       "qv-3": {"slots": {"s1": "museum"}},
                       "lqv-1": {"slots": {"s1": "yes"}}})
        result = score_attempt(a, keys, scorer, six)
        assert result.raw_score == 4 and result.band == Decimal("5.0")
        assert result.per_section["reading"]["raw"] == 3.0
        assert result.per_section["listening"]["raw"] == 1.0
        # 3 of 6 on the six-mark table would read 5.0 for Reading, which is not
        # the band for a full-marks Reading section on anybody's scale.
        assert result.per_section["reading"]["band"] is None
        assert result.per_section["listening"]["band"] is None

    def test_a_one_skill_papers_section_band_is_the_headline_band(self, scorer):
        a = attempt("one", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "wrong"})
        result = score_attempt(a, OLD_KEYS, scorer, BAND_MAP)
        assert result.band == Decimal("6.0")
        assert result.per_section["reading"]["band"] == 6.0

    def test_a_void_item_still_belongs_to_its_section(self, scorer):
        """An unkeyed item counts against the paper's maximum and against its
        section's, so a one-skill paper with a void item is still one section
        that is the whole paper."""
        keys = {k: v for k, v in OLD_KEYS.items() if k != "qv-2"}
        a = attempt("void", {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"})
        result = score_attempt(a, keys, scorer, BAND_MAP)
        assert result.raw_score == 2 and result.max_raw == 3
        assert result.per_section["reading"] == {"raw": 2.0, "band": 6.0}


class TestAnItemWithNoKey:
    """The comment in `score_attempt` said an unkeyed item is "marked void rather
    than incorrect: the student did nothing wrong, the test did." The code
    counted the marks into the maximum and emitted nothing, so the item was
    absent from review entirely — the numbering skipped and the product said
    nothing anywhere about why.

    `Verdict.VOID` was in the contract and handled by the student's marking
    display before anything produced it.
    """

    def _run(self, scorer, answers):
        keys = {k: v for k, v in OLD_KEYS.items() if k != "qv-2"}   # qv-2 loses its key
        return run(scorer, attempt("void-1", answers), keys)

    def test_the_item_is_scored_void_rather_than_dropped(self, scorer):
        result = self._run(scorer, {"qv-1": "bicycle", "qv-2": "library",
                                    "qv-3": "museum"})
        scored = {qv for qv, _ in result.item_scores}
        assert "qv-2" in scored, "the unkeyed item vanished from the run"
        item = next(s for qv, s in result.item_scores if qv == "qv-2")
        assert [s.verdict.value for s in item.slots] == ["void"]
        assert item.awarded == Decimal(0)
        assert item.slots[0].explain["reason"] == "no_answer_key"

    def test_it_does_not_move_the_raw_or_the_band(self, scorer):
        """A void item costs the student nothing and gives them nothing. The
        maximum already counted it before this change and still does; what moved
        is only whether they can SEE it."""
        answers = {"qv-1": "bicycle", "qv-2": "library", "qv-3": "museum"}
        result = self._run(scorer, answers)
        assert result.raw_score == Decimal(2)      # qv-1 and qv-3
        assert result.max_raw == Decimal(3)        # qv-2 still counts against them
        assert result.band == Decimal("6.0")

    def test_the_void_item_names_no_key_version(self, scorer):
        """`key_versions` is the map a regrade reproduces a score from, and there
        is genuinely no key here. It must not gain an invented entry — the
        persistence writes NULL for that column, which is the honest value."""
        result = self._run(scorer, {"qv-1": "bicycle"})
        assert "qv-2" not in result.key_versions
