"""Item statistics, and mainly the point-biserial.

`discrimination` is the automated signal that finds a bad answer key. A NEGATIVE
value means strong students get the item wrong more often than weak ones, which
is almost never a hard question. The boundary cases matter as much as the value:
returning a confident `0.0` where the number cannot be computed would put items
on the flagged list that belong nowhere near it, and an author who learns the
list is noise stops reading it.
"""

from __future__ import annotations

import pytest

from app.modules.analytics.stats import (
    MIN_RESPONSES, Response, analyse, burn_score, exposure_recommendation,
    point_biserial, suggested_action,
)


def r(name: str, correct: bool, total: float, raw: str | None = None) -> Response:
    return Response(user_xid=name, correct=correct, total_score=total, raw_response=raw)


class TestPointBiserial:
    def test_a_good_item_discriminates_positively(self):
        """Strong students get it right, weak ones do not. This is what a healthy
        item looks like."""
        responses = ([r(f"strong{i}", True, 35 + i) for i in range(10)]
                     + [r(f"weak{i}", False, 12 + i) for i in range(10)])
        assert point_biserial(responses) > 0.8

    def test_a_bad_key_discriminates_negatively(self):
        """The signal. Strong students wrote the right answer and were marked
        wrong, so the paper's best performers fail this one item."""
        responses = ([r(f"strong{i}", False, 35 + i) for i in range(10)]
                     + [r(f"weak{i}", True, 12 + i) for i in range(10)])
        assert point_biserial(responses) < -0.8

    def test_a_coin_flip_discriminates_near_zero(self):
        responses = [r(f"u{i}", i % 2 == 0, 20 + (i % 4)) for i in range(20)]
        assert abs(point_biserial(responses)) < 0.5

    @pytest.mark.parametrize("responses,why", [
        ([], "no responses"),
        ([r("only", True, 20)], "one response"),
        ([r("a", True, 20), r("b", True, 30)], "everyone correct"),
        ([r("a", False, 20), r("b", False, 30)], "nobody correct"),
        ([r("a", True, 20), r("b", False, 20)], "no variance in total score"),
    ])
    def test_none_rather_than_a_misleading_zero(self, responses, why):
        """`None` and `0.0` are different answers and an author acts on them
        differently. A confident zero here would say "this item does not
        discriminate", which is a claim the data cannot support."""
        assert point_biserial(responses) is None, why


class TestAnalyse:
    def test_p_value_is_the_proportion_correct(self):
        stats = analyse([r("a", True, 30), r("b", True, 25), r("c", False, 10),
                         r("d", False, 8)])
        assert stats.p_value == 0.5
        assert stats.n_responses == 4 and stats.n_correct == 2

    def test_common_wrong_answers_are_ranked_by_frequency(self):
        """The highest-value output in this module: "38 students wrote 'bike'"
        is how a missing key alternative is found without anyone complaining."""
        responses = ([r(f"w{i}", False, 20, "bike") for i in range(8)]
                     + [r(f"x{i}", False, 20, "cycle") for i in range(3)]
                     + [r(f"c{i}", True, 30, "bicycle") for i in range(9)])
        stats = analyse(responses)
        assert stats.common_wrong[0]["value"] == "bike"
        assert stats.common_wrong[0]["count"] == 8
        assert stats.common_wrong[1]["value"] == "cycle"

    def test_wrong_answers_are_case_and_space_normalised(self):
        stats = analyse([r("a", False, 20, " Bike "), r("b", False, 20, "bike")])
        assert stats.common_wrong[0] == {"value": "bike", "count": 2, "share": 1.0}

    def test_nothing_is_flagged_below_the_response_floor(self):
        """Flagging on noise trains authors to ignore the flags."""
        stats = analyse([r(f"u{i}", False, 20) for i in range(MIN_RESPONSES - 1)])
        assert stats.flagged is False

    def test_a_near_zero_p_value_is_flagged(self):
        """A genuinely hard item: one person got it, and it was the strongest.

        The one correct student is deliberately the TOP scorer. Make them the
        weakest instead and the item also discriminates negatively, which is a
        different diagnosis — a bad key rather than a hard question — and the
        advice changes accordingly. The next test pins that.
        """
        stats = analyse([r(f"u{i}", i == 39, 20 + i) for i in range(40)])
        assert stats.flag_reasons == ["near_zero_p"]
        assert suggested_action(stats) == "review_item"

    def test_hard_plus_negative_discrimination_points_at_the_key_instead(self):
        stats = analyse([r(f"u{i}", i == 0, 20 + i) for i in range(40)])
        assert "near_zero_p" in stats.flag_reasons
        assert "negative_discrimination" in stats.flag_reasons
        # The stronger signal wins: one student got it and it was the weakest one.
        assert suggested_action(stats) == "review_key"

    def test_negative_discrimination_is_flagged_and_points_at_the_key(self):
        stats = analyse([r(f"s{i}", False, 35 + i) for i in range(15)]
                        + [r(f"w{i}", True, 10 + i) for i in range(15)])
        assert "negative_discrimination" in stats.flag_reasons
        assert suggested_action(stats) == "review_key"

    def test_a_frequent_wrong_answer_is_flagged(self):
        responses = ([r(f"w{i}", False, 20, "bike") for i in range(10)]
                     + [r(f"c{i}", True, 30, "bicycle") for i in range(20)])
        stats = analyse(responses)
        assert "common_wrong_answer" in stats.flag_reasons
        assert suggested_action(stats) == "review_key"

    def test_an_item_everyone_gets_right_is_flagged_as_too_easy(self):
        stats = analyse([r(f"u{i}", True, 20 + i) for i in range(40)])
        assert "near_one_p" in stats.flag_reasons
        assert suggested_action(stats) == "retire_too_easy"

    def test_a_healthy_item_is_not_flagged(self):
        stats = analyse([r(f"s{i}", True, 30 + i) for i in range(12)]
                        + [r(f"w{i}", False, 12 + i) for i in range(12)])
        assert stats.flagged is False
        assert suggested_action(stats) == "none"

    def test_an_empty_item_is_not_an_error(self):
        stats = analyse([])
        assert stats.n_responses == 0 and stats.p_value is None
        assert stats.flagged is False


class TestBurnScore:
    def test_a_fresh_item_is_near_zero(self):
        assert burn_score(times_sat=0, distinct_orgs=0) == 0.0
        assert exposure_recommendation(burn_score(times_sat=5, distinct_orgs=1)) \
            == "fresh"

    def test_heavy_use_at_one_centre_burns_it(self):
        assert burn_score(times_sat=600, distinct_orgs=1) > 0.9

    def test_light_use_across_many_centres_also_burns_it(self):
        """Different route, same conclusion: an item four centres have used has
        left the building even if each used it once."""
        assert burn_score(times_sat=12, distinct_orgs=8) > 0.7

    def test_the_two_routes_compound(self):
        both = burn_score(times_sat=200, distinct_orgs=4)
        assert both > burn_score(times_sat=200, distinct_orgs=1)
        assert both > burn_score(times_sat=20, distinct_orgs=4)

    def test_it_is_monotonic_and_bounded(self):
        previous = -1.0
        for sat in (0, 10, 50, 100, 400, 5_000):
            value = burn_score(times_sat=sat, distinct_orgs=1)
            assert 0.0 <= value <= 1.0
            assert value >= previous
            previous = value

    def test_recommendations_cross_at_the_documented_thresholds(self):
        assert exposure_recommendation(0.2) == "fresh"
        assert exposure_recommendation(0.5) == "watch"
        assert exposure_recommendation(0.8) == "retire"
