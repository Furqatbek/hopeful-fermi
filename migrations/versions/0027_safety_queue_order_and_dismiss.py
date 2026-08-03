"""The safety queue served its most urgent reports last, and could never be emptied

Two defects that only show up together, which is why neither was noticed.

**Ordering.** `safety_reports.priority` is text checked against
('normal','high','critical'), and both the queue query and
`safety_reports_queue_idx` ordered it `DESC`. Over those three strings,
descending alphabetical order is normal, high, critical — so `critical`, which
is what a minor plus a grooming or sexual-content report is set to, sorted to
the BOTTOM of the queue that exists to surface it. Storage and query agreed with
each other, which is exactly why it read as correct.

The index is redeclared over an explicit rank so it still covers the query. A
plain `ORDER BY CASE ...` without this would leave the queue doing a sort on
every read; small today, and the wrong thing to leave behind on the one table
whose response time is a written commitment.

**Dismissal.** `safety_reports.status` was read by the minors filter and written
by no code path anywhere. A report could never be triaged, actioned or
dismissed, so the minors queue — which the contract gives its own response SLA —
never emptied, and a moderator had no way to distinguish "not looked at yet"
from "looked at, nothing in it". `dismiss` joins the action CHECK so a report
can be closed without acting against a person, which is the outcome most reports
deserve and the only one that was unavailable.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

#: Written once here and mirrored by `platform_ops.moderation_queue`. Changing
#: one without the other makes the index stop covering the query, silently.
RANK = ("CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END")


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS safety_reports_queue_idx")
    op.execute(f"""
        CREATE INDEX safety_reports_queue_idx
        ON safety_reports (({RANK}), created_at)
    """)
    op.execute("""
        ALTER TABLE moderation_actions
        DROP CONSTRAINT IF EXISTS moderation_actions_action_check
    """)
    op.execute("""
        ALTER TABLE moderation_actions
        ADD CONSTRAINT moderation_actions_action_check
        CHECK (action IN ('warn', 'mute', 'suspend', 'ban', 'content_hide',
                          'content_remove', 'shadow_limit', 'dismiss'))
    """)


def downgrade() -> None:
    # Any row using the new action has to go first, or the old constraint cannot
    # be re-added. Deleting evidence is not something a downgrade should do
    # quietly, so this refuses rather than destroying it.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM moderation_actions WHERE action = 'dismiss')
            THEN
                RAISE EXCEPTION 'moderation_actions holds dismiss rows; '
                                'downgrading would require deleting audit '
                                'evidence. Resolve them by hand first.';
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE moderation_actions
        DROP CONSTRAINT IF EXISTS moderation_actions_action_check
    """)
    op.execute("""
        ALTER TABLE moderation_actions
        ADD CONSTRAINT moderation_actions_action_check
        CHECK (action IN ('warn', 'mute', 'suspend', 'ban', 'content_hide',
                          'content_remove', 'shadow_limit'))
    """)
    op.execute("DROP INDEX IF EXISTS safety_reports_queue_idx")
    op.execute("""
        CREATE INDEX safety_reports_queue_idx
        ON safety_reports (priority DESC, created_at)
    """)
