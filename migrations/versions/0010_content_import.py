"""content: bulk import jobs (dry-run then commit)

Revision ID: 0010
Revises: 0009

One table, because the pipeline is deliberately linear:
    DOCX / CSV / JSON  ->  adapter  ->  canonical Import JSON  ->  validate
                        ->  dry-run report + diff  ->  (author confirms)  ->  commit
The canonical JSON is the contract (ADR-0001 section 8.5); adapters are replaceable.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE import_jobs (
        id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                       uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id                    bigint      REFERENCES organizations(id),
        created_by                bigint      NOT NULL REFERENCES users(id),
        source_format             text        NOT NULL CHECK (source_format IN ('docx','csv','json')),
        source_media_id           bigint      REFERENCES media_assets(id),
        -- NULL = create a new test; set = import as a new VERSION of an existing test,
        -- which is what makes offline round-tripping (export, edit, re-import) work.
        target_test_id            bigint      REFERENCES tests(id),
        status                    text        NOT NULL DEFAULT 'received'
                                              CHECK (status IN ('received','parsing','validated','failed',
                                                                'committing','committed','cancelled')),
        -- The parsed canonical Import JSON. Stored so a commit re-reads exactly what
        -- the author saw in the dry-run diff, not a re-parse that might differ.
        canonical                 jsonb,
        -- {"errors":[...],"warnings":[...],"diff":{...},"counts":{...}} — the dry-run
        -- output the author confirms against.
        report                    jsonb,
        parse_error               text,
        dry_run_at                timestamptz,
        confirmed_by              bigint      REFERENCES users(id),
        committed_at              timestamptz,
        committed_test_version_id bigint      REFERENCES test_versions(id),
        created_at                timestamptz NOT NULL DEFAULT now(),
        updated_at                timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX import_jobs_xid_uq ON import_jobs (xid);
    -- A centre's import history, which doubles as their "what did we upload" evidence.
    CREATE INDEX import_jobs_org_idx ON import_jobs (org_id, created_at DESC);
    -- The worker's queue plus stuck-job detection.
    CREATE INDEX import_jobs_active_idx ON import_jobs (created_at)
        WHERE status IN ('received','parsing','committing');
    CREATE TRIGGER import_jobs_updated_at BEFORE UPDATE ON import_jobs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS import_jobs CASCADE;")
