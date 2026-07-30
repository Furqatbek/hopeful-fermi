"""Fields the contract declares, now carrying values.

`scripts/check_schema_conformance.py` found these mechanically: a response field
pinned to a literal `None`, or a request field nothing reads. The script proves
the *shape* is right — that a value is computed at all. These prove the value is
the RIGHT one, which no amount of AST walking can.

Every one of them is the same defect in a different room: the client is invited to
send something the server drops, or told about a thing the server never fills in.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token

ATTEST = json.dumps({"claim": "original", "statement_version": "1"})


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


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201, 202)), response.text
    return response.json()


class TestCueCardSets:
    """`current_version_xid` was null on both the create and the list, so a
    teacher could make a cue-card set and never address the version they needed
    to attach to a speaking slot."""

    def test_creating_one_returns_its_version(self, client, author):
        body = _ok(client.post("/api/v1/cue-card-sets",
                               json={"title": "Hometown", "tags": ["part1"],
                                     "body": {"cards": []}}, headers=author), 201)
        assert uuid.UUID(body["current_version_xid"])

    def test_the_listing_agrees(self, client, author):
        created = _ok(client.post("/api/v1/cue-card-sets",
                                  json={"title": "Hometown", "body": {}},
                                  headers=author), 201)
        listed = _ok(client.get("/api/v1/cue-card-sets", headers=author))
        mine = next(s for s in listed if s["title"] == "Hometown")
        assert mine["current_version_xid"] == created["current_version_xid"]


class TestSpeakingSlotCueCards:
    """Accepted on the request, never stored, echoed back as null — so a teacher
    chose a cue-card set and got a room with no prompts in it."""

    @pytest.fixture
    def cue_cards(self, client, author):
        return _ok(client.post("/api/v1/cue-card-sets",
                               json={"title": "Hometown", "body": {}},
                               headers=author), 201)

    def test_a_slot_keeps_the_set_it_was_given(self, client, db, seed, author,
                                               cue_cards):
        body = _ok(client.post("/api/v1/speaking/slots", headers=author,
                               json={"starts_at": (dt.datetime.now(dt.UTC)
                                                   + dt.timedelta(hours=1)).isoformat(),
                                     "age_band": "adult",
                                     "cue_card_set_version_xid":
                                         cue_cards["current_version_xid"]}), 201)
        assert body["cue_card_set_version_xid"] == cue_cards["current_version_xid"]
        assert db.scalar(text(
            "SELECT cue_card_set_version_id IS NOT NULL FROM speaking_slots"))

    def test_a_slot_without_one_reports_null(self, client, author):
        body = _ok(client.post("/api/v1/speaking/slots", headers=author,
                               json={"starts_at": (dt.datetime.now(dt.UTC)
                                                   + dt.timedelta(hours=1)).isoformat(),
                                     "age_band": "adult"}), 201)
        assert body["cue_card_set_version_xid"] is None

    def test_another_centres_set_cannot_be_attached(self, client, db, seed, author):
        """A reference by xid is how a competitor's material would be pulled into
        your session, so it is authorized on the way in."""
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        version_xid = db.scalar(text("""
            WITH s AS (
                INSERT INTO cue_card_sets (org_id, owner_user_id, title, visibility)
                VALUES (:o, :u, 'Theirs', 'org_private') RETURNING id
            )
            INSERT INTO cue_card_set_versions (set_id, version_no, body, created_by)
            SELECT id, 1, '{}'::jsonb, :u FROM s RETURNING xid
        """).bindparams(o=rival.id, u=seed["author"].id))
        db.flush()
        refused = client.post("/api/v1/speaking/slots", headers=author,
                              json={"starts_at": (dt.datetime.now(dt.UTC)
                                                  + dt.timedelta(hours=1)).isoformat(),
                                    "age_band": "adult",
                                    "cue_card_set_version_xid": str(version_xid)})
        assert refused.status_code == 404, refused.text


class TestGroupDiagram:
    """`diagram_media_xid` was popped off the update body and discarded, and
    reported back as null — so a labelling or map question could be authored and
    never rendered."""

    @pytest.fixture
    def group_version(self, db, seed):
        from app.modules.content.models import QuestionGroup, QuestionGroupVersion

        group = QuestionGroup(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                              title="Map", skill="listening", visibility="org_private")
        db.add(group)
        db.flush()
        gv = QuestionGroupVersion(group_id=group.id, version_no=1, status="draft",
                                  created_by=seed["author"].id)
        db.add(gv)
        db.flush()
        return gv

    @pytest.fixture
    def media_xid(self, db, seed):
        return db.scalar(text("""
            INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                      content_type, bytes, checksum_sha256, status)
            VALUES (:u, 'image', 'test-media', 'diagrams/map.png', 'image/png',
                    2048, 'abc', 'ready')
            RETURNING xid
        """).bindparams(u=seed["author"].id))

    def test_a_diagram_can_be_attached_and_read_back(self, client, db, author,
                                                     group_version, media_xid):
        body = _ok(client.patch(
            f"/api/v1/question-group-versions/{group_version.xid}",
            json={"diagram_media_xid": str(media_xid)}, headers=author))
        assert body["diagram_media_xid"] == str(media_xid)
        db.expire_all()
        assert db.scalar(text("""
            SELECT diagram_media_id IS NOT NULL FROM question_group_versions
            WHERE id = :g
        """).bindparams(g=group_version.id))

    def test_a_group_without_one_reports_null(self, client, author, group_version):
        body = _ok(client.get(
            f"/api/v1/question-group-versions/{group_version.xid}", headers=author))
        assert body["diagram_media_xid"] is None

    def test_someone_elses_upload_cannot_be_attached(self, client, db, seed, author,
                                                     group_version):
        other = db.scalar(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998915558001', 'Sardor', CAST('1999-01-01' AS date), 'active')
            RETURNING id
        """))
        theirs = db.scalar(text("""
            INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                      content_type, bytes, checksum_sha256, status)
            VALUES (:u, 'image', 'test-media', 'diagrams/theirs.png', 'image/png',
                    2048, 'def', 'ready')
            RETURNING xid
        """).bindparams(u=other))
        db.flush()
        assert client.patch(
            f"/api/v1/question-group-versions/{group_version.xid}",
            json={"diagram_media_xid": str(theirs)},
            headers=author).status_code == 404


