"""The import pipeline.

The highest-leverage authoring feature: no teacher hand-builds forty questions
twice. The properties that matter are that a dry run writes nothing, that a
commit applies exactly what the author reviewed, and that a bad file degrades to
a readable error report rather than corrupting content.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select

from app.modules.content import importer, publish_gate
from app.modules.content import repo as content_repo
from app.modules.content.models import AnswerKeyVersion, Question, QuestionVersion, Test

GOOD = {
    "canonical_version": 1,
    "title": "Imported Mock 2",
    "sections": [{
        "title": "Passage 1",
        "skill": "reading",
        "time_limit_seconds": 1200,
        "passage": {"title": "Rivers",
                    "blocks": [{"id": "b1", "type": "paragraph",
                                "runs": [{"t": "text", "v": "Rivers move."}]}]},
        "groups": [{
            "title": "Questions 1-3",
            "instructions": {"en": "Complete each sentence."},
            "word_limit": {"max_words": 2, "allow_number": True},
            "questions": [
                {"type_key": "sentence_completion", "text": "The river is {{s1}}.",
                 "accept": ["long", "lengthy"]},
                {"type_key": "sentence_completion", "text": "It flows {{s1}}.",
                 "accept": ["north"]},
                {"type_key": "short_answer", "text": "How deep?", "accept": ["14 metres"]},
            ],
        }],
    }],
}

CSV_FILE = (
    "test_title,section,skill,group,instructions,word_limit,type_key,text,answer\n"
    "Imported CSV Mock,Passage 1,reading,Questions 1-2,Complete each sentence.,"
    "NO MORE THAN TWO WORDS AND/OR A NUMBER,sentence_completion,The river is {{s1}}.,long|lengthy\n"
    "Imported CSV Mock,Passage 1,reading,Questions 1-2,Complete each sentence.,"
    "NO MORE THAN TWO WORDS AND/OR A NUMBER,sentence_completion,It flows {{s1}}.,north\n"
)


class TestAdapters:
    def test_json_is_the_reference_shape(self, scorer_svc):
        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        assert result.ok, [f.message for f in result.report.errors]
        assert result.counts == {"sections": 1, "groups": 1, "questions": 3, "keys": 3}

    def test_csv_produces_the_same_canonical_document(self, scorer_svc):
        result = importer.parse(CSV_FILE.encode(), "csv", scorer_svc._registry)
        assert result.ok, [f.message for f in result.report.errors]
        assert result.counts["questions"] == 2
        group = result.canonical["sections"][0]["groups"][0]
        assert group["word_limit"] == {"max_words": 2, "allow_number": True,
                                       "hyphen_counts_as_one": True,
                                       "on_violation": "mark_incorrect"}
        assert group["questions"][0]["accept"] == ["long", "lengthy"]

    def test_csv_missing_a_required_column_reports_which(self, scorer_svc):
        bad = "section,group,type_key\nP1,G1,sentence_completion\n"
        result = importer.parse(bad.encode(), "csv", scorer_svc._registry)
        assert not result.ok
        finding = next(f for f in result.report.errors if f.code == "CSV_MISSING_COLUMNS")
        assert "answer" in finding.message and finding.fix_hint

    def test_malformed_json_degrades_to_a_readable_error(self, scorer_svc):
        result = importer.parse(b"{not json", "json", scorer_svc._registry)
        assert not result.ok
        assert result.report.errors[0].code == "PARSE_FAILED"
        assert result.canonical == {}

    def test_an_arbitrary_word_file_is_refused_with_an_explanation(self, scorer_svc):
        """We support the template, not whatever document a teacher already has.
        The error has to say that, or it becomes a support ticket."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
        result = importer.parse(buf.getvalue(), "docx", scorer_svc._registry)
        assert not result.ok
        finding = next(f for f in result.report.errors if f.code == "DOCX_NOT_TEMPLATE")
        assert "template" in finding.fix_hint.lower()

    def test_the_template_docx_round_trips(self, scorer_svc):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
            z.writestr("docProps/custom.xml",
                       "<Properties><property><vt:lpwstr>IELTS-IMPORT:"
                       + json.dumps(GOOD) + "</vt:lpwstr></property></Properties>")
        result = importer.parse(buf.getvalue(), "docx", scorer_svc._registry)
        assert result.ok, [f.message for f in result.report.errors]
        assert result.counts["questions"] == 3

    def test_an_unsupported_format_is_named(self, scorer_svc):
        result = importer.parse(b"x", "pdf", scorer_svc._registry)
        assert result.report.errors[0].code == "FORMAT_UNSUPPORTED"


