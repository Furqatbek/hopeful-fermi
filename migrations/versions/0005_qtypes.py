"""qtypes: the question-type registry and the scoring lexicon

Revision ID: 0005
Revises: 0004

This is the migration that must never need a sibling. Adding a question type after
this point is an INSERT into question_type_defs, not a schema change. See
docs/design/0002-data-model.md section 4 for the end-to-end proof.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE question_type_defs (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        -- Stable identifier, e.g. 'matching_headings'. Skill-neutral where a type is
        -- used by both Reading and Listening, so one row serves both.
        key             text        NOT NULL,
        -- Content binds to (key, version), so changing a def can never retroactively
        -- alter a published question or reinterpret a sat attempt.
        version         int         NOT NULL,
        status          text        NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('draft','active','deprecated')),
        title           text        NOT NULL,
        description     text,
        -- Which section skills may use this type; the publish gate enforces it.
        skills          text[]      NOT NULL,
        -- JSON Schema for what the AUTHOR writes (stems, options, blanks, layout).
        payload_schema  jsonb       NOT NULL,
        -- JSON Schema for the ANSWER KEY. Separate because keys are versioned
        -- independently of the question they belong to (see answer_key_versions).
        key_schema      jsonb       NOT NULL,
        -- JSON Schema for what the STUDENT submits. The exam engine validates
        -- incoming autosaves against this and needs no per-type code to do it.
        response_schema jsonb       NOT NULL,
        -- Scoring COMPOSITION over a closed set of primitives:
        --   {"primitive": "choice_per_slot" | "text_per_slot" | "set_selection",
        --    "options": {...}, "normalizers": [...]}
        -- All 19 MVP question types decompose into these three (ADR-0001 section 8.3).
        scoring         jsonb       NOT NULL,
        -- Cross-field rules JSON Schema cannot express, e.g. "every key slot must
        -- appear in payload.slots" or "option_bank size >= slot count".
        validation      jsonb       NOT NULL DEFAULT '{}',
        -- UI hints. The teacher-facing authoring form is GENERATED from this; without
        -- it, adding a type without a redeploy would still require new frontend code.
        authoring       jsonb       NOT NULL DEFAULT '{}',
        source          text        NOT NULL DEFAULT 'builtin'
                                    CHECK (source IN ('builtin','custom')),
        -- sha256 of the canonical def. Detects drift between repo files and the DB row.
        checksum        text        NOT NULL,
        created_by      bigint      REFERENCES users(id),
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now(),
        deprecated_at   timestamptz
    );
    -- The FK target for question_versions(type_key, type_version).
    CREATE UNIQUE INDEX question_type_defs_key_version_uq ON question_type_defs (key, version);
    -- The authoring UI's "what can I add" list.
    CREATE INDEX question_type_defs_active_idx ON question_type_defs (key) WHERE status = 'active';
    -- Filter the type picker by the section the teacher is editing.
    CREATE INDEX question_type_defs_skills_idx ON question_type_defs USING gin (skills);
    CREATE TRIGGER question_type_defs_updated_at BEFORE UPDATE ON question_type_defs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- The tolerance lexicon. This is what makes "British/American spelling variants"
    -- and "14 / fourteen" DATA rather than a hardcoded dict in the scorer — a platform
    -- admin adds a missing variant without a deploy.
    CREATE TABLE lexicon_entries (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind       text        NOT NULL
                               CHECK (kind IN ('spelling_variant','number_word','contraction','article','unit_form')),
        a          text        NOT NULL,
        b          text        NOT NULL,
        bidirectional boolean  NOT NULL DEFAULT true,
        locale     text,
        note       text,
        created_by bigint      REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX lexicon_entries_uq ON lexicon_entries (kind, a, b);
    -- The scorer's hot lookup. In practice the whole table is cached in process and
    -- invalidated by a Redis pub/sub message; this index backs the cold path.
    CREATE INDEX lexicon_entries_lookup_idx ON lexicon_entries (kind, a);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS lexicon_entries, question_type_defs CASCADE;
    """)
