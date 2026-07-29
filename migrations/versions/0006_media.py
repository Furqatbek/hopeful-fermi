"""content: media assets, resumable uploads, copyright attestation

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- A storage object. Never a URL: (bucket, key) plus the platform.storage port is
    -- what makes the S3 provider swappable without a rewrite (ADR-0001 section 5.4).
    CREATE TABLE media_assets (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid              uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id           bigint      REFERENCES organizations(id),
        owner_user_id    bigint      NOT NULL REFERENCES users(id),
        kind             text        NOT NULL CHECK (kind IN ('image','audio','document','archive')),
        bucket           text        NOT NULL,
        storage_key      text        NOT NULL,
        content_type     text        NOT NULL,
        bytes            bigint      NOT NULL,
        checksum_sha256  text        NOT NULL,
        width            int,
        height           int,
        duration_ms      int,
        loudness_lufs    numeric(5,2),
        sample_rate      int,
        channels         int,
        -- Transcoded delivery output points back at the master upload, so a format
        -- change is a re-transcode rather than asking the teacher to re-upload.
        derived_from_id  bigint      REFERENCES media_assets(id),
        status           text        NOT NULL DEFAULT 'uploading'
                                     CHECK (status IN ('uploading','processing','ready','failed','quarantined','removed')),
        processing_error text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX media_assets_xid_uq ON media_assets (xid);
    CREATE UNIQUE INDEX media_assets_storage_uq ON media_assets (bucket, storage_key);
    CREATE INDEX media_assets_owner_idx ON media_assets (owner_user_id, created_at DESC);
    CREATE INDEX media_assets_org_idx ON media_assets (org_id, kind, created_at DESC);
    -- Content-integrity workhorse: the SAME Cambridge audio file uploaded by three
    -- different centres has one checksum. This index is how you find that out.
    CREATE INDEX media_assets_checksum_idx ON media_assets (checksum_sha256);
    -- The transcode worker's queue.
    CREATE INDEX media_assets_processing_idx ON media_assets (created_at)
        WHERE status IN ('uploading','processing');
    CREATE TRIGGER media_assets_updated_at BEFORE UPDATE ON media_assets
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Resumable chunked upload state, mapped onto S3 multipart. A teacher on a
    -- dropping 4G connection resumes from `parts` instead of restarting a 40 MB wav.
    CREATE TABLE media_uploads (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        media_asset_id     bigint      NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
        provider_upload_id text        NOT NULL,
        part_size          int         NOT NULL,
        expected_bytes     bigint,
        received_bytes     bigint      NOT NULL DEFAULT 0,
        -- [{"n": 1, "etag": "...", "bytes": 5242880}] — the resume manifest the client
        -- reads on reconnect to learn which parts it still owes.
        parts              jsonb       NOT NULL DEFAULT '[]',
        status             text        NOT NULL DEFAULT 'open'
                                       CHECK (status IN ('open','completing','completed','aborted','expired')),
        created_by         bigint      NOT NULL REFERENCES users(id),
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        expires_at         timestamptz NOT NULL
    );
    CREATE UNIQUE INDEX media_uploads_xid_uq ON media_uploads (xid);
    -- Abandoned multipart uploads cost money at the storage provider; this drives the
    -- sweeper that aborts them.
    CREATE INDEX media_uploads_gc_idx ON media_uploads (expires_at) WHERE status = 'open';
    CREATE TRIGGER media_uploads_updated_at BEFORE UPDATE ON media_uploads
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Copyright attestation, captured per upload rather than once per account, with
    -- enough provenance to be evidence in a dispute (ADR-0001 section 9.3).
    CREATE TABLE content_attestations (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        subject_type      text        NOT NULL
                                      CHECK (subject_type IN ('media_asset','passage','audio_track','test','question','import_job')),
        subject_id        bigint      NOT NULL,
        user_id           bigint      NOT NULL REFERENCES users(id),
        org_id            bigint      REFERENCES organizations(id),
        claim             text        NOT NULL
                                      CHECK (claim IN ('original','licensed','public_domain','permitted_excerpt')),
        licence_note      text,
        -- Which wording the uploader actually agreed to, and its hash. "They ticked a
        -- box" is not a defence; "they ticked THIS box, whose text hashed to X" is.
        statement_key     text        NOT NULL,
        statement_version text        NOT NULL,
        statement_hash    text        NOT NULL,
        ip                inet,
        user_agent_hash   text,
        affirmed_at       timestamptz NOT NULL DEFAULT now()
    );
    -- "Show me the attestation for this asset" — the first question in a takedown.
    CREATE INDEX content_attestations_subject_idx ON content_attestations (subject_type, subject_id);
    -- "Everything this uploader ever attested to" — the second question.
    CREATE INDEX content_attestations_user_idx ON content_attestations (user_id, affirmed_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS content_attestations, media_uploads, media_assets CASCADE;
    """)
