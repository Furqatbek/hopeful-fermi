"""Two admin endpoints wrote tables that nothing ever read.

This is the product's core architectural bet — "a new question type can be added
to a running production system with no migration and no redeploy", ADR-0001 §8.3
— and it did not survive a deploy.

`POST /admin/question-types` inserted into `question_type_defs` and then mutated
this process's registry singleton. `default_registry()` loaded a DIRECTORY and
never read that table, so with `--workers 4` a new type was live in one process
of four, and after a restart in none. `question_versions` carries a foreign key
on `(type_key, type_version)`, so content authored against a custom type outlived
the definition that scores it.

`POST /admin/lexicon` was the same shape and worse in effect. The scorer's
lexicon came from `StaticLexiconSource` over JSON files, so a UK/US pair added
through the API was recorded and never marked anything — and that endpoint is the
entire remedy for "38 students wrote a form the key does not accept", which is
the finding the item-analysis screen exists to surface.

The tests that matter here are the ones that build a registry the way a FRESH
PROCESS does, from a directory plus the database, rather than reading the
singleton the writing request happened to mutate. A test that asks the same
process it just posted to would have passed against the bug.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token
from app.modules.qtypes import registry as qreg
from app.platform.errors import RegistryError


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


@pytest.fixture
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return seed["author"]


@pytest.fixture(autouse=True)
def _isolate_singletons():
    """The module caches a registry, a scorer and a generation stamp for the
    life of the process. Left alone, one test's registration leaks into the
    next and a later assertion passes for the wrong reason."""
    saved = (qreg._registry, qreg._scorer, qreg._generation, qreg._checked_at)
    qreg._registry = qreg._scorer = qreg._generation = None
    qreg._checked_at = 0.0
    yield
    (qreg._registry, qreg._scorer, qreg._generation, qreg._checked_at) = saved


DEFINITION = {
    "key": "sentence_endings", "version": 1, "status": "active",
    "title": "Matching sentence endings",
    "description": "Choose the ending that completes each sentence.",
    "skills": ["reading"],
    "payload_schema": {"type": "object",
                       "properties": {"stem": {"type": "string"}},
                       "required": ["stem"]},
    "key_schema": {"type": "object"},
    "response_schema": {"type": "string"},
    # One of the three closed primitives. A fourth is code plus a deploy, which
    # ADR-0001 §8.3 states plainly rather than pretending away.
    "scoring": {"primitive": "choice_per_slot",
                "options": {"aggregate": "per_slot", "points_per_slot": 1},
                "normalizers": ["trim", "casefold"]},
    "validation": {}, "authoring": {},
}


class TestAQuestionTypeSurvivesTheProcessThatRegisteredIt:
    def test_registering_one_puts_it_in_the_listing(self, client, admin):
        response = client.post("/api/v1/admin/question-types", json=DEFINITION,
                               headers=auth(admin.xid))
        assert response.status_code in (200, 201), response.text
        listed = client.get("/api/v1/question-types?include_deprecated=true",
                            headers=auth(admin.xid)).json()
        assert any(t["key"] == "sentence_endings" for t in listed)

    def test_a_fresh_process_still_has_it(self, client, db, admin):
        """The test that would have caught it.

        `Registry.from_directory` is what a booting worker calls, and it raised
        `RegistryError` for a type registered a second earlier. Building the
        registry the way a new process does — files plus rows — is the whole
        fix, so this asserts against that rather than against the singleton the
        POST mutated.
        """
        client.post("/api/v1/admin/question-types", json=DEFINITION,
                    headers=auth(admin.xid))

        from_files_only = qreg.Registry.from_directory(
            qreg.REGISTRY_ROOT / "question_types")
        with pytest.raises(RegistryError):
            from_files_only.get("sentence_endings", 1)

        rebuilt = qreg._registry_from(db)
        assert rebuilt.get("sentence_endings", 1).title == "Matching sentence endings"

    def test_the_files_are_still_the_floor(self, db):
        """A fresh install and every unit test run with no rows at all, so the
        shipped types must survive the database becoming the source."""
        rebuilt = qreg._registry_from(db)
        assert rebuilt.get("short_answer", 1) is not None

    def test_a_row_wins_over_a_file_of_the_same_ref(self, db):
        """A file is what shipped; a row is what an operator did afterwards.

        Written straight to the table rather than through the endpoint, because
        the endpoint refuses a duplicate `(key, version)` with 409 — definitions
        are never edited in place. Precedence still has to be decided, since the
        seed and a real deployment both carry rows for file-shipped types, and
        loading them in the wrong order would silently serve whichever the dict
        happened to see last.
        """
        from sqlalchemy import text

        db.execute(text("""
            UPDATE question_type_defs SET title = 'Short answer, retuned'
            WHERE key = 'short_answer' AND version = 1
        """))
        db.flush()
        assert qreg._registry_from(db).get("short_answer", 1).title \
            == "Short answer, retuned"

    def test_a_malformed_row_does_not_take_the_registry_down(self, db):
        """This is what every exam in progress is scored against, so one bad row
        must cost that row and nothing else."""
        from sqlalchemy import text

        db.execute(text("""
            INSERT INTO question_type_defs (key, version, status, title, skills,
                payload_schema, key_schema, response_schema, scoring, validation,
                authoring, source, checksum)
            VALUES ('broken', 1, 'active', 'Broken', ARRAY['reading'],
                    '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    '{"primitive": "no_such_thing"}'::jsonb,
                    '{}'::jsonb, '{}'::jsonb, 'custom', 'x')
        """))
        db.flush()
        rebuilt = qreg._registry_from(db)
        assert rebuilt.get("short_answer", 1) is not None


class TestALexiconPairActuallyMarksSomething:
    """Agent D measured the defect end to end; this reproduces the measurement.

    `short_answer@v1` runs the `spelling_uk_us` normalizer. `curbside` against a
    key accepting `kerbside` was marked INCORRECT; posting the pair answered 201
    and the pair appeared in `GET /admin/lexicon`; scoring again was still
    incorrect. The control, `colour`/`color`, lives in
    `registry/lexicon/spelling_variants.json` and scored correct throughout.
    """

    def _score(self, scorer, response: str, accept: list[str]) -> bool:
        from app.modules.qtypes.registry import ScoreRequest
        from app.modules.qtypes.schemas import GroupRules

        # `short_answer` scores with the `text_per_slot` primitive, so both the
        # key and the response are slot-shaped — the same shape the seed builds.
        definition = scorer._registry.get("short_answer", 1)
        result = scorer.score_item(ScoreRequest(
            type_key="short_answer", type_version=definition.version,
            payload={"text": "The answer is {{s1}}.", "slots": ["s1"]},
            group=GroupRules(),
            response={"slots": {"s1": response}},
            key={"slots": {"s1": {"accept": accept}}}, tolerance={},
        ))
        return result.verdict == "correct"

    def test_the_control_pair_from_the_files_marks_correct(self, db):
        scorer = qreg.Scorer(qreg._registry_from(db), qreg._lexicon_from(db))
        assert self._score(scorer, "color", ["colour"])

    def test_a_pair_added_through_the_api_marks_correct(self, client, db, admin):
        before = qreg.Scorer(qreg._registry_from(db), qreg._lexicon_from(db))
        assert not self._score(before, "curbside", ["kerbside"]), \
            "the pair must be absent to begin with, or this proves nothing"

        added = client.post("/api/v1/admin/lexicon", headers=auth(admin.xid),
                            json={"kind": "spelling_variant", "a": "kerbside",
                                  "b": "curbside", "bidirectional": True})
        assert added.status_code in (200, 201), added.text

        after = qreg.Scorer(qreg._registry_from(db), qreg._lexicon_from(db))
        assert self._score(after, "curbside", ["kerbside"]), \
            "the row is written and the scorer still does not read it"

    def test_the_running_scorer_picks_it_up_too(self, client, db, admin):
        """Not only a freshly built one: the process-wide singleton the exam
        path actually uses has to see it, which is what `refresh_from_db`
        forces on the write and polls for elsewhere."""
        qreg.refresh_from_db(db, force=True)
        assert not self._score(qreg.default_scorer(), "curbside", ["kerbside"])

        client.post("/api/v1/admin/lexicon", headers=auth(admin.xid),
                    json={"kind": "spelling_variant", "a": "kerbside",
                          "b": "curbside", "bidirectional": True})
        assert self._score(qreg.default_scorer(), "curbside", ["kerbside"])


class TestTheGenerationStamp:
    def test_an_unchanged_database_does_not_rebuild(self, db):
        assert qreg.refresh_from_db(db, force=True) is True
        # Past the rate limit but with the same stamp: the check runs and
        # decides there is nothing to do, which is the common case on every
        # request and must not rebuild a registry.
        qreg._checked_at = 0.0
        assert qreg.refresh_from_db(db) is False

    def test_a_new_row_rebuilds(self, db):
        from sqlalchemy import text

        qreg.refresh_from_db(db, force=True)
        db.execute(text("""
            INSERT INTO lexicon_entries (kind, a, b, bidirectional)
            VALUES ('spelling_variant', 'kerbside', 'curbside', true)
        """))
        db.flush()
        assert qreg.refresh_from_db(db, force=True) is True

    def test_a_deleted_row_rebuilds(self, db):
        """`max(id)` alone misses a deletion that is not of the newest row, so
        the stamp carries a count as well.

        Two rows, and the LOWER one is removed — deleting the highest id moves
        `max(id)` on its own, so a test that did that would pass with the count
        taken out and prove nothing about it. Without the count, withdrawing a
        bad pair leaves every worker marking against it until the next restart.
        """
        from sqlalchemy import text

        db.execute(text("""
            INSERT INTO lexicon_entries (kind, a, b, bidirectional)
            VALUES ('spelling_variant', 'zzfirstpair', 'zzfirstvar', true),
                   ('spelling_variant', 'zzsecondpair', 'zzsecondvar', true)
        """))
        db.flush()
        qreg.refresh_from_db(db, force=True)
        db.execute(text("""
            DELETE FROM lexicon_entries WHERE id = (
                SELECT min(id) FROM lexicon_entries)
        """))
        db.flush()
        # Past the rate limit, so the stamp is genuinely re-read.
        qreg._checked_at = 0.0
        assert qreg.refresh_from_db(db) is True

    def test_the_check_is_rate_limited(self, db):
        """One stamp query per worker per interval, not one per answer scored.
        A sitting is thousands of answers."""
        qreg.refresh_from_db(db, force=True)
        assert qreg.refresh_from_db(db) is False
        assert qreg.RELOAD_AFTER_SECONDS >= 1
