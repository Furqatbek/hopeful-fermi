"""The three scoring primitives, and every shipped question type through them."""

from __future__ import annotations

import pytest

from app.modules.qtypes.registry import ScoreRequest
from app.modules.qtypes.schemas import GroupRules, Option, Primitive, Verdict, WordLimit

BANK = tuple(Option(id=c, text=f"heading {c}") for c in "ABCDEF")


class TestChoicePerSlot:
    def _mcq(self, scorer, response):
        return scorer.score_item(ScoreRequest(
            type_key="mcq_single", type_version=1,
            payload={"stem": "Q", "options": [{"id": "A", "text": "a"},
                                              {"id": "B", "text": "b"},
                                              {"id": "C", "text": "c"}]},
            key={"slots": {"s1": {"accept": ["B"]}}},
            response={"slots": {"s1": response}},
        )).slots[0]

    def test_correct_choice(self, scorer):
        assert self._mcq(scorer, "B").verdict is Verdict.CORRECT

    def test_lowercase_choice_is_accepted(self, scorer):
        assert self._mcq(scorer, "b").verdict is Verdict.CORRECT

    def test_wrong_choice(self, scorer):
        assert self._mcq(scorer, "A").verdict is Verdict.INCORRECT

    def test_unanswered(self, scorer):
        assert self._mcq(scorer, None).verdict is Verdict.UNANSWERED

    def test_option_outside_the_set_is_incorrect_not_an_error(self, scorer):
        """Unreachable through the UI, reachable with curl. Be defensive."""
        slot = self._mcq(scorer, "Z")
        assert slot.verdict is Verdict.INCORRECT
        assert slot.explain["reason"] == "not_in_option_set"

    @pytest.mark.parametrize("response,expected", [
        ("TRUE", Verdict.CORRECT), ("true", Verdict.CORRECT),
        ("FALSE", Verdict.INCORRECT), ("NOT GIVEN", Verdict.INCORRECT),
    ])
    def test_true_false_notgiven_uses_a_fixed_option_set(self, scorer, response, expected):
        score = scorer.score_item(ScoreRequest(
            type_key="true_false_notgiven", type_version=1,
            payload={"statement": "S"},
            key={"slots": {"s1": {"accept": ["TRUE"]}}},
            response={"slots": {"s1": response}},
        ))
        assert score.slots[0].verdict is expected

    def test_matching_headings_scores_each_paragraph_independently(self, scorer):
        score = scorer.score_item(ScoreRequest(
            type_key="matching_headings", type_version=1,
            payload={"slots": [{"key": "s1", "paragraph": "A"},
                               {"key": "s2", "paragraph": "B"},
                               {"key": "s3", "paragraph": "C"}]},
            key={"slots": {"s1": {"accept": ["B"]},
                           "s2": {"accept": ["D"]},
                           "s3": {"accept": ["A"]}}},
            response={"slots": {"s1": "B", "s2": "C", "s3": "A"}},
            group=GroupRules(option_bank=BANK, unique_options=True),
        ))
        assert score.awarded == 2 and score.max_points == 3
        assert score.verdict is Verdict.PARTIAL

    def test_duplicate_selection_is_not_double_penalised(self, scorer):
        """`unique_options` is an authoring constraint checked by the publish gate,
        not a scoring rule. A student who reuses a heading simply gets at most one
        of them right, which is how the real exam marks."""
        score = scorer.score_item(ScoreRequest(
            type_key="matching_headings", type_version=1,
            payload={"slots": [{"key": "s1", "paragraph": "A"},
                               {"key": "s2", "paragraph": "B"}]},
            key={"slots": {"s1": {"accept": ["B"]}, "s2": {"accept": ["C"]}}},
            response={"slots": {"s1": "B", "s2": "B"}},
            group=GroupRules(option_bank=BANK, unique_options=True),
        ))
        assert score.awarded == 1

    def test_matching_information_options_come_from_the_passage(self, scorer):
        score = scorer.score_item(ScoreRequest(
            type_key="matching_information", type_version=1,
            payload={"statement": "S"},
            key={"slots": {"s1": {"accept": ["C"]}}},
            response={"slots": {"s1": "C"}},
            paragraph_labels=("A", "B", "C", "D"),
        ))
        assert score.slots[0].verdict is Verdict.CORRECT

    def test_matching_information_accepts_several_paragraphs(self, scorer):
        """Some information genuinely appears in more than one paragraph."""
        for answer in ("B", "D"):
            score = scorer.score_item(ScoreRequest(
                type_key="matching_information", type_version=1,
                payload={"statement": "S"},
                key={"slots": {"s1": {"accept": ["B", "D"]}}},
                response={"slots": {"s1": answer}},
                paragraph_labels=("A", "B", "C", "D"),
            ))
            assert score.slots[0].verdict is Verdict.CORRECT


