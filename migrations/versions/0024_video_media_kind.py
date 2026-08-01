"""media_assets: video is a kind of file this platform stores

Revision ID: 0024
Revises: 0023

`kind` allowed `image|audio|document|archive`. Media now lives on the disk of the
machine the backend runs on rather than in an object store, and video is one of
the things that has to go there.

This adds the kind and nothing else. There is deliberately no transcode pipeline
behind it: the audio one exists because exam audio must be loudness-normalised
and play-once, which are properties of a listening section rather than of a file.
A video has no such requirement stated, so it is stored and served as uploaded,
through the same signed short-TTL grant every other object uses.
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE media_assets DROP CONSTRAINT media_assets_kind_check;
    ALTER TABLE media_assets ADD CONSTRAINT media_assets_kind_check
        CHECK (kind IN ('image','audio','video','document','archive'));
    """)


def downgrade() -> None:
    op.execute("""
    DELETE FROM media_assets WHERE kind = 'video';
    ALTER TABLE media_assets DROP CONSTRAINT media_assets_kind_check;
    ALTER TABLE media_assets ADD CONSTRAINT media_assets_kind_check
        CHECK (kind IN ('image','audio','document','archive'));
    """)
