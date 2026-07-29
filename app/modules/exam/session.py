"""The exam session lifecycle — phase 8.

    issue -> in_progress -> submitted -> scored

Every rule here exists because of a specific way a student on an Uzbek mobile
network loses an exam. The server is the sole authority on time and on scoring;
the client is trusted with neither.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.modules.content.models import (
    AnswerKeyVersion, BandMapVersion, Question, QuestionGroupItem, QuestionGroupVersion,
    QuestionVersion, TestVersion, TestVersionGroup, TestVersionSection,
)
from app.modules.qtypes.registry import Scorer
from app.modules.qtypes.schemas import GroupRules
from app.platform import grants
from app.platform.clock import Clock
from app.platform.config import settings
from app.platform.errors import Conflict, Forbidden, NotFound

from .models import Attempt, AttemptAnswer, AttemptSection, ItemScore, Outbox, ScoreRun
from .scoring import AttemptInput, BandMap, ItemInput, KeyVersion, score_attempt


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    question_version_xid: str
    slot_key: str
    response: Any
    client_seq: int
    client_ts: dt.datetime | None = None
    time_spent_ms: int = 0


@dataclass(frozen=True, slots=True)
class SaveResult:
    accepted: int
    rejected: list[dict[str, str]]
    last_accepted_seq: int
    server_now: dt.datetime
    expires_at: dt.datetime | None
    seconds_remaining: int | None


class ExamSession:
    """All writes go through here. The HTTP layer owns no exam rules."""

    def __init__(self, session: Session, scorer: Scorer, clock: Clock,
                 grace_seconds: int = 30) -> None:
        self._s = session
        self._scorer = scorer
        self._clock = clock
        self._grace = grace_seconds

    # ── issue ────────────────────────────────────────────────────────
    def start(self, *, user_id: int, test_version_id: int, mode: str = "exam",
              assignment_id: int | None = None, org_context_id: int | None = None,
              time_limit_seconds: int | None = None) -> Attempt:
        """Idempotent by (user, assignment): a client that retries because the
        response was lost gets the SAME attempt back, never a second one against
        its attempt limit."""
        if assignment_id is not None:
            existing = self._s.scalars(
                select(Attempt).where(
                    Attempt.user_id == user_id,
                    Attempt.assignment_id == assignment_id,
                    Attempt.status.in_(["issued", "in_progress"]),
                )
            ).first()
            if existing is not None:
                return existing

        tv = self._s.get(TestVersion, test_version_id)
        if tv is None:
            raise NotFound("Test version not found.")
        if tv.status != "published" and mode != "preview":
            raise Conflict("Only a published test version can be sat.",
                           code="test_version_not_published")

        now = self._clock.now()
        limit = time_limit_seconds or (tv.config or {}).get("time_limit_seconds") or 3600
        attempt = Attempt(
            user_id=user_id, test_version_id=test_version_id,
            assignment_id=assignment_id, org_context_id=org_context_id,
            mode=mode, status="in_progress",
            issued_at=now, started_at=now,
            expires_at=now + dt.timedelta(seconds=limit),
            attempt_no=self._next_attempt_no(user_id, assignment_id),
        )
        self._s.add(attempt)
        self._s.flush()

        for s in self._s.scalars(
            select(TestVersionSection)
            .where(TestVersionSection.test_version_id == test_version_id)
            .order_by(TestVersionSection.position)
        ):
            self._s.add(AttemptSection(attempt_id=attempt.id, section_id=s.id,
                                       position=s.position))
        self._s.flush()
        self._emit(attempt, "attempt.started")
        return attempt

    def _next_attempt_no(self, user_id: int, assignment_id: int | None) -> int:
        if assignment_id is None:
            return 1
        used = self._s.scalars(
            select(Attempt.attempt_no).where(Attempt.user_id == user_id,
                                             Attempt.assignment_id == assignment_id)
        ).all()
        return max(used, default=0) + 1

    # ── in progress ──────────────────────────────────────────────────
    def payload(self, attempt: Attempt) -> dict[str, Any]:
        """One row read: the snapshot materialized at publish."""
        tv = self._s.get(TestVersion, attempt.test_version_id)
        if tv is None or tv.snapshot is None:
            raise NotFound("This test version has no published snapshot.")
        return tv.snapshot

    def save_answers(self, attempt: Attempt, deltas: list[AnswerDelta]) -> SaveResult:
        """Autosave. Batched, per-slot ordered, and partially acceptable.

        A delta whose `client_seq` is at or below the stored revision is dropped:
        retries plus out-of-order delivery on a flaky link would otherwise
        resurrect an older answer over a newer one. A delta that fails validation
        is rejected individually — one malformed answer must never cost a batch
        of forty.
        """
        now = self._clock.now()
        if attempt.status in ("submitted", "scored", "voided"):
            raise Conflict("This attempt is submitted; answers are frozen.",
                           code="attempt_frozen")
        if attempt.expires_at and now > attempt.expires_at + dt.timedelta(seconds=self._grace):
            # Sweeper has not run yet; do its job rather than accepting late work.
            self.submit(attempt, via="auto_expiry")
            raise Conflict("The attempt deadline has passed.", code="attempt_expired")

        xids = {d.question_version_xid for d in deltas}
        versions = {
            str(qv.xid): qv for qv in self._s.scalars(
                select(QuestionVersion).where(QuestionVersion.xid.in_(xids or {""}))
            )
        }
        existing = {
            (a.question_version_id, a.slot_key): a for a in self._s.scalars(
                select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt.id)
            )
        }

        accepted = 0
        rejected: list[dict[str, str]] = []
        last_seq = 0

        for d in deltas:
            qv = versions.get(d.question_version_xid)
            if qv is None:
                rejected.append({"slot_key": d.slot_key, "reason": "unknown_slot"})
                continue
            if d.slot_key not in (qv.slot_keys or []):
                rejected.append({"slot_key": d.slot_key, "reason": "unknown_slot"})
                continue

            row = existing.get((qv.id, d.slot_key))
            if row is not None and row.client_seq is not None and d.client_seq <= row.client_seq:
                rejected.append({"slot_key": d.slot_key, "reason": "stale_seq"})
                continue

            if row is None:
                row = AttemptAnswer(
                    attempt_id=attempt.id, question_version_id=qv.id, slot_key=d.slot_key,
                    response=d.response, revision=1, client_seq=d.client_seq,
                    client_ts=d.client_ts, first_answered_at=now, updated_at=now,
                    time_spent_ms=d.time_spent_ms,
                )
                self._s.add(row)
                existing[(qv.id, d.slot_key)] = row
            else:
                row.response = d.response
                row.revision += 1
                row.client_seq = d.client_seq
                row.client_ts = d.client_ts
                row.updated_at = now
                row.time_spent_ms += d.time_spent_ms
            accepted += 1
            last_seq = max(last_seq, d.client_seq)

        self._s.flush()
        return SaveResult(
            accepted=accepted, rejected=rejected, last_accepted_seq=last_seq,
            server_now=now, expires_at=attempt.expires_at,
            seconds_remaining=self._remaining(attempt, now),
        )

    def _remaining(self, attempt: Attempt, now: dt.datetime) -> int | None:
        if attempt.expires_at is None:
            return None
        return max(0, int((attempt.expires_at - now).total_seconds()))

    def enter_section(self, attempt: Attempt, position: int) -> AttemptSection:
        row = self._s.scalars(
            select(AttemptSection).where(AttemptSection.attempt_id == attempt.id,
                                         AttemptSection.position == position)
        ).first()
        if row is None:
            raise NotFound("Section not found in this attempt.")
        if row.entered_at is None:
            row.entered_at = self._clock.now()
            self._s.flush()
        return row

    def audio_grant(self, attempt: Attempt, position: int) -> dict[str, Any]:
        """Play-once, enforced server-side.

        A client-side play counter is a suggestion. This is not DRM and does not
        pretend to be: it stops replay through the app, not a determined student
        recording their screen. The goal is that ordinary replay is impossible
        and unusual behaviour leaves a trace.
        """
        row = self.enter_section(attempt, position)
        section = self._s.get(TestVersionSection, row.section_id)
        now = self._clock.now()

        media_xid = self._delivery_media_xid(section)
        if media_xid is None:
            # BEFORE the lock is burned. Asking for an audio grant on a section
            # with no audio is a client bug, and the placeholder implementation
            # answered it with a meaningless token while consuming the student's
            # single play — so a stray call cost them the section.
            raise NotFound("This section has no audio.", code="section_has_no_audio")

        play_once = bool(section and section.play_once) and attempt.mode == "exam"
        if play_once and row.audio_locked_at is not None:
            raise Conflict("This section's audio has already been played.",
                           code="audio_already_played")

        row.audio_play_count += 1
        row.audio_started_at = row.audio_started_at or now
        if play_once:
            row.audio_locked_at = now
        self._s.flush()

        ttl = settings().media_grant_ttl_seconds
        return {
            "grant": self._sign_grant(attempt, media_xid, play_once, now, ttl),
            "media_xid": media_xid,
            "expires_at": now + dt.timedelta(seconds=ttl),
            "plays_remaining": 0 if play_once else None,
        }

    def _delivery_media_xid(self, section) -> str | None:
        """The audio TRACK's delivery asset, not the track itself.

        `attempt_sections` names a track; a track owns a master upload and a
        transcoded delivery file. Students get the delivery file — a 14 MB m4a
        rather than a 400 MB wav — and the grant must name the object the media
        endpoint will actually serve.
        """
        if section is None or section.audio_track_id is None:
            return None
        return self._s.scalar(text("""
            SELECT coalesce(d.xid, m.xid)::text
            FROM audio_tracks t
            LEFT JOIN media_assets d ON d.id = t.delivery_media_id
            LEFT JOIN media_assets m ON m.id = t.master_media_id
            WHERE t.id = :t
        """).bindparams(t=section.audio_track_id))

    def _sign_grant(self, attempt: Attempt, media_xid: str | None,
                    play_once: bool, now: dt.datetime, ttl: int) -> str | None:
        """A real HMAC over the app secret, bound to user, object and expiry.

        Replaces a placeholder SHA-256 of the same inputs, which verified nothing
        — anyone could compute it. `purpose` carries whether this was a play-once
        exam grant, so a review-mode grant cannot be replayed as an exam one.
        """
        if media_xid is None:
            return None
        user_xid = self._s.scalar(
            text("SELECT xid::text FROM users WHERE id = :u")
            .bindparams(u=attempt.user_id))
        return grants.issue(user_xid=user_xid, media_xid=media_xid,
                            purpose="exam" if play_once else "practice",
                            attempt_xid=str(attempt.xid), ttl_seconds=ttl, now=now)

    # ── submit and score ─────────────────────────────────────────────
    def submit(self, attempt: Attempt, *, via: str = "user",
               final_answers: list[AnswerDelta] | None = None) -> ScoreRun:
        """Freeze, score, record.

        A submission arriving slightly after the deadline is ACCEPTED and the
        overrun recorded in `late_by_ms`. On these networks four seconds late is a
        hiccup, not cheating: anti-cheat reads that field, the scorer does not.
        """
        now = self._clock.now()
        if attempt.status in ("submitted", "scored"):
            run = self._current_run(attempt)
            if run is not None:
                return run

        if final_answers:
            try:
                self.save_answers(attempt, final_answers)
            except Conflict:
                pass  # Deadline passed mid-flush; the answers already stored stand.

        if attempt.expires_at and now > attempt.expires_at:
            attempt.late_by_ms = int((now - attempt.expires_at).total_seconds() * 1000)
        attempt.status = "submitted"
        attempt.submitted_at = now
        attempt.submitted_via = via
        self._s.flush()

        run = self._score(attempt, reason="initial")
        attempt.status = "scored"
        attempt.scored_at = self._clock.now()
        attempt.current_score_run_id = run.id
        self._s.flush()
        self._emit(attempt, "attempt.scored", {"score_run_id": run.id})
        return run

    def _current_run(self, attempt: Attempt) -> ScoreRun | None:
        return self._s.scalars(
            select(ScoreRun).where(ScoreRun.attempt_id == attempt.id,
                                   ScoreRun.is_current.is_(True))
        ).first()

    def _score(self, attempt: Attempt, *, reason: str) -> ScoreRun:
        items, keys, band_map = self._scoring_inputs(attempt)
        responses: dict[str, Any] = {}
        for a in self._s.scalars(
            select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt.id)
        ):
            responses.setdefault(str(a.question_version_id), {}).setdefault("slots", {})
            responses[str(a.question_version_id)]["slots"][a.slot_key] = a.response

        result = score_attempt(
            AttemptInput(attempt_xid=str(attempt.xid), user_xid=str(attempt.user_id),
                         items=tuple(items), responses=responses,
                         competition_xid=(str(attempt.competition_id)
                                          if attempt.competition_id else None),
                         mode=attempt.mode),
            keys, self._scorer, band_map, reason=reason,
        )

        # Exactly one current run per attempt is a database constraint; flip the
        # old one first so a bug produces an error rather than two truths.
        self._s.execute(
            update(ScoreRun).where(ScoreRun.attempt_id == attempt.id,
                                   ScoreRun.is_current.is_(True))
            .values(is_current=False)
        )
        run = ScoreRun(
            attempt_id=attempt.id, reason=reason,
            engine_version=result.engine_version,
            band_map_version_id=(int(result.band_map_xid) if result.band_map_xid else None),
            key_versions=result.key_versions,
            raw_score=result.raw_score, max_raw=result.max_raw,
            band=result.band, per_section=result.per_section,
            is_current=True, computed_at=self._clock.now(),
        )
        self._s.add(run)
        self._s.flush()

        qid_by_qv = {
            str(qv.id): qv.question_id for qv in self._s.scalars(
                select(QuestionVersion).where(
                    QuestionVersion.id.in_([int(x) for x in result.key_versions] or [0]))
            )
        }
        for qv_id, item in result.item_scores:
            for slot in item.slots:
                self._s.add(ItemScore(
                    score_run_id=run.id,
                    question_id=qid_by_qv.get(qv_id, 0),
                    question_version_id=int(qv_id),
                    answer_key_version_id=int(result.key_versions[qv_id]),
                    slot_key=slot.slot_key,
                    awarded=slot.awarded, max_points=slot.max_points,
                    verdict=slot.verdict.value,
                    raw_response=slot.raw_response,
                    normalized_response=slot.normalized_response,
                    matched_alternative=slot.matched_alternative,
                    explain=slot.explain,
                ))
        self._s.flush()
        return run

    def _scoring_inputs(self, attempt: Attempt
                        ) -> tuple[list[ItemInput], dict[str, KeyVersion], BandMap | None]:
        rows = self._s.execute(
            select(QuestionVersion, QuestionGroupVersion, TestVersionSection, Question)
            .join(QuestionGroupItem,
                  QuestionGroupItem.question_version_id == QuestionVersion.id)
            .join(QuestionGroupVersion,
                  QuestionGroupVersion.id == QuestionGroupItem.group_version_id)
            .join(TestVersionGroup,
                  TestVersionGroup.group_version_id == QuestionGroupVersion.id)
            .join(TestVersionSection,
                  TestVersionSection.id == TestVersionGroup.section_id)
            .join(Question, Question.id == QuestionVersion.question_id)
            .where(TestVersionSection.test_version_id == attempt.test_version_id)
            .order_by(TestVersionSection.position, TestVersionGroup.position,
                      QuestionGroupItem.position)
        ).all()

        items: list[ItemInput] = []
        for qv, gv, section, question in rows:
            items.append(ItemInput(
                question_xid=str(question.id),
                question_version_xid=str(qv.id),
                type_key=qv.type_key, type_version=qv.type_version,
                payload=qv.payload or {}, slot_keys=tuple(qv.slot_keys or ()),
                skill=section.skill,
                group=GroupRules.from_dict({
                    "word_limit": gv.word_limit,
                    "option_bank": gv.option_bank or [],
                    "unique_options": (gv.display or {}).get("unique_options"),
                }),
            ))

        keys = {
            str(k.question_version_id): KeyVersion(
                xid=str(k.id), key=k.key or {}, tolerance=k.tolerance or {})
            for k in self._s.scalars(
                select(AnswerKeyVersion).where(
                    AnswerKeyVersion.question_version_id.in_(
                        [int(i.question_version_xid) for i in items] or [0]),
                    AnswerKeyVersion.is_current.is_(True),
                )
            )
        }

        band_map = None
        tv = self._s.get(TestVersion, attempt.test_version_id)
        if tv is not None and tv.band_map_version_id:
            bmv = self._s.get(BandMapVersion, tv.band_map_version_id)
            if bmv is not None:
                band_map = BandMap(
                    xid=str(bmv.id), max_raw=bmv.max_raw,
                    rows=tuple((int(r["raw_min"]), int(r["raw_max"]), Decimal(str(r["band"])))
                               for r in (bmv.mapping or [])),
                )
        return items, keys, band_map

    # ── sweeper ──────────────────────────────────────────────────────
    def auto_submit_expired(self, limit: int = 200) -> list[int]:
        """Backed by a partial index on in-progress attempts, so it scans a few
        hundred live rows rather than the whole history."""
        now = self._clock.now()
        due = self._s.scalars(
            select(Attempt)
            .where(Attempt.status == "in_progress",
                   Attempt.expires_at < now - dt.timedelta(seconds=self._grace))
            .limit(limit)
        ).all()
        submitted = []
        for attempt in due:
            self.submit(attempt, via="auto_expiry")
            submitted.append(attempt.id)
        return submitted

    # ── review ───────────────────────────────────────────────────────
    def review(self, attempt: Attempt) -> list[dict[str, Any]]:
        """Per-item, with the marking explanation. Gated by the assignment's
        `allow_review_after` at the API layer, not here."""
        run = self._current_run(attempt)
        if run is None:
            raise Conflict("This attempt has not been scored.", code="not_scored")
        keys = {
            k.id: k for k in self._s.scalars(
                select(AnswerKeyVersion).where(
                    AnswerKeyVersion.id.in_(
                        [int(v) for v in (run.key_versions or {}).values()] or [0]))
            )
        }
        out = []
        for s in self._s.scalars(
            select(ItemScore).where(ItemScore.score_run_id == run.id).order_by(ItemScore.id)
        ):
            key = keys.get(s.answer_key_version_id)
            accepted = []
            if key:
                slot = (key.key or {}).get("slots", {}).get(s.slot_key, {})
                accepted = slot.get("accept", []) or (key.key or {}).get("correct", [])
            out.append({
                "question_version_id": s.question_version_id,
                "slot_key": s.slot_key,
                "verdict": s.verdict,
                "awarded": float(s.awarded),
                "max_points": float(s.max_points),
                "raw_response": s.raw_response,
                "normalized_response": s.normalized_response,
                "accepted_answers": accepted,
                "matched_alternative": s.matched_alternative,
                "explain": s.explain,
            })
        return out

    # ── outbox ───────────────────────────────────────────────────────
    def _emit(self, attempt: Attempt, event_type: str,
              extra: dict[str, Any] | None = None) -> None:
        """Same transaction as the domain change — that is the whole point."""
        self._s.add(Outbox(
            aggregate_type="attempt", aggregate_id=str(attempt.xid),
            event_type=event_type,
            payload={"attempt_id": attempt.id, "user_id": attempt.user_id,
                     "mode": attempt.mode, **(extra or {})},
            created_at=self._clock.now(), available_at=self._clock.now(),
        ))


def response_hash(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
