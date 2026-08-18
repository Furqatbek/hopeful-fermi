"""The cursor nine listings promised and one issued.

`{items, next_cursor}` is the envelope this contract declares for every paged
listing, and the `cursor` query parameter was declared on eight of the ten
endpoints below. Exactly one handler ever issued a cursor. The rest returned
`next_cursor: null` unconditionally under a default `limit` of 25, so:

  * a centre with four hundred students had twenty-five, and nothing anywhere
    said the others existed;
  * a library of two hundred passages had twenty-five;
  * four of the listings had **no ORDER BY at all**, so which twenty-five could
    differ between two calls a second apart.

The property under test is the one that matters and the one an offset cannot
give you: walking a listing page by page returns **every row exactly once**.
Each test seeds more rows than a page holds and walks to the end.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def client(engine, db):
    """The request-scoped session IS the test's, so a seeded row is visible to
    the handler without a commit race."""
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def walk(client, path: str, headers: dict, limit: int = 3,
         key=lambda row: row["xid"]) -> list:
    """Every row, page by page, following the cursor to the end.

    Capped at 50 pages: a cursor that does not advance — the classic sign of a
    keyset walking the wrong way against a DESC ordering — returns the same page
    for ever, and a test that hangs is a worse report than one that fails.
    """
    seen, cursor = [], None
    for _ in range(50):
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        response = client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(key(row) for row in body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            return seen
        assert len(body["items"]) == limit, "a page short of the limit handed back a cursor"
    raise AssertionError("the cursor never ran out — it is not advancing")


class TestTheRoster:
    """The listing where the missing cursor cost the most: a centre's people."""

    @pytest.fixture
    def crowded(self, db, seed):
        from app.modules.identity.models import OrgMembership, User

        for n in range(7):
            u = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                     given_name=f"Student{n}", date_of_birth=date(2007, 1, 1))
            db.add(u)
            db.flush()
            db.add(OrgMembership(org_id=seed["org"].id, user_id=u.id,
                                 role="student", status="active"))
        db.flush()
        return seed

    def test_every_member_appears_exactly_once(self, client, db, seed, crowded):
        total = db.scalar(text("""
            SELECT count(*) FROM org_memberships
             WHERE org_id = :o AND status = 'active'
        """).bindparams(o=seed["org"].id))
        assert total > 3, "the fixture must exceed one page or this proves nothing"

        head = auth(seed["author"].xid)
        seen = walk(client, f"/api/v1/orgs/{seed['org'].xid}/members", head,
                    key=lambda row: row["user"]["xid"])
        assert len(seen) == total
        assert len(set(seen)) == total, "a row was returned on two pages"

    def test_one_page_is_not_the_whole_roster(self, client, seed, crowded):
        """The defect itself: the first page used to be all there was."""
        body = client.get(f"/api/v1/orgs/{seed['org'].xid}/members?limit=3",
                          headers=auth(seed["author"].xid)).json()
        assert len(body["items"]) == 3
        assert body["next_cursor"], "the rest of the centre was unreachable"


class TestTheLibrary:
    """Four listings that had no ordering at all, so "the first 25" was whatever
    the query plan happened to emit."""

    @pytest.fixture
    def many(self, db, seed):
        from app.modules.content.models import Passage

        for n in range(7):
            db.add(Passage(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                           title=f"Passage {n}", visibility="org_private"))
        db.flush()
        return seed

    def test_every_passage_appears_exactly_once(self, client, seed, many):
        seen = walk(client, "/api/v1/passages", auth(seed["author"].xid))
        assert len(seen) == len(set(seen)), "a passage was returned on two pages"
        assert len(seen) >= 7

    def test_the_order_is_stable_across_calls(self, client, seed, many):
        """Two identical requests return the same page.

        **Weaker than it reads, and said so rather than trusted.** Calibrated by
        removing the ORDER BY, and it still passed: at this size PostgreSQL
        returns a sequential scan in physical order both times, so the test
        cannot see the difference. The ordering matters — an unordered LIMIT is
        free to change with a plan change, a VACUUM or an update that moves a row
        — but this assertion is not what proves it. What proves it is the walk
        above: without a stable order, paging returns duplicates and misses, and
        `test_every_passage_appears_exactly_once` does fail then.
        """
        head = auth(seed["author"].xid)
        first = client.get("/api/v1/passages?limit=5", headers=head).json()["items"]
        again = client.get("/api/v1/passages?limit=5", headers=head).json()["items"]
        assert [p["xid"] for p in first] == [p["xid"] for p in again]


class TestAnExhaustedListingHandsBackNothing:
    def test_a_page_that_ends_exactly_on_the_limit_has_no_cursor(
            self, client, db, seed):
        """Fetching `limit + 1` is what makes this exact. Comparing
        `len(rows) == limit` would hand back a cursor for a listing whose total
        is exactly one page, and the client would fetch an empty page to find
        out — which is what a "Show more" button that turns into nothing looks
        like."""
        head = auth(seed["author"].xid)
        # Ask for exactly as many as exist. Counted rather than deleted-and-
        # reseeded: the seeded passage is referenced by a published paper, and
        # tearing that down would be testing the fixture rather than the cursor.
        total = len(walk(client, "/api/v1/passages", head, limit=50))
        body = client.get(f"/api/v1/passages?limit={total}", headers=head).json()
        assert len(body["items"]) == total
        assert body["next_cursor"] is None


class TestACursorIsOpaqueAndForgiving:
    def test_nonsense_starts_from_the_top_rather_than_500ing(self, client, seed):
        """A hand-edited URL is a client bug, not a server error. Raising would
        turn a typo into a 500 on a screen someone is trying to read."""
        response = client.get("/api/v1/passages?limit=3&cursor=not-a-cursor",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 200, response.text

    def test_a_cursor_from_another_listing_is_ignored(self, client, seed):
        """Different listings have different key arities, and a cursor minted
        for one of them decodes to the wrong shape rather than to a plausible
        position."""
        from app.api.paging import encode_key

        wrong = encode_key("2026-01-01T00:00:00+00:00", 5, 9)   # a three-part key
        response = client.get(f"/api/v1/passages?limit=3&cursor={wrong}",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 200, response.text
