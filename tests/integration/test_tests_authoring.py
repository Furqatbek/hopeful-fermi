"""Composition: the test library, versions, sections and placement.

`test_authoring_flow.py` covers what an author drives on the happy path —
numbering is test-wide, cloning copies composition and not assets. This covers
the 16% of `tests_authoring.py` that no test had ever executed, which included
the whole of `update_section` and `delete_section`.

**`update_section` could not move a section at all.** Both directions were broken
and the downhill one was worse than a 500.

`_make_room()` is *insert* semantics: park every row at or after the target in the
negative range, then bring them back one higher. Its docstring explains why the
two-step shift is necessary and it is right about that. `update_section` parked
the moving row itself first — and `_make_room`'s second statement is
`WHERE position < 0`, so it swept the moving row up with everything else.

Moving a section to a LATER position:

    sections 1, 2, 3 — move section 1 to position 3
    park            -1,  2,  3
    _make_room(3)   -1,  2, -3
    step two         2,  2,  4     <-- ERROR: duplicate key on (test_version_id, position)

A 500, every time.

Moving to an EARLIER position "worked" and left a gap: 1, 2, 4. That is the more
dangerous of the two, because nothing downstream complains until a student does.
`AttemptSection.position` is copied straight from the section's position and
`ExamSession.enter_section(attempt, position)` looks it up by that number, so a
published test with sections at 1, 2, 4 is a test where **the student cannot enter
the last section** — "Section not found in this attempt." The publish gate never
checked contiguity, so nothing between the author's click and the exam room would
have caught it.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.deps import issue_access_token
from app.modules.content.models import TestVersionSection


@pytest.fixture
def client(engine, db):
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


@pytest.fixture
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return auth(seed["author"].xid)


@pytest.fixture
def centre_admin(db, seed):
    """A second user who holds `centre_admin` at the seeded centre.

    The seeded author is a `teacher`, and several checks here are exactly the ones
    that separate the two roles.
    """
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Gulnora",
                date_of_birth=__import__("datetime").date(1980, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def _positions(db, test_version_id: int) -> list[int]:
    return list(db.scalars(
        select(TestVersionSection.position)
        .where(TestVersionSection.test_version_id == test_version_id)
        .order_by(TestVersionSection.position)))


def _section(client, headers, tv, position: int, title: str = "Passage"):
    return _ok(client.post(f"/api/v1/test-versions/{tv.xid}/sections",
                           json={"position": position, "skill": "reading",
                                 "title": title}, headers=headers), 201)


def _csv_rows(response) -> list[list[str]]:
    """Parsed, not string-matched. The writer emits CRLF and quotes any cell with
    a comma in it, so `text.split(",")` reads a different file than the importer
    does."""
    import csv
    import io as _io

    return list(csv.reader(_io.StringIO(response.text)))


def _by_title(db, title: str) -> TestVersionSection:
    return db.scalars(select(TestVersionSection)
                      .where(TestVersionSection.title == title)).one()


# ── moving a section ─────────────────────────────────────────────────

class TestMovingASection:
    """The endpoint had never been executed. Both directions were broken."""

    @pytest.fixture
    def three(self, client, author, db, seed):
        """The seeded section at position 1, plus two more. Positions 1, 2, 3."""
        tv = seed["test_version"]
        _section(client, author, tv, 2, "Second")
        _section(client, author, tv, 3, "Third")
        db.flush()
        assert _positions(db, tv.id) == [1, 2, 3]
        return tv

    def _move(self, client, headers, section, position: int):
        return client.patch(f"/api/v1/sections/{section.xid}",
                            json={"position": position, "skill": section.skill,
                                  "title": section.title}, headers=headers)

    def test_moving_a_section_later_does_not_500(self, client, author, db, three):
        """`_make_room`'s second statement swept the row `update_section` had
        parked, colliding it with the row still sitting at that position."""
        first = _by_title(db, "Passage 1")
        response = self._move(client, author, first, 3)
        assert response.status_code == 200, response.text

    def test_moving_later_puts_it_there_and_shifts_the_rest_up(self, client, author,
                                                               db, three):
        first = _by_title(db, "Passage 1")
        _ok(self._move(client, author, first, 3))
        db.expire_all()
        order = db.execute(text("""
            SELECT title FROM test_version_sections WHERE test_version_id = :tv
            ORDER BY position
        """).bindparams(tv=three.id)).scalars().all()
        assert order == ["Second", "Third", "Passage 1"]
        assert _positions(db, three.id) == [1, 2, 3]

    def test_moving_a_middle_section_earlier_leaves_no_gap(self, client, author, db,
                                                           three):
        """This is the one that reaches a student. Positions 1, 2, 4 publish
        cleanly, and then `enter_section(attempt, 3)` is a 404 mid-exam.

        Deliberately a MIDDLE section. Moving the last section to the front
        happens to survive the old code — `_make_room` swept every row including
        the parked mover, and the mover's position was then overwritten anyway — so
        a test that picked the last row would have passed against the bug.
        """
        _ok(self._move(client, author, _by_title(db, "Second"), 1))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]
        order = db.execute(text("""
            SELECT title FROM test_version_sections WHERE test_version_id = :tv
            ORDER BY position
        """).bindparams(tv=three.id)).scalars().all()
        assert order == ["Second", "Passage 1", "Third"]

    def test_moving_the_last_section_to_the_front(self, client, author, db, three):
        third = _by_title(db, "Third")
        _ok(self._move(client, author, third, 1))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]
        order = db.execute(text("""
            SELECT title FROM test_version_sections WHERE test_version_id = :tv
            ORDER BY position
        """).bindparams(tv=three.id)).scalars().all()
        assert order == ["Third", "Passage 1", "Second"]

    def test_moving_to_the_middle_from_either_side(self, client, author, db, three):
        _ok(self._move(client, author, _by_title(db, "Third"), 2))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]
        _ok(self._move(client, author, _by_title(db, "Passage 1"), 2))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]
        order = db.execute(text("""
            SELECT title FROM test_version_sections WHERE test_version_id = :tv
            ORDER BY position
        """).bindparams(tv=three.id)).scalars().all()
        assert order == ["Third", "Passage 1", "Second"]

    def test_a_position_past_the_end_lands_at_the_end(self, client, author, db,
                                                      three):
        """Clamped rather than honoured. A client sending 99 means "last", and
        taking it literally would leave a permanent gap."""
        _ok(self._move(client, author, _by_title(db, "Passage 1"), 99))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]
        assert db.scalar(text("""
            SELECT title FROM test_version_sections
            WHERE test_version_id = :tv ORDER BY position DESC LIMIT 1
        """).bindparams(tv=three.id)) == "Passage 1"

    def test_moving_a_section_to_where_it_already_is_changes_nothing(
            self, client, author, db, three):
        _ok(self._move(client, author, _by_title(db, "Second"), 2))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]

    def test_moving_the_last_section_past_the_end_is_a_no_op(self, client, author,
                                                             db, three):
        """The clamp lands on the position it is already at, so the shift must be
        skipped rather than run with an empty span."""
        _ok(self._move(client, author, _by_title(db, "Third"), 99))
        db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]
        assert db.scalar(text("""
            SELECT title FROM test_version_sections
            WHERE test_version_id = :tv ORDER BY position DESC LIMIT 1
        """).bindparams(tv=three.id)) == "Third"

    def test_repeated_moves_stay_contiguous(self, client, author, db, three):
        """Each broken move leaked one gap, so the damage accumulated silently
        over an afternoon's editing."""
        for _ in range(4):
            _ok(self._move(client, author, _by_title(db, "Second"), 3))
            db.expire_all()
            _ok(self._move(client, author, _by_title(db, "Second"), 1))
            db.expire_all()
        assert _positions(db, three.id) == [1, 2, 3]

    def test_the_other_fields_still_update(self, client, author, db, seed):
        section = seed["section"]
        body = _ok(client.patch(f"/api/v1/sections/{section.xid}",
                                json={"position": section.position,
                                      "skill": "listening", "title": "Retitled",
                                      "time_limit_seconds": 1800,
                                      "declared_question_count": 13,
                                      "play_once": False}, headers=author))
        assert body["skill"] == "listening"
        assert body["title"] == "Retitled"
        assert body["time_limit_seconds"] == 1800
        assert body["declared_question_count"] == 13
        assert body["play_once"] is False

    def test_a_published_version_refuses_the_edit(self, client, admin, db, published):
        section = published["section"]
        refused = client.patch(f"/api/v1/sections/{section.xid}",
                               json={"position": 1, "skill": "reading",
                                     "title": "Nope"}, headers=admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_immutable"

    def test_an_unknown_section_is_a_404(self, client, author):
        assert client.patch(f"/api/v1/sections/{uuid.uuid4()}",
                            json={"position": 1, "skill": "reading", "title": "x"},
                            headers=author).status_code == 404


class TestCreatingASection:
    def test_a_position_past_the_end_appends(self, client, author, db, seed):
        """Same clamp as the move, for the same reason: `_make_room` shifts
        nothing, so honouring 99 would insert a gap at creation time."""
        tv = seed["test_version"]
        body = _section(client, author, tv, 99, "Appended")
        assert body["position"] == 2
        assert _positions(db, tv.id) == [1, 2]

    def test_inserting_in_the_middle_shifts_the_rest(self, client, author, db, seed):
        tv = seed["test_version"]
        _section(client, author, tv, 2, "Was second")
        inserted = _section(client, author, tv, 2, "Now second")
        assert inserted["position"] == 2
        db.expire_all()
        assert _positions(db, tv.id) == [1, 2, 3]
        assert db.execute(text("""
            SELECT title FROM test_version_sections WHERE test_version_id = :tv
            ORDER BY position
        """).bindparams(tv=tv.id)).scalars().all() \
            == ["Passage 1", "Now second", "Was second"]


class TestDeletingASection:
    def test_deleting_closes_the_gap(self, client, author, db, seed):
        """A hole left by a delete reaches a student the same way a hole left by a
        move does."""
        tv = seed["test_version"]
        _section(client, author, tv, 2, "Doomed")
        _section(client, author, tv, 3, "Survivor")
        doomed = _by_title(db, "Doomed")
        assert client.delete(f"/api/v1/sections/{doomed.xid}",
                             headers=author).status_code == 204
        db.expire_all()
        assert _positions(db, tv.id) == [1, 2]
        assert db.execute(text("""
            SELECT title FROM test_version_sections WHERE test_version_id = :tv
            ORDER BY position
        """).bindparams(tv=tv.id)).scalars().all() == ["Passage 1", "Survivor"]

    def test_deleting_takes_its_group_placements_with_it(self, client, author, db,
                                                         seed):
        tv = seed["test_version"]
        before = db.scalar(text("SELECT count(*) FROM test_version_groups"))
        assert before == 1
        section = seed["section"]
        assert client.delete(f"/api/v1/sections/{section.xid}",
                             headers=author).status_code == 204
        assert db.scalar(text("SELECT count(*) FROM test_version_groups")) == 0
        assert _positions(db, tv.id) == []

    def test_a_published_version_refuses_the_delete(self, client, admin, published):
        refused = client.delete(f"/api/v1/sections/{published['section'].xid}",
                                headers=admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_immutable"


# ── references are authorized on the way in ──────────────────────────

class TestCompositionReferencesAreScoped:
    """"Composition is the one place where a reference could smuggle a
    competitor's passage into a test the actor owns." A centre's material must
    never leak to a competitor, and a reference by xid is the obvious way to try.
    """

    @pytest.fixture
    def rival(self, db, seed):
        """A passage and an audio track at another centre entirely."""
        from app.modules.content.models import AudioTrack, Passage, PassageVersion
        from app.modules.identity.models import Organization

        org = Organization(name="Rival", slug=f"rival-{uuid.uuid4().hex[:6]}",
                           status="active")
        db.add(org)
        db.flush()
        passage = Passage(org_id=org.id, owner_user_id=seed["author"].id,
                          title="Theirs", visibility="org_private")
        db.add(passage)
        db.flush()
        pv = PassageVersion(passage_id=passage.id, version_no=1, title="Theirs",
                            blocks=[], word_count=10,
                            created_by=seed["author"].id)
        track = AudioTrack(org_id=org.id, owner_user_id=seed["author"].id,
                           title="Their audio", status="ready", duration_ms=1000)
        db.add_all([pv, track])
        db.flush()
        return {"passage_version": pv, "audio_track": track}

    def test_a_rival_centres_passage_cannot_be_placed(self, client, author, db,
                                                      seed, rival):
        response = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/sections",
                               json={"position": 2, "skill": "reading",
                                     "title": "Smuggled",
                                     "passage_version_xid":
                                         str(rival["passage_version"].xid)},
                               headers=author)
        assert response.status_code == 404, response.text

    def test_a_rival_centres_audio_cannot_be_placed(self, client, author, db, seed,
                                                    rival):
        response = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/sections",
                               json={"position": 2, "skill": "listening",
                                     "title": "Smuggled",
                                     "audio_track_xid": str(rival["audio_track"].xid)},
                               headers=author)
        assert response.status_code == 404, response.text

    def test_it_is_refused_on_update_too(self, client, author, db, seed, rival):
        """The check has to be on the way in at BOTH doors — the reference is just
        as smuggled if it arrives by PATCH."""
        section = seed["section"]
        response = client.patch(f"/api/v1/sections/{section.xid}",
                                json={"position": 1, "skill": "reading",
                                      "title": "Passage 1",
                                      "passage_version_xid":
                                          str(rival["passage_version"].xid)},
                                headers=author)
        assert response.status_code == 404, response.text

    def test_own_passage_and_audio_are_accepted(self, client, author, db, seed,
                                                with_audio):
        body = _ok(client.patch(f"/api/v1/sections/{seed['section'].xid}",
                                json={"position": 1, "skill": "listening",
                                      "title": "Passage 1",
                                      "passage_version_xid":
                                          str(seed["passage_version"].xid),
                                      "audio_track_xid":
                                          str(seed["audio_track"].xid)},
                                headers=author))
        assert body["passage_version"]["xid"] == str(seed["passage_version"].xid)
        assert body["audio_track"]["xid"] == str(seed["audio_track"].xid)


