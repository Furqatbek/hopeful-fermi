"""Read models. Raw SQL only — analytics imports no other module by contract.

That contract (`pyproject.toml`, "analytics reads nothing at runtime; it owns its
read models") is not bureaucracy. Reporting is the part of a system that grows
tentacles into everything, and the day it imports the exam module is the day a
slow dashboard query can hold a lock that a student's submit is waiting on. It
reads tables and owns its own.

Everything here is an UPSERT over a window, so a job that runs twice produces the
same rows. That is what lets the relay be at-least-once.
"""

from __future__ import annotations

import datetime as dt
import json

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from .stats import Response, analyse, burn_score

log = structlog.get_logger()

# Statistics are computed over a rolling window. Ninety days is long enough to
# accumulate responses at MVP volumes and short enough that a key fixed in March
# is not still dragging down the item's p-value in June.
WINDOW = dt.timedelta(days=90)


def refresh_cohort_progress(session: Session) -> None:
    """`REFRESH ... CONCURRENTLY`, which is why the unique index exists.

    Without CONCURRENTLY this takes an ACCESS EXCLUSIVE lock and every teacher
    looking at a dashboard gets a 500 for its duration.
    """
    session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_cohort_progress"))
    log.info("mv_refreshed", view="mv_cohort_progress")


def refresh_item_stats(session: Session, *, now: dt.datetime,
                       question_ids: list[int] | None = None) -> int:
    """Difficulty, discrimination, distractors and common wrong answers.

    Computed per item in Python rather than as one enormous SQL statement: the
    point-biserial has four boundary cases that must return NULL rather than a
    confident zero, and expressing those in SQL is where the bug would hide.
    """
    window_start = (now - WINDOW).date()
    scope = "AND s.question_id = ANY(:qids)" if question_ids else ""

    rows = session.execute(text(f"""
        SELECT s.question_id, s.question_version_id, s.answer_key_version_id,
               a.org_context_id AS org_id, a.user_id,
               bool_and(s.verdict = 'correct') AS correct,
               max(s.raw_response) AS raw_response,
               max(totals.total) AS total_score
        FROM item_scores s
        JOIN score_runs r ON r.id = s.score_run_id AND r.is_current
        JOIN attempts a   ON a.id = r.attempt_id AND a.mode <> 'preview'
        JOIN LATERAL (SELECT r.raw_score AS total) totals ON true
        WHERE a.submitted_at >= :since {scope}
        GROUP BY s.question_id, s.question_version_id, s.answer_key_version_id,
                 a.org_context_id, a.user_id, r.id
    """).bindparams(since=now - WINDOW,
                    **({"qids": question_ids} if question_ids else {}))).mappings().all()

    grouped: dict[tuple, list[Response]] = {}
    for row in rows:
        # Two rows per item where there is an organization: one global and one
        # per centre. A centre wants to know how ITS cohort did on the item,
        # which is a different and more actionable number than the platform
        # average.
        #
        # A SET, not a tuple. `(None, row["org_id"])` collapses to `(None, None)`
        # for a self-serve attempt and counts every response twice — which shows
        # up as "6 students wrote 'bike'" when three did, and would have made the
        # most useful number in this table quietly wrong.
        scopes = {None} | ({row["org_id"]} if row["org_id"] is not None else set())
        for org in scopes:
            key = (row["question_id"], row["question_version_id"],
                   row["answer_key_version_id"], org)
            grouped.setdefault(key, []).append(Response(
                user_xid=str(row["user_id"]), correct=bool(row["correct"]),
                total_score=float(row["total_score"] or 0),
                raw_response=row["raw_response"]))

    written = 0
    for (question_id, qv_id, key_id, org_id), responses in grouped.items():
        stats = analyse(responses)
        session.execute(text("""
            INSERT INTO item_stats (question_id, question_version_id,
                                    answer_key_version_id, org_id, window_start,
                                    window_end, n_responses, n_correct, p_value,
                                    discrimination, mean_time_ms, option_distribution,
                                    common_wrong, flagged, flag_reasons, computed_at)
            VALUES (:q, :qv, :k, :org, :ws, :we, :n, :nc, :p, :d, :t,
                    CAST(:opts AS jsonb), CAST(:wrong AS jsonb), :flagged,
                    CAST(:reasons AS text[]), :now)
            ON CONFLICT (question_version_id, coalesce(org_id, 0), window_start)
            DO UPDATE SET
                n_responses = EXCLUDED.n_responses, n_correct = EXCLUDED.n_correct,
                p_value = EXCLUDED.p_value, discrimination = EXCLUDED.discrimination,
                mean_time_ms = EXCLUDED.mean_time_ms,
                option_distribution = EXCLUDED.option_distribution,
                common_wrong = EXCLUDED.common_wrong, flagged = EXCLUDED.flagged,
                flag_reasons = EXCLUDED.flag_reasons, computed_at = EXCLUDED.computed_at
        """).bindparams(
            q=question_id, qv=qv_id, k=key_id, org=org_id, ws=window_start,
            we=now.date(), n=stats.n_responses, nc=stats.n_correct,
            p=stats.p_value, d=stats.discrimination, t=stats.mean_time_ms,
            opts=json.dumps(stats.option_distribution),
            wrong=json.dumps(stats.common_wrong), flagged=stats.flagged,
            reasons=stats.flag_reasons, now=now))
        written += 1

    log.info("item_stats_refreshed", items=written)
    return written


