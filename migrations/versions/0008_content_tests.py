"""content: tests, versions, composition, publish gate, review

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE tests (
        id                           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                          uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id                       bigint      REFERENCES organizations(id),
        owner_user_id                bigint      NOT NULL REFERENCES users(id),
        visibility                   text        NOT NULL DEFAULT 'org_private'
                                                 CHECK (visibility IN ('author_private','org_private','platform_global')),
        title                        text        NOT NULL,
        description                  text,
        kind                         text        NOT NULL DEFAULT 'mock'
                                                 CHECK (kind IN ('mock','practice','competition','placement')),
        variant                      text        NOT NULL DEFAULT 'academic'
                                                 CHECK (variant IN ('academic','general_training')),
        skills                       text[]      NOT NULL,
        -- Marketplace seam (ADR-0001 section 7). Nothing reads this at MVP; a future
        -- "sell this test bank" is rows in content_grants plus an order, not a migration.
        licence                      text        NOT NULL DEFAULT 'proprietary',
        tags                         text[]      NOT NULL DEFAULT '{}',
        current_published_version_id bigint,
        archived_at                  timestamptz,
        created_at                   timestamptz NOT NULL DEFAULT now(),
        updated_at                   timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX tests_xid_uq ON tests (xid);
    -- The hottest authz-filtered query in the product: a teacher opening their library.
    CREATE INDEX tests_library_idx ON tests (org_id, visibility, kind) WHERE archived_at IS NULL;
    CREATE INDEX tests_title_trgm ON tests USING gin (title gin_trgm_ops);
    CREATE INDEX tests_tags_gin ON tests USING gin (tags);
    CREATE TRIGGER tests_updated_at BEFORE UPDATE ON tests
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE test_versions (
        id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                    uuid        NOT NULL DEFAULT gen_random_uuid(),
        test_id                bigint      NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        version_no             int         NOT NULL,
        status                 text        NOT NULL DEFAULT 'draft'
                                           CHECK (status IN ('draft','in_review','published','archived')),
        title                  text        NOT NULL,
        config                 jsonb       NOT NULL DEFAULT '{}',
        band_map_version_id    bigint      REFERENCES band_map_versions(id),
        total_questions        int         NOT NULL DEFAULT 0,
        max_raw                numeric(7,2) NOT NULL DEFAULT 0,
        -- The single most valuable denormalization in the schema. At publish time the
        -- whole resolved student-facing tree is materialized here, so serving a test is
        -- ONE row read, competition prefetch is a cache fill, and immutability becomes
        -- structural. It deliberately contains NO answer keys and NO transcripts.
        snapshot               jsonb,
        snapshot_bytes         int,
        checksum               text,
        created_by             bigint      NOT NULL REFERENCES users(id),
        created_at             timestamptz NOT NULL DEFAULT now(),
        submitted_for_review_at timestamptz,
        published_at           timestamptz,
        published_by           bigint      REFERENCES users(id),
        archived_at            timestamptz,
        -- Clone provenance: "this test was duplicated from that one".
        cloned_from_version_id bigint      REFERENCES test_versions(id)
    );
    CREATE UNIQUE INDEX test_versions_uq ON test_versions (test_id, version_no);
    CREATE UNIQUE INDEX test_versions_xid_uq ON test_versions (xid);
    -- "The current published version of this test" — resolved on every assignment.
    CREATE INDEX test_versions_published_idx ON test_versions (test_id, published_at DESC)
        WHERE status = 'published';
    -- The content-reviewer queue.
    CREATE INDEX test_versions_review_idx ON test_versions (submitted_for_review_at)
        WHERE status = 'in_review';
    ALTER TABLE tests ADD CONSTRAINT tests_current_published_version_fk
        FOREIGN KEY (current_published_version_id) REFERENCES test_versions(id);

    -- Published test versions are immutable. Enforced in the database, because an
    -- attempt binds to a version and a silent edit would retroactively change what a
    -- student sat. Only archiving and the snapshot backfill are permitted.
    CREATE OR REPLACE FUNCTION test_versions_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status = 'published' THEN
            IF NEW.status NOT IN ('published','archived') THEN
                RAISE EXCEPTION 'published test_version % cannot return to %', OLD.id, NEW.status
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF (NEW.title, NEW.config, NEW.band_map_version_id, NEW.total_questions,
                NEW.max_raw, NEW.checksum)
               IS DISTINCT FROM
               (OLD.title, OLD.config, OLD.band_map_version_id, OLD.total_questions,
                OLD.max_raw, OLD.checksum) THEN
                RAISE EXCEPTION 'published test_version % is immutable; create a new version', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$;
    CREATE TRIGGER test_versions_immutable_trg BEFORE UPDATE ON test_versions
        FOR EACH ROW EXECUTE FUNCTION test_versions_immutable();

    -- Composition layer: a section POINTS AT an asset version. This is what "pull an
    -- existing passage into a new test without copying it" means concretely.
    CREATE TABLE test_version_sections (
        id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                     uuid   NOT NULL DEFAULT gen_random_uuid(),
        test_version_id         bigint NOT NULL REFERENCES test_versions(id) ON DELETE CASCADE,
        position                int    NOT NULL,
        skill                   text   NOT NULL
                                       CHECK (skill IN ('reading','listening','writing','speaking')),
        title                   text   NOT NULL,
        passage_version_id      bigint REFERENCES passage_versions(id),
        audio_track_id          bigint REFERENCES audio_tracks(id),
        time_limit_seconds      int,
        -- What the author says the section contains; the publish gate compares this to
        -- the actual item count and refuses to publish on a mismatch.
        declared_question_count int,
        -- Play-once is a SERVER-side property of the section in exam mode; practice
        -- attempts override it per attempt.
        play_once               boolean NOT NULL DEFAULT true,
        config                  jsonb   NOT NULL DEFAULT '{}'
    );
    CREATE UNIQUE INDEX test_version_sections_pos_uq ON test_version_sections (test_version_id, position);
    -- "Which tests use this passage" — shown before an author edits a shared asset.
    CREATE INDEX tvs_passage_idx ON test_version_sections (passage_version_id)
        WHERE passage_version_id IS NOT NULL;
    CREATE INDEX tvs_audio_idx ON test_version_sections (audio_track_id)
        WHERE audio_track_id IS NOT NULL;

    CREATE TABLE test_version_groups (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        section_id       bigint NOT NULL REFERENCES test_version_sections(id) ON DELETE CASCADE,
        group_version_id bigint NOT NULL REFERENCES question_group_versions(id),
        position         int    NOT NULL,
        -- Test-wide IELTS numbering (1..40). Computed at composition time so the
        -- snapshot and the answer sheet agree without recalculating.
        number_start     int    NOT NULL,
        -- Audio timestamp markers: which segment this group's questions come from.
        -- The publish gate checks audio_end_ms <= audio_tracks.duration_ms, which is
        -- the "audio shorter than the question span" rule.
        audio_start_ms   int,
        audio_end_ms     int,
        CHECK (audio_end_ms IS NULL OR audio_start_ms IS NULL OR audio_end_ms > audio_start_ms)
    );
    CREATE UNIQUE INDEX tvg_pos_uq ON test_version_groups (section_id, position);
    -- Reverse: "which tests use this question group".
    CREATE INDEX tvg_group_idx ON test_version_groups (group_version_id);

    -- The publish gate's output. Persisted rather than returned-and-forgotten so an
    -- author can leave, come back, and still see the full problem list.
    CREATE TABLE test_version_validations (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        test_version_id bigint      NOT NULL REFERENCES test_versions(id) ON DELETE CASCADE,
        passed          boolean     NOT NULL,
        -- EVERY problem at once, never just the first:
        -- [{"code":"KEY_MISSING","severity":"error","path":"section[1].group[2].q[3]",
        --   "message":"...","fix_hint":"..."}]
        findings        jsonb       NOT NULL,
        error_count     int         NOT NULL DEFAULT 0,
        warning_count   int         NOT NULL DEFAULT 0,
        duration_ms     int,
        run_by          bigint      REFERENCES users(id),
        run_at          timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX tvv_latest_idx ON test_version_validations (test_version_id, run_at DESC);

    CREATE TABLE content_reviews (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        test_version_id bigint      NOT NULL REFERENCES test_versions(id) ON DELETE CASCADE,
        requested_by    bigint      NOT NULL REFERENCES users(id),
        reviewer_id     bigint      REFERENCES users(id),
        state           text        NOT NULL DEFAULT 'requested'
                                    CHECK (state IN ('requested','approved','changes_requested','withdrawn')),
        notes           text,
        created_at      timestamptz NOT NULL DEFAULT now(),
        decided_at      timestamptz
    );
    -- The reviewer's inbox.
    CREATE INDEX content_reviews_queue_idx ON content_reviews (created_at) WHERE state = 'requested';
    CREATE INDEX content_reviews_tv_idx ON content_reviews (test_version_id, created_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS content_reviews, test_version_validations, test_version_groups,
        test_version_sections, test_versions, tests CASCADE;
    DROP FUNCTION IF EXISTS test_versions_immutable();
    """)
