"""competitions: scheduled contests, leaderboards, anti-cheat signals

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE competitions (
        id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                     uuid        NOT NULL DEFAULT gen_random_uuid(),
        -- NULL org_id = platform-wide; set = scoped to a single centre.
        org_id                  bigint      REFERENCES organizations(id),
        test_version_id         bigint      NOT NULL REFERENCES test_versions(id),
        title                   text        NOT NULL,
        description             text,
        registration_opens_at   timestamptz,
        registration_closes_at  timestamptz,
        -- Prefetch window opens here (T-120s). Clients pull the ENCRYPTED payload with
        -- per-client jitter, turning a 320 Mbps burst into a trickle (ADR-0001 5.6).
        lobby_opens_at          timestamptz NOT NULL,
        starts_at               timestamptz NOT NULL,
        ends_at                 timestamptz NOT NULL,
        duration_seconds        int         NOT NULL,
        status                  text        NOT NULL DEFAULT 'scheduled'
                                            CHECK (status IN ('scheduled','registration','lobby','live',
                                                              'grading','final','cancelled')),
        visibility              text        NOT NULL DEFAULT 'org'
                                            CHECK (visibility IN ('public','org','invite')),
        max_participants        int,
        entry_requirement       jsonb       NOT NULL DEFAULT '{}',
        -- Ordered comparator list, evaluated left to right. Data rather than code so a
        -- centre can run "highest score, then fastest" without a deploy.
        tiebreak                jsonb       NOT NULL
                                            DEFAULT '["raw_score_desc","duration_asc","submitted_at_asc"]',
        -- {"key_versions": {...}, "band_map_version_id": n, "frozen_at": "..."} captured at
        -- start. A later key fix regrades practice attempts but CANNOT silently
        -- re-rank this contest (ADR-0001 section 8.4).
        key_freeze              jsonb,
        -- Identifier of the AES-GCM key released at T-0. The key itself lives in Redis
        -- and is never persisted.
        payload_key_id          text,
        created_by              bigint      NOT NULL REFERENCES users(id),
        created_at              timestamptz NOT NULL DEFAULT now(),
        updated_at              timestamptz NOT NULL DEFAULT now(),
        CHECK (ends_at > starts_at AND starts_at >= lobby_opens_at)
    );
    CREATE UNIQUE INDEX competitions_xid_uq ON competitions (xid);
    -- The scheduler tick: "which contests change state in the next minute".
    CREATE INDEX competitions_schedule_idx ON competitions (starts_at)
        WHERE status IN ('scheduled','registration','lobby');
    CREATE INDEX competitions_org_idx ON competitions (org_id, starts_at DESC);
    -- The public "upcoming contests" list.
    CREATE INDEX competitions_public_idx ON competitions (starts_at DESC)
        WHERE visibility = 'public' AND status <> 'cancelled';
    CREATE TRIGGER competitions_updated_at BEFORE UPDATE ON competitions
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE competition_entries (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        competition_id      bigint      NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
        user_id             bigint      NOT NULL REFERENCES users(id),
        registered_at       timestamptz NOT NULL DEFAULT now(),
        attempt_id          bigint      REFERENCES attempts(id),
        status              text        NOT NULL DEFAULT 'registered'
                                        CHECK (status IN ('registered','prefetched','started','submitted',
                                                          'disqualified','withdrawn','no_show')),
        -- Lobby telemetry: who actually pulled the payload, and when the key went out.
        -- Also the evidence trail if someone claims they never got the test.
        payload_fetched_at  timestamptz,
        key_released_at     timestamptz,
        disqualified_reason text,
        disqualified_by     bigint      REFERENCES users(id),
        UNIQUE (competition_id, user_id)
    );
    -- Lobby dashboard: how many registered vs prefetched vs started.
    CREATE INDEX competition_entries_comp_idx ON competition_entries (competition_id, status);
    CREATE INDEX competition_entries_user_idx ON competition_entries (user_id, registered_at DESC);

    CREATE TABLE competition_results (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        competition_id bigint       NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
        user_id        bigint       NOT NULL REFERENCES users(id),
        attempt_id     bigint       NOT NULL REFERENCES attempts(id),
        score_run_id   bigint       NOT NULL REFERENCES score_runs(id),
        raw_score      numeric(7,2) NOT NULL,
        band           numeric(2,1),
        duration_ms    int          NOT NULL,
        submitted_at   timestamptz  NOT NULL,
        rank           int,
        -- Materialized sort key from the tiebreak rules, so a re-render of the final
        -- board is deterministic and does not re-evaluate comparator logic.
        tiebreak_key   text,
        is_provisional boolean      NOT NULL DEFAULT true,
        computed_at    timestamptz  NOT NULL DEFAULT now(),
        UNIQUE (competition_id, user_id)
    );
    CREATE INDEX competition_results_rank_idx ON competition_results (competition_id, rank);
    -- Matches the DEFAULT tiebreak ordering exactly, so the leaderboard is a plain
    -- index scan with no sort node. The live board is a Redis ZSET; this is the
    -- durable final record.
    CREATE INDEX competition_results_board_idx
        ON competition_results (competition_id, raw_score DESC, duration_ms ASC, submitted_at ASC);

    -- The governance record for ADR-0001 section 8.4: a post-hoc key fix that touches
    -- a finished competition produces a DECISION, not a silent re-ranking.
    CREATE TABLE competition_regrade_decisions (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        competition_id bigint      NOT NULL REFERENCES competitions(id),
        regrade_job_id bigint      NOT NULL REFERENCES regrade_jobs(id),
        -- {"items": [...], "attempts": 42, "rank_changes": 12, "podium_changes": 1}
        impact         jsonb       NOT NULL,
        decision       text        CHECK (decision IN ('leave_as_is','regrade_and_republish')),
        decided_by     bigint      REFERENCES users(id),
        decided_at     timestamptz,
        rationale      text,
        public_notice  text,
        created_at     timestamptz NOT NULL DEFAULT now(),
        UNIQUE (competition_id, regrade_job_id)
    );
    -- The admin's "decisions waiting on me" queue.
    CREATE INDEX crd_pending_idx ON competition_regrade_decisions (created_at)
        WHERE decision IS NULL;

    -- Anti-cheat signals. Deliberately simple and present from day one: detection and
    -- evidence, not prevention (ADR-0001 section 8.9).
    CREATE TABLE attempt_signals (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        attempt_id     bigint      NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        user_id        bigint      NOT NULL REFERENCES users(id),
        competition_id bigint      REFERENCES competitions(id),
        kind           text        NOT NULL
                                   CHECK (kind IN ('impossible_speed','answer_burst','shared_device',
                                                   'ip_collision','duplicate_account','clock_skew',
                                                   'tab_blur_excess','paste_burst','resubmit_anomaly')),
        severity       text        NOT NULL CHECK (severity IN ('info','warn','high')),
        detail         jsonb       NOT NULL DEFAULT '{}',
        detected_at    timestamptz NOT NULL DEFAULT now(),
        reviewed_at    timestamptz,
        reviewed_by    bigint      REFERENCES users(id),
        outcome        text
    );
    CREATE INDEX attempt_signals_attempt_idx ON attempt_signals (attempt_id);
    -- The moderator triage queue, highest severity first, unreviewed only.
    CREATE INDEX attempt_signals_triage_idx ON attempt_signals (severity, detected_at DESC)
        WHERE reviewed_at IS NULL;
    -- Repeat-offender view across attempts.
    CREATE INDEX attempt_signals_user_idx ON attempt_signals (user_id, detected_at DESC);

    ALTER TABLE attempts ADD CONSTRAINT attempts_competition_fk
        FOREIGN KEY (competition_id) REFERENCES competitions(id);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE IF EXISTS attempts DROP CONSTRAINT IF EXISTS attempts_competition_fk;
    DROP TABLE IF EXISTS attempt_signals, competition_regrade_decisions,
        competition_results, competition_entries, competitions CASCADE;
    """)