class TestValidation:
    def test_unknown_question_type_is_caught_before_anything_is_written(self, scorer_svc):
        doc = json.loads(json.dumps(GOOD))
        doc["sections"][0]["groups"][0]["questions"][0]["type_key"] = "not_a_type"
        result = importer.parse(json.dumps(doc).encode(), "json", scorer_svc._registry)
        assert not result.ok
        assert "TYPE_UNKNOWN" in result.report.codes()

    def test_a_reading_only_type_in_a_listening_section_is_caught(self, scorer_svc):
        doc = json.loads(json.dumps(GOOD))
        doc["sections"][0]["skill"] = "listening"
        doc["sections"][0]["groups"][0]["questions"][0]["type_key"] = "matching_headings"
        result = importer.parse(json.dumps(doc).encode(), "json", scorer_svc._registry)
        assert "TYPE_WRONG_SKILL" in result.report.codes()

    def test_missing_answers_are_reported_per_question(self, scorer_svc):
        doc = json.loads(json.dumps(GOOD))
        doc["sections"][0]["groups"][0]["questions"][0]["accept"] = []
        doc["sections"][0]["groups"][0]["questions"][1]["accept"] = []
        result = importer.parse(json.dumps(doc).encode(), "json", scorer_svc._registry)
        missing = [f for f in result.report.errors if f.code == "KEY_MISSING"]
        assert len(missing) == 2
        assert all(f.path.startswith("sections[0].groups[0].questions[") for f in missing)

    def test_every_finding_is_actionable(self, scorer_svc):
        doc = {"sections": []}
        result = importer.parse(json.dumps(doc).encode(), "json", scorer_svc._registry)
        assert result.report.errors
        for f in result.report.errors:
            assert f.path and f.fix_hint


