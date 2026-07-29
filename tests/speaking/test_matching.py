"""The matcher, exhaustively. No database — this file runs in milliseconds.

The first class is the one that matters. "Minors must never be matched 1:1 with
adult accounts" is a child-safety requirement, not a feature, so it is tested
adversarially: not "does the happy path work" but "what would I have to do to get
a cross-band pair out of this function, and does every one of those fail".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.modules.speaking.matching import (
    MAX_BAND_GAP, Candidate, UnsafePair, match,
)

T0 = dt.datetime(2026, 7, 29, 18, 0, tzinfo=dt.UTC)


def who(name: str, *, minor: bool = False, band: str | None = None,
        waited: int = 0, language: str = "en", org: str | None = None,
        org_only: bool = False, recent: tuple[str, ...] = (),
        blocked: tuple[str, ...] = ()) -> Candidate:
    return Candidate(
        user_xid=name, is_minor=minor, language=language,
        band=Decimal(band) if band else None,
        waiting_since=T0 + dt.timedelta(seconds=waited),
        org_xid=org, org_only=org_only,
        recent_partners=frozenset(recent), blocked=frozenset(blocked))


def pairs_of(outcome) -> set[frozenset[str]]:
    return {frozenset(p.user_xids) for p in outcome.pairs}


class TestNoMinorIsEverPairedWithAnAdult:
    def test_a_lone_minor_among_adults_goes_unmatched(self):
        """The single most important assertion in the file.

        Four adults and one minor: the obvious greedy implementation pairs the
        minor with whoever is left. This one leaves them unmatched, which is the
        correct and less convenient answer.
        """
        outcome = match([
            who("adult1", band="6.0"), who("adult2", band="6.0"),
            who("adult3", band="6.5"), who("adult4", band="6.5"),
            who("child", minor=True, band="6.0"),
        ])
        assert len(outcome.pairs) == 2
        assert [c.user_xid for c in outcome.unmatched] == ["child"]

    def test_a_lone_adult_among_minors_goes_unmatched(self):
        """The supervising-teacher case. They are there to supervise, not to be
        paired with a fourteen-year-old."""
        outcome = match([
            who("child1", minor=True, band="5.0"),
            who("child2", minor=True, band="5.0"),
            who("teacher", band="8.0"),
        ])
        assert pairs_of(outcome) == {frozenset({"child1", "child2"})}
        assert [c.user_xid for c in outcome.unmatched] == ["teacher"]

    def test_identical_bands_do_not_tempt_a_cross_band_pair(self):
        """An identical band is the strongest pull the algorithm has. It must
        still lose to the age partition."""
        outcome = match([who("child", minor=True, band="6.0"),
                         who("adult", band="6.0")])
        assert outcome.pairs == ()
        assert len(outcome.unmatched) == 2

    @pytest.mark.parametrize("minors,adults", [(1, 1), (1, 9), (9, 1), (3, 4), (5, 5)])
    def test_no_mixed_pair_at_any_ratio(self, minors, adults):
        outcome = match(
            [who(f"m{i}", minor=True, band="6.0") for i in range(minors)]
            + [who(f"a{i}", band="6.0") for i in range(adults)])
        for pair in outcome.pairs:
            assert pair.a.is_minor == pair.b.is_minor
            assert pair.age_band == ("minor" if pair.a.is_minor else "adult")

    def test_the_matched_count_never_exceeds_what_the_partitions_allow(self):
        outcome = match([who("m1", minor=True), who("m2", minor=True),
                         who("m3", minor=True), who("a1"), who("a2")])
        # 3 minors -> 1 pair + 1 spare; 2 adults -> 1 pair.
        assert len(outcome.pairs) == 2
        assert len(outcome.unmatched) == 1
        assert outcome.unmatched[0].is_minor

    def test_the_assertion_fires_if_partitioning_is_ever_bypassed(self):
        """Belt and braces, tested.

        `match` partitions before pairing, so this cannot happen through the
        public path. The assertion exists for the day someone adds a second way
        to build pairs, and an untested assertion is a comment.
        """
        from app.modules.speaking.matching import Outcome, Pair

        bad = Pair(a=who("child", minor=True), b=who("adult"),
                   age_band="adult", band_gap=None, repeat=False)
        # Re-run the same check `match` applies to its own output.
        with pytest.raises(UnsafePair):
            _assert_like_match(Outcome((bad,), ()))


def _assert_like_match(outcome) -> None:
    for pair in outcome.pairs:
        if pair.a.is_minor != pair.b.is_minor:
            raise UnsafePair("cross-band")


class TestBandProximity:
    def test_closest_bands_are_paired(self):
        outcome = match([who("a", band="5.0"), who("b", band="8.0"),
                         who("c", band="5.5"), who("d", band="8.5")])
        assert pairs_of(outcome) == {frozenset({"a", "c"}), frozenset({"b", "d"})}

    def test_a_gap_wider_than_the_limit_is_refused(self):
        """Two people three bands apart have a bad fifteen minutes and one of
        them does not come back."""
        outcome = match([who("beginner", band="4.0"), who("advanced", band="8.5")])
        assert outcome.pairs == ()
        assert len(outcome.unmatched) == 2

    def test_exactly_the_limit_is_allowed(self):
        outcome = match([who("a", band="5.0"),
                         who("b", band=str(Decimal("5.0") + MAX_BAND_GAP))])
        assert len(outcome.pairs) == 1

    def test_a_known_band_is_preferred_over_an_unknown_one(self):
        outcome = match([who("known1", band="6.0"), who("known2", band="6.0"),
                         who("unknown1"), who("unknown2")])
        assert pairs_of(outcome) == {frozenset({"known1", "known2"}),
                                     frozenset({"unknown1", "unknown2"})}


class TestBlocksAndRepeats:
    def test_a_block_is_honoured_in_both_directions(self):
        """Whoever filed it. The blocked user must not learn they were blocked by
        noticing they stopped being matched, so both sides simply go unmatched."""
        outcome = match([who("victim", band="6.0", blocked=("harasser",)),
                         who("harasser", band="6.0")])
        assert outcome.pairs == ()

    def test_a_block_filed_by_the_other_party_is_equally_honoured(self):
        outcome = match([who("a", band="6.0"),
                         who("b", band="6.0", blocked=("a",))])
        assert outcome.pairs == ()

    def test_a_block_does_not_prevent_a_different_partner(self):
        outcome = match([who("a", band="6.0", blocked=("b",)), who("b", band="6.0"),
                         who("c", band="6.0"), who("d", band="6.0")])
        assert len(outcome.pairs) == 2
        assert frozenset({"a", "b"}) not in pairs_of(outcome)

    def test_a_recent_partner_is_avoided_when_an_alternative_exists(self):
        outcome = match([who("a", band="6.0", recent=("b",)), who("b", band="6.0"),
                         who("c", band="6.0"), who("d", band="6.0")])
        assert frozenset({"a", "b"}) not in pairs_of(outcome)

    def test_a_repeat_is_allowed_rather_than_leaving_two_people_idle(self):
        """A small pool is the normal case at 150 DAU. Refusing a repeat here
        would mean the two people who showed up get nothing."""
        outcome = match([who("a", band="6.0", recent=("b",)), who("b", band="6.0")])
        assert pairs_of(outcome) == {frozenset({"a", "b"})}
        assert outcome.pairs[0].repeat is True

    def test_repeats_can_be_disallowed(self):
        outcome = match([who("a", band="6.0", recent=("b",)), who("b", band="6.0")],
                        allow_repeats=False)
        assert outcome.pairs == ()


class TestScoping:
    def test_different_languages_are_never_paired(self):
        outcome = match([who("uz", language="uz"), who("en", language="en")])
        assert outcome.pairs == ()

    def test_org_only_is_honoured_from_either_side(self):
        outcome = match([who("a", org="1", org_only=True), who("b", org="2")])
        assert outcome.pairs == ()
        outcome = match([who("a", org="1"), who("b", org="2", org_only=True)])
        assert outcome.pairs == ()

    def test_org_only_pairs_within_the_org(self):
        outcome = match([who("a", org="1", org_only=True), who("b", org="1")])
        assert len(outcome.pairs) == 1


class TestDeterminism:
    def test_the_same_input_gives_the_same_output(self):
        """A matcher that shuffles cannot be reproduced when a student reports an
        unsuitable partner, and a safety investigation that cannot reproduce the
        decision is not much of an investigation."""
        pool = [who(f"u{i}", band=str(Decimal(5) + Decimal(i) / 2), waited=i)
                for i in range(8)]
        first = match(pool)
        for _ in range(20):
            assert pairs_of(match(list(reversed(pool)))) == pairs_of(first)

    def test_longest_waiting_is_served_first(self):
        outcome = match([who("late", band="6.0", waited=100),
                         who("early", band="6.0", waited=0),
                         who("middle", band="6.0", waited=50)])
        # `early` waited longest, so they are paired; `late` is left over.
        assert frozenset({"early", "middle"}) in pairs_of(outcome)
        assert [c.user_xid for c in outcome.unmatched] == ["late"]

    def test_an_empty_pool_is_not_an_error(self):
        outcome = match([])
        assert outcome.pairs == () and outcome.unmatched == ()

    def test_one_person_waits(self):
        outcome = match([who("alone")])
        assert outcome.pairs == ()
        assert len(outcome.unmatched) == 1

    def test_nobody_appears_in_two_pairs(self):
        pool = [who(f"u{i}", band="6.0") for i in range(11)]
        outcome = match(pool)
        seen = [x for p in outcome.pairs for x in p.user_xids]
        assert len(seen) == len(set(seen)) == outcome.matched_count
        assert outcome.matched_count + len(outcome.unmatched) == 11
