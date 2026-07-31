"""The unit of work, running for real.

Every other integration test overrides `deps.db` with the suite's own session, so
**the transaction boundary every HTTP request in production runs inside had never
executed under test.** Its promise is specific and load-bearing —

    "Committed on success, rolled back on any exception — including a DomainError
    that becomes a 4xx, because a partial write behind a 409 is worse than no
    write."

— and it is the thing every other guarantee in this system sits on top of: the
outbox is written inside this transaction, which is the whole reason a regrade
enqueued alongside a key change cannot be lost.

It was also three implementations. `platform/db.py` had the canonical one and
nothing called it; `workers/runtime.py` and `api/deps.py` each carried a
byte-for-byte copy, and the copies were what ran. They agreed, which is the good
case and not one to rely on — three copies of a transaction boundary is three
chances for one of them to grow a `commit()` in the wrong branch.

These tests use a client whose `deps.db` is NOT overridden, so the request opens
its own session against the same scratch database. `platform.db.reset_engine()`
exists for exactly this and had never been called either.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def live_client(database_url, db, seed, admin):
    """A client that runs the REAL `deps.db`, against the scratch database.

    Depends on `seed` and `admin` so they are built first, and commits the
    suite's own session before yielding: the request runs in a DIFFERENT session
    and would otherwise see none of the flushed-but-uncommitted rows.
    """
    from app.api.main import create_app
    from app.platform import db as platform_db
    from app.platform.config import settings

    db.commit()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    # `settings()` is `lru_cache`d, so the environment change alone is invisible
    # to it — and `reset_engine` would then rebuild the engine against exactly the
    # URL it had before.
    settings.cache_clear()
    platform_db.reset_engine()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        settings.cache_clear()
        platform_db.reset_engine()
        db.rollback()


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


@pytest.fixture
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return auth(seed["author"].xid)


def _validations(db, seed) -> int:
    db.rollback()          # see what the request's transaction actually left
    return db.scalar(text("SELECT count(*) FROM test_version_validations "
                          "WHERE test_version_id = :v")
                     .bindparams(v=seed["test_version"].id))


class TestTheRequestTransaction:
    def test_a_successful_request_commits(self, live_client, db, seed, admin):
        """`POST /validate` persists its findings "so an author can close the tab
        and come back". That is only true if the request commits."""
        response = live_client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/validate",
            headers=admin)
        assert response.status_code == 200, response.text
        assert _validations(db, seed) == 1

    def test_a_4xx_rolls_back_what_the_handler_had_already_written(
            self, live_client, db, seed, admin):
        """The promise, on the path that actually exercises it.

        `publish_version` adds a `TestVersionValidation` row and THEN raises 422
        when the gate fails. So a failed publish must leave no trace — the author
        gets every finding in the response body, and the database is untouched.
        """
        db.execute(text("UPDATE test_version_sections SET declared_question_count = 40"))
        db.commit()

        refused = live_client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=admin)
        assert refused.status_code == 422, refused.text
        assert refused.json()["findings"]
        assert _validations(db, seed) == 0
        assert db.scalar(text("SELECT status FROM test_versions WHERE id = :v")
                         .bindparams(v=seed["test_version"].id)) == "draft"

    def test_a_later_request_is_unaffected_by_an_earlier_failure(
            self, live_client, db, seed, admin):
        """A rolled-back request must not poison the one behind it.

        **This does not test `session.close()`, and no test here can.** Deleting
        the `finally: session.close()` entirely leaves every assertion in this
        file passing: CPython refcounts, so the leaked `Session` is collected the
        instant the generator frame dies and its connection goes straight back to
        the pool. Even `pool.checkedout()` reads zero — I tried it, and an
        assertion that cannot fail is worse than no assertion, so it is not here.

        A real leak would need a reference held across requests, and it would
        surface as pool exhaustion under a burst of errors rather than as anything
        one request can see. What this DOES pin is the part that goes wrong
        silently: the rollback leaving the next request a clean transaction.
        """
        db.execute(text("UPDATE test_version_sections SET declared_question_count = 40"))
        db.commit()
        assert live_client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=admin).status_code == 422

        after = live_client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/validate",
            headers=admin)
        assert after.status_code == 200, after.text
        assert _validations(db, seed) == 1

    def test_an_unexpected_error_rolls_back(self, live_client, db, seed, admin):
        """A 500 is the case where a partial write is likeliest and worst: the
        handler stopped somewhere nobody planned for."""
        from app.modules.content import repo as content_repo

        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        original = content_repo.publish
        content_repo.publish = explode
        try:
            response = live_client.post(
                f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                headers=admin)
        finally:
            content_repo.publish = original
        assert response.status_code == 500
        assert _validations(db, seed) == 0


class TestTheEngineIsCached:
    def test_the_same_engine_is_returned(self, live_client):
        """`engine()` memoizes because `pool_pre_ping` and a pool of five are
        worth nothing if every request builds its own."""
        from app.platform.db import engine

        assert engine() is engine()

    def test_the_session_factory_is_cached_too(self, live_client):
        from app.platform.db import session_factory

        assert session_factory() is session_factory()

    def test_reset_engine_actually_drops_the_cache(self, live_client):
        """The mechanism this entire file rests on.

        `reset_engine` exists so a test can repoint `DATABASE_URL`, and if it
        disposed the engine without clearing the module globals it would dispose
        it and hand the same dead object back — every request afterwards running
        against the wrong database, or none. Asserting only that it does not raise
        misses that completely, which is how this test was written first.
        """
        from app.platform import db as platform_db

        first = platform_db.engine()
        platform_db.reset_engine()
        assert platform_db.engine() is not first
        assert platform_db.session_factory() is not None

    def test_reset_engine_is_safe_before_anything_is_built(self):
        """It must also cope with being called before the first connection —
        the state a worker is in when its configuration is loaded."""
        from app.platform import db as platform_db

        platform_db.reset_engine()
        platform_db.reset_engine()
