"""The publish gate.

Nineteen checks, and it returns EVERY finding at once. Returning the first
failure would mean an author fixes one problem, republishes, waits, and finds the
next — nineteen times. That is the difference between a teacher building a mock
in one sitting and giving up.

The gate is a pure function of (composition, registry). No database, no clock,
no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

from app.modules.qtypes import wordlimit
from app.modules.qtypes.registry import Registry
from app.modules.qtypes.schemas import Primitive, WordLimit
from app.platform.findings import Report, Severity

from .composition import GroupNode, QuestionNode, SectionNode, TestComposition

_BLANK_MARKER = re.compile(r"\{\{(s[0-9]+)\}\}")
_TEXT_FIELDS = ("text", "summary", "question", "statement", "stem")


def run(composition: TestComposition, registry: Registry) -> Report:
    report = Report()
    _check_structure(composition, report)
    _check_band_map(composition, report)
    for section in composition.sections:
        _check_section(section, composition, registry, report)
    _check_numbering(composition, report)
    return report


# ── whole-test checks ────────────────────────────────────────────────

def _check_structure(c: TestComposition, r: Report) -> None:
    if not c.sections:
        r.add("NO_SECTIONS", "The test has no sections.", path="sections",
              fix_hint="Add at least one Reading or Listening section.")
    if not any(g.questions for s in c.sections for g in s.groups):
        r.add("NO_QUESTIONS", "The test has no questions.", path="sections",
              fix_hint="Add a question group with at least one question.")


def _check_band_map(c: TestComposition, r: Report) -> None:
    """Check 13. Without a band map a score is a raw count, which is not what a
    student or a centre asked for."""
    if c.band_map is None:
        r.add("BAND_MAP_MISSING", "No band map is attached to this version.",
              path="band_map", fix_hint="Choose a band map, or inherit the platform default.")
        return
    total = c.total_slots
    if c.band_map.max_raw < total:
        r.add("BAND_MAP_TOO_SHORT",
              f"Band map covers up to {c.band_map.max_raw} marks but the test is worth {total}.",
              path="band_map",
              fix_hint="Extend the band map, or use one built for this test length.")
    covered = set()
    for row in c.band_map.mapping:
        covered.update(range(int(row["raw_min"]), int(row["raw_max"]) + 1))
    missing = sorted(set(range(0, min(total, c.band_map.max_raw) + 1)) - covered)
    if missing:
        r.add("BAND_MAP_GAP",
              f"Band map has no band for raw score(s): {missing[:8]}"
              + (" …" if len(missing) > 8 else ""),
              path="band_map", fix_hint="Every raw score from 0 to the maximum needs a band.")


def _check_numbering(c: TestComposition, r: Report) -> None:
    """Check 12. IELTS numbering runs 1..40 across the whole paper. A gap or an
    overlap means two questions share a number on the answer sheet."""
    expected = 1
    for s in c.sections:
        for g in s.groups:
            path = f"sections[{s.position}].groups[{g.number_start}]"
            if g.number_start != expected:
                r.add("NUMBERING_DISCONTINUOUS",
                      f"Group starts at question {g.number_start}, expected {expected}.",
                      path=path, subject_xid=g.xid,
                      fix_hint="Reorder the groups, or correct the starting number.")
            expected = max(expected, g.number_start) + sum(len(q.slot_keys) for q in g.questions)


# ── section checks ───────────────────────────────────────────────────

def _check_section(s: SectionNode, c: TestComposition, registry: Registry, r: Report) -> None:
    path = f"sections[{s.position}]"

    if s.time_limit_seconds is None:  # check 19
        r.warn("SECTION_NO_TIME_LIMIT",
               f"Section '{s.title}' has no time limit; the test-level limit will apply.",
               path=path, fix_hint="Set a per-section limit if this section is separately timed.")

    if s.skill == "reading" and s.passage is None:
        r.add("PASSAGE_MISSING", f"Reading section '{s.title}' has no passage.", path=path,
              fix_hint="Attach a passage version.")
    if s.passage is not None:  # checks 17, 18
        if not s.passage.has_attestation:
            r.add("ATTESTATION_MISSING",
                  f"Passage '{s.passage.title}' has no copyright attestation.",
                  path=f"{path}.passage", subject_xid=s.passage.xid,
                  fix_hint="The uploader must affirm the material is original or licensed.")
        if s.passage.under_takedown:
            r.add("TAKEDOWN_OPEN",
                  f"Passage '{s.passage.title}' is subject to an open takedown request.",
                  path=f"{path}.passage", subject_xid=s.passage.xid,
                  fix_hint="Resolve the claim before republishing this material.")

    if s.skill == "listening":
        _check_audio(s, r, path)

    for index, g in enumerate(s.groups):
        _check_group(g, s, c, registry, r, f"{path}.groups[{index}]")

    # Check 11: declared vs actual.
    if s.declared_question_count is not None:
        actual = sum(len(q.slot_keys) for g in s.groups for q in g.questions)
        if actual != s.declared_question_count:
            r.add("SECTION_COUNT_MISMATCH",
                  f"Section '{s.title}' declares {s.declared_question_count} questions "
                  f"but contains {actual}.",
                  path=path,
                  fix_hint="Correct the declared count, or add/remove questions.")


def _check_audio(s: SectionNode, r: Report, path: str) -> None:
    """Checks 9, 10, 17, 18."""
    if s.audio is None:
        r.add("AUDIO_MISSING", f"Listening section '{s.title}' has no audio track.",
              path=path, fix_hint="Upload or attach an audio track.")
        return
    if s.audio.status != "ready":
        r.add("AUDIO_NOT_READY",
              f"Audio for '{s.title}' is '{s.audio.status}', not ready.",
              path=f"{path}.audio", subject_xid=s.audio.xid,
              fix_hint="Wait for transcoding to finish, or re-upload if it failed.")
    if not s.audio.has_attestation:
        r.add("ATTESTATION_MISSING", f"Audio for '{s.title}' has no copyright attestation.",
              path=f"{path}.audio", subject_xid=s.audio.xid,
              fix_hint="The uploader must affirm the material is original or licensed.")
    if s.audio.under_takedown:
        r.add("TAKEDOWN_OPEN", f"Audio for '{s.title}' is subject to an open takedown request.",
              path=f"{path}.audio", subject_xid=s.audio.xid,
              fix_hint="Resolve the claim before republishing this material.")

    if s.audio.duration_ms is None:
        return
    for index, g in enumerate(s.groups):
        if g.audio_end_ms is not None and g.audio_end_ms > s.audio.duration_ms:
            r.add("AUDIO_TOO_SHORT",
                  f"Questions {g.number_start}+ are marked to {g.audio_end_ms} ms but the "
                  f"recording is only {s.audio.duration_ms} ms long.",
                  path=f"{path}.groups[{index}]", subject_xid=g.xid,
                  fix_hint="Fix the timestamp markers, or attach the full-length recording.")


# ── group and question checks ────────────────────────────────────────

def _check_group(g: GroupNode, s: SectionNode, c: TestComposition,
                 registry: Registry, r: Report, path: str) -> None:
    if not g.questions:
        r.add("GROUP_EMPTY", "Question group contains no questions.", path=path,
              subject_xid=g.xid, fix_hint="Add a question, or remove the group.")
        return
    for index, q in enumerate(g.questions):
        _check_question(q, g, s, registry, r, f"{path}.questions[{index}]")


def _check_question(q: QuestionNode, g: GroupNode, s: SectionNode,
                    registry: Registry, r: Report, path: str) -> None:
    try:
        definition = registry.get(q.type_key, q.type_version)
    except Exception:
        r.add("TYPE_UNKNOWN", f"Question type {q.type_key}@v{q.type_version} is not registered.",
              path=path, subject_xid=q.xid,
              fix_hint="Register the type, or change the question to a known one.")
        return

    if s.skill not in definition.skills:
        r.add("TYPE_WRONG_SKILL",
              f"{definition.title} cannot be used in a {s.skill} section.",
              path=path, subject_xid=q.xid, fix_hint=f"Allowed: {', '.join(definition.skills)}.")

    _schema_findings(definition.payload_schema, q.payload, "PAYLOAD_INVALID",
                     "question content", path, q.xid, r)                       # check 2

    if q.key is None:                                                          # check 1
        r.add("KEY_MISSING", "Question has no answer key.", path=path, subject_xid=q.xid,
              fix_hint="Add the correct answer before publishing.")
        return

    _schema_findings(definition.key_schema, q.key, "KEY_INVALID",
                     "answer key", path, q.xid, r)                             # check 3

    primitive = definition.scoring.primitive
    if primitive is Primitive.SET_SELECTION:
        _check_set_selection_key(q, definition, r, path)
    else:
        _check_slot_key(q, g, s, definition, r, path)                          # checks 4, 5, 6, 14

    if primitive is Primitive.TEXT_PER_SLOT:
        _check_word_limits(q, g, definition, r, path)                          # checks 7, 8

    _check_diagram(q, g, definition, r, path)                                  # check 16


def _schema_findings(schema: dict, instance: Any, code: str, what: str,
                     path: str, xid: str, r: Report) -> None:
    for err in sorted(Draft202012Validator(schema).iter_errors(instance),
                      key=lambda e: list(e.path)):
        pointer = ".".join(str(p) for p in err.path)
        r.add(code, f"Invalid {what}{': ' + pointer if pointer else ''} — {err.message}",
              path=f"{path}.{pointer}" if pointer else path, subject_xid=xid,
              fix_hint="Correct the field, or check the question type's expected shape.")


def _check_slot_key(q: QuestionNode, g: GroupNode, s: SectionNode,
                    definition, r: Report, path: str) -> None:
    key_slots = set((q.key or {}).get("slots", {}))
    declared = set(q.slot_keys)

    if key_slots != declared:                                                  # check 4
        missing, extra = sorted(declared - key_slots), sorted(key_slots - declared)
        if missing:
            r.add("KEY_SLOTS_MISSING",
                  f"No answer for blank(s): {', '.join(missing)}.",
                  path=f"{path}.key", subject_xid=q.xid,
                  fix_hint="Every blank needs an accepted answer.")
        if extra:
            r.add("KEY_SLOTS_ORPHANED",
                  f"Answer key references blank(s) that do not exist: {', '.join(extra)}.",
                  path=f"{path}.key", subject_xid=q.xid,
                  fix_hint="Remove the stale key entries, or add the missing blanks.")

    # Check 5: markers embedded in the question text must match the declared slots.
    markers: set[str] = set()
    for field in _TEXT_FIELDS:
        if isinstance(text := q.payload.get(field), str):
            markers |= set(_BLANK_MARKER.findall(text))
    for container in ("steps", "blocks", "fields", "rows"):
        markers |= _markers_in(q.payload.get(container))
    if markers and markers != declared:
        r.add("BLANK_MARKERS_MISMATCH",
              f"Blank markers in the text {sorted(markers)} do not match the declared "
              f"blanks {sorted(declared)}.",
              path=path, subject_xid=q.xid,
              fix_hint="Re-insert the blanks, or fix the numbering.")

    # Check 6: option references must exist. Check 14: bank needs spare distractors.
    source = definition.scoring.options.get("option_source")
    valid: set[str] = set()
    if source == "group.option_bank":
        valid = {str(o["id"]).casefold() for o in g.option_bank}
        if not valid:
            r.add("OPTION_BANK_MISSING",
                  f"{definition.title} needs a shared option list on its question group.",
                  path=f"{path}.group", subject_xid=g.xid,
                  fix_hint="Add the list of headings, features or bank words to the group.")
        elif (offset := _bank_offset(definition)) is not None:
            needed = sum(len(qq.slot_keys) for qq in g.questions) + offset
            if len(valid) < needed:
                r.add("OPTION_BANK_TOO_SMALL",
                      f"{len(valid)} options for {needed - offset} questions; this type "
                      f"needs at least {offset} spare distractor(s).",
                      path=f"{path}.group", subject_xid=g.xid,
                      fix_hint="Add more options, otherwise the last questions answer themselves.")
    elif source == "payload.options":
        valid = {str(o["id"]).casefold() for o in (q.payload.get("options") or [])}
    elif source == "fixed":
        valid = {str(o["id"]).casefold()
                 for o in definition.scoring.options.get("fixed_options", [])}
    elif source == "section.passage_version.paragraph_labels":
        valid = {label.casefold() for label in (s.passage.paragraph_labels if s.passage else ())}
        if not valid:
            r.add("PARAGRAPH_LABELS_MISSING",
                  "This question refers to passage paragraphs, but the passage has no lettering.",
                  path=path, subject_xid=q.xid,
                  fix_hint="Add paragraphs to the passage; letters are assigned automatically.")

    if valid:
        for slot, spec in (q.key or {}).get("slots", {}).items():
            for accepted in spec.get("accept", []):
                if str(accepted).casefold() not in valid:
                    r.add("KEY_OPTION_UNKNOWN",
                          f"Answer '{accepted}' for blank {slot} is not one of the options.",
                          path=f"{path}.key.{slot}", subject_xid=q.xid,
                          fix_hint="Pick an answer from the option list.")

    if definition.scoring.options.get("unique_options") is True:
        used = [str(spec["accept"][0]).casefold()
                for spec in (q.key or {}).get("slots", {}).values() if spec.get("accept")]
        if len(used) != len(set(used)):
            r.add("KEY_OPTION_REUSED",
                  "The same option is the answer to more than one blank, but this type "
                  "uses each option at most once.",
                  path=f"{path}.key", subject_xid=q.xid,
                  fix_hint="Give each blank a distinct answer.")


def _markers_in(container: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(container, list):
        for entry in container:
            found |= _markers_in(entry)
    elif isinstance(container, dict):
        for value in container.values():
            found |= _markers_in(value)
    elif isinstance(container, str):
        found |= set(_BLANK_MARKER.findall(container))
    return found


def _bank_offset(definition) -> int | None:
    for rule in definition.validation.get("rules", []):
        if rule.get("rule") == "option_bank_at_least":
            return int(rule.get("offset", 1))
    return None


def _check_set_selection_key(q: QuestionNode, definition, r: Report, path: str) -> None:
    correct = [str(c) for c in (q.key or {}).get("correct", [])]
    if not correct:
        r.add("KEY_MISSING", "Question has no correct answers.", path=f"{path}.key",
              subject_xid=q.xid, fix_hint="Select the correct letters.")
        return
    if len(set(c.casefold() for c in correct)) != len(correct):
        r.add("KEY_DUPLICATE", "The same letter is listed twice as correct.",
              path=f"{path}.key", subject_xid=q.xid, fix_hint="Remove the duplicate.")
    expected = q.payload.get("select_count")
    if expected is not None and len(correct) != int(expected):
        r.add("KEY_COUNT_MISMATCH",
              f"The question asks for {expected} answers but the key lists {len(correct)}.",
              path=f"{path}.key", subject_xid=q.xid,
              fix_hint="Match the key to the number of answers requested.")
    valid = {str(o["id"]).casefold() for o in (q.payload.get("options") or [])}
    for c in correct:
        if valid and c.casefold() not in valid:
            r.add("KEY_OPTION_UNKNOWN", f"Answer '{c}' is not one of the options.",
                  path=f"{path}.key", subject_xid=q.xid, fix_hint="Pick a listed option.")


def _check_word_limits(q: QuestionNode, g: GroupNode, definition,
                       r: Report, path: str) -> None:
    """Checks 7 and 8.

    Check 8 catches the authoring mistake that is otherwise invisible until a
    student hits it: an accepted answer longer than the stated limit can never be
    entered legally, so the item is unscoreable by construction.
    """
    rule = WordLimit.from_dict(g.word_limit)
    if rule is None:
        if _requires_word_limit(definition):
            r.add("WORD_LIMIT_MISSING",
                  f"{definition.title} needs a word limit on its question group.",
                  path=f"{path}.group", subject_xid=g.xid,
                  fix_hint="Set e.g. NO MORE THAN TWO WORDS AND/OR A NUMBER.")
        return
    for slot, spec in (q.key or {}).get("slots", {}).items():
        effective = WordLimit.from_dict(spec.get("word_limit")) or rule
        for accepted in spec.get("accept", []):
            violated, detail = wordlimit.violates(str(accepted), effective)
            if violated:
                r.add("KEY_EXCEEDS_WORD_LIMIT",
                      f"Accepted answer '{accepted}' for blank {slot} breaks this group's "
                      f"own word limit ({detail['counted_words']} words, "
                      f"{detail['counted_numbers']} number(s); limit "
                      f"{effective.max_words}).",
                      path=f"{path}.key.{slot}", subject_xid=q.xid,
                      fix_hint="Shorten the accepted answer, or raise the word limit.")


def _requires_word_limit(definition) -> bool:
    return any(rule.get("rule") == "word_limit_present_on_group"
               for rule in definition.validation.get("rules", []))


def _check_diagram(q: QuestionNode, g: GroupNode, definition, r: Report, path: str) -> None:
    """Check 16. A numbered blank with no position on the image cannot be rendered."""
    rules = {rule.get("rule") for rule in definition.validation.get("rules", [])}
    if "group_has_diagram_media" not in rules:
        return
    if g.diagram_media is None:
        r.add("DIAGRAM_MISSING", f"{definition.title} needs an image on its question group.",
              path=f"{path}.group", subject_xid=g.xid, fix_hint="Upload the diagram or map.")
        return
    if g.diagram_media.status != "ready":
        r.add("DIAGRAM_NOT_READY",
              f"The group's image is '{g.diagram_media.status}', not ready.",
              path=f"{path}.group", subject_xid=g.xid,
              fix_hint="Wait for processing, or re-upload.")
    if not g.diagram_media.has_attestation:
        r.add("ATTESTATION_MISSING", "The group's image has no copyright attestation.",
              path=f"{path}.group", subject_xid=g.xid,
              fix_hint="The uploader must affirm the material is original or licensed.")

    placed = {str(h.get("slot")) for h in g.hotspots}
    if "hotspots_cover_slots" in rules:
        unplaced = sorted(set(q.slot_keys) - placed)
        if unplaced:
            r.add("HOTSPOTS_INCOMPLETE",
                  f"No position on the image for blank(s): {', '.join(unplaced)}.",
                  path=f"{path}.group", subject_xid=g.xid,
                  fix_hint="Click the image to place each numbered blank.")
    if "hotspots_cover_option_bank" in rules:
        unplaced = sorted({str(o["id"]) for o in g.option_bank} - placed)
        if unplaced:
            r.add("HOTSPOTS_INCOMPLETE",
                  f"No position on the map for label(s): {', '.join(unplaced)}.",
                  path=f"{path}.group", subject_xid=g.xid,
                  fix_hint="Drop each lettered marker onto the map.")
    for h in g.hotspots:
        x, y = float(h.get("x", -1)), float(h.get("y", -1))
        if not (0 <= x <= 1 and 0 <= y <= 1):
            r.add("HOTSPOT_OUT_OF_BOUNDS",
                  f"Hotspot for {h.get('slot')} is outside the image.",
                  path=f"{path}.group", subject_xid=g.xid,
                  severity=Severity.ERROR,
                  fix_hint="Coordinates are fractions of the image, between 0 and 1.")