# ── the library ──────────────────────────────────────────────────────

class TestTheLibraryFilters:
    """Five query filters, none of which had ever been exercised. A filter that
    silently matches nothing looks identical to an empty library."""

    @pytest.fixture
    def library(self, db, seed):
        from app.modules.content.models import Test, TestVersion

        rows = [
            Test(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                 title="Listening mock A", kind="practice", variant="academic",
                 skills=["listening"], tags=["hard"], visibility="org_private"),
            Test(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                 title="Placement paper", kind="placement", variant="academic",
                 skills=["reading"], tags=["easy"], visibility="author_private"),
        ]
        db.add_all(rows)
        db.flush()
        for row in rows:
            db.add(TestVersion(test_id=row.id, title=row.title,
                               created_by=seed["author"].id))
        db.flush()
        return rows

    def _titles(self, client, headers, query: str = "") -> list[str]:
        body = _ok(client.get(f"/api/v1/tests{query}", headers=headers))
        return sorted(t["title"] for t in body["items"])

    def test_everything_is_listed_without_a_filter(self, client, author, library):
        assert self._titles(client, author) == \
            ["Listening mock A", "Mock 1", "Placement paper"]

    def test_a_text_search(self, client, author, library):
        assert self._titles(client, author, "?q=mock") == \
            ["Listening mock A", "Mock 1"]

    def test_a_kind_filter(self, client, author, library):
        assert self._titles(client, author, "?kind=placement") == ["Placement paper"]

    def test_a_skill_filter(self, client, author, library):
        assert self._titles(client, author, "?skill=listening") == ["Listening mock A"]

    def test_a_tag_filter(self, client, author, library):
        assert self._titles(client, author, "?tag=easy") == ["Placement paper"]

    def test_a_visibility_filter(self, client, author, library):
        assert self._titles(client, author, "?visibility=author_private") == \
            ["Placement paper"]

    def test_filters_compose(self, client, author, library):
        assert self._titles(client, author, "?q=mock&skill=listening") == \
            ["Listening mock A"]

    def test_an_archived_test_is_not_listed(self, client, author, db, library):
        db.execute(text("UPDATE tests SET archived_at = now() "
                        "WHERE title = 'Placement paper'"))
        db.flush()
        assert "Placement paper" not in self._titles(client, author)


