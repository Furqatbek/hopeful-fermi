"""authz + safety: platform role grants and the immutable audit log

Revision ID: 0004
Revises: 0003

The audit log lands this early because content authoring (0007), the publish gate
(0008) and regrade (0011) all write to it. It is the one table with a legal role.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Platform-wide roles are grants with provenance, not a column on users:
    -- who gave someone admin power, when, and why is itself audit-relevant.
    CREATE TABLE platform_role_grants (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id    bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role       text        NOT NULL
                               CHECK (role IN ('platform_admin','support','moderator','content_reviewer')),
        granted_by bigint      REFERENCES users(id),
        granted_at timestamptz NOT NULL DEFAULT now(),
        revoked_by bigint      REFERENCES users(id),
        revoked_at timestamptz,
        reason     text
    );
    -- One live grant per (user, role); revoked history is retained.
    CREATE UNIQUE INDEX platform_role_grants_active_uq
        ON platform_role_grants (user_id, role) WHERE revoked_at IS NULL;
    -- Principal construction on each request reads a user's live platform roles.
    CREATE INDEX platform_role_grants_user_idx
        ON platform_role_grants (user_id) WHERE revoked_at IS NULL;

    -- Append-only audit trail for safety events AND every authoring/regrade action.
    -- Range-partitioned by month: this becomes the largest table in the system and
    -- old partitions are DETACHed and archived rather than DELETEd.
    CREATE TABLE audit_log (
        id              bigint GENERATED ALWAYS AS IDENTITY,
        xid             uuid        NOT NULL DEFAULT gen_random_uuid(),
        occurred_at     timestamptz NOT NULL DEFAULT now(),
        actor_kind      text        NOT NULL
                                    CHECK (actor_kind IN ('user','system','admin','provider')),
        actor_user_id   bigint,
        org_id          bigint,
        action          text        NOT NULL,
        subject_type    text        NOT NULL,
        subject_id      text        NOT NULL,
        before          jsonb,
        after           jsonb,
        reason          text,
        request_id      text,
        ip              inet,
        user_agent_hash text,
        PRIMARY KEY (id, occurred_at)
    ) PARTITION BY RANGE (occurred_at);

    -- A DEFAULT partition means an insert can never fail because the monthly
    -- partition job did not run. Rows land here and get migrated, rather than lost.
    CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT;
    CREATE TABLE audit_log_2026_07 PARTITION OF audit_log
        FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
    CREATE TABLE audit_log_2026_08 PARTITION OF audit_log
        FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
    CREATE TABLE audit_log_2026_09 PARTITION OF audit_log
        FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

    -- "Everything that ever happened to this test version / this attempt / this user."
    CREATE INDEX audit_log_subject_idx ON audit_log (subject_type, subject_id, occurred_at DESC);
    -- "Everything this admin did" — the first query in any misconduct investigation.
    CREATE INDEX audit_log_actor_idx ON audit_log (actor_user_id, occurred_at DESC);
    -- Per-centre export for a B2B contractual dispute.
    CREATE INDEX audit_log_org_idx ON audit_log (org_id, occurred_at DESC);
    CREATE INDEX audit_log_action_idx ON audit_log (action, occurred_at DESC);

    -- Enforcement layer 1: trigger. PG 13+ propagates BEFORE ROW triggers from the
    -- partitioned parent to every partition, including ones created later.
    CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS audit_log CASCADE;
    DROP TABLE IF EXISTS platform_role_grants;
    """)
