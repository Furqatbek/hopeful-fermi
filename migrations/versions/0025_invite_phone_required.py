"""org_invites: an invitation is addressed to a phone number

Revision ID: 0025
Revises: 0024

`phone` was nullable, which was accurate while nothing read it back. Redemption
now matches the invite's number against the caller's own verified number, so a
row without one is an invitation that can never be accepted by anybody — and,
before this change, one that could be accepted by *everybody*, because the column
was written at creation and selected by nothing at redemption.

Making it NOT NULL is what stops a second insert path — an import, a fixture, a
future bulk-invite endpoint — quietly recreating the bearer-token invite this
release removes. `InviteCreate.phone` has been required since the contract was
written; this is the same rule one layer down, where it cannot be bypassed.

The DELETE is not a data loss this deployment can suffer: the product is
pre-launch, and even with rows present, an invite is a spent coupon. What an
accepted invite produced is the row in `org_memberships`, which this does not
touch; what an unaccepted one holds is a token that is about to stop working
either way. Backfilling from `users.phone` via `accepted_by` was the alternative
and it was rejected: that records who TOOK the invite, not who it was FOR, and
writing the second into a column that means the first is how an audit trail
starts lying.
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM org_invites WHERE phone IS NULL")
    op.execute("ALTER TABLE org_invites ALTER COLUMN phone SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE org_invites ALTER COLUMN phone DROP NOT NULL")
