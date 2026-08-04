"""A band map that cannot score, and content that could not be retired.

**`POST /band-maps` validated nothing.** A band map turns a raw mark into the
band a student is told they got, and `scoring.BandMap.band_for` answers `None`
for a mark the table does not cover — so a gap is a whole cohort given no band,
in silence. `publish_gate` checks coverage, but only at publish time and only
for the map attached to the test being published, so a broken curve could be
created, listed and selected first. A map with no organization is the PLATFORM
DEFAULT every centre inherits, and any account belonging to no org could make
one — while the response said `is_platform_default: false` about it.

**`archived_at` was read by four listings and written by nothing.** "Retire this
item" had no action behind it, which got sharper once content grants started
working: revoking a grant removes one partner's access, and retiring the item
removes it from everybody's. Only the first was possible.
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
    """MANAGE_BAND_MAP and ARCHIVE are both centre-admin and above."""
    from app.modules.identity.models import OrgMembership, User

    boss = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Gulnora",
                date_of_birth=dt.date(1980, 1, 1), status="active")
    db.add(boss)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=boss.id,
                         role="centre_admin", status="active"))
    db.flush()
    return boss


def _map(client, who, mapping, max_raw=4, name="Centre curve"):
    return client.post("/api/v1/band-maps", headers=auth(who.xid), json={
        "name": name, "skill": "reading", "variant": "academic",
        "max_raw": max_raw, "mapping": mapping})


GOOD = [{"raw_min": 0, "raw_max": 1, "band": 4.0},
        {"raw_min": 2, "raw_max": 3, "band": 6.0},
        {"raw_min": 4, "raw_max": 4, "band": 7.0}]


class TestABandMapMustBeAbleToScore:
    def test_a_complete_curve_is_accepted(self, client, centre_admin):
        response = _map(client, centre_admin, GOOD)
        assert response.status_code == 201, response.text
        assert response.json()["is_platform_default"] is False

    def test_a_gap_is_refused(self, client, centre_admin):
        """The failure this exists for: mark 2 scores no band at all, and the
        student is simply told nothing."""
        response = _map(client, centre_admin, [
            {"raw_min": 0, "raw_max": 1, "band": 4.0},
            {"raw_min": 3, "raw_max": 4, "band": 7.0}])
        assert response.status_code == 422, response.text
        assert "BAND_MAP_GAP" in response.text

    def test_an_overlap_is_refused(self, client, centre_admin):
        """`band_for` returns the FIRST matching row, so an overlap makes the
        band depend on row order — and a map that scores differently after an
        edit that "only reordered things" is the worst kind to be asked about."""
        response = _map(client, centre_admin, [
            {"raw_min": 0, "raw_max": 2, "band": 4.0},
            {"raw_min": 2, "raw_max": 4, "band": 7.0}])
        assert response.status_code == 422, response.text
        assert "BAND_MAP_OVERLAP" in response.text

    def test_a_curve_that_goes_down_is_refused(self, client, centre_admin):
        """Not a formatting rule. A transposed pair means 4 marks scores lower
        than 3, which no amount of eyeballing a JSON blob catches."""
        response = _map(client, centre_admin, [
            {"raw_min": 0, "raw_max": 1, "band": 4.0},
            {"raw_min": 2, "raw_max": 3, "band": 7.0},
            {"raw_min": 4, "raw_max": 4, "band": 6.0}])
        assert response.status_code == 422, response.text
        assert "BAND_MAP_NOT_MONOTONIC" in response.text

    def test_a_band_that_is_not_a_band_is_refused(self, client, centre_admin):
        response = _map(client, centre_admin, [
            {"raw_min": 0, "raw_max": 4, "band": 7.3}])
        assert response.status_code == 422
        assert "BAND_MAP_BAND_INVALID" in response.text

    def test_every_problem_comes_back_at_once(self, client, centre_admin):
        """A centre retuning a curve wants the list, not a fix-and-resubmit loop
        nine times over."""
        body = _map(client, centre_admin, [
            {"raw_min": 0, "raw_max": 1, "band": 4.0},
            {"raw_min": 3, "raw_max": 4, "band": 7.7}]).json()
        codes = {f["code"] for f in body["findings"]}
        assert {"BAND_MAP_BAND_INVALID", "BAND_MAP_GAP"} <= codes

    def test_an_empty_mapping_is_refused(self, client, centre_admin):
        assert _map(client, centre_admin, []).status_code == 422

    def test_a_row_outside_the_range_is_refused(self, client, centre_admin):
        response = _map(client, centre_admin, [
            {"raw_min": 0, "raw_max": 9, "band": 6.0}], max_raw=4)
        assert response.status_code == 422
        assert "BAND_MAP_RANGE_OUTSIDE" in response.text


class TestOnlyAPlatformAdminMakesAPlatformDefault:
    def test_an_account_in_no_org_is_refused(self, client, db):
        """`org_ids[0] if actor.org_ids else None` let anyone with no
        organization create the curve every centre inherits."""
        from app.modules.identity.models import User

        drifter = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                       given_name="Nobody", date_of_birth=dt.date(1990, 1, 1),
                       status="active")
        db.add(drifter)
        db.flush()
        response = _map(client, drifter, GOOD)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "platform_band_map_not_permitted"

    def test_a_platform_admin_may_and_it_says_so(self, client, db, seed):
        from app.modules.identity.models import PlatformRoleGrant, User

        admin = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                     given_name="Platform", date_of_birth=dt.date(1985, 1, 1),
                     status="active")
        db.add(admin)
        db.flush()
        db.add(PlatformRoleGrant(user_id=admin.id, role="platform_admin",
                                 granted_by=admin.id))
        db.flush()
        response = _map(client, admin, GOOD)
        assert response.status_code == 201, response.text
        assert response.json()["is_platform_default"] is True


class TestRetiringAnAsset:
    def _passage(self, client, who):
        return client.post("/api/v1/passages", headers=auth(who.xid), json={
            "title": "Burned out", "blocks": [],
            "attestation": {"claim": "original", "statement_version": "1"}}).json()

    def test_a_retired_passage_leaves_the_listing(self, client, seed, centre_admin):
        created = self._passage(client, centre_admin)
        assert client.post(f"/api/v1/passages/{created['xid']}/archive",
                           headers=auth(centre_admin.xid)).status_code == 200

        listed = [p["xid"] for p in client.get(
            "/api/v1/passages", headers=auth(centre_admin.xid)).json()["items"]]
        assert created["xid"] not in listed

    def test_and_can_be_put_back(self, client, seed, centre_admin):
        created = self._passage(client, centre_admin)
        client.post(f"/api/v1/passages/{created['xid']}/archive",
                    headers=auth(centre_admin.xid))
        restored = client.delete(f"/api/v1/passages/{created['xid']}/archive",
                                 headers=auth(centre_admin.xid))
        assert restored.status_code == 200, restored.text
        assert restored.json()["archived_at"] is None
        listed = [p["xid"] for p in client.get(
            "/api/v1/passages", headers=auth(centre_admin.xid)).json()["items"]]
        assert created["xid"] in listed

    def test_a_teacher_may_not_retire(self, client, seed, centre_admin):
        """ARCHIVE is centre-admin and above: retiring a shared asset can break
        another author's draft, so it is not a teacher's call."""
        created = self._passage(client, centre_admin)
        response = client.post(f"/api/v1/passages/{created['xid']}/archive",
                               headers=auth(seed["author"].xid))
        assert response.status_code == 403, response.text

    def test_a_rival_centre_cannot_retire_someone_elses(self, client, db, seed,
                                                        centre_admin):
        from app.modules.identity.models import Organization, OrgMembership, User

        org = Organization(name="Rival", slug=f"rival-{uuid.uuid4().hex[:8]}")
        db.add(org)
        db.flush()
        rival = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                     given_name="Rival", date_of_birth=dt.date(1980, 1, 1),
                     status="active")
        db.add(rival)
        db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=rival.id,
                             role="centre_admin", status="active"))
        db.flush()

        created = self._passage(client, centre_admin)
        response = client.post(f"/api/v1/passages/{created['xid']}/archive",
                               headers=auth(rival.xid))
        assert response.status_code in (403, 404), response.text

    def test_retiring_does_not_delete_the_row(self, client, db, seed, centre_admin):
        """Archived, never deleted: attempts reference this material and a
        copyright investigation needs the evidence."""
        created = self._passage(client, centre_admin)
        client.post(f"/api/v1/passages/{created['xid']}/archive",
                    headers=auth(centre_admin.xid))
        row = db.execute(text("""
            SELECT archived_at FROM passages WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=created["xid"])).mappings().one()
        assert row["archived_at"] is not None

    @pytest.mark.parametrize("resource,table", [
        ("questions", "questions"),
        ("question-groups", "question_groups"),
        ("audio-tracks", "audio_tracks"),
    ])
    def test_the_other_three_retire_too(self, client, db, seed, with_audio,
                                        centre_admin, resource, table):
        """All four carry `archived_at` and all four listings filter on it, so
        all four need the endpoint — a subset would be the same gap in three
        fewer places."""
        xid = db.scalar(text(f"""
            SELECT xid::text FROM {table} WHERE org_id = :o
            ORDER BY id LIMIT 1
        """).bindparams(o=seed["org"].id))
        # `with_audio` is requested above so none of the three can skip. A
        # skipped case in a "does this work" set reads as covered and is not.
        assert xid is not None, f"no {table} row to retire"
        response = client.post(f"/api/v1/{resource}/{xid}/archive",
                               headers=auth(centre_admin.xid))
        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is not None