class TestSetSelection:
    def _multi(self, scorer, selected, correct=("B", "D"), expected=2):
        return scorer.score_item(ScoreRequest(
            type_key="mcq_multi", type_version=1,
            payload={"stem": "Choose TWO", "select_count": expected,
                     "options": [{"id": c, "text": c} for c in "ABCDE"]},
            key={"correct": list(correct)},
            response={"selected": list(selected)},
        ))

    def test_both_correct(self, scorer):
        s = self._multi(scorer, ["B", "D"])
        assert s.verdict is Verdict.CORRECT and s.awarded == 2

    def test_order_does_not_matter(self, scorer):
        assert self._multi(scorer, ["D", "B"]).awarded == 2

    def test_one_correct_gives_partial_credit(self, scorer):
        s = self._multi(scorer, ["B", "A"])
        assert s.verdict is Verdict.PARTIAL and s.awarded == 1

    def test_over_selection_scores_zero_for_the_whole_item(self, scorer):
        """Ticking four boxes when asked for two must not shotgun into marks."""
        s = self._multi(scorer, ["A", "B", "C", "D"])
        assert s.awarded == 0
        assert s.slots[0].explain["reason"] == "over_selection"

    def test_under_selection_still_earns_what_it_got_right(self, scorer):
        s = self._multi(scorer, ["B"])
        assert s.awarded == 1 and s.verdict is Verdict.PARTIAL

    def test_nothing_selected_is_unanswered(self, scorer):
        assert self._multi(scorer, []).verdict is Verdict.UNANSWERED


class TestMultiSlotAndAggregation:
    def test_completion_with_several_blanks_scores_each(self, scorer):
        score = scorer.score_item(ScoreRequest(
            type_key="summary_completion", type_version=1,
            payload={"summary": "{{s1}} and {{s2}} and {{s3}}",
                     "slots": ["s1", "s2", "s3"]},
            key={"slots": {"s1": {"accept": ["alpha"]},
                           "s2": {"accept": ["beta"]},
                           "s3": {"accept": ["gamma"]}}},
            response={"slots": {"s1": "alpha", "s2": "wrong", "s3": None}},
            group=GroupRules(word_limit=WordLimit(max_words=2)),
        ))
        verdicts = [s.verdict for s in score.slots]
        assert verdicts == [Verdict.CORRECT, Verdict.INCORRECT, Verdict.UNANSWERED]
        assert score.awarded == 1 and score.max_points == 3
        assert score.verdict is Verdict.PARTIAL

    def test_item_is_unanswered_only_when_every_slot_is(self, scorer):
        score = scorer.score_item(ScoreRequest(
            type_key="summary_completion", type_version=1,
            payload={"summary": "{{s1}} {{s2}}", "slots": ["s1", "s2"]},
            key={"slots": {"s1": {"accept": ["a"]}, "s2": {"accept": ["b"]}}},
            response={"slots": {"s1": None, "s2": None}},
            group=GroupRules(word_limit=WordLimit(max_words=1)),
        ))
        assert score.verdict is Verdict.UNANSWERED