def refresh_exposure(session: Session, *, now: dt.datetime) -> int:
    """How burned each item is. Drives the "retire this" prompt.

    `burn_score` is computed in Python (`stats.burn_score`) so its two components
    — how often an item has been sat and how many centres have seen it — stay
    separately testable. An item that four centres have used is spent even if
    each used it once.
    """
    rows = session.execute(text("""
        SELECT question_id, count(*) AS times_sat,
               count(DISTINCT user_id) AS distinct_users,
               count(DISTINCT org_id) FILTER (WHERE org_id IS NOT NULL) AS distinct_orgs,
               min(occurred_at) AS first_seen, max(occurred_at) AS last_seen
        FROM item_exposures
        WHERE context <> 'preview'
        GROUP BY question_id
    """)).mappings().all()

    for row in rows:
        session.execute(text("""
            INSERT INTO item_exposure_stats (question_id, times_sat, distinct_users,
                                             distinct_orgs, first_seen_at,
                                             last_seen_at, burn_score, computed_at)
            VALUES (:q, :sat, :users, :orgs, :first, :last, :burn, :now)
            ON CONFLICT (question_id) DO UPDATE SET
                times_sat = EXCLUDED.times_sat,
                distinct_users = EXCLUDED.distinct_users,
                distinct_orgs = EXCLUDED.distinct_orgs,
                first_seen_at = LEAST(item_exposure_stats.first_seen_at,
                                      EXCLUDED.first_seen_at),
                last_seen_at = EXCLUDED.last_seen_at,
                burn_score = EXCLUDED.burn_score, computed_at = EXCLUDED.computed_at
        """).bindparams(
            q=row["question_id"], sat=row["times_sat"], users=row["distinct_users"],
            orgs=row["distinct_orgs"] or 0, first=row["first_seen"],
            last=row["last_seen"], now=now,
            burn=burn_score(times_sat=row["times_sat"],
                            distinct_orgs=row["distinct_orgs"] or 0)))
    log.info("exposure_refreshed", items=len(rows))
    return len(rows)


def record_exposure(session: Session, attempt_id: int) -> int:
    """Which items an attempt put in front of a student.

    Written when the attempt is SCORED rather than when it starts: an attempt
    that was issued and abandoned did not expose anything, and counting it would
    make every item look more burned than it is.

    Idempotent by `NOT EXISTS` rather than a unique index — the table is
    partitioned by time and a unique constraint would have to include the
    partition key, which would let the same attempt be counted twice across a
    month boundary.
    """
    result = session.execute(text("""
        INSERT INTO item_exposures (question_id, question_version_id, test_version_id,
                                    attempt_id, user_id, org_id, context, occurred_at)
        SELECT DISTINCT s.question_id, s.question_version_id, a.test_version_id,
               a.id, a.user_id, a.org_context_id,
               CASE WHEN a.competition_id IS NOT NULL THEN 'competition'
                    ELSE a.mode END,
               coalesce(a.submitted_at, now())
        FROM item_scores s
        JOIN score_runs r ON r.id = s.score_run_id AND r.is_current
        JOIN attempts a   ON a.id = r.attempt_id
        WHERE a.id = :a AND a.mode <> 'preview'
          AND NOT EXISTS (SELECT 1 FROM item_exposures e WHERE e.attempt_id = a.id)
    """).bindparams(a=attempt_id))
    return result.rowcount or 0


