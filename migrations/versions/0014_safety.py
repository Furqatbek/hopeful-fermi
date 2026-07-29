"""safety: reports, blocks, mutes, moderation, takedowns

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE safety_reports (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid               uuid        NOT NULL DEFAULT gen_random_uuid(),
        reporter_user_id  bigint      NOT NULL REFERENCES users(id),
        subject_kind      text        NOT NULL
                                      CHECK (subject_kind IN ('user','speaking_pair','content','message')),
        subject_user_id   bigint      REFERENCES users(id),
        subject_ref       text,
        category          text        NOT NULL
                                      CHECK (category IN ('harassment','sexual_content','grooming','hate',
                                                          'violence','spam','cheating','other')),
        description       text,
        -- Enough for an admin to act without asking the reporter follow-up questions:
        -- pair id, timestamps, what was on screen.
        context           jsonb       NOT NULL DEFAULT '{}',
        evidence_media_id bigint      REFERENCES media_assets(id),
        -- Set by the system from the participants' ages, never by the reporter.
        involves_minor    boolean     NOT NULL DEFAULT false,
        priority          text        NOT NULL DEFAULT 'normal'
                                      CHECK (priority IN ('normal','high','critical')),
        status            text        NOT NULL DEFAULT 'new'
                                      CHECK (status IN ('new','triage','investigating','actioned','dismissed')),
        assigned_to       bigint      REFERENCES users(id),
        resolution        text,
        resolved_at       timestamptz,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX safety_reports_xid_uq ON safety_reports (xid);
    -- The general moderation queue.
    CREATE INDEX safety_reports_queue_idx ON safety_reports (priority DESC, created_at)
        WHERE status IN ('new','triage');
    -- The SEPARATE minor-involving queue with its own SLA (ADR-0001 section 8.7).
    -- A partial index is the cheapest possible implementation of that promise.
    CREATE INDEX safety_reports_minor_idx ON safety_reports (created_at)
        WHERE involves_minor AND status <> 'dismissed';
    -- Pattern detection: has this user been reported before?
    CREATE INDEX safety_reports_subject_idx ON safety_reports (subject_user_id, created_at DESC);
    CREATE TRIGGER safety_reports_updated_at BEFORE UPDATE ON safety_reports
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE user_blocks (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        blocker_user_id  bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        blocked_user_id  bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        reason           text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        UNIQUE (blocker_user_id, blocked_user_id),
        CHECK (blocker_user_id <> blocked_user_id)
    );
    -- The matcher checks BOTH directions before pairing, so the reverse index is not
    -- optional: A blocking B must also stop B being matched with A.
    CREATE INDEX user_blocks_blocked_idx ON user_blocks (blocked_user_id);

    CREATE TABLE user_mutes (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id       bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        muted_user_id bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        scope         text        NOT NULL DEFAULT 'all' CHECK (scope IN ('all','session')),
        expires_at    timestamptz,
        created_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (user_id, muted_user_id, scope),
        CHECK (user_id <> muted_user_id)
    );
    CREATE INDEX user_mutes_user_idx ON user_mutes (user_id);

    CREATE TABLE moderation_actions (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                 uuid        NOT NULL DEFAULT gen_random_uuid(),
        target_user_id      bigint      REFERENCES users(id),
        target_subject_type text,
        target_subject_id   bigint,
        action              text        NOT NULL
                                        CHECK (action IN ('warn','mute','suspend','ban','content_hide',
                                                          'content_remove','shadow_limit')),
        reason              text        NOT NULL,
        report_id           bigint      REFERENCES safety_reports(id),
        actor_user_id       bigint      NOT NULL REFERENCES users(id),
        starts_at           timestamptz NOT NULL DEFAULT now(),
        expires_at          timestamptz,
        reversed_at         timestamptz,
        reversed_by         bigint      REFERENCES users(id),
        reversal_reason     text,
        created_at          timestamptz NOT NULL DEFAULT now()
    );
    -- "What has been done to this user" — shown on every moderation screen.
    CREATE INDEX moderation_actions_target_idx ON moderation_actions (target_user_id, created_at DESC);
    -- The expiry sweeper that lifts temporary suspensions.
    CREATE INDEX moderation_actions_expiry_idx ON moderation_actions (expires_at)
        WHERE reversed_at IS NULL AND expires_at IS NOT NULL;
    CREATE INDEX moderation_actions_content_idx
        ON moderation_actions (target_subject_type, target_subject_id)
        WHERE target_subject_id IS NOT NULL;

    -- Copyright takedown flow (ADR-0001 section 9.3). Note hidden_at: content is
    -- soft-hidden on RECEIPT, before review, and never hard-deleted while disputed --
    -- destroying the material would also destroy the evidence.
    CREATE TABLE takedown_requests (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid              uuid        NOT NULL DEFAULT gen_random_uuid(),
        claimant_name    text        NOT NULL,
        claimant_org     text,
        claimant_email   text        NOT NULL,
        claimant_phone   text,
        rights_basis     text        NOT NULL,
        sworn_statement  boolean     NOT NULL DEFAULT false,
        subject_type     text        NOT NULL,
        subject_id       bigint      NOT NULL,
        description      text        NOT NULL,
        evidence         jsonb       NOT NULL DEFAULT '{}',
        status           text        NOT NULL DEFAULT 'received'
                                     CHECK (status IN ('received','reviewing','upheld','rejected',
                                                       'counter_noticed','withdrawn')),
        hidden_at        timestamptz,
        actioned_at      timestamptz,
        actioned_by      bigint      REFERENCES users(id),
        outcome_note     text,
        received_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX takedown_requests_xid_uq ON takedown_requests (xid);
    CREATE INDEX takedown_requests_queue_idx ON takedown_requests (received_at)
        WHERE status IN ('received','reviewing');
    -- "Is this asset already subject to a claim" — checked before publish and before
    -- any cross-org share.
    CREATE INDEX takedown_requests_subject_idx ON takedown_requests (subject_type, subject_id);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS takedown_requests, moderation_actions, user_mutes,
        user_blocks, safety_reports CASCADE;
    """)
