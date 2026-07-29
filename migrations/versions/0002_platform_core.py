"""platform: outbox, idempotency, notifications, feature flags

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Transactional outbox. Written in the SAME transaction as the domain change,
    -- drained by a relay into Dramatiq. This is why Redis loss can never lose a
    -- regrade, and why we do not need Kafka (ADR-0001 section 6).
    CREATE TABLE outbox (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        aggregate_type text        NOT NULL,
        aggregate_id   text        NOT NULL,
        event_type     text        NOT NULL,
        payload        jsonb       NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT now(),
        available_at   timestamptz NOT NULL DEFAULT now(),
        dispatched_at  timestamptz,
        attempts       int         NOT NULL DEFAULT 0,
        last_error     text
    );
    -- Partial index: the relay only ever scans undispatched rows, so dispatched
    -- history costs nothing to keep.
    CREATE INDEX outbox_pending_idx ON outbox (available_at) WHERE dispatched_at IS NULL;
    -- Replay/debug path: "what happened to this attempt".
    CREATE INDEX outbox_aggregate_idx ON outbox (aggregate_type, aggregate_id, created_at DESC);

    -- Generic HTTP idempotency. Used by exam autosave, payment callbacks and any
    -- POST a flaky mobile client may retry (ADR-0001 section 8.2).
    CREATE TABLE idempotency_keys (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        scope           text        NOT NULL,
        key             text        NOT NULL,
        user_id         bigint,
        request_hash    text        NOT NULL,
        response_status int,
        response_body   jsonb,
        created_at      timestamptz NOT NULL DEFAULT now(),
        expires_at      timestamptz NOT NULL
    );
    -- The dedupe anchor: a retried request with the same key returns the stored response.
    CREATE UNIQUE INDEX idempotency_keys_uq ON idempotency_keys (scope, key);
    -- Drives the nightly GC sweep.
    CREATE INDEX idempotency_keys_gc_idx ON idempotency_keys (expires_at);

    -- Outbound messages across all channels. Telegram first, SMS as paid fallback.
    CREATE TABLE notifications (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid           uuid        NOT NULL DEFAULT gen_random_uuid(),
        user_id       bigint      NOT NULL,
        channel       text        NOT NULL CHECK (channel IN ('telegram','sms','email','in_app','push')),
        template      text        NOT NULL,
        params        jsonb       NOT NULL DEFAULT '{}',
        locale        text        NOT NULL DEFAULT 'uz-Latn',
        dedupe_key    text,
        status        text        NOT NULL DEFAULT 'queued'
                                  CHECK (status IN ('queued','sent','failed','suppressed')),
        scheduled_at  timestamptz NOT NULL DEFAULT now(),
        sent_at       timestamptz,
        attempts      int         NOT NULL DEFAULT 0,
        failed_reason text,
        -- Per-message cost in tiyin. SMS is the only user-linear line item on the
        -- infra bill; tracking it per message is how you notice it growing.
        cost_minor    bigint,
        created_at    timestamptz NOT NULL DEFAULT now()
    );
    -- Stops a retried regrade job from notifying the same student twice.
    CREATE UNIQUE INDEX notifications_dedupe_uq ON notifications (dedupe_key)
        WHERE dedupe_key IS NOT NULL;
    -- The sender's work queue.
    CREATE INDEX notifications_due_idx ON notifications (scheduled_at) WHERE status = 'queued';
    -- In-app notification list for one user.
    CREATE INDEX notifications_user_idx ON notifications (user_id, created_at DESC);
    -- Monthly SMS spend report.
    CREATE INDEX notifications_cost_idx ON notifications (channel, sent_at) WHERE cost_minor IS NOT NULL;

    -- Kill switches and staged rollout. Small, but the alternative is redeploying
    -- to turn off the live speaking queue at 22:00 on a Friday.
    CREATE TABLE feature_flags (
        key        text PRIMARY KEY,
        enabled    boolean     NOT NULL DEFAULT false,
        rules      jsonb       NOT NULL DEFAULT '{}',
        note       text,
        updated_by bigint,
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS feature_flags, notifications, idempotency_keys, outbox;
    """)