class TestAttribution:
    """`org` is in the spec's `Test` schema and the DTO emitted nothing, so the
    library never said which centre a paper came from — which for a platform admin
    looking at every centre's material at once is the one thing they need."""

    def test_a_test_names_its_owning_centre(self, client, author, db, seed):
        body = _ok(client.get("/api/v1/tests", headers=author))
        mine = next(t for t in body["items"] if t["title"] == "Mock 1")
        assert mine["org"]["xid"] == str(seed["org"].xid)
        assert mine["org"]["name"] == "Tashkent Prep"

    def test_the_owning_centres_settings_are_not_included(self, client, author, db,
                                                          seed):
        """`identity.org_dto` also returns `settings`, and a shared paper appears in
        another centre's library — emitting its owner's configuration there would
        hand a competitor how that centre is set up. Whose paper it is, is
        attribution; how they run their centre, is not."""
        seed["org"].settings = {"teacher_can_publish": True}
        db.flush()
        body = _ok(client.get("/api/v1/tests", headers=author))
        mine = next(t for t in body["items"] if t["title"] == "Mock 1")
        assert set(mine["org"]) == {"xid", "name", "slug", "kind", "status"}
        assert "teacher_can_publish" not in client.get("/api/v1/tests",
                                                       headers=author).text

    def test_a_deliberately_shared_paper_is_attributed_to_its_owner(
            self, client, db, seed):
        """The decision, stated: attribution travels with content the viewer is
        already entitled to see.

        Every route by which centre B sees centre A's test is a deliberate act —
        A set it `platform_global`, A issued a `content_grant`, or a platform admin
        is looking. In all three, "this paper came from Tashkent Prep" is the
        expected behaviour of shared material, not a leak of it. The thing that
        must never travel is the paper itself when it was NOT shared, which
        `policy.filter_content` handles and `test_authz_leaks.py` guards on
        org-private content.
        """
        from app.modules.identity.models import Organization, OrgMembership, User

        seed["test"].visibility = "platform_global"
        db.flush()
        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        spy = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Sardor",
                   date_of_birth=__import__("datetime").date(1990, 1, 1))
        db.add(spy)
        db.flush()
        db.add(OrgMembership(org_id=rival.id, user_id=spy.id, role="teacher",
                             status="active"))
        db.flush()
        body = _ok(client.get("/api/v1/tests", headers=auth(spy.xid)))
        shared = next(t for t in body["items"] if t["title"] == "Mock 1")
        assert shared["org"]["name"] == "Tashkent Prep"
        assert "settings" not in shared["org"]

    def test_an_org_private_paper_is_still_invisible_to_a_rival(self, client, db,
                                                               seed):
        """The half that is not negotiable. Attribution is only reachable through
        content the viewer may already see."""
        from app.modules.identity.models import Organization, OrgMembership, User

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        spy = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Sardor",
                   date_of_birth=__import__("datetime").date(1990, 1, 1))
        db.add(spy)
        db.flush()
        db.add(OrgMembership(org_id=rival.id, user_id=spy.id, role="teacher",
                             status="active"))
        db.flush()
        body = _ok(client.get("/api/v1/tests", headers=auth(spy.xid)))
        assert body["items"] == []


