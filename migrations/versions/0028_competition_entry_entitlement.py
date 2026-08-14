"""Record WHICH entitlement a competition entry was charged against.

`Entitlements.consume` had no caller anywhere in the product, so nothing had
ever needed to remember what an entry cost. Wiring it up makes the question
unavoidable: withdrawing before a contest returns the entry to the pack, and a
refund must go back to the row the unit came off.

Re-resolving by `(user, feature)` at withdrawal time is the plausible shortcut
and it is wrong. `check` returns whichever live entitlement it prefers now,
which after a renewal is not the one that was spent — so a student holding a
used-up pack and a fresh one gets the credit on the fresh row, inventing a unit
there and stranding a spent one on the old pack. The link has to be stored at
the moment it is charged.

Nullable, and it stays nullable: an entry charged against an UNLIMITED
entitlement has nothing to give back, and `quantity IS NULL` means exactly that.
So does every entry that predates this column. Both are honestly "no consumable
was spent here", which is what NULL says.

`ON DELETE SET NULL` rather than RESTRICT. An entitlement is never hard-deleted
in normal operation — `revoked_at` is the documented retirement — but if one
ever is, losing the audit link is a smaller harm than a foreign key that blocks
the delete and strands the row.
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE competition_entries
        ADD COLUMN entitlement_id bigint
            REFERENCES entitlements(id) ON DELETE SET NULL;

    -- "What did this pack pay for" — the query a billing dispute runs, and the
    -- one a refund needs when it has to find the entries still holding units.
    CREATE INDEX competition_entries_entitlement_idx
        ON competition_entries (entitlement_id)
        WHERE entitlement_id IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS competition_entries_entitlement_idx;
    ALTER TABLE competition_entries DROP COLUMN IF EXISTS entitlement_id;
    """)
