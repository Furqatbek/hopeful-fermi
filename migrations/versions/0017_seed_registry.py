"""seed: question-type registry + tolerance lexicon

Revision ID: 0017
Revises: 0016

This is the LAST time a question type appears in a migration. It exists only so a
freshly created database is usable; the application otherwise loads the same
directory at boot via `Registry.from_directory`. Every question type added after
this point is an INSERT through `POST /admin/question-types`, never a schema
change. See docs/design/0002-data-model.md section 4.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

REGISTRY_DIR = Path(__file__).resolve().parents[2] / "registry" / "question_types"
LEXICON_DIR = Path(__file__).resolve().parents[2] / "registry" / "lexicon"


def _canonical(defn: dict) -> str:
    """Stable serialization so the checksum detects real drift, not key reordering."""
    payload = {k: v for k, v in defn.items() if k != "checksum"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def upgrade() -> None:
    conn = op.get_bind()

    for path in sorted(REGISTRY_DIR.glob("*.json")):
        defn = json.loads(path.read_text(encoding="utf-8"))
        checksum = hashlib.sha256(_canonical(defn).encode("utf-8")).hexdigest()
        conn.execute(
            text("""
                INSERT INTO question_type_defs
                    (key, version, status, title, description, skills, payload_schema,
                     key_schema, response_schema, scoring, validation, authoring,
                     source, checksum)
                VALUES
                    (:key, :version, :status, :title, :description, :skills,
                     CAST(:payload_schema AS jsonb), CAST(:key_schema AS jsonb),
                     CAST(:response_schema AS jsonb), CAST(:scoring AS jsonb),
                     CAST(:validation AS jsonb), CAST(:authoring AS jsonb),
                     'builtin', :checksum)
                ON CONFLICT (key, version) DO NOTHING
            """),
            {
                "key": defn["key"],
                "version": defn["version"],
                "status": defn.get("status", "active"),
                "title": defn["title"],
                "description": defn.get("description"),
                "skills": defn["skills"],
                "payload_schema": json.dumps(defn["payload_schema"]),
                "key_schema": json.dumps(defn["key_schema"]),
                "response_schema": json.dumps(defn["response_schema"]),
                "scoring": json.dumps(defn["scoring"]),
                "validation": json.dumps(defn.get("validation", {})),
                "authoring": json.dumps(defn.get("authoring", {})),
                "checksum": checksum,
            },
        )

    for path in sorted(LEXICON_DIR.glob("*.json")):
        entries = json.loads(path.read_text(encoding="utf-8"))
        for entry in entries:
            conn.execute(
                text("""
                    INSERT INTO lexicon_entries (kind, a, b, bidirectional, locale, note)
                    VALUES (:kind, :a, :b, :bidirectional, :locale, :note)
                    ON CONFLICT (kind, a, b) DO NOTHING
                """),
                {
                    "kind": entry["kind"],
                    "a": entry["a"],
                    "b": entry["b"],
                    "bidirectional": entry.get("bidirectional", True),
                    "locale": entry.get("locale"),
                    "note": entry.get("note"),
                },
            )

    # The platform-default band maps. Data, so a centre can override the curve for its
    # own cohort without a code change, and every historical score records which
    # version produced it.
    conn.execute(text("""
        INSERT INTO band_maps (name, skill, variant)
        VALUES ('IELTS Academic Reading (default)', 'reading', 'academic'),
               ('IELTS Listening (default)', 'listening', 'academic')
        ON CONFLICT DO NOTHING
    """))


def downgrade() -> None:
    # Only unreferenced definitions are removed. A type that published content is
    # bound to must survive, because question_versions holds an FK to (key, version)
    # and deleting it would orphan every attempt ever sat against those questions.
    # Downgrading a seed is not a reason to destroy content.
    op.execute("""
    DELETE FROM band_maps WHERE name LIKE '%(default)';
    DELETE FROM lexicon_entries;
    DELETE FROM question_type_defs d
     WHERE d.source = 'builtin'
       AND NOT EXISTS (
           SELECT 1 FROM question_versions qv
            WHERE (qv.type_key, qv.type_version) = (d.key, d.version)
       );
    """)
