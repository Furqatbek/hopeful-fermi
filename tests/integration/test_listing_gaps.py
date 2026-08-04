"""Three listings that lied in different ways.

`GET /questions` declared a `q` parameter, accepted it, and never applied it —
so a search box would have returned the whole bank while looking like it had
filtered, which is worse than having no search at all.

`Question.burn_score` was declared in the contract ("0..1, rises with exposure
count and org spread") and hardcoded `None` in `question_dto`, on the only
listing that carries it. Migration 0009 builds `item_exposure_stats_burn_idx`,
commented "The author's 'most burned items' view" — an index for a reader that
did not exist.

`GET /content-grants` read a fixed batch of raw rows, filtered them by policy in
Python, truncated, and always answered `next_cursor: null`. Past a couple of
hundred live grants platform-wide a centre's own grant could fall off the list
with nothing saying so — silent truncation on the screen that answers "who can
see our material".
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


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
def centre_admin(db, seed):
    from app.modules.identity.models import OrgMembership, User

    boss = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Gulnora",
                date_of_birth=dt.date(1980, 1, 1), status="active")
    db.add(boss)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=boss.id,
                         role="centre_admin", status="active"))
    db.flush()
    return boss


class TestTheQuestionSearchActuallySearches:
    @pytest.fixture
    def bank(self, db, seed):
        """Two questions with distinguishable tags and stems."""
        from app.modules.content.models import Question, QuestionVersion

        made = []
        for tag, stem in (("glass", "The history of glass making"),
                          ("rivers", "Sediment in the delta")):
            question = Question(org_id=seed["org"].id,
                                owner_user_id=seed["author"].id,
                                type_key="short_answer", skill="reading",
                                tags=[tag])
            db.add(question)
            db.flush()
            db.add(QuestionVersion(question_id=question.id, type_key="short_answer",
                                   type_version=1, payload={"text": stem,
                                                            "slots": ["s1"]},
                                   slot_keys=["s1"], points=1,
                                   created_by=seed["author"].id))
            made.append(question)
        db.flush()
        return made

    def _search(self, client, seed, term):
        return client.get(f"/api/v1/questions?q={term}&limit=100",
                          headers=auth(seed["author"].xid)).json()["items"]

    def test_a_tag_matches(self, client, seed, bank):
        found = self._search(client, seed, "glass")
        assert [q["tags"] for q in found] == [["glass"]]

    def test_a_stem_matches(self, client, seed, bank):
        """A question has no title, so the searchable text is its tags and the
        stem inside the current version's payload."""
        found = self._search(client, seed, "sediment")
        assert len(found) == 1
        assert found[0]["tags"] == ["rivers"]

    def test_a_term_that_matches_nothing_returns_nothing(self, client, seed, bank):
        """The assertion the old behaviour failed: it returned the whole bank."""
        assert self._search(client, seed, "zzzznothing") == []

    def test_search_is_case_insensitive(self, client, seed, bank):
        assert len(self._search(client, seed, "GLASS")) == 1

    def test_without_a_term_everything_comes_back(self, client, seed, bank):
        everything = client.get("/api/v1/questions?limit=100",
                                headers=auth(seed["author"].xid)).json()["items"]
        assert len(everything) >= 2


class TestBurnScoreIsReported:
    def test_a_question_with_exposure_carries_its_burn(self, client, db, seed):
        question_id = db.scalar(text("""
            SELECT question_id FROM question_versions WHERE id = :v
        """).bindparams(v=seed["question_versions"][0].id))
        db.execute(text("""
            INSERT INTO item_exposure_stats (question_id, times_sat, distinct_users,
                                             distinct_orgs, burn_score, computed_at)
            VALUES (:q, 120, 90, 4, 0.72, now())
        """).bindparams(q=question_id))
        db.flush()

        rows = client.get("/api/v1/questions?limit=100",
                          headers=auth(seed["author"].xid)).json()["items"]
        scored = [r for r in rows if r["burn_score"] is not None]
        assert len(scored) == 1
        assert scored[0]["burn_score"] == pytest.approx(0.72)

    def test_a_question_nobody_has_sat_reports_null(self, client, seed):
        """Null because there is no measurement, not zero — zero would claim the
        item is fresh, and an unsat item and an unmeasured one differ."""
        rows = client.get("/api/v1/questions?limit=100",
                          headers=auth(seed["author"].xid)).json()["items"]
        assert rows and all(r["burn_score"] is None for r in rows)

    def test_the_page_costs_one_query_for_all_of_them(self, client, db, seed):
        """`limit` reaches 200 from the console, and a lookup per row is 200
        round trips to draw a list."""
        from sqlalchemy import event

        counted: list[int] = []

        def count(*_a, **_k) -> None:
            counted.append(1)

        engine = db.get_bind()

        def queries_for() -> int:
            db.expire_all()
            counted.clear()
            event.listen(engine, "before_cursor_execute", count)
            try:
                client.get("/api/v1/questions?limit=100",
                           headers=auth(seed["author"].xid))
            finally:
                event.remove(engine, "before_cursor_execute", count)
            return len(counted)

        few = queries_for()
        from app.modules.content.models import Question

        for n in range(6):
            db.add(Question(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                            type_key="short_answer", skill="reading",
                            tags=[f"t{n}"]))
        db.flush()
        assert queries_for() == few


class TestGrantsPage:
    @pytest.fixture
    def many(self, db, seed, centre_admin):
        """Sixty live grants from this centre, so a page of 25 is genuinely
        partial."""
        for n in range(60):
            db.execute(text("""
                INSERT INTO content_grants (subject_type, subject_id, grantee_kind,
                                            grantee_id, permission, granted_by,
                                            granted_at)
                VALUES ('passage', :sid, 'public', NULL, 'view', :by,
                        now() - (:n * interval '1 minute'))
            """).bindparams(sid=seed["passage_version"].passage_id,
                            by=centre_admin.id, n=n))
        db.flush()

    def test_a_full_page_hands_back_a_cursor(self, client, centre_admin, many):
        body = client.get("/api/v1/content-grants?limit=25",
                          headers=auth(centre_admin.xid)).json()
        assert len(body["items"]) == 25
        assert body["next_cursor"], "a full page must say there is more"

    def test_the_cursor_continues_without_repeating(self, client, centre_admin,
                                                     many):
        first = client.get("/api/v1/content-grants?limit=25",
                           headers=auth(centre_admin.xid)).json()
        second = client.get(
            f"/api/v1/content-grants?limit=25&cursor={first['next_cursor']}",
            headers=auth(centre_admin.xid)).json()
        assert len(second["items"]) == 25
        assert not ({g["xid"] for g in first["items"]}
                    & {g["xid"] for g in second["items"]})

    def test_walking_reaches_every_grant(self, client, centre_admin, many):
        """The property that was broken: past the fixed batch, rows were
        unreachable and `next_cursor` said null anyway."""
        seen: set[str] = set()
        cursor = None
        for _ in range(10):
            url = "/api/v1/content-grants?limit=25"
            if cursor:
                url += f"&cursor={cursor}"
            page = client.get(url, headers=auth(centre_admin.xid)).json()
            seen.update(g["xid"] for g in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(seen) == 60

    def test_the_last_page_says_it_is_the_last(self, client, centre_admin, many):
        cursor = None
        for _ in range(10):
            url = "/api/v1/content-grants?limit=50"
            if cursor:
                url += f"&cursor={cursor}"
            page = client.get(url, headers=auth(centre_admin.xid)).json()
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert cursor is None
