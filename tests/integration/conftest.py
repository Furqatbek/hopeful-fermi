"""Integration fixtures: a real PostgreSQL, real migrations, real ORM.

Skipped cleanly when no database is reachable, so `pytest` still runs the pure
domain suites on a laptop with nothing installed. Set `TEST_DATABASE_URL` (or
`DATABASE_URL`) to a server you do not mind a scratch database being created on.

**Per-test reset costs ~2 ms.** It used to cost 670 ms, which at 400 integration
tests was five minutes — essentially the whole suite. Two measurements changed
the design:

  * `TRUNCATE` of 32 nearly-empty tables took 668 ms; `DELETE` from all 79 took
    2 ms. The cost was never per-row. It is per-RELATION: `TRUNCATE` takes an
    ACCESS EXCLUSIVE lock and rewrites the file for every table and every index,
    and this schema has 430 relations. `synchronous_commit = off` changed
    nothing, which is how we know it was not fsync.
  * The old `TRUNCATE ... CASCADE` list reached 64 of the 79 tables. Fifteen —
    including `notifications`, `audit_log`, `idempotency_keys`, `item_stats` and
    `attendance_facts` — were never cleaned between tests, because nothing in
    them has a foreign key into the truncated set. Rows leaked across the whole
    session. Deriving the list from the catalog fixes that and keeps fixing it
    when a migration adds a table.

The migration run, which is what I had assumed was the cost, is 5.4 seconds ONCE
per session. It is now ~0.4 s via a template database — real, but it was never
the five minutes.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.modules.qtypes.registry import Registry, Scorer, load_lexicon
from app.platform.clock import FrozenClock
from tests.conftest import strict_environment

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def _admin_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


# Tables whose contents are SEEDED, not test data. Wiping them would mean
# re-reading 19 JSON files from disk on every test (78 ms), and would break the
# `(type_key, type_version)` foreign key every content row depends on.
SEEDED = {"question_type_defs", "lexicon_entries"}

# Built once per session and reused with `CREATE DATABASE ... TEMPLATE`, which
# turns a 5.4-second `alembic upgrade head` into a ~0.4-second file copy. Worth
# little for one session; worth a lot the day this suite runs under `pytest -n`,
# where every worker would otherwise migrate from scratch.
TEMPLATE = "ielts_it_template"


# An arbitrary but fixed key, so every worker in a `pytest -n` run contends for
# the same lock while building the template.
TEMPLATE_LOCK = 0x1E175_7E57


@pytest.fixture(scope="session")
def database_url() -> str:
    base = _admin_url()
    if not base:
        # The ONLY legitimate skip, and only off CI. Below this line a database
        # was configured, so a failure is a broken environment and must be
        # reported as one.
        if strict_environment():
            raise RuntimeError(
                "TEST_DATABASE_URL is unset under CI. The integration suite is "
                "two thirds of this repository's coverage; a run without it is "
                "not a run. Configure the service container."
            )
        pytest.skip("set TEST_DATABASE_URL to run integration tests")

    # make_url, not string surgery: a unix-socket DSN carries `host=/tmp` in the
    # query string, and splitting on "/" mangles it.
    parsed = make_url(base)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"ielts_it_{uuid.uuid4().hex[:8]}"

    # NOT wrapped in try/except-skip. It used to be, and under `pytest -n 4` the
    # workers raced to build the template, four `CREATE DATABASE` calls collided,
    # and the run reported "355 passed, 400 skipped" — green, with more than half
    # the suite never executed. A suite that reports success while testing
    # nothing is worse than one that fails.
    _ensure_template(admin, parsed)
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE}"'))

    yield parsed.set(database=name).render_as_string(hide_password=False)

    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _ensure_template(admin, parsed) -> None:
    """Migrate once into a template database, then copy it per session.

    Real migrations, not `create_all`. The models describe a subset of the
    schema; the migrations ARE the schema, and proving the two agree is the whole
    point of these tests — so the template is built by running them.

    Rebuilt whenever the migration set changes, keyed on a hash of the migration
    files. A stale template is the nastiest failure mode of this trick: the suite
    would pass against last week's schema.

    Serialized with a session advisory lock, because under `pytest -n` every
    worker reaches this at the same instant. The first builds; the rest block,
    then find the fingerprint already current and return. Double-checked: the
    fingerprint is read again AFTER the lock is held.
    """
    import subprocess

    fingerprint = _migration_fingerprint()
    connection = admin.connect()
    try:
        connection.execute(text("SELECT pg_advisory_lock(:k)")
                           .bindparams(k=TEMPLATE_LOCK))
        if _template_fingerprint(connection) == fingerprint:
            return

        # `WITH (FORCE)` because a previous session's connection may linger.
        connection.execute(text(f'DROP DATABASE IF EXISTS "{TEMPLATE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{TEMPLATE}"'))

        url = parsed.set(database=TEMPLATE).render_as_string(hide_password=False)
        result = subprocess.run(["alembic", "upgrade", "head"], cwd=ROOT,
                                env={**os.environ, "DATABASE_URL": url},
                                capture_output=True, text=True)
        if result.returncode != 0:  # pragma: no cover
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{TEMPLATE}" WITH (FORCE)'))
            raise RuntimeError(f"migrations failed: {result.stderr[-2000:]}")

        # Written LAST, so a template whose migration died halfway is unlabelled
        # and gets rebuilt rather than silently reused. Interpolated, not bound:
        # COMMENT takes no parameters. Safe because the value is a fixed prefix
        # plus a hex digest, and asserted to be exactly that.
        assert all(ch.isalnum() or ch in ":-" for ch in fingerprint)
        connection.execute(
            text(f"COMMENT ON DATABASE \"{TEMPLATE}\" IS '{fingerprint}'"))
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:k)")
                           .bindparams(k=TEMPLATE_LOCK))
        connection.close()


def _template_fingerprint(connection) -> str | None:
    return connection.execute(text("""
        SELECT shobj_description(oid, 'pg_database') FROM pg_database
        WHERE datname = :n
    """).bindparams(n=TEMPLATE)).scalar()


def _migration_fingerprint() -> str:
    """A hash of every migration file. Changes when the schema does."""
    import hashlib
    from pathlib import Path

    digest = hashlib.sha256()
    for path in sorted(Path(ROOT, "migrations", "versions").glob("*.py")):
        digest.update(path.read_bytes())
    return f"ielts-template:{digest.hexdigest()[:32]}"


@pytest.fixture(scope="session")
def engine(database_url):
    eng = create_engine(database_url, future=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def wipe_statement(engine) -> str:
    """The reset, derived from the catalogue rather than hand-listed.

    Built once per session, because `pg_class` does not change between tests.

    `session_replication_role = replica` does two things a plain ordered DELETE
    cannot, and both are load-bearing:

      * it disables foreign-key triggers, so no topological ordering is needed —
        and this schema has SEVEN dependency cycles (`tests` ↔ `test_versions`,
        `attempts` ↔ `score_runs`, and five asset/version pairs) that no
        ordering could satisfy;
      * it disables user triggers, so the append-only guards on `audit_log` and
        `item_exposures` — which exist precisely to make those tables
        undeletable — do not block the teardown.

    `SET LOCAL`, so it applies to the teardown transaction and nothing else.
    """
    with engine.connect() as c:
        tables = [r[0] for r in c.execute(text("""
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p')
              AND c.relispartition IS NOT TRUE
              AND c.relname <> 'alembic_version'
            ORDER BY c.relname
        """))]

    statements = ["SET LOCAL session_replication_role = replica;"]
    for table in tables:
        if table == "question_type_defs":
            # Keep the seeded registry; drop only what a test registered. The
            # `created_by` reference into `users` is why it cannot simply be left
            # alone: a test-registered type would outlive the user who made it.
            statements.append("DELETE FROM question_type_defs WHERE source <> 'builtin';")
        elif table == "lexicon_entries":
            statements.append("DELETE FROM lexicon_entries WHERE created_by IS NOT NULL;")
        else:
            statements.append(f"DELETE FROM {table};")
    return " ".join(statements)


@pytest.fixture(scope="session", autouse=True)
def _rate_limits_are_per_worker():
    """One Redis database per xdist worker.

    `make coverage` runs `-n 4`. The limiter counts in Redis, which is not rolled
    back with the database and is not partitioned by process — so a per-test
    `DEL rl:*` wipes the counters of the three tests running beside it, and the
    test that fails is whichever one happened to be mid-assertion. Five tests
    failed exactly that way under `-n 4` while passing alone, which is the
    signature of shared mutable state and not of a bug in any of them.

    Numbered databases exist for this. Each worker gets its own, so the clear
    below is precise by construction and no worker can see another's keys.
    """
    from app.platform import ratelimit
    from app.platform.config import settings

    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    index = int(worker.removeprefix("gw")) if worker.startswith("gw") else 0
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    base = url.rpartition("/")[0]
    # Redis ships with 16 databases (0-15). Wrapping rather than overflowing
    # keeps a `PARALLEL` raised past four working — two workers sharing a
    # database is a flake, a worker pointed at database 20 is an error on every
    # command.
    os.environ["REDIS_URL"] = f"{base}/{index % 8}"
    settings.cache_clear()
    ratelimit.reset()
    yield
    os.environ["REDIS_URL"] = url
    settings.cache_clear()
    ratelimit.reset()


@pytest.fixture(autouse=True)
def _reset_rate_limits(_rate_limits_are_per_worker):
    """Every test starts with a full budget.

    Without this, two tests that each start twelve attempts inside the same
    minute make the second one fail — and it fails in whichever test happens to
    run second, which changes when a file is added. That is the worst kind of
    flake: the failure names a test that is not the problem.

    Deleting only the `rl:` namespace rather than flushing, because the same
    Redis carries the Dramatiq broker and the worker smoke test.

    A no-op when Redis is absent, which is honest rather than convenient: the
    limiter fails open there too, so the tests that assert it BITES are the ones
    that would notice, and they should.
    """
    from app.platform import ratelimit

    def clear():
        try:
            client = ratelimit.client()
            keys = list(client.scan_iter("rl:*", count=500))
            if keys:
                client.delete(*keys)
        except Exception:                                      # noqa: BLE001
            pass

    clear()
    yield
    clear()


@pytest.fixture(autouse=True, scope="session")
def _assert_reset_is_complete(wipe_statement, engine):
    """Fail loudly if the reset stops covering the schema.

    The previous hand-written TRUNCATE list silently missed fifteen tables for
    months. This asserts the catalogue query still finds the tables that matter,
    so a future refactor that narrows it fails here rather than leaking rows
    between tests for another few months.
    """
    for table in ("notifications", "audit_log", "idempotency_keys", "item_stats",
                  "media_assets", "speaking_pairs", "competition_entries"):
        assert f"DELETE FROM {table};" in wipe_statement, (
            f"{table} is not in the per-test reset")
    assert "question_type_defs WHERE source" in wipe_statement
    yield


@pytest.fixture
def db(engine, wipe_statement):
    """One session per test, wiped afterwards.

    A transaction rolled back would be cheaper still, but the schema has triggers
    that read committed state (`attempt_answers_frozen` looks up
    `attempts.status`) and several tests commit deliberately — the
    `REFRESH MATERIALIZED VIEW CONCURRENTLY` one cannot run inside a transaction
    block at all. So the tests run for real and clean up after themselves, which
    at ~2 ms is no longer worth optimising away.

    Sequences are NOT reset. `TRUNCATE ... RESTART IDENTITY` used to, and nothing
    depended on it — no test asserts a specific row id, and one that did would be
    asserting something the database does not promise.
    """
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(engine, expire_on_commit=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as c:
            c.execute(text(wipe_statement))


@pytest.fixture(scope="session")
def scorer_svc():
    from pathlib import Path
    root = Path(ROOT)
    return Scorer(Registry.from_directory(root / "registry" / "question_types"),
                  load_lexicon(root / "registry" / "lexicon"))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def seed(db):
    """A published one-section Reading test, an author and a student.

    Built through the ORM rather than raw SQL so the models are exercised on the
    way in — a mapping that disagrees with the migrations fails here.
    """
    from app.modules.content.models import (
        AnswerKeyVersion,
        BandMap,
        BandMapVersion,
        Passage,
        PassageVersion,
        Question,
        QuestionGroup,
        QuestionGroupItem,
        QuestionGroupVersion,
        QuestionVersion,
        Test,
        TestVersion,
        TestVersionGroup,
        TestVersionSection,
    )
    from app.modules.identity.models import Organization, OrgMembership, User

    org = Organization(name="Tashkent Prep", slug=f"tp-{uuid.uuid4().hex[:6]}", status="active")
    author = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Dilnoza",
                  date_of_birth=datetime(1995, 4, 1).date())
    student = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Aziza",
                   date_of_birth=datetime(2008, 3, 1).date())
    db.add_all([org, author, student])
    db.flush()
    db.add_all([
        OrgMembership(org_id=org.id, user_id=author.id, role="teacher"),
        OrgMembership(org_id=org.id, user_id=student.id, role="student"),
    ])

    bm = BandMap(name="Reading default", skill="reading", org_id=org.id)
    db.add(bm)
    db.flush()
    bmv = BandMapVersion(
        band_map_id=bm.id, max_raw=3, status="published",
        mapping=[{"raw_min": 0, "raw_max": 0, "band": 4.0},
                 {"raw_min": 1, "raw_max": 1, "band": 5.0},
                 {"raw_min": 2, "raw_max": 2, "band": 6.0},
                 {"raw_min": 3, "raw_max": 3, "band": 7.0}],
    )
    db.add(bmv)

    passage = Passage(org_id=org.id, owner_user_id=author.id, title="Cartography")
    db.add(passage)
    db.flush()
    pv = PassageVersion(
        passage_id=passage.id, title="Cartography", status="published",
        blocks=[{"id": "b1", "type": "paragraph", "label": "A",
                 "runs": [{"t": "text", "v": "Maps are old."}]}],
        paragraph_labels=["A", "B", "C"], word_count=3, checksum="x",
        created_by=author.id,
    )
    db.add(pv)

    group = QuestionGroup(org_id=org.id, owner_user_id=author.id,
                          title="Questions 1-3", skill="reading")
    db.add(group)
    db.flush()
    gv = QuestionGroupVersion(
        group_id=group.id, status="published", checksum="x", created_by=author.id,
        instructions={"en": "Complete each sentence."},
        word_limit={"max_words": 2, "allow_number": True},
    )
    db.add(gv)
    db.flush()

    answers = ["bicycle", "library", "museum"]
    qvs = []
    for i, answer in enumerate(answers, start=1):
        q = Question(org_id=org.id, owner_user_id=author.id,
                     type_key="sentence_completion", skill="reading")
        db.add(q)
        db.flush()
        qv = QuestionVersion(
            question_id=q.id, type_key="sentence_completion", type_version=1,
            payload={"text": f"Item {i} answer is {{{{s1}}}}.", "slots": ["s1"]},
            slot_keys=["s1"], status="published", checksum="x", created_by=author.id,
        )
        db.add(qv)
        db.flush()
        db.add(AnswerKeyVersion(question_version_id=qv.id, version_no=1,
                                key={"slots": {"s1": {"accept": [answer]}}},
                                created_by=author.id))
        db.add(QuestionGroupItem(group_version_id=gv.id, question_version_id=qv.id,
                                 position=i))
        qvs.append(qv)

    test = Test(org_id=org.id, owner_user_id=author.id, title="Mock 1",
                kind="mock", skills=["reading"])
    db.add(test)
    db.flush()
    tv = TestVersion(test_id=test.id, version_no=1, title="Mock 1 v1", status="draft",
                     band_map_version_id=bmv.id, created_by=author.id,
                     config={"time_limit_seconds": 3600})
    db.add(tv)
    db.flush()
    section = TestVersionSection(
        test_version_id=tv.id, position=1, skill="reading", title="Passage 1",
        passage_version_id=pv.id, time_limit_seconds=1200, declared_question_count=3)
    db.add(section)
    db.flush()
    db.add(TestVersionGroup(section_id=section.id, group_version_id=gv.id,
                            position=1, number_start=1))
    db.flush()

    return {
        "org": org, "author": author, "student": student,
        "test": test, "test_version": tv, "section": section,
        "group_version": gv, "question_versions": qvs, "band_map_version": bmv,
        "passage_version": pv,
    }


@pytest.fixture
def published(db, seed, clock):
    from app.modules.content import repo as content_repo

    content_repo.publish(db, seed["test_version"].id, seed["author"].id, clock.now())
    db.flush()
    return seed


@pytest.fixture
def with_audio(db, seed):
    """Attach a ready audio track to the seeded section.

    The play-once tests need one: a grant is issued for a specific media object,
    so a section with no audio has nothing to grant. The seeded test is a reading
    paper, and using it as a stand-in only worked while the grant was a
    placeholder hash of the section id.
    """
    from sqlalchemy import text as _text

    from app.modules.content.models import AudioTrack

    media_id = db.scalar(_text("""
        INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                  content_type, bytes, checksum_sha256, status,
                                  duration_ms)
        VALUES (:u, 'audio', 'test-media', 'seed/section1.m4a', 'audio/mp4',
                4096, 'seed-checksum', 'ready', 30000)
        RETURNING id
    """).bindparams(u=seed["author"].id))
    track = AudioTrack(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                       title="Section 1 audio", status="ready",
                       master_media_id=media_id, delivery_media_id=media_id,
                       duration_ms=30000)
    db.add(track)
    db.flush()
    seed["section"].audio_track_id = track.id
    db.flush()
    seed["audio_track"] = track
    seed["media_id"] = media_id
    return seed


@pytest.fixture
def entitled(db, seed):
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="user", subject_id=seed["student"].id,
                          feature="mock.unlimited", source_kind="order",
                          starts_at=NOW - timedelta(days=1)))
    db.flush()
    return seed