class TestTheTestRecord:
    def test_reading_one_returns_its_versions_and_permissions(self, client, author,
                                                              db, seed):
        body = _ok(client.get(f"/api/v1/tests/{seed['test'].xid}", headers=author))
        assert [v["version_no"] for v in body["versions"]] == [1]
        assert "permissions" in body

    def test_a_title_and_tags_can_be_edited(self, client, author, seed):
        body = _ok(client.patch(f"/api/v1/tests/{seed['test'].xid}",
                                json={"title": "Renamed", "description": "why",
                                      "tags": ["reading", "week-3"]},
                                headers=author))
        assert body["title"] == "Renamed"
        assert body["tags"] == ["reading", "week-3"]

    def test_a_teacher_cannot_widen_visibility(self, client, author, seed):
        """"Widening visibility is a share, not an edit: a teacher who may edit a
        test must not be able to publish it to the whole platform." That is the
        contractual promise about centre material, at the one endpoint where an
        ordinary field update could break it."""
        refused = client.patch(f"/api/v1/tests/{seed['test'].xid}",
                               json={"visibility": "platform_global"},
                               headers=author)
        assert refused.status_code == 403

    def test_a_centre_admin_can(self, client, centre_admin, db, seed):
        body = _ok(client.patch(f"/api/v1/tests/{seed['test'].xid}",
                                json={"visibility": "platform_global"},
                                headers=centre_admin))
        assert body["visibility"] == "platform_global"

    def test_setting_the_same_visibility_is_not_a_share(self, client, author, seed):
        """A no-op write must not need authority the author does not have —
        otherwise a client that PATCHes the whole object gets a 403 for changing
        nothing."""
        body = _ok(client.patch(f"/api/v1/tests/{seed['test'].xid}",
                                json={"title": "Renamed",
                                      "visibility": seed["test"].visibility},
                                headers=author))
        assert body["title"] == "Renamed"

    def test_an_unarchived_draft_test_can_be_deleted(self, client, author, db, seed):
        assert client.delete(f"/api/v1/tests/{seed['test'].xid}",
                             headers=author).status_code == 204
        assert db.scalar(text("SELECT archived_at IS NOT NULL FROM tests "
                              "WHERE id = :t").bindparams(t=seed["test"].id))

    def test_a_test_with_a_published_version_cannot_be_deleted(self, client, admin,
                                                               published):
        """"Attempts reference published versions, and a takedown investigation
        needs the evidence intact." """
        refused = client.delete(f"/api/v1/tests/{published['test'].xid}",
                                headers=admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "published_content_not_deletable"

    def test_a_test_with_no_org_has_no_org_settings_to_consult(self, client, db,
                                                               seed):
        """Platform-owned material belongs to no centre, so there is no
        `settings.teacher_can_publish` to read. The policy engine is a pure
        function of its arguments and must be handed `{}`, not asked to cope with
        a missing organization."""
        from app.modules.identity.models import PlatformRoleGrant, User

        staff = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                     given_name="Platform",
                     date_of_birth=__import__("datetime").date(1990, 1, 1))
        db.add(staff)
        db.flush()
        db.add(PlatformRoleGrant(user_id=staff.id, role="platform_admin",
                                 granted_by=staff.id))
        db.flush()
        headers = auth(staff.xid)
        created = _ok(client.post("/api/v1/tests", json={"title": "Platform mock"},
                                  headers=headers), 201)
        assert created["org"] is None
        renamed = _ok(client.patch(f"/api/v1/tests/{created['xid']}",
                                   json={"title": "Platform mock v2"},
                                   headers=headers))
        assert renamed["title"] == "Platform mock v2"

    def test_creating_against_an_unknown_org_is_a_404(self, client, author):
        assert client.post("/api/v1/tests",
                           json={"title": "x", "org_xid": str(uuid.uuid4())},
                           headers=author).status_code == 404

    def test_creating_against_another_centres_org_is_a_404(self, client, author, db):
        from app.modules.identity.models import Organization

        org = Organization(name="Rival", slug=f"rival-{uuid.uuid4().hex[:6]}",
                           status="active")
        db.add(org)
        db.flush()
        assert client.post("/api/v1/tests",
                           json={"title": "x", "org_xid": str(org.xid)},
                           headers=author).status_code == 404


