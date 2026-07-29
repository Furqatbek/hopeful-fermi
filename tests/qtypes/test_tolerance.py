"""Every answer-tolerance rule from the spec, one test class each.

The spec (§2d) lists: case-insensitivity, leading/trailing whitespace,
British/American spelling variants, number-vs-word forms, optional articles, and
a per-key list of explicit alternatives. Each gets exhaustive coverage here,
plus the rules the registry adds (punctuation, hyphens, contractions, ordinals)
and the per-key overrides that can switch any of them off.
"""

from __future__ import annotations

import pytest

from app.modules.qtypes.registry import ScoreRequest
from app.modules.qtypes.schemas import GroupRules, Verdict, WordLimit

LIMIT_2 = GroupRules(word_limit=WordLimit(max_words=2, allow_number=True))
LIMIT_3 = GroupRules(word_limit=WordLimit(max_words=3, allow_number=True))


def completion(scorer, response, accept, *, group=LIMIT_3, key_extra=None, tolerance=None):
    """Score one `sentence_completion` slot. The workhorse of this file."""
    slot = {"accept": accept}
    slot.update(key_extra or {})
    return scorer.score_item(ScoreRequest(
        type_key="sentence_completion",
        type_version=1,
        payload={"text": "The answer is {{s1}}.", "slots": ["s1"]},
        key={"slots": {"s1": slot}},
        response={"slots": {"s1": response}},
        group=group,
        tolerance=tolerance,
    ))


def verdict(scorer, response, accept, **kw) -> Verdict:
    return completion(scorer, response, accept, **kw).slots[0].verdict


class TestCaseInsensitivity:
    @pytest.mark.parametrize("response", ["library", "Library", "LIBRARY", "LiBrArY"])
    def test_any_casing_is_accepted(self, scorer, response):
        assert verdict(scorer, response, ["library"]) is Verdict.CORRECT

    def test_key_casing_does_not_matter_either(self, scorer):
        assert verdict(scorer, "library", ["LIBRARY"]) is Verdict.CORRECT

    def test_case_sensitive_override_rejects_wrong_casing(self, scorer):
        """Proper nouns spelled out in a listening recording do expect a capital."""
        assert verdict(scorer, "smith", ["Smith"],
                       key_extra={"case_sensitive": True}) is Verdict.INCORRECT
        assert verdict(scorer, "Smith", ["Smith"],
                       key_extra={"case_sensitive": True}) is Verdict.CORRECT


class TestWhitespace:
    @pytest.mark.parametrize("response", [
        " library", "library ", "  library  ", "\tlibrary\n", "the  library",
    ])
    def test_surrounding_and_internal_whitespace_is_tolerated(self, scorer, response):
        assert verdict(scorer, response, ["the library"]) in {Verdict.CORRECT}

    def test_whitespace_only_answer_is_unanswered_not_incorrect(self, scorer):
        """The distinction matters: unanswered feeds `high_unanswered` item flags."""
        assert verdict(scorer, "   ", ["library"]) is Verdict.UNANSWERED

    def test_none_is_unanswered(self, scorer):
        assert verdict(scorer, None, ["library"]) is Verdict.UNANSWERED


class TestSpellingVariants:
    @pytest.mark.parametrize("response,accept", [
        ("color", "colour"), ("colour", "color"),
        ("center", "centre"), ("centre", "center"),
        ("organization", "organisation"), ("organisation", "organization"),
        ("theater", "theatre"), ("aluminum", "aluminium"),
        ("traveled", "travelled"), ("jewelry", "jewellery"),
    ])
    def test_uk_us_variants_match_in_both_directions(self, scorer, response, accept):
        assert verdict(scorer, response, [accept]) is Verdict.CORRECT

    def test_multi_word_phrase_variant(self, scorer):
        """Often cited as the classic broken key. It is not: the lexicon absorbs
        it, so it never reaches a regrade. Real broken keys are missing
        SYNONYMS, which no normalizer can rescue."""
        assert verdict(scorer, "carpark", ["car park"]) is Verdict.CORRECT
        assert verdict(scorer, "car park", ["carpark"]) is Verdict.CORRECT

    def test_unrelated_word_is_still_wrong(self, scorer):
        assert verdict(scorer, "colander", ["colour"]) is Verdict.INCORRECT


class TestNumberWordForms:
    @pytest.mark.parametrize("response,accept", [
        ("14", "fourteen"), ("fourteen", "14"),
        ("7", "seven"), ("seven", "7"),
        ("20", "twenty"), ("twenty", "20"),
        ("100", "one hundred"), ("one hundred", "100"),
    ])
    def test_digits_and_words_are_equivalent(self, scorer, response, accept):
        assert verdict(scorer, response, [accept]) is Verdict.CORRECT

    @pytest.mark.parametrize("response", ["twenty five", "twenty-five", "25"])
    def test_compound_numbers(self, scorer, response):
        assert verdict(scorer, response, ["25"]) is Verdict.CORRECT

    def test_scale_words(self, scorer):
        assert verdict(scorer, "three hundred", ["300"]) is Verdict.CORRECT

    def test_number_with_unit_is_not_mangled(self, scorer):
        assert verdict(scorer, "15%", ["15%"]) is Verdict.CORRECT

    def test_wrong_number_is_wrong(self, scorer):
        assert verdict(scorer, "fifteen", ["14"]) is Verdict.INCORRECT


