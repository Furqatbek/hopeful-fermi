"""tuning: fillfactor and autovacuum on the update-heavy tables

Revision ID: 0018
Revises: 0017

Applied pre-emptively, not in response to a problem, because the analysis in
`docs/design/0005-scaling-triggers.md` §5 identifies table bloat on
`attempt_answers` as the second thing likely to bite — and unlike the others it
degrades silently, showing up as gradually slower autosaves rather than an error.

`attempt_answers` is the only genuinely UPDATE-heavy table in the system: every
autosave upserts a row, so a 40-question attempt generates roughly 200 row
versions. Leaving free space on each page keeps those updates HOT (heap-only
tuple), which means the index is never touched and the dead tuples are reclaimed
by the next page prune rather than waiting for a vacuum. HOT requires two things:
free space on the page, and no indexed column changing. `response`, `revision`
and `updated_at` are not in any index, so fillfactor is the only missing half.
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- ~200 row versions per attempt. 20% free space per page keeps the updates
    -- HOT; the cost is 20% more heap pages for a table that is small anyway.
    ALTER TABLE attempt_answers SET (
        fillfactor = 80,
        autovacuum_vacuum_scale_factor = 0.02,
        autovacuum_analyze_scale_factor = 0.02
    );

    -- A handful of status transitions per attempt (issued -> in_progress ->
    -- submitted -> scored), plus updated_at. Less churn, so less headroom.
    ALTER TABLE attempts SET (
        fillfactor = 90,
        autovacuum_vacuum_scale_factor = 0.05
    );
    ALTER TABLE attempt_sections SET (fillfactor = 90);

    -- Insert, then exactly one update (dispatched_at), then eventually deleted.
    -- The relay polls this table constantly, so bloat here is felt immediately.
    ALTER TABLE outbox SET (
        fillfactor = 85,
        autovacuum_vacuum_scale_factor = 0.01,
        autovacuum_vacuum_cost_delay = 0
    );

    -- One row updated once per uploaded part: a 40 MB audio file at 5 MB parts is
    -- eight updates to the same `parts` jsonb.
    ALTER TABLE media_uploads SET (fillfactor = 80);

    -- Insert, then one update on send. High volume once Telegram notifications
    -- carry assignment and competition reminders.
    ALTER TABLE notifications SET (
        fillfactor = 90,
        autovacuum_vacuum_scale_factor = 0.05
    );

    -- Written once per request, read once on retry, swept nightly.
    ALTER TABLE idempotency_keys SET (
        fillfactor = 90,
        autovacuum_vacuum_scale_factor = 0.05
    );

    -- The live leaderboard reads this on finalization and the ranking pass
    -- updates every row once. Small table, but updated in one burst.
    ALTER TABLE competition_results SET (fillfactor = 85);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE competition_results RESET (fillfactor);
    ALTER TABLE idempotency_keys RESET (fillfactor, autovacuum_vacuum_scale_factor);
    ALTER TABLE notifications RESET (fillfactor, autovacuum_vacuum_scale_factor);
    ALTER TABLE media_uploads RESET (fillfactor);
    ALTER TABLE outbox RESET (fillfactor, autovacuum_vacuum_scale_factor,
                              autovacuum_vacuum_cost_delay);
    ALTER TABLE attempt_sections RESET (fillfactor);
    ALTER TABLE attempts RESET (fillfactor, autovacuum_vacuum_scale_factor);
    ALTER TABLE attempt_answers RESET (fillfactor, autovacuum_vacuum_scale_factor,
                                       autovacuum_analyze_scale_factor);
    """)