# ── versions ─────────────────────────────────────────────────────────

class TestVersions:
    def test_versions_are_listed_newest_first(self, client, author, db, seed):
        _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions", json={},
                        headers=author), 201)
        body = _ok(client.get(f"/api/v1/tests/{seed['test'].xid}/versions",
                              headers=author))
        assert [v["version_no"] for v in body] == [2, 1]

    def test_a_new_version_seeds_from_the_latest(self, client, author, db, seed):
        """"How a published test is edited: seed a new draft from it and leave the
        published version untouched, because students have sat it." """
        body = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions", json={},
                               headers=author), 201)
        assert body["version_no"] == 2
        assert body["status"] == "draft"
        assert db.scalar(text("""
            SELECT count(*) FROM test_version_sections s
            JOIN test_versions v ON v.id = s.test_version_id
            WHERE v.version_no = 2
        """)) == 1

    def test_a_named_source_version_is_used(self, client, author, db, seed):
        body = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/versions",
                               json={"from_version_xid":
                                     str(seed["test_version"].xid)},
                               headers=author), 201)
        assert body["version_no"] == 2

    def test_a_source_from_another_test_is_a_404(self, client, author, db, seed):
        """Seeding from someone else's version would copy their composition into
        your test under the guise of a version bump."""
        from app.modules.content.models import Test, TestVersion

        other = Test(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                     title="Other", kind="mock", variant="academic")
        db.add(other)
        db.flush()
        their_version = TestVersion(test_id=other.id, title="Other",
                                    created_by=seed["author"].id)
        db.add(their_version)
        db.flush()
        assert client.post(f"/api/v1/tests/{seed['test'].xid}/versions",
                           json={"from_version_xid": str(their_version.xid)},
                           headers=author).status_code == 404

    def test_a_version_carries_an_etag_once_it_has_a_checksum(self, client, admin,
                                                              published):
        response = client.get(f"/api/v1/test-versions/{published['test_version'].xid}",
                              headers=admin)
        assert response.headers.get("ETag")

    def test_config_and_the_band_map_can_be_set(self, client, author, db, seed):
        body = _ok(client.patch(f"/api/v1/test-versions/{seed['test_version'].xid}",
                                json={"title": "v1 renamed",
                                      "config": {"shuffle": False},
                                      "band_map_version_xid":
                                          str(seed["band_map_version"].xid)},
                                headers=author))
        assert body["title"] == "v1 renamed"
        # `config` is write-only: the spec's TestVersion schema does not carry it.
        # The band map xid IS in the schema, and used to be hardcoded null.
        assert body["band_map_version_xid"] == str(seed["band_map_version"].xid)
        db.expire_all()
        row = db.execute(text("""
            SELECT config, band_map_version_id FROM test_versions WHERE id = :v
        """).bindparams(v=seed["test_version"].id)).mappings().one()
        assert row["config"] == {"shuffle": False}
        assert row["band_map_version_id"] == seed["band_map_version"].id

    def test_an_unknown_band_map_is_a_404(self, client, author, seed):
        assert client.patch(f"/api/v1/test-versions/{seed['test_version'].xid}",
                            json={"band_map_version_xid": str(uuid.uuid4())},
                            headers=author).status_code == 404

    def test_archiving_clears_the_tests_current_published_pointer(
            self, client, admin, db, published):
        """Otherwise the test still advertises a version nobody may sit."""
        tv = published["test_version"]
        # The `published` fixture calls `content_repo.publish` directly; setting
        # the test's pointer is the publish ENDPOINT's job, so do it here.
        published["test"].current_published_version_id = tv.id
        db.flush()
        body = _ok(client.post(f"/api/v1/test-versions/{tv.xid}/archive",
                               headers=admin))
        assert body["status"] == "archived"
        db.expire_all()
        assert db.scalar(text("SELECT current_published_version_id FROM tests "
                              "WHERE id = :t").bindparams(t=published["test"].id)) \
            is None


