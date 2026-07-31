"""content_reviews.content_checksum: what, exactly, was approved

Revision ID: 0022
Revises: 0021

An approval that names only a test VERSION is not evidence. A version stays
editable while it is `in_review` — `_mutable()` refuses `published` and
`archived`, nothing else — so the sequence

    submit → reviewer approves → author changes an answer key → publish

is available to anyone, and the published paper is one nobody signed off on. The
row in `content_reviews` still says "approved by Gulnora", which is worse than
having no row: it is a false record pointing at a named human.

So the approval records a fingerprint of the composition it was granted over, and
publish refuses when the content has moved since. Nullable because a review
decided before this migration attested to a content state nobody captured —
backfilling a checksum would manufacture exactly the claim this column exists to
make honest. `changes_requested` and `withdrawn` leave it NULL for good: only an
approval attests to anything.
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE content_reviews ADD COLUMN content_checksum text;
    COMMENT ON COLUMN content_reviews.content_checksum IS
        'sha256 over the composition tree at the moment of approval, answer keys '
        'included. Publish compares it against the version as it stands; a '
        'mismatch means the approval is for content that no longer exists. NULL '
        'on requested/changes_requested/withdrawn rows, and on approvals decided '
        'before migration 0022.';
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE content_reviews DROP COLUMN IF EXISTS content_checksum;")
