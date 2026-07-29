"""exam: assignments, attempts, autosave, scoring, regrade

Revision ID: 0011
Revises: 0010

The invariant this schema exists to guarantee:

    score = f(responses, key_versions, band_map_version, engine_version)

Responses are frozen at submit; keys and band maps are versioned; every score run
records exactly which versions produced it. That makes regrade a recomputation
rather than a mutation, and it is why event sourcing is not needed here
(ADR-0001 section 6).
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE assignments (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id             bigint      REFERENCES organizations(id),
        cohort_id          bigint      REFERENCES cohorts(id),
        test_version_id    bigint      NOT NULL REFERENCES test_versions(id),
        assigned_by        bigint      NOT NULL REFERENCES users(id),
        target_kind        text        NOT NULL CHECK (target_kind IN ('cohort','users','self_serve')),
        opens_at           timestamptz NOT NULL,
        closes_at          timestamptz NOT NULL,
        -- NULL = inherit the test's own timing config.
        time_limit_seconds int,
        max_attempts       int         NOT NULL DEFAULT 1,
        mode               text        NOT NULL DEFAULT 'exam' CHECK (mode IN ('exam','practice')),
        allow_review_after text        NOT NULL DEFAULT 'close'
                                       CHECK (allow_review_after IN ('never','submit','close')),
        status             text        NOT NULL DEFAULT 'active'
                                       CHECK (status IN ('active','cancelled')),
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        CHECK (closes_at > opens_at)
    );
    CREATE UNIQUE INDEX assignments_xid_uq ON assignments (xid);
    -- The teacher's cohort view and the student's "what's due" list.
    CREATE INDEX assignments_cohort_idx ON assignments (cohort_id, opens_at DESC)
        WHERE status = 'active';
    -- Window sweeper: which assignments just opened or closed.
    CREATE INDEX assignments_window_idx ON assignments (opens_at, closes_at) WHERE status = 'active';
    CREATE TRIGGER assignments_updated_at BEFORE UPDATE ON assignments
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE assignment_targets (
        assignment_id bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
        user_id       bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (assignment_id, user_id)
    );
    -- "What am I assigned" for one student, without scanning the assignment table.
    CREATE INDEX assignment_targets_user_idx ON assignment_targets (user_id);

    CREATE TABLE attempts (
        id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                   uuid        NOT NULL DEFAULT gen_random_uuid(),
        user_id               bigint      NOT NULL REFERENCES users(id),
        test_version_id       bigint      NOT NULL REFERENCES test_versions(id),
        assignment_id         bigint      REFERENCES assignments(id),
        competition_id        bigint,
        -- Which tenancy this attempt belongs to. NULL = the student's private use of
        -- the app. The same human can hold both, and analytics must not mix them:
        -- a centre sees its cohort's assigned attempts, never a student's private practice.
        org_context_id        bigint      REFERENCES organizations(id),
        mode                  text        NOT NULL CHECK (mode IN ('exam','practice','preview')),
        attempt_no            int         NOT NULL DEFAULT 1,
        status                text        NOT NULL DEFAULT 'issued'
                                          CHECK (status IN ('issued','in_progress','submitted','scored','abandoned','voided')),
        issued_at             timestamptz NOT NULL DEFAULT now(),
        started_at            timestamptz,
        -- Absolute server deadline. The client never computes this and is never trusted
        -- with it; it receives (server_now, expires_at) on every autosave response.
        expires_at            timestamptz,
        submitted_at          timestamptz,
        scored_at             timestamptz,
        submitted_via         text        CHECK (submitted_via IN ('user','auto_expiry','admin')),
        -- Recorded, not punished: a 4-second-late submit on a mobile network is a
        -- hiccup. anti-cheat reads this; the scorer does not.
        late_by_ms            int,
        current_score_run_id  bigint,
        client                jsonb       NOT NULL DEFAULT '{}',
        created_at            timestamptz NOT NULL DEFAULT now(),
        updated_at            timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX attempts_xid_uq ON attempts (xid);
    CREATE INDEX attempts_user_idx ON attempts (user_id, created_at DESC);
    -- Item analysis and exposure stats read this, and must never see author previews.
    CREATE INDEX attempts_tv_idx ON attempts (test_version_id) WHERE mode <> 'preview';
    CREATE INDEX attempts_assignment_idx ON attempts (assignment_id, user_id)
        WHERE assignment_id IS NOT NULL;
    -- The auto-submit sweeper. Partial, so it scans only live attempts (a few hundred
    -- rows) rather than the whole history.
    CREATE INDEX attempts_expiry_idx ON attempts (expires_at) WHERE status = 'in_progress';
    -- Enforces max_attempts at the database level, not just in a service check.
    CREATE UNIQUE INDEX attempts_assignment_no_uq
        ON attempts (assignment_id, user_id, attempt_no) WHERE assignment_id IS NOT NULL;
    CREATE INDEX attempts_competition_idx ON attempts (competition_id)
        WHERE competition_id IS NOT NULL;
    CREATE TRIGGER attempts_updated_at BEFORE UPDATE ON attempts
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Per-section state, including the play-once enforcement point. Server-side:
    -- the audio token is issued once and audio_locked_at closes the door.
    CREATE TABLE attempt_sections (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        attempt_id       bigint      NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        section_id       bigint      NOT NULL REFERENCES test_version_sections(id),
        position         int         NOT NULL,
        entered_at       timestamptz,
        expires_at       timestamptz,
        completed_at     timestamptz,
        audio_play_count int         NOT NULL DEFAULT 0,
        audio_started_at timestamptz,
        audio_locked_at  timestamptz,
        UNIQUE (attempt_id, section_id)
    );
    CREATE INDEX attempt_sections_attempt_idx ON attempt_sections (attempt_id, position);

    -- CURRENT answer state, one row per (attempt, question version, slot). Upserted
    -- during the attempt and frozen at submit by the trigger below.
    --
    -- Note on the ADR's "append-only" language: what must be immutable is the SUBMITTED
    -- response set, not every keystroke. Freezing at submit gives the pure-function
    -- property that regrade depends on; keystroke-level history lives in the telemetry
    -- table below, where it can be dropped on a retention schedule.
    CREATE TABLE attempt_answers (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        attempt_id          bigint      NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        question_version_id bigint      NOT NULL REFERENCES question_versions(id),
        slot_key            text        NOT NULL,
        -- Shaped by question_type_defs.response_schema. Validated generically; the exam
        -- engine has no per-type code.
        response            jsonb       NOT NULL,
        revision            int         NOT NULL DEFAULT 1,
        client_seq          bigint,
        client_ts           timestamptz,
        first_answered_at   timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now(),
        time_spent_ms       int         NOT NULL DEFAULT 0
    );
    -- The upsert target for autosave, and the reason a retried save is idempotent.
    CREATE UNIQUE INDEX attempt_answers_uq
        ON attempt_answers (attempt_id, question_version_id, slot_key);
    -- Scoring reads every answer for one attempt in one index scan.
    CREATE INDEX attempt_answers_attempt_idx ON attempt_answers (attempt_id);

    CREATE OR REPLACE FUNCTION attempt_answers_frozen() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        s text;
    BEGIN
        SELECT status INTO s FROM attempts
            WHERE id = COALESCE(NEW.attempt_id, OLD.attempt_id);
        IF s IN ('submitted','scored','voided') THEN
            RAISE EXCEPTION 'attempt % is %; answers are frozen',
                COALESCE(NEW.attempt_id, OLD.attempt_id), s
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$;
    CREATE TRIGGER attempt_answers_frozen_trg
        BEFORE INSERT OR UPDATE OR DELETE ON attempt_answers
        FOR EACH ROW EXECUTE FUNCTION attempt_answers_frozen();

    -- Answer telemetry. Feeds impossible-speed and paste-burst detection, and is the
    -- forensic record if a result is disputed. Partitioned and dropped after 90 days;
    -- deliberately lower fidelity than the answer table (batched, hashed values).
    CREATE TABLE attempt_answer_events (
        id                  bigint GENERATED ALWAYS AS IDENTITY,
        occurred_at         timestamptz NOT NULL DEFAULT now(),
        attempt_id          bigint      NOT NULL,
        question_version_id bigint,
        slot_key            text,
        kind                text        NOT NULL
                                        CHECK (kind IN ('set','clear','focus','blur','paste','navigate','audio_play')),
        value_hash          text,
        elapsed_ms          int,
        PRIMARY KEY (id, occurred_at)
    ) PARTITION BY RANGE (occurred_at);
    CREATE TABLE attempt_answer_events_default PARTITION OF attempt_answer_events DEFAULT;
    CREATE TABLE attempt_answer_events_2026_07 PARTITION OF attempt_answer_events
        FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
    CREATE TABLE attempt_answer_events_2026_08 PARTITION OF attempt_answer_events
        FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
    CREATE TABLE attempt_answer_events_2026_09 PARTITION OF attempt_answer_events
        FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
    -- Replay one attempt's timeline during an investigation.
    CREATE INDEX attempt_answer_events_attempt_idx
        ON attempt_answer_events (attempt_id, occurred_at);

    -- An immutable scoring result. Never updated: a regrade INSERTS a new run and
    -- supersedes the old one, so the history of what a student was told is preserved.
    CREATE TABLE score_runs (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                 uuid        NOT NULL DEFAULT gen_random_uuid(),
        attempt_id          bigint      NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        reason              text        NOT NULL
                                        CHECK (reason IN ('initial','regrade_key','regrade_band_map',
                                                          'manual_override','recompute')),
        regrade_job_id      bigint,
        -- The scoring code version. Without it, a fixed normalizer bug would silently
        -- make old and new scores incomparable.
        engine_version      text        NOT NULL,
        band_map_version_id bigint      REFERENCES band_map_versions(id),
        -- {"<question_version_id>": <answer_key_version_id>} — the exact inputs.
        -- This is the column that makes a score reproducible years later.
        key_versions        jsonb       NOT NULL,
        raw_score           numeric(7,2) NOT NULL,
        max_raw             numeric(7,2) NOT NULL,
        band                numeric(2,1),
        per_section         jsonb       NOT NULL DEFAULT '{}',
        is_current          boolean     NOT NULL DEFAULT true,
        superseded_by_id    bigint      REFERENCES score_runs(id),
        computed_at         timestamptz NOT NULL DEFAULT now(),
        computed_by         bigint      REFERENCES users(id),
        note                text
    );
    -- Exactly one current score per attempt, enforced by the database.
    CREATE UNIQUE INDEX score_runs_current_uq ON score_runs (attempt_id) WHERE is_current;
    CREATE INDEX score_runs_attempt_idx ON score_runs (attempt_id, computed_at DESC);
    -- Regrade progress reporting and rollback.
    CREATE INDEX score_runs_job_idx ON score_runs (regrade_job_id) WHERE regrade_job_id IS NOT NULL;
    ALTER TABLE attempts ADD CONSTRAINT attempts_current_score_run_fk
        FOREIGN KEY (current_score_run_id) REFERENCES score_runs(id);

    CREATE TABLE item_scores (
        id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        score_run_id           bigint       NOT NULL REFERENCES score_runs(id) ON DELETE CASCADE,
        -- Denormalized from question_versions so item analysis never needs the join.
        question_id            bigint       NOT NULL,
        question_version_id    bigint       NOT NULL,
        answer_key_version_id  bigint       REFERENCES answer_key_versions(id),
        slot_key               text         NOT NULL,
        awarded                numeric(6,2) NOT NULL DEFAULT 0,
        max_points             numeric(6,2) NOT NULL DEFAULT 1,
        verdict                text         NOT NULL
                                            CHECK (verdict IN ('correct','incorrect','partial','unanswered','void')),
        raw_response           text,
        normalized_response    text,
        matched_alternative    text,
        -- Which normalizers ran, in what order, and what was compared to what. This is
        -- the answer to "why was my answer marked wrong", and it is the single most
        -- useful support tool in the product.
        explain                jsonb        NOT NULL DEFAULT '{}'
    );
    CREATE INDEX item_scores_run_idx ON item_scores (score_run_id);
    -- Item analysis: p-value and discrimination per item across all attempts.
    CREATE INDEX item_scores_question_idx ON item_scores (question_id, verdict);
    -- "Show me every wrong answer text for this item" — finds missing key alternatives.
    CREATE INDEX item_scores_qv_idx ON item_scores (question_version_id, verdict);

    CREATE TABLE regrade_jobs (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                 uuid        NOT NULL DEFAULT gen_random_uuid(),
        trigger             text        NOT NULL
                                        CHECK (trigger IN ('answer_key_change','band_map_change','engine_fix','manual')),
        subject_type        text        NOT NULL
                                        CHECK (subject_type IN ('question_version','test_version','band_map_version','attempt')),
        subject_id          bigint      NOT NULL,
        from_key_version_id bigint      REFERENCES answer_key_versions(id),
        to_key_version_id   bigint      REFERENCES answer_key_versions(id),
        initiated_by        bigint      NOT NULL REFERENCES users(id),
        reason              text        NOT NULL,
        -- {"include_competitions": false, "from_date": "2026-01-01"} — competitions are
        -- excluded by default and require an explicit decision (ADR-0001 section 8.4).
        scope               jsonb       NOT NULL DEFAULT '{}',
        -- Regrade PREVIEWS before it acts. A job starts as a dry run and only applies
        -- once someone has seen the impact numbers.
        dry_run             boolean     NOT NULL DEFAULT true,
        status              text        NOT NULL DEFAULT 'planning'
                                        CHECK (status IN ('planning','ready','running','completed','failed','cancelled')),
        attempts_total      int         NOT NULL DEFAULT 0,
        attempts_processed  int         NOT NULL DEFAULT 0,
        scores_changed      int         NOT NULL DEFAULT 0,
        bands_changed       int         NOT NULL DEFAULT 0,
        -- {"competitions": [{"id": 1, "rank_changes": 12, "podium_changes": 1}]} — surfaced
        -- to a platform admin as a decision, never applied silently.
        competition_impact  jsonb,
        report              jsonb,
        created_at          timestamptz NOT NULL DEFAULT now(),
        started_at          timestamptz,
        finished_at         timestamptz
    );
    CREATE UNIQUE INDEX regrade_jobs_xid_uq ON regrade_jobs (xid);
    CREATE INDEX regrade_jobs_status_idx ON regrade_jobs (status, created_at DESC);
    CREATE INDEX regrade_jobs_subject_idx ON regrade_jobs (subject_type, subject_id, created_at DESC);
    ALTER TABLE score_runs ADD CONSTRAINT score_runs_regrade_job_fk
        FOREIGN KEY (regrade_job_id) REFERENCES regrade_jobs(id);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE IF EXISTS attempts DROP CONSTRAINT IF EXISTS attempts_current_score_run_fk;
    DROP TABLE IF EXISTS regrade_jobs, item_scores, score_runs CASCADE;
    DROP TABLE IF EXISTS attempt_answer_events CASCADE;
    DROP TABLE IF EXISTS attempt_answers, attempt_sections, attempts,
        assignment_targets, assignments CASCADE;
    DROP FUNCTION IF EXISTS attempt_answers_frozen();
    """)