class TestAttachingABandMap:
    """The round trip a centre must walk before it can publish anything, and every
    step of it was broken.

    `band_map_versions` had no `xid` column — the one table in the system that
    never got one. `GET /band-maps` papered over that by emitting
    `{"xid": str(current.id)}`, so a client was handed `"7"` and told it was an
    xid: the internal primary key on the public surface. Feeding that back to
    `PATCH /test-versions/{xid}` failed request validation (not a uuid), and a real
    uuid hit `BandMapVersion.xid`, an attribute that did not exist, for a 500.

    `publish_gate` refuses a version with no band map, so with the only attach
    path shut in both directions **no test could be published through the API at
    all.** The suite missed it because its fixtures set `band_map_version_id` in
    Python. `/band-maps` was reached only by the authz leak sweep, which collects
    any key ending in `xid` and compares it against a rival's — and `"7"` never
    matches a uuid, so it passed.
    """

    def test_a_created_band_maps_version_xid_is_a_uuid(self, client, centre_admin):
        body = _ok(client.post("/api/v1/band-maps",
                               json={"name": "House curve", "skill": "reading",
                                     "max_raw": 40,
                                     "mapping": [{"raw_min": 0, "raw_max": 40,
                                                  "band": 6.0}]},
                               headers=centre_admin), 201)
        version_xid = body["current_version"]["xid"]
        assert uuid.UUID(version_xid)                    # not str(id)

    def test_the_listing_agrees_with_what_creation_returned(self, client,
                                                            centre_admin):
        created = _ok(client.post("/api/v1/band-maps",
                                  json={"name": "House curve", "skill": "reading",
                                        "max_raw": 40,
                                        "mapping": [{"raw_min": 0, "raw_max": 40,
                                                     "band": 6.0}]},
                                  headers=centre_admin), 201)
        listed = _ok(client.get("/api/v1/band-maps", headers=centre_admin))
        mine = [b for b in listed if b["name"] == "House curve"]
        assert len(mine) == 1
        assert mine[0]["current_version"]["xid"] == created["current_version"]["xid"]

    def test_the_version_xid_the_api_hands_out_can_be_attached(self, client, db,
                                                               centre_admin, seed):
        """The whole round trip in one test. This is the sequence a centre has to
        complete before its first test can go live."""
        created = _ok(client.post("/api/v1/band-maps",
                                  json={"name": "House curve", "skill": "reading",
                                        "max_raw": 40,
                                        "mapping": [{"raw_min": 0, "raw_max": 40,
                                                     "band": 6.0}]},
                                  headers=centre_admin), 201)
        version_xid = created["current_version"]["xid"]
        attached = _ok(client.patch(f"/api/v1/test-versions/{seed['test_version'].xid}",
                                    json={"band_map_version_xid": version_xid},
                                    headers=centre_admin))
        assert attached["band_map_version_xid"] == version_xid
        db.expire_all()
        assert db.scalar(text("""
            SELECT bmv.max_raw FROM test_versions tv
            JOIN band_map_versions bmv ON bmv.id = tv.band_map_version_id
            WHERE tv.id = :v
        """).bindparams(v=seed["test_version"].id)) == 40

    def test_a_version_with_no_band_map_reports_null(self, client, author, db, seed):
        db.execute(text("UPDATE test_versions SET band_map_version_id = NULL "
                        "WHERE id = :v").bindparams(v=seed["test_version"].id))
        db.flush()
        db.expire_all()
        body = _ok(client.get(f"/api/v1/test-versions/{seed['test_version'].xid}",
                              headers=author))
        assert body["band_map_version_xid"] is None


