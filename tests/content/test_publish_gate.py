"""The publish gate.

Two properties matter more than any individual check:

  1. A sound test publishes cleanly. A gate that cries wolf gets bypassed.
  2. A broken test yields EVERY problem in one pass, not the first one.
"""

from __future__ import annotations

import pytest

from app.modules.content import publish_gate
from app.modules.content.composition import BandMapRef, MediaRef, PassageRef

from .conftest import composition, group, listening_section, question, section


def codes(comp, registry) -> set[str]:
    return publish_gate.run(comp, registry).codes()


class TestHappyPath:
    def test_a_sound_test_passes(self, valid_test, registry):
        report = publish_gate.run(valid_test, registry)
        assert report.passed, [f.message for f in report.errors]

    def test_a_sound_listening_test_passes(self, registry):
        comp = composition(sections=[listening_section()])
        report = publish_gate.run(comp, registry)
        assert report.passed, [f.message for f in report.errors]


class TestReturnsEverythingAtOnce:
    def test_multiple_independent_faults_all_surface(self, registry):
        """The headline promise. One pass, every problem."""
        broken = composition(sections=[
            listening_section(
                audio=MediaRef(xid="a-1", status="processing", duration_ms=1_000,
                               has_attestation=False),
                declared_question_count=9,
                time_limit_seconds=None,
                groups=[group(audio_start_ms=0, audio_end_ms=120_000, questions=[
                    question(key=None),
                    question(xid="q-2", slot_keys=("s1", "s2")),
                ])],
            ),
        ], band_map=None)
        found = codes(broken, registry)
        assert {
            "AUDIO_NOT_READY", "ATTESTATION_MISSING", "AUDIO_TOO_SHORT",
            "KEY_MISSING", "KEY_SLOTS_MISSING", "SECTION_COUNT_MISMATCH",
            "BAND_MAP_MISSING", "SECTION_NO_TIME_LIMIT",
        } <= found, sorted(found)

    def test_report_separates_errors_from_warnings(self, registry):
        comp = composition(sections=[section(time_limit_seconds=None)])
        report = publish_gate.run(comp, registry)
        assert report.passed                      # a missing section limit is not fatal
        assert "SECTION_NO_TIME_LIMIT" in {f.code for f in report.warnings}

    def test_every_finding_carries_a_path_and_a_fix_hint(self, registry):
        report = publish_gate.run(composition(sections=[
            section(groups=[group(questions=[question(key=None)])])]), registry)
        for f in report.findings:
            assert f.path, f"{f.code} has no path to deep-link to"
            assert f.fix_hint, f"{f.code} tells the author nothing actionable"


class TestKeysAndSlots:
    def test_missing_key_blocks_publication(self, registry):
        assert "KEY_MISSING" in codes(
            composition(sections=[section(groups=[group(questions=[question(key=None)])])]),
            registry)

    def test_blank_without_an_answer(self, registry):
        q = question(payload={"text": "{{s1}} and {{s2}}", "slots": ["s1", "s2"]},
                     slot_keys=("s1", "s2"))
        assert "KEY_SLOTS_MISSING" in codes(
            composition(sections=[section(
                groups=[group(questions=[q])], declared_question_count=2)]), registry)

    def test_answer_for_a_blank_that_does_not_exist(self, registry):
        q = question(key={"slots": {"s1": {"accept": ["a"]}, "s9": {"accept": ["b"]}}})
        assert "KEY_SLOTS_ORPHANED" in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)

    def test_markers_in_the_text_must_match_the_declared_blanks(self, registry):
        q = question(payload={"text": "{{s1}} and {{s2}}", "slots": ["s1"]}, slot_keys=("s1",))
        assert "BLANK_MARKERS_MISMATCH" in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)

    def test_payload_that_violates_the_type_schema(self, registry):
        q = question(payload={"text": 42, "slots": ["s1"]})
        assert "PAYLOAD_INVALID" in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)

    def test_unknown_question_type(self, registry):
        q = question(type_key="does_not_exist")
        assert "TYPE_UNKNOWN" in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)

    def test_type_used_in_the_wrong_skill(self, registry):
        """`matching_headings` is Reading-only."""
        q = question(type_key="matching_headings",
                     payload={"slots": [{"key": "s1", "paragraph": "A"}]},
                     key={"slots": {"s1": {"accept": ["A"]}}})
        comp = composition(sections=[listening_section(groups=[group(
            questions=[q], option_bank=tuple({"id": c, "text": c} for c in "ABCDEF"))])])
        assert "TYPE_WRONG_SKILL" in codes(comp, registry)