def refresh_attendance(session: Session, *, now: dt.datetime) -> int:
    """Assigned / started / completed / late, per student per assignment.

    A TABLE rather than a view: "was this assignment ever started" is a fact that
    must survive the assignment window closing and the cohort being archived.
    """
    result = session.execute(text("""
        INSERT INTO attendance_facts (org_id, cohort_id, user_id, assignment_id, day,
                                      assigned, started, completed, late, computed_at)
        SELECT asg.org_id, asg.cohort_id, t.user_id, asg.id,
               (asg.opens_at AT TIME ZONE 'Asia/Tashkent')::date,
               true,
               a.id IS NOT NULL,
               -- coalesce, not a bare IN: the LEFT JOIN yields NULL for a
               -- student who never started, and NULL is not false here — the
               -- column is NOT NULL and the row would be rejected.
               coalesce(a.status IN ('submitted', 'scored'), false),
               coalesce(a.submitted_at > asg.closes_at, false),
               :now
        FROM assignment_targets t
        JOIN assignments asg ON asg.id = t.assignment_id
        LEFT JOIN LATERAL (
            SELECT * FROM attempts x
            WHERE x.assignment_id = t.assignment_id AND x.user_id = t.user_id
            ORDER BY x.attempt_no DESC LIMIT 1
        ) a ON true
        WHERE asg.cohort_id IS NOT NULL
        ON CONFLICT (assignment_id, user_id) DO UPDATE SET
            started = EXCLUDED.started, completed = EXCLUDED.completed,
            late = EXCLUDED.late, computed_at = EXCLUDED.computed_at
    """).bindparams(now=now))
    log.info("attendance_refreshed", rows=result.rowcount)
    return result.rowcount or 0


def refresh_user_progress(session: Session, *, now: dt.datetime,
                          user_id: int | None = None) -> int:
    """Per-user, per-skill, per-day, with the question types they lose marks on.

    `weak_types` is what turns a band into an action: "practise matching
    headings" is advice, "you scored 6.0" is not.
    """
    scope = "AND a.user_id = :uid" if user_id else ""
    result = session.execute(text(f"""
        INSERT INTO user_skill_progress (user_id, skill, day, attempts_count,
                                         best_band, avg_band, weak_types, computed_at)
        SELECT a.user_id, sec.skill,
               (a.submitted_at AT TIME ZONE 'Asia/Tashkent')::date AS day,
               count(DISTINCT a.id),
               max(r.band), round(avg(r.band), 1),
               coalesce((
                   SELECT jsonb_agg(x)
                   FROM (
                       SELECT jsonb_build_object(
                                  'type_key', q.type_key,
                                  'accuracy', round(avg((s2.verdict = 'correct')::int), 2)
                              ) AS x
                       FROM item_scores s2
                       JOIN score_runs r2 ON r2.id = s2.score_run_id AND r2.is_current
                       JOIN attempts a2 ON a2.id = r2.attempt_id
                       JOIN questions q ON q.id = s2.question_id
                       WHERE a2.user_id = a.user_id AND a2.mode <> 'preview'
                       GROUP BY q.type_key
                       HAVING avg((s2.verdict = 'correct')::int) < 0.6
                       ORDER BY avg((s2.verdict = 'correct')::int)
                       LIMIT 5
                   ) weak
               ), '[]'::jsonb),
               :now
        FROM attempts a
        JOIN score_runs r ON r.attempt_id = a.id AND r.is_current
        JOIN test_version_sections sec ON sec.test_version_id = a.test_version_id
        WHERE a.status = 'scored' AND a.mode <> 'preview' {scope}
        GROUP BY a.user_id, sec.skill, day
        ON CONFLICT (user_id, skill, day) DO UPDATE SET
            attempts_count = EXCLUDED.attempts_count,
            best_band = EXCLUDED.best_band, avg_band = EXCLUDED.avg_band,
            weak_types = EXCLUDED.weak_types, computed_at = EXCLUDED.computed_at
    """).bindparams(now=now, **({"uid": user_id} if user_id else {})))
    return result.rowcount or 0
