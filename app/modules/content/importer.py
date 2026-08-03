"""Bulk import — phase 7.

    DOCX / CSV / JSON  ->  adapter  ->  canonical Import JSON  ->  validate
                       ->  dry-run report + diff  ->  (author confirms)  ->  commit

The canonical JSON is the contract. Adapters are replaceable and a weak DOCX
parse degrades to a partial result with an error report rather than corrupting a
test (ADR-0001 §8.5). Everything downstream of the adapter is tested once, against
JSON, and works identically for every source.

Commit applies the STORED canonical document the author reviewed, never a
re-parse, because a re-parse can differ from what was on screen.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.qtypes.registry import Registry
from app.platform.findings import Report, Severity

from .models import (
    AnswerKeyVersion,
    Passage,
    PassageVersion,
    Question,
    QuestionGroup,
    QuestionGroupItem,
    QuestionGroupVersion,
    QuestionVersion,
    Test,
    TestVersion,
    TestVersionGroup,
    TestVersionSection,
)

CANONICAL_VERSION = 1


@dataclass(slots=True)
class ImportResult:
    canonical: dict[str, Any]
    report: Report = field(default_factory=Report)
    counts: dict[str, int] = field(default_factory=dict)
    diff: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report.passed

    def as_dict(self) -> dict[str, Any]:
        return {**self.report.as_dict(), "counts": self.counts, "diff": self.diff}


# ── adapters ─────────────────────────────────────────────────────────

def from_json(raw: bytes | str) -> tuple[dict[str, Any], Report]:
    """The reference adapter. The other two are measured against this shape."""
    report = Report()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        report.add("PARSE_FAILED", f"The file is not valid JSON: {exc}", path="file",
                   fix_hint="Export again from the template, or check for a truncated upload.")
        return {}, report
    if not isinstance(doc, dict):
        report.add("PARSE_FAILED", "The top level of the file must be an object.",
                   path="file", fix_hint="Start from the JSON template.")
        return {}, report
    return doc, report


def from_csv(raw: bytes | str) -> tuple[dict[str, Any], Report]:
    """One row per question, with group-level columns repeated.

    Repetition is redundant but it is what a teacher can produce in Excel without
    help, which is the only test that matters for this adapter.
    """
    report = Report()
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        report.add("PARSE_FAILED", "The file has no data rows.", path="file",
                   fix_hint="Add at least one question row below the header.")
        return {}, report

    required = {"section", "group", "type_key", "answer"}
    missing = required - set(rows[0].keys() or ())
    if missing:
        report.add("CSV_MISSING_COLUMNS",
                   f"Missing required column(s): {', '.join(sorted(missing))}.",
                   path="file.header",
                   fix_hint=f"The template requires: {', '.join(sorted(required))}.")
        return {}, report

    sections: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=2):   # row 1 is the header
        section_title = (row.get("section") or "Section 1").strip()
        group_title = (row.get("group") or "Group 1").strip()
        section = sections.setdefault(section_title, {
            "title": section_title,
            "skill": (row.get("skill") or "reading").strip(),
            "groups": {},
        })
        group = section["groups"].setdefault(group_title, {
            "title": group_title,
            "instructions": {"en": (row.get("instructions") or "").strip()},
            "word_limit": _word_limit(row.get("word_limit")),
            "questions": [],
        })
        accepted = [a.strip() for a in (row.get("answer") or "").split("|") if a.strip()]
        if not accepted:
            report.add("CSV_NO_ANSWER", "Row has no answer.", path=f"file.row[{index}]",
                       severity=Severity.ERROR,
                       fix_hint="Put the accepted answer in the `answer` column; "
                                "separate alternatives with |.")
        group["questions"].append({
            "type_key": (row.get("type_key") or "sentence_completion").strip(),
            "type_version": int(row.get("type_version") or 1),
            "text": (row.get("text") or "").strip(),
            "accept": accepted,
        })

    return {
        "canonical_version": CANONICAL_VERSION,
        "title": (rows[0].get("test_title") or "Imported test").strip(),
        "sections": [
            {**s, "groups": list(s["groups"].values())} for s in sections.values()
        ],
    }, report


def _word_limit(raw: str | None) -> dict[str, Any] | None:
    """`NO MORE THAN TWO WORDS AND/OR A NUMBER` as a teacher writes it."""
    if not raw:
        return None
    text = raw.strip().upper()
    words = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4}
    limit = next((n for w, n in words.items() if w in text), None)
    if limit is None:
        return None
    return {"max_words": limit, "allow_number": "NUMBER" in text,
            "hyphen_counts_as_one": True, "on_violation": "mark_incorrect"}


def from_docx(raw: bytes) -> tuple[dict[str, Any], Report]:
    """Parses the LOCKED template, not an arbitrary Word file.

    Be explicit with centres about that distinction: parsing a document a teacher
    already has is open-ended, and parsing this template is bounded. The template
    carries the canonical JSON in a `IELTS-IMPORT` document property, produced by
    the macro the template ships with, so the adapter is a lift rather than a
    natural-language parse.

    Structural DOCX parsing (styles, tables, numbering) is the phase-9 upgrade;
    it slots in here and nothing downstream changes.
    """
    report = Report()
    try:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = set(z.namelist())
            if "docProps/custom.xml" in names:
                custom = z.read("docProps/custom.xml").decode("utf-8", "replace")
                marker = "IELTS-IMPORT:"
                if marker in custom:
                    payload = custom.split(marker, 1)[1]
                    payload = payload.split("</vt:lpwstr>", 1)[0]
                    return from_json(payload)
            report.add(
                "DOCX_NOT_TEMPLATE",
                "This .docx was not produced by the import template.",
                path="file",
                fix_hint="Download the template from Import > Get template, fill it in, "
                         "and upload that file. Arbitrary Word documents are not supported.")
    except Exception as exc:  # zipfile raises a family of errors on corrupt input
        report.add("PARSE_FAILED", f"The file could not be opened as a .docx: {exc}",
                   path="file", fix_hint="Re-save from Word and upload again.")
    return {}, report


ADAPTERS = {"json": from_json, "csv": from_csv, "docx": from_docx}


# ── validation ───────────────────────────────────────────────────────

def validate(canonical: dict[str, Any], registry: Registry,
             report: Report | None = None) -> ImportResult:
    """Structural validation of the canonical document.

    Deliberately NOT the publish gate: this checks the document can be turned
    into content at all. The publish gate then runs against the created draft,
    so an import can succeed and still tell the author their test is not ready.
    """
    report = report or Report()
    counts = {"sections": 0, "groups": 0, "questions": 0, "keys": 0}

    if not canonical:
        return ImportResult(canonical={}, report=report, counts=counts)

    if canonical.get("canonical_version") not in (None, CANONICAL_VERSION):
        report.warn("CANONICAL_VERSION_MISMATCH",
                    f"File declares canonical_version "
                    f"{canonical.get('canonical_version')}, this server speaks "
                    f"{CANONICAL_VERSION}.",
                    path="canonical_version",
                    fix_hint="Re-export from this server's template.")
    if not canonical.get("title"):
        report.add("TITLE_MISSING", "The test has no title.", path="title",
                   fix_hint="Give the test a name teachers will recognise.")

    sections = canonical.get("sections") or []
    if not sections:
        report.add("NO_SECTIONS", "The file contains no sections.", path="sections",
                   fix_hint="Add at least one Reading or Listening section.")

    for si, section in enumerate(sections):
        counts["sections"] += 1
        path = f"sections[{si}]"
        if section.get("skill") not in ("reading", "listening"):
            report.add("SECTION_SKILL_INVALID",
                       f"Unknown skill {section.get('skill')!r}.", path=path,
                       fix_hint="Use `reading` or `listening`.")
        for gi, group in enumerate(section.get("groups") or []):
            counts["groups"] += 1
            gpath = f"{path}.groups[{gi}]"
            questions = group.get("questions") or []
            if not questions:
                report.add("GROUP_EMPTY", "Group contains no questions.", path=gpath,
                           fix_hint="Add a question, or remove the group.")
            for qi, question in enumerate(questions):
                counts["questions"] += 1
                qpath = f"{gpath}.questions[{qi}]"
                type_key = question.get("type_key")
                type_version = int(question.get("type_version") or 1)
                try:
                    definition = registry.get(type_key, type_version)
                except Exception:
                    report.add("TYPE_UNKNOWN",
                               f"Unknown question type {type_key!r} v{type_version}.",
                               path=qpath,
                               fix_hint="Check the spelling against the type list, or "
                                        "ask a platform admin to register it.")
                    continue
                if section.get("skill") not in definition.skills:
                    report.add("TYPE_WRONG_SKILL",
                               f"{definition.title} cannot be used in a "
                               f"{section.get('skill')} section.", path=qpath,
                               fix_hint=f"Allowed: {', '.join(definition.skills)}.")
                accepted = question.get("accept") or []
                if accepted:
                    counts["keys"] += 1
                else:
                    report.add("KEY_MISSING", "Question has no accepted answer.",
                               path=qpath,
                               fix_hint="Every question needs at least one answer.")

    return ImportResult(canonical=canonical, report=report, counts=counts)


def parse(raw: bytes, source_format: str, registry: Registry) -> ImportResult:
    """Adapter, then validation. The dry run stops here — nothing is written."""
    adapter = ADAPTERS.get(source_format)
    if adapter is None:
        report = Report()
        report.add("FORMAT_UNSUPPORTED", f"Unsupported format {source_format!r}.",
                   path="file", fix_hint="Use docx, csv or json.")
        return ImportResult(canonical={}, report=report)
    canonical, report = adapter(raw)
    return validate(canonical, registry, report)


# ── commit ───────────────────────────────────────────────────────────

def commit(session: Session, canonical: dict[str, Any], *, org_id: int | None,
           owner_user_id: int, now: dt.datetime, registry: Registry,
           target_test_id: int | None = None,
           import_job_id: int | None = None) -> TestVersion:
    """Create content from the reviewed canonical document.

    Always lands as a DRAFT. Publication is a separate, permissioned action, so
    bulk import can never become a publish bypass.

    `import_job_id` carries the attestation forward. The uploader affirmed the
    copyright position of the whole file at `POST /imports`, and that row is
    stored against the JOB — so the passages the file produces inherited
    nothing, and once the publish gate started actually checking attestations
    every imported paper became unpublishable. The claim is the uploader's, made
    once, about all of it; recording it per passage is what makes it reachable
    from the material a rights holder would name.
    """
    if target_test_id is not None:
        test = session.get(Test, target_test_id)
        version_no = 1 + (session.scalar(
            select(TestVersion.version_no).where(TestVersion.test_id == test.id)
            .order_by(TestVersion.version_no.desc()).limit(1)) or 0)
    else:
        test = Test(org_id=org_id, owner_user_id=owner_user_id,
                    title=canonical.get("title") or "Imported test",
                    kind=canonical.get("kind") or "mock",
                    skills=[s.get("skill", "reading") for s in canonical.get("sections", [])])
        session.add(test)
        session.flush()
        version_no = 1

    tv = TestVersion(test_id=test.id, version_no=version_no, status="draft",
                     title=canonical.get("title") or test.title,
                     created_by=owner_user_id,
                     config=canonical.get("config") or {})
    session.add(tv)
    session.flush()

    number = 1
    for si, section_doc in enumerate(canonical.get("sections") or [], start=1):
        passage_version_id = None
        if passage_doc := section_doc.get("passage"):
            passage = Passage(org_id=org_id, owner_user_id=owner_user_id,
                              title=passage_doc.get("title") or f"Passage {si}")
            session.add(passage)
            session.flush()
            _inherit_attestation(session, import_job_id, passage.id,
                                 owner_user_id, org_id)
            pv = PassageVersion(
                passage_id=passage.id, title=passage.title, status="draft",
                blocks=passage_doc.get("blocks") or [],
                paragraph_labels=_letters(len(passage_doc.get("blocks") or [])),
                word_count=passage_doc.get("word_count") or 0,
                checksum="", created_by=owner_user_id)
            session.add(pv)
            session.flush()
            passage_version_id = pv.id

        section = TestVersionSection(
            test_version_id=tv.id, position=si,
            skill=section_doc.get("skill") or "reading",
            title=section_doc.get("title") or f"Section {si}",
            passage_version_id=passage_version_id,
            time_limit_seconds=section_doc.get("time_limit_seconds"),
            declared_question_count=sum(
                len(g.get("questions") or []) for g in section_doc.get("groups") or []),
        )
        session.add(section)
        session.flush()

        for gi, group_doc in enumerate(section_doc.get("groups") or [], start=1):
            group = QuestionGroup(org_id=org_id, owner_user_id=owner_user_id,
                                  title=group_doc.get("title") or f"Group {gi}",
                                  skill=section.skill)
            session.add(group)
            session.flush()
            gv = QuestionGroupVersion(
                group_id=group.id, status="draft", checksum="", created_by=owner_user_id,
                instructions=group_doc.get("instructions") or {},
                word_limit=group_doc.get("word_limit"),
                option_bank=group_doc.get("option_bank"),
            )
            session.add(gv)
            session.flush()

            for qi, question_doc in enumerate(group_doc.get("questions") or [], start=1):
                q = Question(org_id=org_id, owner_user_id=owner_user_id,
                             type_key=question_doc["type_key"], skill=section.skill)
                session.add(q)
                session.flush()
                try:
                    definition = registry.get(question_doc["type_key"],
                                              int(question_doc.get("type_version") or 1))
                except Exception:
                    definition = None
                payload = question_doc.get("payload") or _payload_from(question_doc, definition)
                qv = QuestionVersion(
                    question_id=q.id, type_key=question_doc["type_key"],
                    type_version=int(question_doc.get("type_version") or 1),
                    payload=payload,
                    # Extracted here, not taken from the file: an importer that
                    # trusts a declared slot list lets a bad file through the gate.
                    slot_keys=_slots_from(payload),
                    status="draft", checksum="", created_by=owner_user_id)
                session.add(qv)
                session.flush()
                session.add(AnswerKeyVersion(
                    question_version_id=qv.id, version_no=1, reason="import",
                    key=question_doc.get("key") or _key_from(question_doc, payload),
                    created_by=owner_user_id))
                session.add(QuestionGroupItem(group_version_id=gv.id,
                                              question_version_id=qv.id, position=qi))

            session.add(TestVersionGroup(section_id=section.id, group_version_id=gv.id,
                                         position=gi, number_start=number))
            number += sum(len(_slots_from(q.get("payload") or _payload_from(q)))
                          for q in group_doc.get("questions") or [])  # noqa: E501
        session.flush()

    return tv


def _inherit_attestation(session: Session, import_job_id: int | None,
                         passage_id: int, user_id: int,
                         org_id: int | None) -> None:
    """Copy the import job's claim onto a passage the import created.

    A copy rather than a join, deliberately: `content_attestations` is evidence,
    and evidence that has to be resolved through two hops to a job row somebody
    may later purge is evidence that goes missing exactly when it is wanted. The
    statement hash is carried across unchanged — it is what the uploader
    actually affirmed, and re-hashing today's statement text would quietly
    restate their claim in words they never saw.
    """
    if import_job_id is None:
        return
    session.execute(text("""
        INSERT INTO content_attestations (subject_type, subject_id, user_id, org_id,
                                          claim, licence_note, statement_key,
                                          statement_version, statement_hash, ip,
                                          user_agent_hash)
        SELECT 'passage', :p, :u, :o, a.claim, a.licence_note, a.statement_key,
               a.statement_version, a.statement_hash, a.ip, a.user_agent_hash
        FROM content_attestations a
        WHERE a.subject_type = 'import_job' AND a.subject_id = :job
        ORDER BY a.affirmed_at DESC LIMIT 1
    """).bindparams(p=passage_id, u=user_id, o=org_id, job=import_job_id))


def _letters(n: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(n)]


# The field a type carries its prose in, in preference order. Mirrors the publish
# gate's `_TEXT_FIELDS`, so the two agree about where blanks live.
_TEXT_FIELDS = ("text", "summary", "question", "statement", "stem")


def _payload_from(question_doc: dict[str, Any],
                  definition: Any | None = None) -> dict[str, Any]:
    """Build a payload shaped by the TYPE's schema, not by a hardcoded guess.

    `sentence_completion` carries its prose in `text`, `short_answer` in
    `question`, `true_false_notgiven` in `statement`. Picking one and hoping is
    how question-type knowledge leaks back into modules that must not have any.
    """
    text = question_doc.get("text") or question_doc.get("question") or ""
    properties: dict[str, Any] = {}
    required: list[str] = []
    if definition is not None:
        properties = definition.payload_schema.get("properties") or {}
        required = definition.payload_schema.get("required") or []

    field = next((f for f in _TEXT_FIELDS if f in properties), "text")
    if "slots" in properties and "{{s" not in text:
        text = f"{text} {{{{s1}}}}".strip()

    payload: dict[str, Any] = {field: text}
    if "slots" in properties or "slots" in required:
        payload["slots"] = ["s1"]
    return payload


def _slots_from(payload: dict[str, Any]) -> list[str]:
    import re
    found: set[str] = set()
    for value in payload.values():
        if isinstance(value, str):
            found |= set(re.findall(r"\{\{(s[0-9]+)\}\}", value))
    return sorted(found) or list(payload.get("slots") or ["s1"])


def _key_from(question_doc: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    accepted = question_doc.get("accept") or []
    slots = _slots_from(payload)
    if len(slots) == 1:
        return {"slots": {slots[0]: {"accept": accepted}}}
    return {"slots": {slot: {"accept": [accepted[i]] if i < len(accepted) else []}
                      for i, slot in enumerate(slots)}}


def diff_against(session: Session, canonical: dict[str, Any],
                 target_test_id: int) -> dict[str, list[str]]:
    """What re-importing over an existing test would change.

    Coarse on purpose: counts and titles, not a per-question three-way merge. An
    author confirming a re-import wants "12 questions added, 3 removed", and a
    detailed diff would be more machinery than that answer is worth.
    """
    current = session.scalar(
        select(TestVersion).where(TestVersion.test_id == target_test_id)
        .order_by(TestVersion.version_no.desc()).limit(1))
    if current is None or not current.snapshot:
        return {"added": ["entire test (no published version to compare)"],
                "changed": [], "removed": []}

    old_titles = [s.get("title") for s in current.snapshot.get("sections", [])]
    new_titles = [s.get("title") for s in canonical.get("sections", [])]
    old_count = current.snapshot.get("total_questions", 0)
    new_count = sum(len(g.get("questions") or [])
                    for s in canonical.get("sections", [])
                    for g in s.get("groups") or [])

    changed = []
    if old_count != new_count:
        changed.append(f"question count {old_count} -> {new_count}")
    return {
        "added": [t for t in new_titles if t not in old_titles],
        "removed": [t for t in old_titles if t not in new_titles],
        "changed": changed,
    }
