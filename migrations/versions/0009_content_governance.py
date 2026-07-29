"""content: sharing grants, exposure tracking, burn stats

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Explicit sharing outside the owning org. This is the marketplace seam: today a
    -- platform admin grants a centre access to a shared bank; tomorrow the same rows
    -- are created by a purchase. No schema change between those two worlds.
    CREATE TABLE content_grants (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid          uuid        NOT NULL DEFAULT gen_random_uuid(),
        subject_type text        NOT NULL
                                 CHECK (subject_type IN ('test','passage','audio_track','question_group','question','cue_card_set','band_map')),
        subject_id   bigint      NOT NULL,
        grantee_kind text        NOT NULL CHECK (grantee_kind IN ('org','user','public')),
        grantee_id   bigint,
        permission   text        NOT NULL CHECK (permission IN ('view','assign','copy')),
        granted_by   bigint      NOT NULL REFERENCES users(id),
        granted_at   timestamptz NOT NULL DEFAULT now(),
        expires_at   timestamptz,
        revoked_at   timestamptz,
        revoked_by   bigint      REFERENCES users(id),
        note         text,
        CHECK ((grantee_kind = 'public') = (grantee_id IS NULL))
    );
    CREATE UNIQUE INDEX content_grants_uq
        ON content_grants (subject_type, subject_id, grantee_kind, grantee_id, permission)
        WHERE revoked_at IS NULL;
    -- authz.filter() unions this into every content list query for the acting principal.
    CREATE INDEX content_grants_grantee_idx ON content_grants (grantee_kind, grantee_id)
        WHERE revoked_at IS NULL;
    CREATE INDEX content_grants_subject_idx ON content_grants (subject_type, subject_id)
        WHERE revoked_at IS NULL;

    -- Per-item exposure. "How many times sat, by whom, when" (spec 2h). Highest-volume
    -- table in the system, so range-partitioned: old months are DETACHed and archived
    -- rather than deleted row by row.
    CREATE TABLE item_exposures (
        id                  bigint GENERATED ALWAYS AS IDENTITY,
        occurred_at         timestamptz NOT NULL DEFAULT now(),
        question_id         bigint      NOT NULL,
        question_version_id bigint      NOT NULL,
        test_version_id     bigint      NOT NULL,
        attempt_id          bigint,
        user_id             bigint      NOT NULL,
        org_id              bigint,
        context             text        NOT NULL
                                        CHECK (context IN ('exam','practice','competition','preview','review')),
        PRIMARY KEY (id, occurred_at)
    ) PARTITION BY RANGE (occurred_at);
    CREATE TABLE item_exposures_default PARTITION OF item_exposures DEFAULT;
    CREATE TABLE item_exposures_2026_07 PARTITION OF item_exposures
        FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
    CREATE TABLE item_exposures_2026_08 PARTITION OF item_exposures
        FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
    CREATE TABLE item_exposures_2026_09 PARTITION OF item_exposures
        FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

    -- "Has this item burned?" — the author-facing exposure report.
    CREATE INDEX item_exposures_question_idx ON item_exposures (question_id, occurred_at DESC);
    -- Anti-scrape anomaly detection: one user touching an abnormal number of items.
    CREATE INDEX item_exposures_user_idx ON item_exposures (user_id, occurred_at DESC);
    -- "Which of my centre's items have leaked to other orgs" — the contractual question.
    CREATE INDEX item_exposures_org_idx ON item_exposures (org_id, occurred_at DESC);
    CREATE TRIGGER item_exposures_append_only
        BEFORE UPDATE OR DELETE ON item_exposures
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

    -- Rollup so the exposure report is a single indexed read rather than a scan over
    -- the partitioned fact table. Recomputed incrementally after each attempt.
    CREATE TABLE item_exposure_stats (
        question_id     bigint PRIMARY KEY,
        times_sat       bigint      NOT NULL DEFAULT 0,
        distinct_users  bigint      NOT NULL DEFAULT 0,
        distinct_orgs   int         NOT NULL DEFAULT 0,
        first_seen_at   timestamptz,
        last_seen_at    timestamptz,
        -- 0..1. Rises with exposure count and org spread; drives the "retire this item"
        -- prompt. Items burn once they circulate, and this is where you watch it happen.
        burn_score      numeric(4,3) NOT NULL DEFAULT 0,
        computed_at     timestamptz NOT NULL DEFAULT now()
    );
    -- The author's "most burned items" view.
    CREATE INDEX item_exposure_stats_burn_idx ON item_exposure_stats (burn_score DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS item_exposure_stats CASCADE;
    DROP TABLE IF EXISTS item_exposures CASCADE;
    DROP TABLE IF EXISTS content_grants CASCADE;
    """)
