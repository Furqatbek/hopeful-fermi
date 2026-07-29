"""analytics: item statistics, cohort progression, attendance

Revision ID: 0016
Revises: 0015

This module reads NOTHING from other modules at runtime. It owns these read models
and they are fed by domain events plus scheduled recomputation, which is the seam
that lets analytics move to a read replica later without touching anything else.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Classical test theory per item. A plain TABLE rather than a materialized view
    -- because it is updated incrementally as attempts complete; an MV would mean a
    -- full recompute over every attempt ever, nightly, forever.
    CREATE TABLE item_stats (
        id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        question_id           bigint       NOT NULL,
        question_version_id   bigint       NOT NULL,
        answer_key_version_id bigint,
        -- NULL org_id = global statistics across the platform. A per-org row lets a
        -- centre see how ITS cohort performed on the same item, which is a different
        -- and more useful number for them.
        org_id                bigint,
        window_start          date         NOT NULL,
        window_end            date         NOT NULL,
        n_responses           int          NOT NULL,
        n_correct             int          NOT NULL,
        -- Difficulty: proportion correct.
        p_value               numeric(5,4),
        -- Discrimination: point-biserial against total score. Negative means strong
        -- students get it wrong more often than weak ones -- almost always a bad key.
        discrimination        numeric(5,4),
        mean_time_ms          int,
        -- Distractor analysis: which options were chosen, how often.
        option_distribution   jsonb        NOT NULL DEFAULT '{}',
        -- The most frequent WRONG strings. This is the highest-value column in the
        -- table: "38 students wrote 'bike', your key only accepts 'bicycle'" finds
        -- a broken key automatically instead of waiting for a complaint. Spelling
        -- and number variants never reach here (the tolerance lexicon absorbs
        -- them), so what surfaces is a genuine missing alternative.
        common_wrong          jsonb        NOT NULL DEFAULT '[]',
        flagged               boolean      NOT NULL DEFAULT false,
        -- 'near_zero_p' | 'negative_discrimination' | 'high_unanswered' | 'key_suspect'
        flag_reasons          text[]       NOT NULL DEFAULT '{}',
        computed_at           timestamptz  NOT NULL DEFAULT now(),
        UNIQUE (question_version_id, org_id, window_start, window_end)
    );
    -- The author's "these items are probably broken" screen, worst first.
    CREATE INDEX item_stats_flagged_idx ON item_stats (p_value) WHERE flagged;
    -- Item history: how has this question behaved over time.
    CREATE INDEX item_stats_question_idx ON item_stats (question_id, window_end DESC);
    CREATE INDEX item_stats_org_idx ON item_stats (org_id, window_end DESC) WHERE org_id IS NOT NULL;

    -- Cohort score progression. A materialized view because it is a pure aggregate
    -- over attempts, refreshed nightly with CONCURRENTLY so the B2B dashboard never
    -- blocks on the refresh.
    CREATE MATERIALIZED VIEW mv_cohort_progress AS
    SELECT
        cm.cohort_id,
        c.org_id,
        a.user_id,
        (date_trunc('week', a.submitted_at AT TIME ZONE 'Asia/Tashkent'))::date AS week,
        count(*)                                                          AS attempts,
        round(avg(sr.band), 1)                                            AS avg_band,
        round(avg((sr.per_section -> 'reading'   ->> 'band')::numeric), 1) AS reading_band,
        round(avg((sr.per_section -> 'listening' ->> 'band')::numeric), 1) AS listening_band,
        max(sr.band)                                                      AS best_band,
        round(avg(sr.raw_score / NULLIF(sr.max_raw, 0)) * 100, 1)         AS avg_pct
    FROM attempts a
    JOIN score_runs sr    ON sr.attempt_id = a.id AND sr.is_current
    JOIN cohort_members cm ON cm.user_id = a.user_id AND cm.status = 'active'
    JOIN cohorts c        ON c.id = cm.cohort_id
    WHERE a.status = 'scored'
      AND a.mode <> 'preview'          -- author previews must never enter reporting
      AND a.org_context_id IS NOT NULL -- a student's private practice is not the centre's data
    GROUP BY 1, 2, 3, 4;
    -- REFRESH MATERIALIZED VIEW CONCURRENTLY requires a unique index. Without it the
    -- nightly refresh takes an ACCESS EXCLUSIVE lock and the dashboard 500s.
    CREATE UNIQUE INDEX mv_cohort_progress_uq ON mv_cohort_progress (cohort_id, user_id, week);
    CREATE INDEX mv_cohort_progress_org_idx ON mv_cohort_progress (org_id, week);

    -- Attendance. One row per (assignment, student): assigned / started / completed.
    -- A table rather than a view because "was this assignment ever started" is a fact
    -- that must survive the assignment window closing.
    CREATE TABLE attendance_facts (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        org_id        bigint      NOT NULL,
        cohort_id     bigint      NOT NULL,
        user_id       bigint      NOT NULL,
        assignment_id bigint      NOT NULL,
        day           date        NOT NULL,
        assigned      boolean     NOT NULL DEFAULT true,
        started       boolean     NOT NULL DEFAULT false,
        completed     boolean     NOT NULL DEFAULT false,
        late          boolean     NOT NULL DEFAULT false,
        computed_at   timestamptz NOT NULL DEFAULT now(),
        UNIQUE (assignment_id, user_id)
    );
    -- The attendance grid a centre shows to parents.
    CREATE INDEX attendance_cohort_day_idx ON attendance_facts (cohort_id, day);
    CREATE INDEX attendance_user_idx ON attendance_facts (user_id, day DESC);

    -- Per-skill progression for the individual student view (B2C) and the per-student
    -- drill-down inside a cohort (B2B).
    CREATE TABLE user_skill_progress (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id        bigint       NOT NULL,
        skill          text         NOT NULL CHECK (skill IN ('reading','listening','writing','speaking')),
        day            date         NOT NULL,
        attempts_count int          NOT NULL DEFAULT 0,
        best_band      numeric(2,1),
        avg_band       numeric(2,1),
        -- Which question types this user loses marks on. Drives "practise this next".
        weak_types     jsonb        NOT NULL DEFAULT '[]',
        computed_at    timestamptz  NOT NULL DEFAULT now(),
        UNIQUE (user_id, skill, day)
    );
    CREATE INDEX user_skill_progress_user_idx ON user_skill_progress (user_id, day DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS user_skill_progress, attendance_facts CASCADE;
    DROP MATERIALIZED VIEW IF EXISTS mv_cohort_progress;
    DROP TABLE IF EXISTS item_stats CASCADE;
    """)