class TestOptionalArticles:
    @pytest.mark.parametrize("response", ["library", "the library", "a library"])
    def test_articles_are_optional(self, scorer, response):
        assert verdict(scorer, response, ["the library"]) is Verdict.CORRECT

    def test_article_only_answer_does_not_collapse_to_empty(self, scorer):
        """Stripping `the` to nothing would read as unanswered and mask a wrong answer."""
        assert verdict(scorer, "the", ["library"]) is Verdict.INCORRECT


class TestExplicitAlternatives:
    def test_any_listed_alternative_is_accepted(self, scorer):
        for response in ["bicycle", "bike", "cycle"]:
            assert verdict(scorer, response, ["bicycle", "bike", "cycle"]) is Verdict.CORRECT

    def test_matched_alternative_is_reported(self, scorer):
        score = completion(scorer, "bike", ["bicycle", "bike"])
        assert score.slots[0].matched_alternative == "bike"

    def test_unlisted_answer_is_wrong(self, scorer):
        assert verdict(scorer, "scooter", ["bicycle", "bike"]) is Verdict.INCORRECT


class TestPunctuationAndHyphens:
    @pytest.mark.parametrize("response", ["library.", "library,", '"library"', "(library)"])
    def test_edge_punctuation_is_stripped(self, scorer, response):
        assert verdict(scorer, response, ["library"]) is Verdict.CORRECT

    @pytest.mark.parametrize("response", ["well-known", "well known"])
    def test_hyphen_is_flexible(self, scorer, response):
        assert verdict(scorer, response, ["well-known"], group=LIMIT_2) is Verdict.CORRECT

    def test_contraction_expands(self, scorer):
        assert verdict(scorer, "do not", ["don't"]) is Verdict.CORRECT

    def test_ordinal_forms(self, scorer):
        assert verdict(scorer, "1st", ["first"]) is Verdict.CORRECT
        assert verdict(scorer, "first", ["1"]) is Verdict.CORRECT


class TestPerKeyOverrides:
    def test_key_can_narrow_the_normalizer_set(self, scorer):
        """With only trim+casefold, a US spelling no longer matches a UK key."""
        assert verdict(scorer, "color", ["colour"],
                       key_extra={"normalizers": ["trim", "casefold"]}) is Verdict.INCORRECT
        assert verdict(scorer, "COLOUR", ["colour"],
                       key_extra={"normalizers": ["trim", "casefold"]}) is Verdict.CORRECT

    def test_tolerance_block_overrides_type_normalizers(self, scorer):
        assert verdict(scorer, "color", ["colour"],
                       tolerance={"normalizers": ["trim", "casefold"]}) is Verdict.INCORRECT


class TestExplainOutput:
    """`explain` is the answer to "why was my answer marked wrong". If it stops
    being useful, support cost goes up and nobody notices until it has."""

    def test_records_pipeline_and_comparison(self, scorer):
        explain = completion(scorer, " The Colour ", ["color"]).slots[0].explain
        assert explain["primitive"] == "text_per_slot"
        assert "normalizers" in explain
        assert explain["normalized_response"] == "colour"
        assert explain["normalized_accepted"] == ["colour"]
        assert [s["normalizer"] for s in explain["steps"]]

    def test_records_why_a_word_limit_rejection_happened(self, scorer):
        explain = completion(scorer, "the big red library", ["library"],
                             group=LIMIT_2).slots[0].explain
        assert explain["reason"] == "word_limit_exceeded"
        assert explain["word_limit"]["effective_words"] == 4
        assert explain["word_limit"]["max_words"] == 2


class TestToleranceCatchesItFirst:
    """What the tolerance layer absorbs never becomes a regrade.

    Worth pinning, because it draws the line between the two mechanisms: variants
    are handled at scoring time and are invisible; a genuinely missing synonym is
    a wrong key and needs the regrade path in `tests/exam/test_regrade.py`.
    """

    @pytest.mark.parametrize("response,accept", [
        ("carpark", "car park"),      # phrase spelling variant
        ("COLOR", "colour"),          # spelling + case
        (" 14 ", "fourteen"),         # number form + whitespace
        ("the library", "library"),   # article
        ("well known", "well-known"), # hyphen
    ])
    def test_variants_are_absorbed_and_never_reach_a_regrade(self, scorer, response, accept):
        assert verdict(scorer, response, [accept]) is Verdict.CORRECT

    @pytest.mark.parametrize("response,accept", [
        ("bike", "bicycle"),          # synonym
        ("car", "vehicle"),           # synonym
        ("physician", "doctor"),      # synonym
    ])
    def test_missing_synonyms_are_wrong_and_DO_need_a_key_fix(self, scorer, response, accept):
        assert verdict(scorer, response, [accept]) is Verdict.INCORRECT
