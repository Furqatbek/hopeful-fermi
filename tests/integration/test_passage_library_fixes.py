"""Two defects in the passage library, both in the path a centre uses daily.

  * `GET /passages` returned `current_version: null` on every row, because
    `list_passages` called `passage_dto(p)` and the version is that function's
    optional second argument. There is no `GET /passages/{xid}` and no version
    listing, so this is the ONLY place a passage's current version xid can be
    obtained — the console's View control had nothing to open, and
    `AddSection`'s picker sent an empty value for every passage. An author who
    reloaded the page could not open a single existing passage.

  * `POST /passages/{xid}/versions` copied from `current_version_id` and never
    advanced it, so a second new version copied v1 again. v2's edits stayed in
    the database with nothing pointing at them — data loss that presents as the
    button not working.

Neither is subtle once written down, and both survived a 2,000-test suite,
because nothing asserted the relationship between the two endpoints.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token

ORIGINAL = {"claim": "original", "statement_version": "1"}
PARAGRAPHS = [
    {"type": "paragraph", "runs": [{"t": "text", "v": "Glass is older than iron."}]},
    {"type": "paragraph", "runs": [{"t": "text", "v": "It was first made in Egypt."}]},
]


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
def author(seed):
    return auth(seed["author"].xid)


def _create(client, author, title="The history of glass"):
    response = client.post("/api/v1/passages", headers=author, json={
        "title": title, "blocks": PARAGRAPHS, "attestation": ORIGINAL})
    assert response.status_code == 201, response.text
    return response.json()


class TestTheListingCarriesTheVersion:
    def test_a_listed_passage_names_its_current_version(self, client, author):
        created = _create(client, author)
        listed = next(p for p in client.get("/api/v1/passages", headers=author)
                      .json()["items"] if p["xid"] == created["xid"])
        assert listed["current_version"] is not None
        assert listed["current_version"]["xid"] == created["current_version"]["xid"]

    def test_the_version_xid_opens_the_version(self, client, author):
        """The whole point: the listing is the only route to this xid, and the
        console's View control does exactly this."""
        created = _create(client, author)
        # By xid, not by index: the seed's own passage has no
        # `current_version_id` at all, which is the imported-passage shape and
        # exactly what the row below must tolerate.
        row = next(p for p in client.get("/api/v1/passages", headers=author)
                   .json()["items"] if p["xid"] == created["xid"])
        xid = row["current_version"]["xid"]
        opened = client.get(f"/api/v1/passage-versions/{xid}", headers=author)
        assert opened.status_code == 200, opened.text
        assert opened.json()["blocks"]

    def test_it_carries_the_word_count_the_library_column_shows(
            self, client, author):
        created = _create(client, author)
        row = next(p for p in client.get("/api/v1/passages", headers=author)
                   .json()["items"] if p["xid"] == created["xid"])
        assert row["current_version"]["word_count"] > 0

    def test_a_passage_with_no_version_still_lists(self, client, db, seed, author):
        """`content.importer` creates passages without setting
        `current_version_id`, so the listing must tolerate a null rather than
        failing the whole page for one row."""
        from app.modules.content.models import Passage

        db.add(Passage(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                       title="Imported, unlinked"))
        db.flush()
        titles = [p["title"] for p in
                  client.get("/api/v1/passages", headers=author).json()["items"]]
        assert "Imported, unlinked" in titles

    def test_one_query_serves_the_whole_page(self, client, db, author):
        """A lazy load per row is two hundred round trips on a real library."""
        from sqlalchemy import event

        counted: list[int] = []

        def count(*_args, **_kwargs) -> None:
            counted.append(1)

        engine = db.get_bind()

        def queries_for(n: int) -> int:
            # The identity map would otherwise answer a per-row `session.get`
            # from memory, so a genuine N+1 issues no SQL here and the test
            # passes against the bug it exists to catch. Verified: without this,
            # reintroducing the per-row lookup does not fail.
            db.expire_all()
            counted.clear()
            event.listen(engine, "before_cursor_execute", count)
            try:
                client.get("/api/v1/passages", headers=author)
            finally:
                event.remove(engine, "before_cursor_execute", count)
            return len(counted)

        _create(client, author, title="Only one")
        few = queries_for(1)
        for n in range(6):
            _create(client, author, title=f"Passage {n}")
        many = queries_for(7)
        # The number, not the size of it: a lazy load per row is two hundred
        # round trips on a real library, and the shape of that bug is that the
        # count tracks the row count.
        assert many == few, f"{few} queries for one passage, {many} for seven"


class TestANewVersionMovesForward:
    def test_the_second_new_version_copies_the_first_new_one(
            self, client, author):
        """The data-loss case. v2 was edited, v3 copied v1, and v2 was still in
        the database with nothing pointing at it."""
        created = _create(client, author)
        xid = created["xid"]

        v2 = client.post(f"/api/v1/passages/{xid}/versions", headers=author).json()
        read = client.get(f"/api/v1/passage-versions/{v2['xid']}", headers=author)
        tag = read.headers["ETag"]
        edited = [{"type": "paragraph",
                   "runs": [{"t": "text", "v": "Rewritten in version two."}]}]
        assert client.patch(f"/api/v1/passage-versions/{v2['xid']}",
                            headers={**author, "If-Match": tag},
                            json={"blocks": edited}).status_code == 200

        v3 = client.post(f"/api/v1/passages/{xid}/versions", headers=author).json()
        body = client.get(f"/api/v1/passage-versions/{v3['xid']}",
                          headers=author).json()
        assert body["blocks"][0]["runs"][0]["v"] == "Rewritten in version two."
        assert v3["version_no"] == 3

    def test_the_listing_follows_the_new_version(self, client, author):
        created = _create(client, author)
        v2 = client.post(f"/api/v1/passages/{created['xid']}/versions",
                         headers=author).json()
        row = next(p for p in client.get("/api/v1/passages", headers=author)
                   .json()["items"] if p["xid"] == created["xid"])
        assert row["current_version"]["xid"] == v2["xid"]

    def test_the_word_count_is_recomputed_on_the_copy(self, client, author):
        """Left at zero before, so the library column read 0 for every version
        after the first."""
        created = _create(client, author)
        v2 = client.post(f"/api/v1/passages/{created['xid']}/versions",
                         headers=author).json()
        assert v2["word_count"] == created["current_version"]["word_count"]
        assert v2["word_count"] > 0

    def test_an_imported_passage_gets_its_text(self, client, db, seed, author):
        """`content.importer` never sets `current_version_id`, so the copy source
        was null and a new version of an imported paper came back empty."""
        from app.modules.content.models import Passage, PassageVersion

        passage = Passage(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                          title="Imported")
        db.add(passage)
        db.flush()
        db.add(PassageVersion(passage_id=passage.id, title="Imported",
                              blocks=PARAGRAPHS, paragraph_labels=["A", "B"],
                              word_count=9, checksum="",
                              created_by=seed["author"].id))
        db.flush()

        new = client.post(f"/api/v1/passages/{passage.xid}/versions",
                          headers=author)
        assert new.status_code == 201, new.text
        assert new.json()["blocks"], "a new version of an imported passage is empty"