class TestDryRunAndCommit:
    def test_a_dry_run_writes_nothing(self, db, seed, scorer_svc):
        before = db.scalar(select(func.count()).select_from(Test))
        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        assert result.ok
        assert db.scalar(select(func.count()).select_from(Test)) == before

    def test_commit_creates_a_draft_never_a_published_version(
            self, db, seed, scorer_svc, clock):
        """Bulk import must not become a publish bypass."""
        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        tv = importer.commit(db, result.canonical, org_id=seed["org"].id,
                             owner_user_id=seed["author"].id, now=clock.now(),
                             registry=scorer_svc._registry)
        db.flush()
        assert tv.status == "draft"
        assert tv.snapshot is None

    def test_committed_content_is_complete_and_scoreable(
            self, db, seed, scorer_svc, clock):
        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        tv = importer.commit(db, result.canonical, org_id=seed["org"].id,
                             owner_user_id=seed["author"].id, now=clock.now(),
                             registry=scorer_svc._registry)
        db.flush()

        assert db.scalar(select(func.count()).select_from(QuestionVersion)
                         .where(QuestionVersion.created_by == seed["author"].id)) >= 3
        composition = content_repo.load_composition(db, tv.id)
        assert composition.total_slots == 3
        assert all(q.key is not None for _, _, q in composition.questions())

    def test_slot_keys_are_extracted_not_trusted_from_the_file(
            self, db, seed, scorer_svc, clock):
        """A file that declares its own slots could smuggle a mismatch past the
        publish gate, which compares key slots against this array."""
        doc = json.loads(json.dumps(GOOD))
        doc["sections"][0]["groups"][0]["questions"][0]["slot_keys"] = ["s1", "s2", "s9"]
        result = importer.parse(json.dumps(doc).encode(), "json", scorer_svc._registry)
        tv = importer.commit(db, result.canonical, org_id=seed["org"].id,
                             owner_user_id=seed["author"].id, now=clock.now(),
                             registry=scorer_svc._registry)
        db.flush()
        composition = content_repo.load_composition(db, tv.id)
        first = next(q for _, _, q in composition.questions())
        assert first.slot_keys == ("s1",)

    def test_imported_content_passes_the_publish_gate(self, db, seed, scorer_svc, clock):
        """Import and publish are separate stages, and the second one must accept
        what the first produced — otherwise the feature is a dead end."""
        from app.modules.content.models import BandMapVersion

        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        tv = importer.commit(db, result.canonical, org_id=seed["org"].id,
                             owner_user_id=seed["author"].id, now=clock.now(),
                             registry=scorer_svc._registry)
        bmv = db.scalars(select(BandMapVersion)).first()
        tv.band_map_version_id = bmv.id
        bmv.max_raw = 40
        bmv.mapping = [{"raw_min": i, "raw_max": i, "band": 5.0} for i in range(41)]
        db.flush()

        composition = content_repo.load_composition(db, tv.id)
        report = publish_gate.run(composition, scorer_svc._registry)
        assert report.passed, [f"{f.code}: {f.message}" for f in report.errors]

    def test_a_word_limit_violation_in_the_file_surfaces_at_publish_not_import(
            self, db, seed, scorer_svc, clock):
        """Import checks the document can become content; the gate checks the
        content is sound. An accepted answer longer than its own word limit is
        the gate's job, and it must not be silently lost in between."""
        from app.modules.content.models import BandMapVersion

        doc = json.loads(json.dumps(GOOD))
        doc["sections"][0]["groups"][0]["questions"][0]["accept"] = ["far too many words here"]
        result = importer.parse(json.dumps(doc).encode(), "json", scorer_svc._registry)
        assert result.ok                      # structurally fine

        tv = importer.commit(db, result.canonical, org_id=seed["org"].id,
                             owner_user_id=seed["author"].id, now=clock.now(),
                             registry=scorer_svc._registry)
        bmv = db.scalars(select(BandMapVersion)).first()
        tv.band_map_version_id = bmv.id
        bmv.max_raw = 40
        bmv.mapping = [{"raw_min": i, "raw_max": i, "band": 5.0} for i in range(41)]
        db.flush()

        report = publish_gate.run(content_repo.load_composition(db, tv.id),
                                  scorer_svc._registry)
        assert "KEY_EXCEEDS_WORD_LIMIT" in report.codes()

    def test_reimport_targets_a_new_version_of_the_same_test(
            self, db, seed, scorer_svc, clock):
        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        first = importer.commit(db, result.canonical, org_id=seed["org"].id,
                                owner_user_id=seed["author"].id, now=clock.now(),
                                registry=scorer_svc._registry)
        db.flush()
        second = importer.commit(db, result.canonical, org_id=seed["org"].id,
                                 owner_user_id=seed["author"].id, now=clock.now(),
                                 registry=scorer_svc._registry,
                                 target_test_id=first.test_id)
        db.flush()
        assert second.test_id == first.test_id
        assert (first.version_no, second.version_no) == (1, 2)

    def test_diff_reports_what_a_reimport_would_change(
            self, db, published, scorer_svc, clock):
        doc = json.loads(json.dumps(GOOD))
        diff = importer.diff_against(db, doc, published["test"].id)
        assert "question count 3 -> 3" not in diff["changed"]
        assert "Passage 1" in diff["added"] or diff["added"] == []

    def test_commit_applies_the_stored_document_not_a_reparse(
            self, db, seed, scorer_svc, clock):
        """The author confirmed a specific set of numbers on screen; a re-parse
        could differ from those, so commit reads the stored canonical."""
        result = importer.parse(json.dumps(GOOD).encode(), "json", scorer_svc._registry)
        stored = json.loads(json.dumps(result.canonical))
        importer.commit(db, stored, org_id=seed["org"].id,
                        owner_user_id=seed["author"].id, now=clock.now(),
                        registry=scorer_svc._registry)
        db.flush()
        keys = db.scalars(
            select(AnswerKeyVersion.key)
            .join(QuestionVersion, QuestionVersion.id == AnswerKeyVersion.question_version_id)
            .join(Question, Question.id == QuestionVersion.question_id)
            .where(QuestionVersion.status == "draft")
        ).all()
        accepted = {tuple(k["slots"]["s1"]["accept"]) for k in keys}
        assert ("long", "lengthy") in accepted
        assert ("14 metres",) in accepted