class TestPassageOrg:
    """`org_xid` was accepted and ignored — `create_passage` always took
    `actor.org_ids[0]`, so a teacher at two centres could not say which one a
    passage was for."""

    def test_the_named_centre_is_used(self, client, db, seed, author):
        from app.modules.identity.models import Organization, OrgMembership

        second = Organization(name="Branch two", slug=f"b-{uuid.uuid4().hex[:6]}",
                              status="active")
        db.add(second)
        db.flush()
        db.add(OrgMembership(org_id=second.id, user_id=seed["author"].id,
                             role="teacher", status="active"))
        db.flush()
        _ok(client.post("/api/v1/passages",
                        json={"title": "Branch two passage", "blocks": [],
                              "org_xid": str(second.xid)}, headers=author), 201)
        assert db.scalar(text("SELECT org_id FROM passages WHERE title = "
                              "'Branch two passage'")) == second.id

    def test_a_centre_the_actor_is_not_in_is_a_404(self, client, db, author):
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        assert client.post("/api/v1/passages",
                           json={"title": "Smuggled", "blocks": [],
                                 "org_xid": str(rival.xid)},
                           headers=author).status_code == 404

    def test_omitting_it_still_defaults_to_the_actors_centre(self, client, db, seed,
                                                             author):
        _ok(client.post("/api/v1/passages",
                        json={"title": "Default", "blocks": []}, headers=author), 201)
        assert db.scalar(text("SELECT org_id FROM passages WHERE title = 'Default'")) \
            == seed["org"].id


