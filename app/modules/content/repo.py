"""PRIVATE to content. Builds the value objects other modules are allowed to see.

Two functions carry the module's contract with the rest of the system:

  * `load_composition()` -> the DB-free tree the publish gate validates.
  * `build_snapshot()`   -> the student-facing document, materialized at publish.

The snapshot deliberately excludes answer keys and transcripts. It is the document
that goes to the device, so anything secret must not be in it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .composition import (
    BandMapRef,
    GroupNode,
    MediaRef,
    PassageRef,
    QuestionNode,
    SectionNode,
    TestComposition,
)
from .models import (
    AnswerKeyVersion,
    AudioTrack,
    BandMapVersion,
    PassageVersion,
    QuestionGroupItem,
    QuestionGroupVersion,
    QuestionVersion,
    TestVersion,
    TestVersionGroup,
    TestVersionSection,
)


def _checksum(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def load_composition(session: Session, test_version_id: int) -> TestComposition:
    """Assemble the whole tree in a bounded number of queries.

    Five round trips regardless of test size, not one per question — the naive
    version is 40+ queries for a mock and shows up immediately as a slow publish.
    """
    tv = session.get(TestVersion, test_version_id)
    if tv is None:
        raise LookupError(f"test_version {test_version_id} not found")

    sections = session.scalars(
        select(TestVersionSection)
        .where(TestVersionSection.test_version_id == test_version_id)
        .order_by(TestVersionSection.position)
    ).all()

    placements = session.scalars(
        select(TestVersionGroup)
        .where(TestVersionGroup.section_id.in_([s.id for s in sections] or [0]))
        .order_by(TestVersionGroup.section_id, TestVersionGroup.position)
    ).all()

    gv_ids = [p.group_version_id for p in placements]
    group_versions = {
        g.id: g for g in session.scalars(
            select(QuestionGroupVersion).where(QuestionGroupVersion.id.in_(gv_ids or [0]))
        )
    }
    items = session.scalars(
        select(QuestionGroupItem)
        .where(QuestionGroupItem.group_version_id.in_(gv_ids or [0]))
        .order_by(QuestionGroupItem.group_version_id, QuestionGroupItem.position)
    ).all()

    qv_ids = [i.question_version_id for i in items]
    question_versions = {
        q.id: q for q in session.scalars(
            select(QuestionVersion).where(QuestionVersion.id.in_(qv_ids or [0]))
        )
    }
    # Only the CURRENT key per question version — a superseded key must never
    # affect a publish decision.
    keys = {
        k.question_version_id: k for k in session.scalars(
            select(AnswerKeyVersion).where(
                AnswerKeyVersion.question_version_id.in_(qv_ids or [0]),
                AnswerKeyVersion.is_current.is_(True),
            )
        )
    }

    passage_versions = {
        p.id: p for p in session.scalars(
            select(PassageVersion).where(
                PassageVersion.id.in_([s.passage_version_id for s in sections
                                       if s.passage_version_id] or [0]))
        )
    }
    audio_tracks = {
        a.id: a for a in session.scalars(
            select(AudioTrack).where(
                AudioTrack.id.in_([s.audio_track_id for s in sections
                                   if s.audio_track_id] or [0]))
        )
    }

    # `PassageRef.has_attestation` defaults to True and `under_takedown` to
    # False, and NOTHING SET EITHER — so `publish_gate` checks 17 and 18, the two
    # copyright checks, could not fire. A passage with no attestation published
    # cleanly, and so did one under an open takedown. Both defaults are the safe
    # direction for a dataclass and the wrong direction for a gate, which is why
    # it read as working.
    #
    # Two queries for the whole composition rather than one per section: the
    # attestation is per subject row, and a takedown is open until it is
    # decided. `hidden_at` is not the test — a rejected claim leaves it set —
    # so the status is.
    passage_ids = {p.passage_id for p in passage_versions.values()}
    track_ids = {a.id for a in audio_tracks.values()}
    attested = {
        (row[0], row[1]) for row in session.execute(text("""
            SELECT subject_type, subject_id FROM content_attestations
            WHERE (subject_type = 'passage' AND subject_id = ANY(:passages))
               OR (subject_type = 'audio_track' AND subject_id = ANY(:tracks))
        """).bindparams(passages=list(passage_ids) or [0],
                        tracks=list(track_ids) or [0]))
    }
    under_takedown = {
        (row[0], row[1]) for row in session.execute(text("""
            SELECT subject_type, subject_id FROM takedown_requests
            WHERE status IN ('received', 'reviewing', 'upheld')
              AND ((subject_type = 'passage' AND subject_id = ANY(:passages))
                OR (subject_type = 'audio_track' AND subject_id = ANY(:tracks)))
        """).bindparams(passages=list(passage_ids) or [0],
                        tracks=list(track_ids) or [0]))
    }

    band_map = None
    if tv.band_map_version_id:
        bmv = session.get(BandMapVersion, tv.band_map_version_id)
        if bmv is not None:
            band_map = BandMapRef(xid=str(bmv.id), max_raw=bmv.max_raw,
                                  mapping=tuple(bmv.mapping or ()))

    items_by_group: dict[int, list[QuestionGroupItem]] = {}
    for i in items:
        items_by_group.setdefault(i.group_version_id, []).append(i)
    placements_by_section: dict[int, list[TestVersionGroup]] = {}
    for p in placements:
        placements_by_section.setdefault(p.section_id, []).append(p)

    section_nodes = []
    for s in sections:
        group_nodes = []
        for p in placements_by_section.get(s.id, []):
            gv = group_versions.get(p.group_version_id)
            if gv is None:
                continue
            question_nodes = []
            for i in items_by_group.get(gv.id, []):
                qv = question_versions.get(i.question_version_id)
                if qv is None:
                    continue
                key = keys.get(qv.id)
                question_nodes.append(QuestionNode(
                    xid=str(qv.xid), type_key=qv.type_key, type_version=qv.type_version,
                    payload=qv.payload or {}, slot_keys=tuple(qv.slot_keys or ()),
                    points=float(qv.points or 1),
                    key=key.key if key else None,
                    key_version_xid=str(key.xid) if key else None,
                    tolerance=(key.tolerance if key else {}) or {},
                ))
            group_nodes.append(GroupNode(
                xid=str(gv.xid), number_start=p.number_start,
                questions=tuple(question_nodes),
                instructions=gv.instructions or {}, word_limit=gv.word_limit,
                option_bank=tuple(gv.option_bank or ()),
                unique_options=(gv.display or {}).get("unique_options"),
                diagram_media=(MediaRef(xid=str(gv.diagram_media_id))
                               if gv.diagram_media_id else None),
                hotspots=tuple(gv.hotspots or ()),
                audio_start_ms=p.audio_start_ms, audio_end_ms=p.audio_end_ms,
            ))

        pv = passage_versions.get(s.passage_version_id) if s.passage_version_id else None
        at = audio_tracks.get(s.audio_track_id) if s.audio_track_id else None
        section_nodes.append(SectionNode(
            position=s.position, skill=s.skill, title=s.title,
            groups=tuple(group_nodes),
            passage=(PassageRef(
                xid=str(pv.xid), title=pv.title,
                paragraph_labels=tuple(pv.paragraph_labels or ()),
                blocks=tuple(pv.blocks or ()),
                # Against the PASSAGE, not the version. An attestation is a
                # claim about the material, and cutting a new version does not
                # make it somebody else's work — nor clear a takedown against it.
                has_attestation=("passage", pv.passage_id) in attested,
                under_takedown=("passage", pv.passage_id) in under_takedown,
            ) if pv else None),
            audio=(MediaRef(
                xid=str(at.xid), status=at.status, duration_ms=at.duration_ms,
                has_attestation=("audio_track", at.id) in attested,
                under_takedown=("audio_track", at.id) in under_takedown,
            ) if at else None),
            time_limit_seconds=s.time_limit_seconds,
            declared_question_count=s.declared_question_count,
            play_once=s.play_once,
        ))

    return TestComposition(xid=str(tv.xid), title=tv.title,
                           sections=tuple(section_nodes), band_map=band_map)


def build_snapshot(composition: TestComposition) -> dict[str, Any]:
    """The student-facing document. NO answer keys, NO transcripts."""
    number = 1
    sections = []
    for s in composition.sections:
        groups = []
        for g in s.groups:
            questions = []
            for q in g.questions:
                questions.append({
                    "number": number,
                    "question_version_xid": q.xid,
                    "type_key": q.type_key,
                    "type_version": q.type_version,
                    "payload": q.payload,
                    "slot_keys": list(q.slot_keys),
                })
                number += len(q.slot_keys)
            groups.append({
                "number_start": g.number_start,
                "instructions": g.instructions,
                "word_limit": g.word_limit,
                "option_bank": list(g.option_bank),
                "diagram_media_xid": g.diagram_media.xid if g.diagram_media else None,
                "hotspots": list(g.hotspots),
                "audio_start_ms": g.audio_start_ms,
                "audio_end_ms": g.audio_end_ms,
                "questions": questions,
            })
        sections.append({
            "position": s.position, "skill": s.skill, "title": s.title,
            "time_limit_seconds": s.time_limit_seconds,
            "passage": ({"title": s.passage.title,
                         "blocks": list(s.passage.blocks),
                         "paragraph_labels": list(s.passage.paragraph_labels)}
                        if s.passage else None),
            # `track_xid`, because that is what it is. `MediaRef.xid` is built
            # from the AudioTrack, and the field was called `media_xid` — so
            # anything that used it against `GET /media/{xid}/content` got 403
            # `grant_wrong_media`, which reads to a user as an expiry and to a
            # developer as nothing. Nothing depends on it: the player takes
            # `media_xid` off the grant response, which names the delivery
            # asset. Renamed rather than resolved to a media id here, because
            # the snapshot must not carry a second identifier for an object the
            # student can only reach through a grant anyway.
            "audio": ({"track_xid": s.audio.xid, "duration_ms": s.audio.duration_ms,
                       "play_once": s.play_once} if s.audio else None),
            "groups": groups,
        })
    return {
        "test_version_xid": composition.xid,
        "title": composition.title,
        "total_questions": composition.total_slots,
        "sections": sections,
    }


def publish(session: Session, test_version_id: int, published_by: int,
            now: dt.datetime) -> TestVersion:
    """Freeze the version and materialize the snapshot.

    The caller runs the publish gate first; this function assumes it passed. It is
    split that way so the gate stays a pure function and this stays a pure write.
    """
    composition = load_composition(session, test_version_id)
    snapshot = build_snapshot(composition)
    payload = json.dumps(snapshot, separators=(",", ":"), default=str)

    # `get_one`, not `get`: the caller resolved the version before running the
    # gate, so a missing row is a programming error, and `NoResultFound` says
    # that where an AttributeError on None would not.
    tv = session.get_one(TestVersion, test_version_id)
    tv.snapshot = snapshot
    tv.snapshot_bytes = len(payload)
    tv.total_questions = composition.total_slots
    tv.max_raw = composition.total_slots
    tv.checksum = _checksum(snapshot)
    tv.status = "published"
    tv.published_at = now
    tv.published_by = published_by
    session.flush()
    return tv