class TestReview:
    def test_deciding_with_no_open_request_is_a_404(self, client, admin, db, seed):
        """The version can be `in_review` with its request already decided — a
        second reviewer clicking approve must not silently succeed."""
        # Set through the ORM instance, not raw SQL: the router resolves the same
        # object from the identity map and would still see 'draft'.
        seed["test_version"].status = "in_review"
        db.flush()
        refused = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/review",
                              json={"decision": "approved"}, headers=admin)
        assert refused.status_code == 404

    def test_requesting_changes_returns_it_to_draft(self, client, admin, db, seed):
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/submit-review",
                        json={"notes": "please look"}, headers=admin))
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/review",
                        json={"decision": "changes_requested",
                              "notes": "section 2 is unanswerable"}, headers=admin))
        db.expire_all()
        assert db.scalar(text("SELECT status FROM test_versions WHERE id = :v")
                         .bindparams(v=seed["test_version"].id)) == "draft"

    def test_a_version_not_in_review_cannot_be_decided(self, client, admin, seed):
        refused = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/review",
                              json={"decision": "approved"}, headers=admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "not_in_review"


# ── clone ────────────────────────────────────────────────────────────

class TestCrossOrgClone:
    """"Cross-org clone is a copy of someone else's material: it needs an explicit
    `copy` grant, not merely read access." This is the contractual promise in the
    one place where read access could become possession."""

    @pytest.fixture
    def target_org(self, db, seed):
        from app.modules.identity.models import Organization, OrgMembership

        org = Organization(name="Second branch", slug=f"br-{uuid.uuid4().hex[:6]}",
                           status="active")
        db.add(org)
        db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=seed["author"].id,
                             role="centre_admin", status="active"))
        db.flush()
        return org

    def test_without_a_grant_it_is_refused(self, client, author, db, seed,
                                           target_org):
        refused = client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                              json={"target_org_xid": str(target_org.xid)},
                              headers=author)
        assert refused.status_code == 403
        assert refused.json()["code"] == "copy_grant_required"

    def test_a_copy_grant_to_the_target_org_allows_it(self, client, author, db,
                                                      seed, target_org):
        db.execute(text("""
            INSERT INTO content_grants (subject_type, subject_id, grantee_kind,
                                        grantee_id, permission, granted_by)
            VALUES ('test', :sid, 'org', :org, 'copy', :by)
        """).bindparams(sid=seed["test"].id, org=target_org.id,
                        by=seed["author"].id))
        db.flush()
        body = _ok(client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                               json={"target_org_xid": str(target_org.xid)},
                               headers=author), 201)
        assert body["title"] == "Mock 1 (copy)"

    def test_a_revoked_grant_does_not(self, client, author, db, seed, target_org):
        db.execute(text("""
            INSERT INTO content_grants (subject_type, subject_id, grantee_kind,
                                        grantee_id, permission, granted_by,
                                        revoked_at)
            VALUES ('test', :sid, 'org', :org, 'copy', :by, now())
        """).bindparams(sid=seed["test"].id, org=target_org.id,
                        by=seed["author"].id))
        db.flush()
        assert client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                           json={"target_org_xid": str(target_org.xid)},
                           headers=author).status_code == 403

    def test_an_expired_grant_does_not(self, client, author, db, seed, target_org):
        db.execute(text("""
            INSERT INTO content_grants (subject_type, subject_id, grantee_kind,
                                        grantee_id, permission, granted_by,
                                        expires_at)
            VALUES ('test', :sid, 'org', :org, 'copy', :by,
                    now() - interval '1 day')
        """).bindparams(sid=seed["test"].id, org=target_org.id,
                        by=seed["author"].id))
        db.flush()
        assert client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                           json={"target_org_xid": str(target_org.xid)},
                           headers=author).status_code == 403

    def test_a_view_grant_is_not_a_copy_grant(self, client, author, db, seed,
                                              target_org):
        """Reading a rival's paper in the app and walking away with a copy of it
        are different permissions. This is the distinction the whole grant model
        exists for."""
        db.execute(text("""
            INSERT INTO content_grants (subject_type, subject_id, grantee_kind,
                                        grantee_id, permission, granted_by)
            VALUES ('test', :sid, 'org', :org, 'view', :by)
        """).bindparams(sid=seed["test"].id, org=target_org.id,
                        by=seed["author"].id))
        db.flush()
        assert client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                           json={"target_org_xid": str(target_org.xid)},
                           headers=author).status_code == 403

    def test_platform_global_material_is_freely_copyable(self, client, author, db,
                                                         seed, target_org):
        seed["test"].visibility = "platform_global"       # via the ORM instance
        db.flush()
        assert client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                           json={"target_org_xid": str(target_org.xid)},
                           headers=author).status_code == 201

    def test_a_platform_admin_needs_no_grant(self, client, admin, db, seed,
                                            target_org):
        assert client.post(f"/api/v1/tests/{seed['test'].xid}/clone",
                           json={"target_org_xid": str(target_org.xid)},
                           headers=admin).status_code == 201


