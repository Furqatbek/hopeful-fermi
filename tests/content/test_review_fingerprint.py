"""What an approval is actually over.

`content_reviews.content_checksum` is the difference between a review gate and a
checkbox. A version stays editable while it is `in_review`, so an approval that
named only a version id was satisfied by content nobody had read — and the answer
key is the field most worth changing after somebody else has signed off.

These run in the no-database tier, like the publish gate itself, because
`fingerprint()` is a pure function of the composition and the properties worth
pinning are all about which edits it can and cannot see.
"""

from __future__ import annotations

import dataclasses

from app.modules.content import repo, review
from app.modules.content.composition import MediaRef, PassageRef

from .conftest import composition, group, listening_section, question, section


def fp(comp) -> str:
    return review.fingerprint(comp)


class TestItIsStable:
    def test_the_same_composition_twice(self, valid_test):
        assert fp(valid_test) == fp(valid_test)

    def test_an_equal_composition_rebuilt_from_scratch(self):
        """Two loads of an unedited version must agree, or every publish after an
        approval fails and the gate gets switched off within a week."""
        assert fp(composition()) == fp(composition())

    def test_it_is_a_hex_digest(self, valid_test):
        assert len(fp(valid_test)) == 64
        assert int(fp(valid_test), 16) >= 0


class TestItSeesTheEditsThatMatter:
    def test_an_answer_key_change(self, valid_test):
        """The one this gate exists for. "Bad keys are the fastest way to lose a
        school client" — and a key edit leaves every student-visible byte
        identical, so it is invisible to anyone re-reading the paper."""
        changed = composition(sections=[section(groups=[group(questions=[
            question(key={"slots": {"s1": {"accept": ["museum"]}}})])])])
        assert fp(valid_test) != fp(changed)

    def test_a_tolerance_change(self, valid_test):
        """Not the key, but what counts as matching it — same effect on a
        student's mark, and even less visible."""
        changed = composition(sections=[section(groups=[group(questions=[
            question(tolerance={"case_sensitive": True})])])])
        assert fp(valid_test) != fp(changed)

    def test_the_question_text(self, valid_test):
        changed = composition(sections=[section(groups=[group(questions=[
            question(payload={"text": "The answer is now {{s1}}.",
                              "slots": ["s1"]})])])])
        assert fp(valid_test) != fp(changed)

    def test_the_passage_body(self, valid_test):
        changed = composition(sections=[section(
            passage=PassageRef(xid="p-1", title="Cartography",
                               blocks=({"id": "b1", "type": "paragraph"},),
                               paragraph_labels=("A", "B", "C", "D")))])
        assert fp(valid_test) != fp(changed)

    def test_swapping_the_passage_for_a_different_version(self, valid_test):
        changed = composition(sections=[section(
            passage=PassageRef(xid="p-2", title="Cartography",
                               paragraph_labels=("A", "B", "C", "D")))])
        assert fp(valid_test) != fp(changed)

    def test_the_band_map(self, valid_test):
        """A different curve is a different band for the same paper."""
        assert fp(valid_test) != fp(composition(band_map=None))

    def test_adding_a_section(self, valid_test):
        assert fp(valid_test) != fp(composition(
            sections=[section(), section(position=2)]))

    def test_the_word_limit_on_a_group(self, valid_test):
        changed = composition(sections=[section(groups=[
            group(word_limit={"max_words": 3, "allow_number": True})])])
        assert fp(valid_test) != fp(changed)

    def test_a_takedown_landing_on_the_passage(self, valid_test):
        """Over-broad on purpose. Every field here is one the publish gate reads,
        which makes it material to whether the test is fit to publish — and a
        second look after a copyright claim arrives is the right outcome, not a
        false positive."""
        changed = composition(sections=[section(
            passage=PassageRef(xid="p-1", title="Cartography",
                               paragraph_labels=("A", "B", "C", "D"),
                               under_takedown=True))])
        assert fp(valid_test) != fp(changed)

    def test_an_audio_track_that_stopped_being_ready(self):
        ready = composition(sections=[listening_section()])
        stale = composition(sections=[listening_section(
            audio=MediaRef(xid="a-1", status="failed", duration_ms=600_000))])
        assert fp(ready) != fp(stale)


class TestWhyNotTheSnapshotChecksum:
    """`test_versions.checksum` already exists, over `repo.build_snapshot()`, and
    reusing it would have been one line. It is the wrong hash for this job."""

    def test_the_snapshot_cannot_see_a_key_change(self, valid_test):
        rekeyed = composition(sections=[section(groups=[group(questions=[
            question(key={"slots": {"s1": {"accept": ["museum"]}}})])])])
        assert repo.build_snapshot(valid_test) == repo.build_snapshot(rekeyed)
        assert fp(valid_test) != fp(rekeyed)

    def test_the_snapshot_carries_no_keys_at_all(self, valid_test):
        """The reason it cannot: the snapshot is the document that goes to the
        device, so anything secret must not be in it."""
        assert "library" not in str(repo.build_snapshot(valid_test))
        assert "library" in str(dataclasses.asdict(valid_test))


class TestTheSetting:
    def test_absent_means_not_required(self):
        assert review.required_by(None) is False
        assert review.required_by({}) is False

    def test_other_settings_do_not_turn_it_on(self):
        assert review.required_by({"teacher_can_publish": True}) is False

    def test_on_when_set(self):
        assert review.required_by({review.SETTING: True}) is True