class TestEveryShippedType:
    """Smoke coverage: every definition in `registry/question_types/` loads and
    scores. A type that ships but cannot be scored is worse than one that does
    not ship."""

    def test_all_seventeen_load(self, registry):
        assert len(registry) == 17

    def test_only_three_primitives_are_used(self, registry):
        used = {d.scoring.primitive for d in registry.all()}
        assert used == {Primitive.CHOICE_PER_SLOT, Primitive.TEXT_PER_SLOT,
                        Primitive.SET_SELECTION}

    @pytest.mark.parametrize("skill,minimum", [("reading", 12), ("listening", 10)])
    def test_both_skills_are_well_covered(self, registry, skill, minimum):
        assert len(registry.for_skill(skill)) >= minimum

    def test_every_text_type_scores_a_correct_answer(self, scorer, registry):
        for d in registry.all():
            if d.scoring.primitive is not Primitive.TEXT_PER_SLOT:
                continue
            score = scorer.score_item(ScoreRequest(
                type_key=d.key, type_version=d.version,
                payload={}, key={"slots": {"s1": {"accept": ["answer"]}}},
                response={"slots": {"s1": "Answer"}},
                group=GroupRules(word_limit=WordLimit(max_words=2)),
            ))
            assert score.verdict is Verdict.CORRECT, f"{d.ref} failed to score"

    def test_every_choice_type_scores_a_correct_answer(self, scorer, registry):
        for d in registry.all():
            if d.scoring.primitive is not Primitive.CHOICE_PER_SLOT:
                continue
            source = d.scoring.options.get("option_source")
            payload: dict = {}
            group = GroupRules()
            labels: tuple[str, ...] = ()
            accept = "A"
            if source == "payload.options":
                payload = {"options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}]}
            elif source == "group.option_bank":
                group = GroupRules(option_bank=BANK)
            elif source == "fixed":
                accept = d.scoring.options["fixed_options"][0]["id"]
            elif source == "section.passage_version.paragraph_labels":
                labels = ("A", "B", "C")
            score = scorer.score_item(ScoreRequest(
                type_key=d.key, type_version=d.version, payload=payload,
                key={"slots": {"s1": {"accept": [accept]}}},
                response={"slots": {"s1": accept}},
                group=group, paragraph_labels=labels,
            ))
            assert score.verdict is Verdict.CORRECT, f"{d.ref} failed to score"


class TestDeterminism:
    def test_same_inputs_always_produce_the_same_score(self, scorer):
        """Regrade is a recomputation over frozen inputs. If scoring were not
        deterministic, every regrade would be a coin toss."""
        req = ScoreRequest(
            type_key="sentence_completion", type_version=1,
            payload={"text": "{{s1}}", "slots": ["s1"]},
            key={"slots": {"s1": {"accept": ["fourteen", "colour"]}}},
            response={"slots": {"s1": " 14 "}},
            group=GroupRules(word_limit=WordLimit(max_words=2)),
        )
        first = scorer.score_item(req)
        for _ in range(20):
            again = scorer.score_item(req)
            assert again.awarded == first.awarded
            assert again.slots[0].normalized_response == first.slots[0].normalized_response


class TestAResponseArrivesInTheShapeTheEngineBuilds:
    """`ExamSession._score` assembles EVERY answer as `{"slots": {key: value}}`.

    It is generic by design — it dispatches on the primitive and knows nothing
    about any question type — so a primitive that reads its response any other way
    is one the running product never feeds correctly. `set_selection` read
    `response["selected"]`, which nothing in the exam flow produces, so every
    `mcq_multi` sitting scored UNANSWERED whatever the student ticked.

    The unit tests above pass the whole-question shape directly and so could never
    see it. These pass the shape the engine actually builds.
    """

    def _assembled(self, scorer, value):
        """Exactly what `_score` produces from one per-slot delta."""
        return scorer.score_item(ScoreRequest(
            type_key="mcq_multi", type_version=1,
            payload={"stem": "Choose TWO", "select_count": 2,
                     "options": [{"id": c, "text": c} for c in "ABCDE"]},
            key={"correct": ["B", "D"]},
            response={"slots": {"s1": value}}))

    def test_a_multi_select_scores_through_the_assembled_shape(self, scorer):
        s = self._assembled(scorer, ["B", "D"])
        assert s.verdict is Verdict.CORRECT and s.awarded == 2

    def test_partial_credit_survives_the_assembled_shape(self, scorer):
        assert self._assembled(scorer, ["B", "A"]).verdict is Verdict.PARTIAL

    def test_a_single_pick_need_not_be_wrapped_in_a_list(self, scorer):
        """A one-choice UI sends the id. Requiring a list there would be a shape
        rule with no reason behind it."""
        assert self._assembled(scorer, "B").awarded == 1

    def test_nothing_chosen_is_still_unanswered(self, scorer):
        assert self._assembled(scorer, None).verdict is Verdict.UNANSWERED

    def test_the_whole_question_shape_still_works(self, scorer):
        """Both shapes, because the planner replays stored responses too."""
        s = scorer.score_item(ScoreRequest(
            type_key="mcq_multi", type_version=1,
            payload={"stem": "Choose TWO", "select_count": 2,
                     "options": [{"id": c, "text": c} for c in "ABCDE"]},
            key={"correct": ["B", "D"]}, response={"selected": ["B", "D"]}))
        assert s.verdict is Verdict.CORRECT


class TestAContainerIsNotATextAnswer:
    """`_as_text` did `str(value)` on anything, so a structure became its Python
    repr and the repr was marked as though the student had typed it."""

    def _text_item(self, scorer, value):
        return scorer.score_item(ScoreRequest(
            type_key="sentence_completion", type_version=1,
            payload={"text": "I came by {{s1}}.", "slots": ["s1"]},
            key={"slots": {"s1": {"accept": ["bicycle"]}}},
            response={"slots": {"s1": value}}))

    def test_an_object_is_unanswered_not_marked_as_its_repr(self, scorer):
        slot = self._text_item(scorer, {"text": "bicycle"}).slots[0]
        assert slot.verdict is Verdict.UNANSWERED
        # The repr must not reach the review screen as the student's own words.
        assert slot.raw_response is None
        assert slot.normalized_response != "text bicycle"

    def test_a_plain_string_is_unaffected(self, scorer):
        assert self._text_item(scorer, "bicycle").slots[0].verdict is Verdict.CORRECT