class TestImportTargetsAnExistingTest:
    """`target_test_xid` is documented as the offline round trip — export, edit in
    Word, import back — and `import_jobs.target_test_id` exists for it. The form
    field was accepted and dropped, so every re-import landed as an unrelated new
    test."""

    def _upload(self, client, headers, **extra):
        doc = {"format": "ielts-hub-import/1", "title": "Re-imported",
               "variant": "academic",
               "sections": [{"title": "S1", "skill": "reading",
                             "passage": {"title": "P", "blocks": [
                                 {"type": "paragraph",
                                  "runs": [{"t": "text", "v": "Text."}]}]},
                             "groups": [{"title": "Q1-1", "instructions": {"en": "Do"},
                                         "questions": [{"type_key": "short_answer",
                                                        "type_version": 1,
                                                        "text": "What?",
                                                        "accept": ["bike"]}]}]}]}
        files = {"file": ("i.json", io.BytesIO(json.dumps(doc).encode()),
                          "application/json")}
        return client.post("/api/v1/imports", headers=headers, files=files,
                           data={"format": "json", "attestation": ATTEST, **extra})

    def test_it_lands_as_a_version_of_the_named_test(self, client, db, seed, author):
        job = _ok(self._upload(client, author,
                               target_test_xid=str(seed["test"].xid)), 202)
        assert db.scalar(text("SELECT target_test_id FROM import_jobs")) \
            == seed["test"].id
        _ok(client.post(f"/api/v1/imports/{job['xid']}/commit", headers=author))
        db.expire_all()
        assert db.scalar(text("""
            SELECT count(*) FROM test_versions WHERE test_id = :t
        """).bindparams(t=seed["test"].id)) == 2

    def test_without_it_a_new_test_is_created(self, client, db, seed, author):
        before = db.scalar(text("SELECT count(*) FROM tests"))
        job = _ok(self._upload(client, author), 202)
        _ok(client.post(f"/api/v1/imports/{job['xid']}/commit", headers=author))
        assert db.scalar(text("SELECT count(*) FROM tests")) == before + 1

    def test_another_centres_test_cannot_be_targeted(self, client, db, seed,
                                                     author):
        """Importing over someone else's test is an overwrite of their material."""
        from app.modules.content.models import Test
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival", slug=f"r-{uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        theirs = Test(org_id=rival.id, owner_user_id=seed["author"].id,
                      title="Theirs",
                      visibility="org_private")
        db.add(theirs)
        db.flush()
        assert self._upload(client, author,
                            target_test_xid=str(theirs.xid)).status_code == 404

    def test_the_job_reports_its_format(self, client, author):
        """`source_format` is `required` on the ImportJob schema and neither the
        create nor the read response carried it."""
        job = _ok(self._upload(client, author), 202)
        assert job["source_format"] == "json"
        assert _ok(client.get(f"/api/v1/imports/{job['xid']}",
                              headers=author))["source_format"] == "json"


class TestOrderReturnUrl:
    """Accepted and dropped, so a payment provider had nowhere to send the payer
    back to."""

    @pytest.fixture
    def price(self, db, seed):
        product = db.scalar(text("""
            INSERT INTO products (code, kind, name)
            VALUES ('mock-pack', 'one_off', 'Mock pack') RETURNING id
        """))
        return db.scalar(text("""
            INSERT INTO prices (product_id, currency, amount_minor, active)
            VALUES (:p, 'UZS', 5000000, true) RETURNING id
        """).bindparams(p=product))

    def test_it_is_stored_with_the_order(self, client, db, seed, author, price):
        _ok(client.post("/api/v1/orders",
                        json={"price_xid": price, "quantity": 1, "provider": "click",
                              "return_url": "https://centre.uz/paid"},
                        headers=author), 201)
        assert db.scalar(text("SELECT metadata->>'return_url' FROM orders")) \
            == "https://centre.uz/paid"

    def test_an_order_without_one_stores_nothing(self, client, db, author, price):
        _ok(client.post("/api/v1/orders",
                        json={"price_xid": price, "quantity": 1, "provider": "click"},
                        headers=author), 201)
        assert db.scalar(text("SELECT metadata FROM orders")) == {}
