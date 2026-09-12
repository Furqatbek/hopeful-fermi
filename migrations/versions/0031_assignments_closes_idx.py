"""The index `list_assignments` says it pages from, which 0011 never created.

`teaching.list_assignments` orders by `(closes_at, id)` and resumes with a
row-value cursor — "compared as a row value, which PostgreSQL supports and can
drive from a composite index" — and `paging.encode_key` says the same. Both are
claims about the schema, and the schema did not bear them out: 0011 created
`assignments_cohort_idx (cohort_id, opens_at DESC)` for the teacher's cohort
view and `assignments_window_idx (opens_at, closes_at)` for the window sweeper,
and nothing ordered on `(closes_at, id)`. The keyset listing arrived well after
0011 and the migration was never revisited, so every page was a filter and a
sort.

`WHERE status = 'active'`, like the two beside it: the listing reads active
rows only, and a cancelled assignment is one nobody pages past again.

Deliberately NOT an `(org_id, closes_at, id)` index for the teacher branch. That
branch's predicate is `org_id IN (...) OR id IN (subselect)` — an OR across
different columns — which the planner answers with a BitmapOr and a Sort
regardless of what leads the index. The plain `(closes_at, id)` index is the one
that can remove the Sort: an ordered scan with the scope predicate as a filter
that stops at `limit + 1`. Revisit only if EXPLAIN on realistic data says
otherwise.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX assignments_closes_idx ON assignments (closes_at, id)
            WHERE status = 'active'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS assignments_closes_idx")
