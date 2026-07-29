"""content: independently reusable, independently versioned authoring assets

Revision ID: 0007
Revises: 0006

The two-layer model that makes "every level independently reusable" true:
  * <asset>          -- stable identity, ownership, visibility, tags
  * <asset>_versions -- immutable content; a test composition references a VERSION
A test therefore reuses a passage by pointing at passage_versions.id. No copy.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ------------------------------------------------------------------ passages
    CREATE TABLE passages (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        -- NULL org_id = platform-owned content.
        org_id             bigint      REFERENCES organizations(id),
        owner_user_id      bigint      NOT NULL REFERENCES users(id),
        -- DEFAULT org_private. The contractual promise that a centre's material never
        -- reaches a competitor is encoded as a column default, not as a code review note.
        visibility         text        NOT NULL DEFAULT 'org_private'
                                       CHECK (visibility IN ('author_private','org_private','platform_global')),
        title              text        NOT NULL,
        skill              text        NOT NULL DEFAULT 'reading' CHECK (skill IN ('reading','listening')),
        topic              text,
        tags               text[]      NOT NULL DEFAULT '{}',
        current_version_id bigint,
        archived_at        timestamptz,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX passages_xid_uq ON passages (xid);
    -- The authoring library list. Every column here is part of the authz filter.
    CREATE INDEX passages_library_idx ON passages (org_id, visibility) WHERE archived_at IS NULL;
    CREATE INDEX passages_title_trgm ON passages USING gin (title gin_trgm_ops);
    CREATE TRIGGER passages_updated_at BEFORE UPDATE ON passages
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE passage_versions (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid              uuid        NOT NULL DEFAULT gen_random_uuid(),
        passage_id       bigint      NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
        version_no       int         NOT NULL,
        status           text        NOT NULL DEFAULT 'draft'
                                     CHECK (status IN ('draft','in_review','published','archived')),
        title            text        NOT NULL,
        -- Ordered block tree, NOT HTML. Automatic paragraph lettering, inline blank
        -- insertion and DOCX round-tripping all need addressable structure; HTML would
        -- mean re-parsing a string on every one of those operations.
        --   [{"id":"b1","type":"paragraph","label":"A",
        --     "runs":[{"t":"text","v":"..."},{"t":"blank","ref":"q12:s1"}]}]
        blocks           jsonb       NOT NULL,
        -- Materialized A..N. matching_headings references paragraph labels, and the
        -- publish gate must check those references without walking the block tree.
        paragraph_labels text[]      NOT NULL DEFAULT '{}',
        word_count       int         NOT NULL DEFAULT 0,
        checksum         text        NOT NULL,
        created_by       bigint      NOT NULL REFERENCES users(id),
        created_at       timestamptz NOT NULL DEFAULT now(),
        published_at     timestamptz,
        published_by     bigint      REFERENCES users(id)
    );
    CREATE UNIQUE INDEX passage_versions_uq ON passage_versions (passage_id, version_no);
    CREATE UNIQUE INDEX passage_versions_xid_uq ON passage_versions (xid);
    ALTER TABLE passages ADD CONSTRAINT passages_current_version_fk
        FOREIGN KEY (current_version_id) REFERENCES passage_versions(id);

    ------------------------------------------------------------------ audio
    -- The content-level wrapper around one or more media_assets. Audio is treated as
    -- immutable: "editing" a track means uploading a new one, because an attempt's
    -- audio must never change under it and audio has no meaningful diff.
    CREATE TABLE audio_tracks (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid               uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id            bigint      REFERENCES organizations(id),
        owner_user_id     bigint      NOT NULL REFERENCES users(id),
        visibility        text        NOT NULL DEFAULT 'org_private'
                                      CHECK (visibility IN ('author_private','org_private','platform_global')),
        title             text        NOT NULL,
        accent            text,
        tags              text[]      NOT NULL DEFAULT '{}',
        -- Original upload, retained so a future codec change is a re-transcode.
        master_media_id   bigint      REFERENCES media_assets(id),
        -- The single compressed format actually served to students.
        delivery_media_id bigint      REFERENCES media_assets(id),
        duration_ms       int,
        loudness_lufs     numeric(5,2),
        status            text        NOT NULL DEFAULT 'draft'
                                      CHECK (status IN ('draft','processing','ready','failed','archived')),
        archived_at       timestamptz,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX audio_tracks_xid_uq ON audio_tracks (xid);
    CREATE INDEX audio_tracks_library_idx ON audio_tracks (org_id, visibility) WHERE archived_at IS NULL;
    CREATE TRIGGER audio_tracks_updated_at BEFORE UPDATE ON audio_tracks
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Never exposed during an exam; served only on the post-exam review screen.
    -- Segment form (not a blob) so review can jump to the moment a question came from.
    CREATE TABLE transcripts (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        audio_track_id bigint      NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
        language       text        NOT NULL DEFAULT 'en',
        body           jsonb       NOT NULL,
        source         text        NOT NULL CHECK (source IN ('uploaded','generated')),
        created_by     bigint      REFERENCES users(id),
        created_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX transcripts_track_lang_uq ON transcripts (audio_track_id, language);

    ------------------------------------------------------------------ questions
    -- Question IDENTITY is first-class and outlives any test. Item analysis and
    -- exposure tracking both need to ask "how does THIS item behave" across every
    -- test it has ever appeared in.
    CREATE TABLE questions (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id             bigint      REFERENCES organizations(id),
        owner_user_id      bigint      NOT NULL REFERENCES users(id),
        visibility         text        NOT NULL DEFAULT 'org_private'
                                       CHECK (visibility IN ('author_private','org_private','platform_global')),
        type_key           text        NOT NULL,
        skill              text        NOT NULL CHECK (skill IN ('reading','listening','writing','speaking')),
        tags               text[]      NOT NULL DEFAULT '{}',
        difficulty_hint    numeric(3,2),
        current_version_id bigint,
        archived_at        timestamptz,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX questions_xid_uq ON questions (xid);
    CREATE INDEX questions_org_type_idx ON questions (org_id, type_key) WHERE archived_at IS NULL;
    -- Question-bank search by topic tag.
    CREATE INDEX questions_tags_gin ON questions USING gin (tags);
    CREATE TRIGGER questions_updated_at BEFORE UPDATE ON questions
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE question_versions (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid          uuid        NOT NULL DEFAULT gen_random_uuid(),
        question_id  bigint      NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        version_no   int         NOT NULL,
        type_key     text        NOT NULL,
        type_version int         NOT NULL,
        -- The author's content, shaped by question_type_defs.payload_schema.
        -- This jsonb column is why a new question type needs no migration.
        payload      jsonb       NOT NULL,
        -- Materialized blank/slot identifiers extracted from payload at save time.
        -- The publish gate compares this array against the key's slots without
        -- re-parsing payload, and the exam engine uses it to lay out the answer sheet.
        slot_keys    text[]      NOT NULL,
        points       numeric(6,2) NOT NULL DEFAULT 1,
        status       text        NOT NULL DEFAULT 'draft'
                                 CHECK (status IN ('draft','in_review','published','archived')),
        checksum     text        NOT NULL,
        created_by   bigint      NOT NULL REFERENCES users(id),
        created_at   timestamptz NOT NULL DEFAULT now(),
        published_at timestamptz,
        -- Hard FK into the registry: a published question can never reference a type
        -- definition that was deleted or reshaped underneath it.
        FOREIGN KEY (type_key, type_version) REFERENCES question_type_defs(key, version)
    );
    CREATE UNIQUE INDEX question_versions_uq ON question_versions (question_id, version_no);
    CREATE UNIQUE INDEX question_versions_xid_uq ON question_versions (xid);
    CREATE INDEX question_versions_type_idx ON question_versions (type_key, type_version);
    ALTER TABLE questions ADD CONSTRAINT questions_current_version_fk
        FOREIGN KEY (current_version_id) REFERENCES question_versions(id);

    -- THE table that makes regrade possible without breaking immutability.
    -- The question version stays frozen; a bad key gets a NEW key version; a score run
    -- records exactly which key version produced it. Score = f(responses, key_version,
    -- band_map_version, engine_version) and is therefore recomputable forever.
    CREATE TABLE answer_key_versions (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                 uuid        NOT NULL DEFAULT gen_random_uuid(),
        question_version_id bigint      NOT NULL REFERENCES question_versions(id) ON DELETE CASCADE,
        version_no          int         NOT NULL,
        -- Per-slot accepted answers / option ids, shaped by key_schema.
        key                 jsonb       NOT NULL,
        -- Normalizer set and per-key overrides. Merges OVER the group's word-limit rule.
        tolerance           jsonb       NOT NULL DEFAULT '{}',
        is_current          boolean     NOT NULL DEFAULT true,
        reason              text        NOT NULL DEFAULT 'initial'
                                        CHECK (reason IN ('initial','key_fix','clarification','import')),
        note                text,
        created_by          bigint      NOT NULL REFERENCES users(id),
        created_at          timestamptz NOT NULL DEFAULT now(),
        superseded_at       timestamptz,
        superseded_by_id    bigint      REFERENCES answer_key_versions(id)
    );
    CREATE UNIQUE INDEX answer_key_versions_uq ON answer_key_versions (question_version_id, version_no);
    -- Exactly one current key per question version, enforced by the database rather
    -- than by application discipline.
    CREATE UNIQUE INDEX answer_key_versions_current_uq
        ON answer_key_versions (question_version_id) WHERE is_current;

    ------------------------------------------------------------------ groups
    CREATE TABLE question_groups (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id             bigint      REFERENCES organizations(id),
        owner_user_id      bigint      NOT NULL REFERENCES users(id),
        visibility         text        NOT NULL DEFAULT 'org_private'
                                       CHECK (visibility IN ('author_private','org_private','platform_global')),
        title              text        NOT NULL,
        skill              text        NOT NULL CHECK (skill IN ('reading','listening','writing','speaking')),
        current_version_id bigint,
        archived_at        timestamptz,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX question_groups_xid_uq ON question_groups (xid);
    CREATE INDEX question_groups_library_idx ON question_groups (org_id, visibility) WHERE archived_at IS NULL;
    CREATE TRIGGER question_groups_updated_at BEFORE UPDATE ON question_groups
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE question_group_versions (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid              uuid        NOT NULL DEFAULT gen_random_uuid(),
        group_id         bigint      NOT NULL REFERENCES question_groups(id) ON DELETE CASCADE,
        version_no       int         NOT NULL,
        -- Shared rubric shown above the questions.
        instructions     jsonb       NOT NULL DEFAULT '{}',
        -- {"max_words": 2, "allow_number": true, "hyphen_counts_as_one": true,
        --  "on_violation":"mark_incorrect"} — the scorer ENFORCES this, it is not
        --  decoration on the instruction line.
        word_limit       jsonb,
        -- Shared options for matching / word-bank types: [{"id":"A","text":"..."}].
        -- Lives on the group because IELTS matching sets share one bank across items.
        option_bank      jsonb,
        display          jsonb       NOT NULL DEFAULT '{}',
        -- Background image for map/plan/diagram labelling.
        diagram_media_id bigint      REFERENCES media_assets(id),
        -- [{"slot": "s1", "x": 0.42, "y": 0.31}] in normalized coordinates, so the same
        -- mapping survives any rendered image size.
        hotspots         jsonb,
        status           text        NOT NULL DEFAULT 'draft'
                                     CHECK (status IN ('draft','in_review','published','archived')),
        checksum         text        NOT NULL,
        created_by       bigint      NOT NULL REFERENCES users(id),
        created_at       timestamptz NOT NULL DEFAULT now(),
        published_at     timestamptz
    );
    CREATE UNIQUE INDEX question_group_versions_uq ON question_group_versions (group_id, version_no);
    CREATE UNIQUE INDEX question_group_versions_xid_uq ON question_group_versions (xid);
    ALTER TABLE question_groups ADD CONSTRAINT question_groups_current_version_fk
        FOREIGN KEY (current_version_id) REFERENCES question_group_versions(id);

    CREATE TABLE question_group_items (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        group_version_id    bigint NOT NULL REFERENCES question_group_versions(id) ON DELETE CASCADE,
        question_version_id bigint NOT NULL REFERENCES question_versions(id),
        position            int    NOT NULL
    );
    CREATE UNIQUE INDEX question_group_items_pos_uq ON question_group_items (group_version_id, position);
    -- Reverse lookup: "which groups use this item". Drives regrade fan-out and the
    -- "you are editing an item used in 3 published tests" warning.
    CREATE INDEX question_group_items_qv_idx ON question_group_items (question_version_id);

    ------------------------------------------------------------------ band maps
    CREATE TABLE band_maps (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        -- NULL org_id = the platform default map every centre inherits.
        org_id             bigint      REFERENCES organizations(id),
        name               text        NOT NULL,
        skill              text        NOT NULL CHECK (skill IN ('reading','listening')),
        variant            text        NOT NULL DEFAULT 'academic'
                                       CHECK (variant IN ('academic','general_training')),
        current_version_id bigint,
        created_by         bigint      REFERENCES users(id),
        created_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX band_maps_xid_uq ON band_maps (xid);

    CREATE TABLE band_map_versions (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        band_map_id  bigint      NOT NULL REFERENCES band_maps(id) ON DELETE CASCADE,
        version_no   int         NOT NULL,
        -- [{"raw_min": 30, "raw_max": 32, "band": 7.0}] — retuning the curve is a data
        -- change, and every historical score records which version produced it.
        mapping      jsonb       NOT NULL,
        max_raw      int         NOT NULL,
        status       text        NOT NULL DEFAULT 'draft'
                                 CHECK (status IN ('draft','published','archived')),
        created_by   bigint      REFERENCES users(id),
        created_at   timestamptz NOT NULL DEFAULT now(),
        published_at timestamptz
    );
    CREATE UNIQUE INDEX band_map_versions_uq ON band_map_versions (band_map_id, version_no);
    ALTER TABLE band_maps ADD CONSTRAINT band_maps_current_version_fk
        FOREIGN KEY (current_version_id) REFERENCES band_map_versions(id);

    ------------------------------------------------------------------ cue cards
    -- Speaking prompts are authored in the content module like any other asset, so a
    -- teacher's cue-card set gets the same versioning, visibility and reuse.
    CREATE TABLE cue_card_sets (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id             bigint      REFERENCES organizations(id),
        owner_user_id      bigint      NOT NULL REFERENCES users(id),
        visibility         text        NOT NULL DEFAULT 'org_private'
                                       CHECK (visibility IN ('author_private','org_private','platform_global')),
        title              text        NOT NULL,
        tags               text[]      NOT NULL DEFAULT '{}',
        current_version_id bigint,
        archived_at        timestamptz,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX cue_card_sets_xid_uq ON cue_card_sets (xid);
    CREATE INDEX cue_card_sets_library_idx ON cue_card_sets (org_id, visibility) WHERE archived_at IS NULL;

    CREATE TABLE cue_card_set_versions (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid          uuid        NOT NULL DEFAULT gen_random_uuid(),
        set_id       bigint      NOT NULL REFERENCES cue_card_sets(id) ON DELETE CASCADE,
        version_no   int         NOT NULL,
        -- {"part1":[...],"part2":{"topic":"...","bullets":[...]},"part3":[...]}
        body         jsonb       NOT NULL,
        status       text        NOT NULL DEFAULT 'draft'
                                 CHECK (status IN ('draft','in_review','published','archived')),
        created_by   bigint      NOT NULL REFERENCES users(id),
        created_at   timestamptz NOT NULL DEFAULT now(),
        published_at timestamptz
    );
    CREATE UNIQUE INDEX cue_card_set_versions_uq ON cue_card_set_versions (set_id, version_no);
    ALTER TABLE cue_card_sets ADD CONSTRAINT cue_card_sets_current_version_fk
        FOREIGN KEY (current_version_id) REFERENCES cue_card_set_versions(id);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS cue_card_set_versions, cue_card_sets, band_map_versions, band_maps,
        question_group_items, question_group_versions, question_groups, answer_key_versions,
        question_versions, questions, transcripts, audio_tracks,
        passage_versions, passages CASCADE;
    """)
