"""Regrade.

A bad answer key is the fastest way to lose a school client, so fixing one must
be safe, previewable and auditable. The shape:

    1. The author fixes a key -> a NEW answer_key_version supersedes the old one.
       The question version stays frozen, so attempts still bind to what was sat.
    2. A regrade job is staged in DRY RUN. Nothing is touched yet.
    3. The author/admin sees the impact: how many attempts, how many scores move,
       how many BANDS move, and whether any finished competition is affected.
    4. On confirm, each affected attempt gets a NEW score run. Old runs are
       retained — a student's score history is preserved, not overwritten.
    5. Only students whose BAND changed are notified. A raw-score wobble that
       leaves the band alone is not worth a push notification.

Step 6 is the one people miss: competitions do NOT auto-regrade. See
`competition_impact` and ADR-0001 §8.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from app.modules.qtypes.registry import Scorer
from app.platform.errors import Conflict

from .scoring import AttemptInput, BandMap, KeyVersion, ScoreRun, score_attempt


@dataclass(frozen=True, slots=True)
class AttemptDelta:
    attempt_xid: str
    user_xid: str
    competition_xid: str | None
    old_raw: Decimal
    new_raw: Decimal
    old_band: Decimal | None
    new_band: Decimal | None
    new_run: ScoreRun

    @property
    def score_changed(self) -> bool:
        return self.old_raw != self.new_raw

    @property
    def band_changed(self) -> bool:
        return self.old_band != self.new_band

    @property
    def improved(self) -> bool:
        return self.new_raw > self.old_raw


@dataclass(frozen=True, slots=True)
class CompetitionImpact:
    competition_xid: str
    attempts: int
    rank_changes: int
    podium_changes: int

    @property
    def decision_required(self) -> bool:
        return self.rank_changes > 0


@dataclass(slots=True)
class RegradeImpact:
    attempts_total: int = 0
    scores_changed: int = 0
    bands_changed: int = 0
    improved: int = 0
    worsened: int = 0
    deltas: list[AttemptDelta] = field(default_factory=list)
    competition_impact: list[CompetitionImpact] = field(default_factory=list)

    @property
    def students_to_notify(self) -> int:
        """Band changes only. Notifying on every raw-score wobble trains students
        to ignore the channel you need for the changes that matter."""
        return self.bands_changed

    @property
    def blocked_on_decision(self) -> bool:
        return any(c.decision_required for c in self.competition_impact)

    def as_dict(self) -> dict:
        return {
            "attempts_total": self.attempts_total,
            "scores_changed": self.scores_changed,
            "bands_changed": self.bands_changed,
            "improved": self.improved,
            "worsened": self.worsened,
            "students_to_notify": self.students_to_notify,
            "competition_impact": [
                {
                    "competition_xid": c.competition_xid,
                    "attempts": c.attempts,
                    "rank_changes": c.rank_changes,
                    "podium_changes": c.podium_changes,
                    "decision_required": c.decision_required,
                }
                for c in self.competition_impact
            ],
        }


def plan(
    attempts: Iterable[AttemptInput],
    previous_runs: dict[str, ScoreRun],
    new_keys: dict[str, KeyVersion],
    scorer: Scorer,
    band_map: BandMap | None,
    *,
    reason: str = "regrade_key",
) -> RegradeImpact:
    """DRY RUN. Recomputes every affected attempt and reports what would change,
    without persisting anything."""
    impact = RegradeImpact()
    by_competition: dict[str, list[AttemptDelta]] = {}

    for attempt in attempts:
        previous = previous_runs.get(attempt.attempt_xid)
        new_run = score_attempt(attempt, new_keys, scorer, band_map, reason=reason)
        delta = AttemptDelta(
            attempt_xid=attempt.attempt_xid,
            user_xid=attempt.user_xid,
            competition_xid=attempt.competition_xid,
            old_raw=previous.raw_score if previous else Decimal(0),
            new_raw=new_run.raw_score,
            old_band=previous.band if previous else None,
            new_band=new_run.band,
            new_run=new_run,
        )
        impact.attempts_total += 1
        impact.deltas.append(delta)
        if delta.score_changed:
            impact.scores_changed += 1
            if delta.improved:
                impact.improved += 1
            else:
                impact.worsened += 1
        if delta.band_changed:
            impact.bands_changed += 1
        if attempt.competition_xid:
            by_competition.setdefault(attempt.competition_xid, []).append(delta)

    for competition_xid, deltas in by_competition.items():
        impact.competition_impact.append(_competition_impact(competition_xid, deltas))
    return impact


def _competition_impact(competition_xid: str, deltas: list[AttemptDelta]) -> CompetitionImpact:
    """Ranking is recomputed both ways so the report can say "12 rank changes,
    one podium change" rather than "some results may differ"."""
    before = _rank({d.attempt_xid: d.old_raw for d in deltas})
    after = _rank({d.attempt_xid: d.new_raw for d in deltas})
    rank_changes = sum(1 for xid in before if before[xid] != after[xid])
    podium_changes = sum(
        1 for xid in before
        if (before[xid] <= 3) != (after[xid] <= 3) or (before[xid] <= 3 and before[xid] != after[xid])
    )
    return CompetitionImpact(
        competition_xid=competition_xid,
        attempts=len(deltas),
        rank_changes=rank_changes,
        podium_changes=podium_changes,
    )


def _rank(scores: dict[str, Decimal]) -> dict[str, int]:
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return {xid: position for position, (xid, _) in enumerate(ordered, start=1)}


def apply(impact: RegradeImpact, *, competition_decisions: set[str] | None = None) -> list[ScoreRun]:
    """Commit a planned regrade.

    Refuses while any affected competition still lacks a recorded decision. A
    leaderboard that changes by itself looks like fraud, so the refusal is
    structural rather than a convention someone has to remember.
    """
    decided = competition_decisions or set()
    undecided = [
        c.competition_xid for c in impact.competition_impact
        if c.decision_required and c.competition_xid not in decided
    ]
    if undecided:
        raise Conflict(
            "Regrade would change finished competition rankings; a platform admin "
            "must decide first.",
            code="competition_decision_required",
            competitions=undecided,
        )
    return [d.new_run for d in impact.deltas if d.score_changed]


def notifications_for(impact: RegradeImpact) -> list[dict]:
    """One notice per student whose band moved, deduplicated by (attempt, run) so
    a retried job cannot message anyone twice."""
    return [
        {
            "user_xid": d.user_xid,
            "template": "regrade.band_changed",
            "dedupe_key": f"regrade:{d.attempt_xid}:{d.new_raw}",
            "params": {
                "attempt_xid": d.attempt_xid,
                "old_band": float(d.old_band) if d.old_band is not None else None,
                "new_band": float(d.new_band) if d.new_band is not None else None,
                "direction": "up" if d.improved else "down",
            },
        }
        for d in impact.deltas if d.band_changed
    ]
