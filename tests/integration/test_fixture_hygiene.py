"""Tests for the test fixtures themselves.

Two of them earn their place because the failures they guard against are silent.

**A stale template database** would run the whole suite against last week's
schema, passing everything. The guard is a fingerprint over the migration files;
this asserts the fingerprint actually moves when they do.

**An incomplete per-test reset** leaks rows between tests. The previous
hand-written `TRUNCATE` list missed fifteen tables — including `notifications`,
`audit_log` and `idempotency_keys` — and nothing noticed for months, because a
leak produces a passing test until the day it produces a baffling one.
"""

from __future__ import annotations


from sqlalchemy import text

from tests.integration.conftest import SEEDED, _migration_fingerprint


class TestTemplateFingerprint:
    def test_it_covers_every_migration_file(self):
        first = _migration_fingerprint()
        assert first.startswith("ielts-template:")
        assert first == _migration_fingerprint(), "not deterministic"

    def test_it_changes_when_a_migration_changes(self, tmp_path, monkeypatch):
        """The whole point. If this stops holding, the suite quietly validates
        against a schema that no longer exists."""
        import tests.integration.conftest as conftest

        versions = tmp_path / "migrations" / "versions"
        versions.mkdir(parents=True)
        (versions / "0001_x.py").write_text("revision = '0001'\n")
        monkeypatch.setattr(conftest, "ROOT", str(tmp_path))

        before = _migration_fingerprint()
        (versions / "0002_y.py").write_text("revision = '0002'\n")
        assert _migration_fingerprint() != before

        (versions / "0002_y.py").write_text("revision = '0002'  # edited\n")
        assert _migration_fingerprint() != before

    def test_it_is_safe_to_interpolate(self):
        """It goes into a `COMMENT ON DATABASE`, which takes no bind parameters."""
        assert all(c.isalnum() or c in ":-" for c in _migration_fingerprint())


class TestTheResetIsComplete:
    def test_every_table_is_either_wiped_or_deliberately_seeded(self, engine,
                                                                wipe_statement):
        """Derived from the catalogue, so a migration that adds a table gets it
        cleaned automatically. This asserts nothing has slipped out."""
        with engine.connect() as c:
            tables = {r[0] for r in c.execute(text("""
                SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND c.relispartition IS NOT TRUE
                  AND c.relname <> 'alembic_version'
            """))}

        uncovered = [t for t in tables
                     if f"DELETE FROM {t};" not in wipe_statement
                     and f"DELETE FROM {t} WHERE" not in wipe_statement]
        assert not uncovered, f"not reset between tests: {sorted(uncovered)}"
        assert tables >= SEEDED

    def test_the_tables_the_old_list_missed_are_covered_now(self, wipe_statement):
        """Named explicitly, because these are the ones that actually leaked."""
        for table in ("notifications", "audit_log", "idempotency_keys", "item_stats",
                      "attendance_facts", "otp_challenges", "media_assets",
                      "item_exposures", "user_skill_progress", "payment_events"):
            assert f"DELETE FROM {table};" in wipe_statement, table

    def test_it_disables_triggers_so_append_only_tables_can_be_cleared(
            self, wipe_statement):
        """`audit_log` and `item_exposures` carry triggers whose entire purpose is
        to make them undeletable. Without `session_replication_role` the reset
        raises rather than leaking — but it raises on every single test."""
        assert "session_replication_role = replica" in wipe_statement

    def test_a_write_to_an_append_only_table_does_not_survive(self, db, engine,
                                                              wipe_statement):
        db.execute(text("""
            INSERT INTO audit_log (actor_kind, action, subject_type, subject_id)
            VALUES ('system', 'fixture.probe', 'test', '1')
        """))
        db.commit()
        with engine.begin() as c:
            c.execute(text(wipe_statement))
        with engine.connect() as c:
            assert c.scalar(text(
                "SELECT count(*) FROM audit_log WHERE action = 'fixture.probe'")) == 0

    def test_the_seeded_registry_survives_the_reset(self, db, engine, wipe_statement):
        """It is read from 19 JSON files. Re-seeding it every test cost 78 ms and
        bought nothing — the rows never change."""
        before = db.scalar(text("SELECT count(*) FROM question_type_defs"))
        assert before >= 17
        with engine.begin() as c:
            c.execute(text(wipe_statement))
        with engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM question_type_defs")) == before

    def test_a_test_registered_question_type_does_not_survive(self, db, engine,
                                                              seed, wipe_statement):
        """The half that must NOT be kept. A custom type carries `created_by`
        into `users`, so leaving it would outlive the user who made it."""
        db.execute(text("""
            INSERT INTO question_type_defs
                (key, version, status, title, skills, payload_schema, key_schema,
                 response_schema, scoring, validation, authoring, source, checksum,
                 created_by)
            VALUES ('fixture_probe_type', 1, 'active', 'Probe', '{reading}',
                    '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    '{}'::jsonb, 'custom', 'x', :u)
        """).bindparams(u=seed["author"].id))
        db.commit()
        with engine.begin() as c:
            c.execute(text(wipe_statement))
        with engine.connect() as c:
            assert c.scalar(text("""
                SELECT count(*) FROM question_type_defs WHERE source = 'custom'
            """)) == 0
            assert c.scalar(text("SELECT count(*) FROM users")) == 0
