"""Regrade execution: turn a staged job into an impact report, then into scores.

PRIVATE to the exam module. Lives beside `session.py` because it reuses the same
machinery for resolving an attempt's scoring inputs, and that machinery is
deliberately not public.

The shape mirrors `regrade.py`'s docstring, which is the contract with the user:

    plan()   — recompute every affected attempt, persist the COUNTS, touch nothing
    apply()  — recompute again and persist the RUNS, refusing on an undecided
               competition

Recomputing twice is deliberate. The dry run's output is a report a human reads,
possibly hours before deciding; keys can move in between, and applying a stale
plan would mark students against a key nobody approved. At 1.24 ms per attempt a
full recompute of every attempt this platform will hold in year one is about a
minute of CPU, so correctness is simply cheaper than caching here.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.modules.qtypes.registry import Scorer

from . import regrade
from .models import Attempt, AttemptAnswer, ItemScore, Outbox, RegradeJob, ScoreRun
from .scoring import AttemptInput, BandMap, ItemInput, KeyVersion
from .scoring import ScoreRun as ScoreRunValue

log = structlog.get_logger()

# One planning pass holds every affected attempt's responses in memory. 5,000 is
# ~40 MB at 40 items each, which is the right ceiling for a 4 GB box; past that
# the job chunks rather than swapping.
CHUNK = 5_000


def plan(session: Session, job: RegradeJob, scorer: Scorer) -> dict:
    """Dry run. Writes counts to the job and nothing else, anywhere."""
    attempts = _affected(session, job)
    if not attempts:
        _finish_planning(session, job, regrade.RegradeImpact())
        return job.report or {}

    impact = _recompute(session, attempts, scorer, reason="regrade_dry_run")
    _finish_planning(session, job, impact)
    return job.report or {}


def apply(session: Session, job: RegradeJob, scorer: Scorer,
          *, now: dt.datetime) -> int:
    """Commit. Every affected attempt whose SCORE moved gets a new run.

    Attempts whose score is unchanged are left alone: inserting an identical run
    would churn `score_runs` and make "what changed" unanswerable from the table
    that exists to answer it.
    """
    attempts = _affected(session, job)
    impact = _recompute(session, attempts, scorer, reason="regrade_key")

    decided = _decided_competitions(session, job)
    # Raises Conflict listing the undecided contests. The API checks this too, so
    # the caller gets a 409 rather than a failed job — but the domain rule lives
    # here, where it cannot be bypassed by a different caller.
    runs = regrade.apply(impact, competition_decisions=decided)

    attempt_ids = {str(a.xid): a.id for a in attempts}
    written = 0
    for run in runs:
        _persist(session, attempt_ids[run.attempt_xid], run, job, now)
        written += 1

    for notice in regrade.notifications_for(impact):
        _notify(session, notice, job)

    job.status = "completed"
    job.dry_run = False
    job.attempts_processed = impact.attempts_total
    job.scores_changed = impact.scores_changed
    job.bands_changed = impact.bands_changed
    job.finished_at = now
    job.report = impact.as_dict()
    session.flush()
    log.info("regrade_applied", job=str(job.xid), attempts=impact.attempts_total,
             runs_written=written, bands_changed=impact.bands_changed)
    return written


# ── selection ────────────────────────────────────────────────────────

def _affected(session: Session, job: RegradeJob) -> list[Attempt]:
    """Sat attempts only. Previews never enter a regrade — an author trying a
    draft is not a student with a band to correct."""
    query = select(Attempt).where(Attempt.mode != "preview",
                                  Attempt.status.in_(("submitted", "scored")))

    if job.subject_type == "question_version":
        query = query.where(Attempt.id.in_(
            select(ScoreRun.attempt_id).join(ItemScore,
                                             ItemScore.score_run_id == ScoreRun.id)
            .where(ItemScore.question_version_id == job.subject_id,
                   ScoreRun.is_current.is_(True))))
    elif job.subject_type == "test_version":
        query = query.where(Attempt.test_version_id == job.subject_id)
    elif job.subject_type == "band_map_version":
        from app.modules.content.models import TestVersion

        query = query.where(Attempt.test_version_id.in_(
            select(TestVersion.id).where(
                TestVersion.band_map_version_id == job.subject_id)))
    elif job.subject_type == "attempt":
        query = query.where(Attempt.id == job.subject_id)
    else:                                                      # pragma: no cover
        return []

    if from_date := (job.scope or {}).get("from_date"):
        query = query.where(Attempt.submitted_at >= dt.date.fromisoformat(from_date))
    # Competitions are never swept in. A finished contest is re-ranked only after
    # a recorded human decision, and that path re-enters here with an explicit
    # attempt scope.
    if not (job.scope or {}).get("_include_competition_attempts"):
        query = query.where(Attempt.competition_id.is_(None))

    return list(session.scalars(query.order_by(Attempt.id).limit(CHUNK)))


def _recompute(session: Session, attempts: list[Attempt], scorer: Scorer,
               *, reason: str) -> regrade.RegradeImpact:
    """Load once per TEST VERSION, not once per attempt.

    Forty students sitting the same mock share one item set, one key set and one
    band map. Resolving them per attempt is the difference between five queries
    and two hundred.
    """
    by_version: dict[int, tuple[list[ItemInput], dict[str, KeyVersion], BandMap | None]] = {}
    inputs: list[AttemptInput] = []
    previous: dict[str, ScoreRunValue] = {}
    keys: dict[str, KeyVersion] = {}

    responses_by_attempt = _responses(session, [a.id for a in attempts])
    previous_runs = {
        r.attempt_id: r for r in session.scalars(
            select(ScoreRun).where(ScoreRun.attempt_id.in_([a.id for a in attempts] or [0]),
                                   ScoreRun.is_current.is_(True)))
    }

    for attempt in attempts:
        if attempt.test_version_id not in by_version:
            by_version[attempt.test_version_id] = _inputs_for(session,
                                                              attempt.test_version_id)
        items, version_keys, band_map = by_version[attempt.test_version_id]
        keys.update(version_keys)
        inputs.append(AttemptInput(
            attempt_xid=str(attempt.xid), user_xid=str(attempt.user_id),
            items=tuple(items), responses=responses_by_attempt.get(attempt.id, {}),
            competition_xid=(str(attempt.competition_id)
                             if attempt.competition_id else None),
            mode=attempt.mode))
        run = previous_runs.get(attempt.id)
        if run is not None:
            previous[str(attempt.xid)] = _as_value(run)

    band_map = next((bm for _, _, bm in by_version.values() if bm is not None), None)
    return regrade.plan(inputs, previous, keys, scorer, band_map, reason=reason)


def _inputs_for(session: Session, test_version_id: int):
    """Same resolution `ExamSession` uses at submit time, by construction.

    Calling into the exam session's own helper rather than a parallel query is
    what keeps a regrade scoring the paper the way it was scored the first time —
    two implementations of "which items are on this test" would diverge, and the
    divergence would look like a scoring bug.
    """
    from .session import ExamSession

    probe = Attempt(test_version_id=test_version_id)
    return ExamSession._scoring_inputs(_Bare(session), probe)


class _Bare:
    """Just enough of an `ExamSession` for `_scoring_inputs`, which touches only
    the database handle. Cheaper and clearer than constructing a real one with a
    scorer and a clock it will not use."""

    __slots__ = ("_s",)

    def __init__(self, session: Session) -> None:
        self._s = session


def _responses(session: Session, attempt_ids: list[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in session.scalars(
        select(AttemptAnswer).where(AttemptAnswer.attempt_id.in_(attempt_ids or [0]))
    ):
        slots = out.setdefault(row.attempt_id, {}).setdefault(
            str(row.question_version_id), {}).setdefault("slots", {})
        slots[row.slot_key] = row.response
    return out


def _as_value(run: ScoreRun) -> ScoreRunValue:
    from decimal import Decimal

    return ScoreRunValue(
        attempt_xid="", reason=run.reason, engine_version=run.engine_version,
        band_map_xid=str(run.band_map_version_id) if run.band_map_version_id else None,
        key_versions=run.key_versions or {},
        raw_score=Decimal(str(run.raw_score)), max_raw=Decimal(str(run.max_raw)),
        band=Decimal(str(run.band)) if run.band is not None else None,
        per_section=run.per_section or {}, item_scores=())


# ── persistence ──────────────────────────────────────────────────────

def _persist(session: Session, attempt_id: int, run: ScoreRunValue,
             job: RegradeJob, now: dt.datetime) -> None:
    """Insert a NEW run and supersede the old one. Nothing is ever edited.

    A student's score history is the evidence that the regrade was fair, so the
    run they were originally shown survives with the reason it was replaced.
    """
    session.execute(
        update(ScoreRun)
        .where(ScoreRun.attempt_id == attempt_id, ScoreRun.is_current.is_(True))
        .values(is_current=False))
    session.flush()

    row = ScoreRun(
        attempt_id=attempt_id, reason=run.reason, regrade_job_id=job.id,
        engine_version=run.engine_version,
        band_map_version_id=int(run.band_map_xid) if run.band_map_xid else None,
        key_versions=run.key_versions, raw_score=run.raw_score, max_raw=run.max_raw,
        band=run.band, per_section=run.per_section, is_current=True,
        computed_at=now, computed_by=job.initiated_by,
        note=f"regrade {job.xid}: {job.reason}"[:500])
    session.add(row)
    session.flush()

    # Every version that produced a score. A void item — one whose question has
    # no answer key — is in `item_scores` and not in `key_versions`, so keying
    # this off the latter loses its question id and raises on the next line.
    qid_by_qv = _question_ids(session, [int(x) for x, _ in run.item_scores])
    for qv_id, item in run.item_scores:
        for slot in item.slots:
            session.add(ItemScore(
                score_run_id=row.id, question_id=qid_by_qv.get(int(qv_id), 0),
                question_version_id=int(qv_id),
                answer_key_version_id=(int(run.key_versions[qv_id])
                                       if qv_id in run.key_versions else None),
                slot_key=slot.slot_key, awarded=slot.awarded,
                max_points=slot.max_points, verdict=slot.verdict.value,
                raw_response=slot.raw_response,
                normalized_response=slot.normalized_response,
                matched_alternative=slot.matched_alternative, explain=slot.explain))
    session.execute(
        update(Attempt).where(Attempt.id == attempt_id)
        .values(current_score_run_id=row.id, scored_at=now))
    session.flush()


def _question_ids(session: Session, qv_ids: list[int]) -> dict[int, int]:
    from app.modules.content.models import QuestionVersion

    return {qv.id: qv.question_id for qv in session.scalars(
        select(QuestionVersion).where(QuestionVersion.id.in_(qv_ids or [0])))}


def _notify(session: Session, notice: dict, job: RegradeJob) -> None:
    """Only band changes reach a student, and only once.

    `dedupe_key` carries a UNIQUE index, so a redelivered job cannot message
    anyone twice — which is the whole reason the relay is allowed to be
    at-least-once.
    """
    session.execute(text("""
        INSERT INTO notifications (user_id, channel, template, params, dedupe_key)
        VALUES (:user_id, 'in_app', :template, CAST(:params AS jsonb), :dedupe)
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
    """).bindparams(
        user_id=int(notice["user_xid"]), template=notice["template"],
        params=json.dumps({**notice["params"], "regrade_job_xid": str(job.xid)}),
        dedupe=notice["dedupe_key"]))


def _finish_planning(session: Session, job: RegradeJob,
                     impact: regrade.RegradeImpact) -> None:
    job.attempts_total = impact.attempts_total
    job.scores_changed = impact.scores_changed
    job.bands_changed = impact.bands_changed
    job.competition_impact = impact.as_dict()["competition_impact"]
    job.report = impact.as_dict()
    # `ready`, not `completed`: a dry run that reports itself finished is one a
    # tired admin will assume was applied.
    job.status = "ready"
    session.flush()
    log.info("regrade_planned", job=str(job.xid), attempts=impact.attempts_total,
             scores_changed=impact.scores_changed, bands_changed=impact.bands_changed,
             blocked=impact.blocked_on_decision)


def _decided_competitions(session: Session, job: RegradeJob) -> set[str]:
    return set(session.scalars(text("""
        SELECT c.xid::text FROM competition_regrade_decisions d
        JOIN competitions c ON c.id = d.competition_id
        WHERE d.regrade_job_id = :j AND d.decision IS NOT NULL
    """).bindparams(j=job.id)))


def emit(session: Session, event_type: str, payload: dict,
         *, aggregate_id: str) -> None:
    session.add(Outbox(aggregate_type="regrade_job", aggregate_id=aggregate_id,
                       event_type=event_type, payload=payload))