# ── placement ────────────────────────────────────────────────────────

class TestPlacement:
    def test_the_same_group_cannot_be_placed_twice_in_one_version(
            self, client, author, db, seed):
        """Placing it twice would number the same questions twice, which is the
        numbering bug the whole `_renumber` pass exists to prevent."""
        second = _section(client, author, seed["test_version"], 2, "Second")
        refused = client.post(f"/api/v1/sections/{second['xid']}/groups",
                              json={"group_version_xid":
                                    str(seed["group_version"].xid), "position": 1},
                              headers=author)
        assert refused.status_code == 409
        assert refused.json()["code"] == "group_already_placed"

    def test_placing_at_an_occupied_position_shifts_the_rest(self, client, author,
                                                             db, seed):
        """`_make_room_for_group`'s second statement had never run: a section with
        one group never needed the shift."""
        from app.modules.content.models import QuestionGroup, QuestionGroupVersion

        group = QuestionGroup(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                              title="Second group", visibility="org_private")
        db.add(group)
        db.flush()
        gv = QuestionGroupVersion(group_id=group.id, version_no=1, status="draft",
                                  created_by=seed["author"].id)
        db.add(gv)
        db.flush()
        placed = _ok(client.post(f"/api/v1/sections/{seed['section'].xid}/groups",
                                 json={"group_version_xid": str(gv.xid),
                                       "position": 1}, headers=author), 201)
        assert placed["position"] == 1
        assert db.execute(text("""
            SELECT position FROM test_version_groups WHERE section_id = :s
            ORDER BY position
        """).bindparams(s=seed["section"].id)).scalars().all() == [1, 2]

    def test_an_unknown_group_version_is_a_404(self, client, author, seed):
        assert client.post(f"/api/v1/sections/{seed['section'].xid}/groups",
                           json={"group_version_xid": str(uuid.uuid4()),
                                 "position": 1},
                           headers=author).status_code == 404


# ── export ───────────────────────────────────────────────────────────

class TestCsvExport:
    def test_the_export_round_trips_the_columns_the_importer_reads(
            self, client, admin, published):
        """"The round-trip claim in the contract is only true if export and import
        speak one format." """
        response = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/export"
            "?format=csv&include_keys=true", headers=admin)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        rows = _csv_rows(response)
        assert rows[0] == ["test_title", "section", "skill", "group", "instructions",
                           "word_limit", "type_key", "text", "answer"]
        assert len(rows) == 4                      # header + three questions

    def test_a_question_with_no_recognised_text_field_exports_blank(
            self, client, admin, db, published):
        """`_question_text` walks four candidate payload fields. A type that uses
        none of them must export an empty cell rather than raise — a broken export
        is how a centre loses an afternoon's work.

        `expire_all()` after the raw UPDATE, because `load_composition` resolves
        `QuestionVersion` through the ORM: without it the identity map hands back
        the pre-update payload and this test passes without testing anything.
        """
        db.execute(text("""
            UPDATE question_versions SET payload = '{"diagram": "x"}'::jsonb
            WHERE id = (SELECT min(id) FROM question_versions)
        """))
        db.execute(text("UPDATE question_group_versions SET word_limit = NULL"))
        db.flush()
        db.expire_all()
        response = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/export"
            "?format=csv", headers=admin)
        assert response.status_code == 200, response.text
        rows = _csv_rows(response)[1:]
        blank = [r for r in rows if r[7] == ""]
        assert len(blank) == 1, [r[7] for r in rows]
        # word_limit must be blank, not the string "None WORDS".
        assert {r[5] for r in rows} == {""}

    def test_a_word_limit_is_rendered_as_ielts_wording(self, client, admin, db,
                                                       published):
        db.execute(text("""
            UPDATE question_group_versions
            SET word_limit = '{"max_words": 2, "allow_number": true}'::jsonb
        """))
        db.flush()
        db.expire_all()
        response = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}/export"
            "?format=csv", headers=admin)
        assert "TWO WORDS AND/OR A NUMBER" in response.text
