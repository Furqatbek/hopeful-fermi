"""Word-limit enforcement.

`NO MORE THAN TWO WORDS AND/OR A NUMBER` is the rule most likely to be
implemented as decoration and then quietly award marks the real exam would not.
These tests pin the counting semantics.
"""

from __future__ import annotations

import pytest

from app.modules.qtypes import wordlimit
from app.modules.qtypes.registry import ScoreRequest
from app.modules.qtypes.schemas import GroupRules, Verdict, WordLimit

TWO_AND_NUMBER = WordLimit(max_words=2, allow_number=True)
TWO_WORDS_ONLY = WordLimit(max_words=2, allow_number=False)
ONE_WORD = WordLimit(max_words=1, allow_number=False)


class TestCounting:
    @pytest.mark.parametrize("raw,words,numbers", [
        ("library", 1, 0),
        ("the old library", 3, 0),
        ("14", 0, 1),
        ("14 September", 1, 1),
        ("well-known", 1, 0),          # hyphenated compound is ONE word
        ("don't", 1, 0),               # contraction is ONE word
        ("  spaced   out  ", 2, 0),
        ("3.5", 0, 1),
        ("1,200", 0, 1),
        ("9:30", 0, 1),
        ("20%", 0, 1),
        ("$15", 0, 1),
    ])
    def test_token_classification(self, raw, words, numbers):
        c = wordlimit.count(raw, TWO_AND_NUMBER)
        assert (c.words, c.numbers) == (words, numbers)

    def test_hyphen_can_be_counted_as_two_when_the_rule_says_so(self):
        rule = WordLimit(max_words=2, hyphen_counts_as_one=False)
        assert wordlimit.count("well-known", rule).words == 2

    def test_punctuation_only_tokens_are_ignored(self):
        assert wordlimit.count("library .", TWO_AND_NUMBER).words == 1


class TestViolation:
    @pytest.mark.parametrize("raw,violated", [
        ("library", False),
        ("public library", False),          # exactly at the limit
        ("the public library", True),       # three words
        ("14", False),                      # number only, within allowance
        ("14 September", False),            # 1 number + 1 word
        ("14 September 1999", True),        # two numbers: the second spends a word slot
        ("well-known author", False),       # hyphen is one word
    ])
    def test_two_words_and_or_a_number(self, raw, violated):
        assert wordlimit.violates(raw, TWO_AND_NUMBER)[0] is violated

    def test_number_allowance_is_exactly_one(self):
        """`AND/OR A NUMBER` is singular: a second number violates on its own,
        however much word allowance is left over."""
        assert wordlimit.violates("14", TWO_AND_NUMBER)[0] is False
        assert wordlimit.violates("3 4", TWO_AND_NUMBER)[0] is True
        assert wordlimit.violates("3 4 5", TWO_AND_NUMBER)[0] is True

    def test_word_and_number_limits_are_independent(self):
        """A comfortable word count does not buy a second number, and vice versa."""
        violated, explain = wordlimit.violates("14 September 1999", TWO_AND_NUMBER)
        assert violated is True
        assert explain["words_ok"] is True      # one word, limit two
        assert explain["numbers_ok"] is False   # two numbers, allowance one

    def test_numbers_count_as_words_when_not_allowed(self):
        assert wordlimit.violates("14", TWO_WORDS_ONLY)[0] is False
        assert wordlimit.violates("14 15 16", TWO_WORDS_ONLY)[0] is True

    def test_no_rule_means_no_violation(self):
        assert wordlimit.violates("as many words as I like", None) == (False, {})


class TestScoringIntegration:
    """The rule has to bite at scoring time, not just in a helper."""

    def _score(self, scorer, response, accept, rule):
        return scorer.score_item(ScoreRequest(
            type_key="short_answer", type_version=1,
            payload={"question": "What?", "slots": ["s1"]},
            key={"slots": {"s1": {"accept": accept}}},
            response={"slots": {"s1": response}},
            group=GroupRules(word_limit=rule),
        )).slots[0]

    def test_over_limit_is_marked_wrong_outright(self, scorer):
        """Never truncated and re-compared — that would award marks the exam would not."""
        slot = self._score(scorer, "the public library", ["public library"], TWO_AND_NUMBER)
        assert slot.verdict is Verdict.INCORRECT
        assert slot.explain["reason"] == "word_limit_exceeded"

    def test_truncation_would_have_matched_and_still_does_not(self, scorer):
        slot = self._score(scorer, "a big public library", ["public library"], TWO_AND_NUMBER)
        assert slot.awarded == 0

    def test_within_limit_scores_normally(self, scorer):
        assert self._score(scorer, "public library", ["public library"],
                           TWO_AND_NUMBER).verdict is Verdict.CORRECT

    def test_limit_is_checked_before_normalization(self, scorer):
        """`strip_articles` would turn this three-word answer into a legal
        two-word one. Counting the raw response is what stops that."""
        slot = self._score(scorer, "the public library", ["public library"], TWO_AND_NUMBER)
        assert slot.verdict is Verdict.INCORRECT
        assert slot.explain["word_limit"]["counted_words"] == 3

    def test_key_level_word_limit_overrides_the_group(self, scorer):
        score = scorer.score_item(ScoreRequest(
            type_key="short_answer", type_version=1,
            payload={"question": "What?", "slots": ["s1"]},
            key={"slots": {"s1": {"accept": ["the public library"],
                                  "word_limit": {"max_words": 3}}}},
            response={"slots": {"s1": "the public library"}},
            group=GroupRules(word_limit=ONE_WORD),
        ))
        assert score.slots[0].verdict is Verdict.CORRECT
