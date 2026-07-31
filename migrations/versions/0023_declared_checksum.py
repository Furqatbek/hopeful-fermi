"""media_uploads.declared_checksum_sha256: the client's claim about its own file

Revision ID: 0023
Revises: 0022

`AudioTrackCreate` has declared a `checksum_sha256` since the contract was
drafted. The router accepted it into `AudioCreate`, never passed it to
`open_upload`, and there was nowhere to put it if it had — so the one value that
can prove the bytes on the server are the bytes the teacher chose was dropped at
the door.

It goes on the upload rather than on the asset because it is a claim about *this
attempt*: `media_assets.checksum_sha256` is the value the ingest worker computes
from what actually arrived, and the whole point is to have both and compare them.

Nullable: a client that does not hash its file is not doing anything wrong, and
`bytes` still gets checked. Not backfilled, for the same reason
`content_reviews.content_checksum` was not — a fabricated claim is worse than an
absent one.
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE media_uploads ADD COLUMN declared_checksum_sha256 text;
    COMMENT ON COLUMN media_uploads.declared_checksum_sha256 IS
        'sha256 the CLIENT said the file has, lowercase hex, captured at open. '
        'The ingest worker compares it against what it computes from the '
        'downloaded master and fails the asset on a mismatch. NULL when the '
        'client declared none.';
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE media_uploads DROP COLUMN IF EXISTS "
               "declared_checksum_sha256;")
