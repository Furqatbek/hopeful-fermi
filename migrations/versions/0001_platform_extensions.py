"""platform: extensions and shared helper functions

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- gen_random_uuid() fallback for xid columns. Application code prefers UUIDv7
    -- (time-ordered, better index locality); the DB default is the safety net for
    -- rows created by admin tooling or data migrations.
    CREATE EXTENSION IF NOT EXISTS pgcrypto;

    -- Trigram search over test/passage titles in the authoring library. This is the
    -- entire reason we do not need Elasticsearch.
    CREATE EXTENSION IF NOT EXISTS pg_trgm;

    -- Case-insensitive email without lower() indexes everywhere.
    CREATE EXTENSION IF NOT EXISTS citext;

    -- Lets a GIN index mix scalar columns with array/jsonb columns, used for
    -- (org_id, tags) filters on the content library.
    CREATE EXTENSION IF NOT EXISTS btree_gin;

    -- Shared updated_at trigger. One function, attached per table, rather than
    -- application code remembering to set it.
    CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$;

    -- Append-only guard, attached to audit_log and item_exposures. Belt to the
    -- braces of the GRANT-level restriction applied in 0004.
    CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'table %.% is append-only', TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END;
    $$;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION IF EXISTS forbid_mutation();
    DROP FUNCTION IF EXISTS set_updated_at();
    """)
