"""worker upsert keys: the constraints the projections need to be idempotent

Revision ID: 0019
Revises: 0018

Every background job in this system is delivered at-least-once, because the
outbox relay sends before it marks (`app/workers/relay.py`). That trade is only
safe if every job is idempotent, and the cheapest way to make a projection
idempotent is to make it an UPSERT — which needs a unique key to conflict on.

`item_stats` was designed to hold one row per (item, org, window) and had no such
key, so `refresh_item_stats` would have inserted a duplicate set on every run and
the flagged-items dashboard would have shown each item several times with
different numbers. Two of the three indexes below close that gap.

Nothing here is a schema change to accommodate a feature. They are the missing
halves of constraints the tables already implied.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- The upsert key for refresh_item_stats. `coalesce(org_id, 0)` rather than a
    -- plain column: NULL means "across the whole platform" and is a real, single
    -- row, but NULLs are distinct in a unique index, so a plain
    -- (question_version_id, org_id, window_start) would let the global row be
    -- inserted again on every run.
    CREATE UNIQUE INDEX item_stats_window_uq
        ON item_stats (question_version_id, coalesce(org_id, 0), window_start);

    -- The sweeper's read: "which attempts are past their deadline". Already
    -- indexed by attempts_expiry_idx; this one covers the OTHER sweep, which is
    -- the notification queue drain.
    CREATE INDEX notifications_retry_idx ON notifications (scheduled_at, attempts)
        WHERE status = 'queued';

    -- The relay's dead-letter query: rows that have given up. Partial and tiny,
    -- because it should always be empty -- an index that is normally empty costs
    -- nothing and makes the alert query instant when it is not.
    CREATE INDEX outbox_stuck_idx ON outbox (created_at)
        WHERE dispatched_at IS NULL AND attempts >= 8;

    -- One exposure row set per attempt. The table is partitioned by time so a
    -- UNIQUE constraint would have to include occurred_at, which would let the
    -- same attempt be counted twice across a month boundary; this plain index
    -- makes the NOT EXISTS guard in record_exposure a lookup rather than a scan.
    CREATE INDEX item_exposures_attempt_idx ON item_exposures (attempt_id);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS item_exposures_attempt_idx;
    DROP INDEX IF EXISTS outbox_stuck_idx;
    DROP INDEX IF EXISTS notifications_retry_idx;
    DROP INDEX IF EXISTS item_stats_window_uq;
    """)