class TestOptionsAndBanks:
    BANK = tuple({"id": c, "text": f"heading {c}"} for c in "ABCDEF")

    def _headings(self, key, bank=None, count=1):
        q = question(type_key="matching_headings",
                     payload={"slots": [{"key": "s1", "paragraph": "A"}]},
                     key=key)
        return composition(sections=[section(
            groups=[group(questions=[q],
                          option_bank=self.BANK if bank is None else bank)],
            declared_question_count=count)])

    def test_answer_outside_the_option_bank(self, registry):
        assert "KEY_OPTION_UNKNOWN" in codes(
            self._headings({"slots": {"s1": {"accept": ["Z"]}}}), registry)

    def test_missing_option_bank(self, registry):
        assert "OPTION_BANK_MISSING" in codes(
            self._headings({"slots": {"s1": {"accept": ["A"]}}}, bank=()), registry)

    def test_bank_without_spare_distractors(self, registry):
        """Matching headings must offer more headings than paragraphs, otherwise
        the last question answers itself by elimination."""
        assert "OPTION_BANK_TOO_SMALL" in codes(
            self._headings({"slots": {"s1": {"accept": ["A"]}}},
                           bank=({"id": "A", "text": "a"},)), registry)

    def test_paragraph_reference_without_a_lettered_passage(self, registry):
        q = question(type_key="matching_information", payload={"statement": "S"},
                     key={"slots": {"s1": {"accept": ["B"]}}})
        comp = composition(sections=[section(
            groups=[group(questions=[q])],
            passage=PassageRef(xid="p-1", title="T", paragraph_labels=()))])
        assert "PARAGRAPH_LABELS_MISSING" in codes(comp, registry)

    def test_mcq_multi_key_size_must_match_the_question(self, registry):
        q = question(type_key="mcq_multi", slot_keys=("selection",),
                     payload={"stem": "Choose TWO", "select_count": 2,
                              "options": [{"id": c, "text": c} for c in "ABCD"]},
                     key={"correct": ["A", "B", "C"]})
        assert "KEY_COUNT_MISMATCH" in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)


class TestWordLimits:
    def test_group_without_a_required_word_limit(self, registry):
        assert "WORD_LIMIT_MISSING" in codes(
            composition(sections=[section(groups=[group(word_limit=None)])]), registry)

    def test_accepted_answer_longer_than_its_own_limit(self, registry):
        """The invisible authoring mistake: an accepted answer that can never be
        entered legally makes the item unscoreable by construction."""
        q = question(key={"slots": {"s1": {"accept": ["the old public library"]}}})
        assert "KEY_EXCEEDS_WORD_LIMIT" in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)

    def test_a_key_level_limit_override_is_respected(self, registry):
        q = question(key={"slots": {"s1": {"accept": ["the old public library"],
                                           "word_limit": {"max_words": 4}}}})
        assert "KEY_EXCEEDS_WORD_LIMIT" not in codes(
            composition(sections=[section(groups=[group(questions=[q])])]), registry)


class TestAudio:
    def test_audio_shorter_than_the_question_span(self, registry):
        comp = composition(sections=[listening_section(
            audio=MediaRef(xid="a-1", status="ready", duration_ms=60_000),
            groups=[group(audio_start_ms=0, audio_end_ms=120_000)])])
        assert "AUDIO_TOO_SHORT" in codes(comp, registry)

    def test_markers_within_the_recording_are_fine(self, registry):
        comp = composition(sections=[listening_section(
            groups=[group(audio_start_ms=0, audio_end_ms=120_000)])])
        assert "AUDIO_TOO_SHORT" not in codes(comp, registry)

    def test_missing_audio(self, registry):
        assert "AUDIO_MISSING" in codes(
            composition(sections=[listening_section(audio=None)]), registry)

    @pytest.mark.parametrize("status", ["uploading", "processing", "failed", "quarantined"])
    def test_audio_that_is_not_ready(self, registry, status):
        comp = composition(sections=[listening_section(
            audio=MediaRef(xid="a-1", status=status, duration_ms=600_000))])
        assert "AUDIO_NOT_READY" in codes(comp, registry)


