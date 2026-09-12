"""`assignments.target_kind` loses `self_serve`, a value nothing could use.

`POST /assignments` accepted `target_kind='self_serve'`, 0011's CHECK stored it,
and `_resolve_targets` resolved it to an empty audience: an assignment no
student could list (`list_assignments` reads `assignment_targets` and the
cohort) and a 404 at `POST /attempts` for anyone who named it. The design doc
has carried "`self_serve` is declared and broken" since the kind was written.

Removed rather than given the lazy meaning its name implies. Three things read
the materialized audience and none of them would hold for a kind resolved at
attempt time: `_resolve_targets` fixes the audience when the work is set so a
later joiner is not silently late; `_require_covered` charges the centre's
seats against exactly that list; and `start_attempt` admits a student to an
assignment because they are on it. A self-serve sitting already exists — an
attempt against a `test_version_xid`, charged to the student — so the kind was
a second spelling of a product that does not need one.

Refuses rather than rewrites if a row carries the value. None can exist that
did anything — no student ever sat one — but a teacher's record of having set
it is still their record, and a migration that deletes rows to satisfy its own
constraint is the wrong place to make that call. Cancel or retarget them by
hand first.

The constraint name is PostgreSQL's own for a column-level CHECK written inline
in CREATE TABLE, `<table>_<column>_check`; 0011 did not name it.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assignments WHERE target_kind = 'self_serve')
            THEN
                RAISE EXCEPTION 'assignments holds self_serve rows; the kind is '
                                'being removed. Cancel or retarget them by hand '
                                'first.';
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE assignments
        DROP CONSTRAINT IF EXISTS assignments_target_kind_check
    """)
    op.execute("""
        ALTER TABLE assignments
        ADD CONSTRAINT assignments_target_kind_check
        CHECK (target_kind IN ('cohort', 'users'))
    """)


def downgrade() -> None:
    # Widening back cannot fail on data: every row that satisfies the narrow
    # constraint satisfies the wide one.
    op.execute("""
        ALTER TABLE assignments
        DROP CONSTRAINT IF EXISTS assignments_target_kind_check
    """)
    op.execute("""
        ALTER TABLE assignments
        ADD CONSTRAINT assignments_target_kind_check
        CHECK (target_kind IN ('cohort', 'users', 'self_serve'))
    """)