class TestNumberingAndCounts:
    def test_numbering_gap_between_groups(self, registry):
        comp = composition(sections=[section(
            groups=[group(xid="g-1", number_start=1),
                    group(xid="g-2", number_start=7)],
            declared_question_count=2)])
        assert "NUMBERING_DISCONTINUOUS" in codes(comp, registry)

    def test_contiguous_numbering_passes(self, registry):
        comp = composition(sections=[section(
            groups=[group(xid="g-1", number_start=1),
                    group(xid="g-2", number_start=2, questions=[question(xid="q-2")])],
            declared_question_count=2)])
        assert "NUMBERING_DISCONTINUOUS" not in codes(comp, registry)

    def test_declared_count_that_does_not_match_reality(self, registry):
        assert "SECTION_COUNT_MISMATCH" in codes(
            composition(sections=[section(declared_question_count=40)]), registry)


class TestBandMap:
    def test_band_map_shorter_than_the_test(self, registry):
        comp = composition(band_map=BandMapRef(
            xid="bm", max_raw=0, mapping=({"raw_min": 0, "raw_max": 0, "band": 0.0},)))
        assert "BAND_MAP_TOO_SHORT" in codes(comp, registry)

    def test_band_map_with_a_hole_in_it(self, registry):
        comp = composition(band_map=BandMapRef(
            xid="bm", max_raw=40,
            mapping=({"raw_min": 1, "raw_max": 40, "band": 5.0},)))   # nothing for 0
        assert "BAND_MAP_GAP" in codes(comp, registry)


class TestContentIntegrity:
    def test_material_without_a_copyright_attestation(self, registry):
        comp = composition(sections=[section(
            passage=PassageRef(xid="p-1", title="T", paragraph_labels=("A",),
                               has_attestation=False))])
        assert "ATTESTATION_MISSING" in codes(comp, registry)

    def test_material_under_an_open_takedown_cannot_be_republished(self, registry):
        """Otherwise a centre re-publishes disputed material inside a new test
        while the claim is still open."""
        comp = composition(sections=[section(
            passage=PassageRef(xid="p-1", title="T", paragraph_labels=("A",),
                               under_takedown=True))])
        assert "TAKEDOWN_OPEN" in codes(comp, registry)


class TestDiagrams:
    def _map_group(self, **kw):
        q = question(type_key="map_labelling",
                     payload={"slots": [{"key": "s1", "label": "Car park"}]},
                     key={"slots": {"s1": {"accept": ["A"]}}})
        base = dict(questions=[q], option_bank=({"id": "A", "text": "A"},
                                                {"id": "B", "text": "B"}),
                    diagram_media=MediaRef(xid="m-1", status="ready"),
                    hotspots=({"slot": "A", "x": 0.1, "y": 0.2},
                              {"slot": "B", "x": 0.3, "y": 0.4}))
        base.update(kw)
        return composition(sections=[listening_section(groups=[group(**base)])])

    def test_complete_map_passes(self, registry):
        report = publish_gate.run(self._map_group(), registry)
        assert report.passed, [f.message for f in report.errors]

    def test_missing_image(self, registry):
        assert "DIAGRAM_MISSING" in codes(self._map_group(diagram_media=None), registry)

    def test_label_with_no_position_on_the_map(self, registry):
        assert "HOTSPOTS_INCOMPLETE" in codes(
            self._map_group(hotspots=({"slot": "A", "x": 0.1, "y": 0.2},)), registry)

    def test_hotspot_outside_the_image(self, registry):
        assert "HOTSPOT_OUT_OF_BOUNDS" in codes(
            self._map_group(hotspots=({"slot": "A", "x": 1.4, "y": 0.2},
                                      {"slot": "B", "x": 0.3, "y": 0.4})), registry)


class TestEmptyShapes:
    def test_test_with_no_sections(self, registry):
        assert "NO_SECTIONS" in codes(composition(sections=[]), registry)

    def test_group_with_no_questions(self, registry):
        assert "GROUP_EMPTY" in codes(
            composition(sections=[section(groups=[group(questions=[])],
                                          declared_question_count=0)]), registry)
