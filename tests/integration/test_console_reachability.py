"""Two endpoints the console could not reach, and now can.

Both existed. Neither could be exercised from a UI, for the same shape of
reason: the endpoint that *acts* was written and the endpoint that *finds the
thing to act on* was not.

  * `PATCH /admin/takedowns/{xid}` decides a takedown. Filing is unauthenticated
    and hands the xid to the claimant, so an admin had no way to learn one.
    `takedown_requests_queue_idx` — a partial index on `received_at` WHERE
    status IN ('received','reviewing') — is the listing this file now covers:
    designed, indexed, never written.

  * `GET /media/{xid}/content` streams against a grant, and the only issuer ran
    inside an exam attempt. An author could upload a listening file and never
    hear it.

The authorization tests matter more than the happy paths. A takedown queue is a
list of allegations against a centre's material, and an authoring grant is a
download link for a listening paper — both are exactly the wrong things to have
made one step easier to reach than intended.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token


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
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return seed["author"]


def _file(client, seed, *, subject_type="passage", subject_xid=None):
    """Filed the way a rights holder files: no bearer token."""
    response = client.post("/api/v1/takedowns", json={
        "claimant_name": "Cambridge University Press",
        "claimant_org": "CUP", "claimant_email": "rights@example.org",
        "rights_basis": "Copyright owner of Cambridge IELTS 17",
        "sworn_statement": True, "subject_type": subject_type,
        "subject_xid": str(subject_xid or seed["passage_version"].xid),
        "description": "This is Reading Passage 1 from Cambridge IELTS 17 Test 2.",
    })
    assert response.status_code == 201, response.text
    return response.json()


class TestTheTakedownQueue:
    def test_a_filed_request_appears_in_the_queue(self, client, seed, admin):
        _file(client, seed)
        body = client.get("/api/v1/admin/takedowns",
                          headers=auth(admin.xid)).json()
        assert [i["claimant_name"] for i in body["items"]] == \
            ["Cambridge University Press"]
        assert body["items"][0]["status"] == "received"
        assert body["items"][0]["hidden_at"] is not None, \
            "filing soft-hides the subject; the queue must show that it did"

    def test_the_queue_carries_the_xid_the_decision_endpoint_needs(
            self, client, seed, admin):
        """The whole point. Read a row, decide it, with nothing in between."""
        _file(client, seed)
        row = client.get("/api/v1/admin/takedowns",
                         headers=auth(admin.xid)).json()["items"][0]
        decided = client.patch(f"/api/v1/admin/takedowns/{row['xid']}",
                               json={"status": "reviewing"},
                               headers=auth(admin.xid))
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "reviewing"

    def test_no_internal_id_leaves_the_process(self, client, seed, admin):
        """`takedown_requests.subject_id` is a bigint. The queue resolves it to
        the subject's xid, because an internal id is not a public identifier and
        a UI that received one could not do anything with it anyway."""
        _file(client, seed)
        row = client.get("/api/v1/admin/takedowns",
                         headers=auth(admin.xid)).json()["items"][0]
        assert "subject_id" not in row
        assert row["subject_type"] == "passage"

    def test_a_subject_type_with_no_title_column_does_not_break_the_queue(
            self, client, seed, admin):
        """`questions` has no `title` and `band_maps` calls it `name`. One
        `SELECT ... title` for every type would 500 the entire queue over a
        single row — and an unopenable queue is the failure this endpoint
        exists to prevent."""
        _file(client, seed, subject_type="question",
              subject_xid=seed["question_versions"][0].xid)
        response = client.get("/api/v1/admin/takedowns", headers=auth(admin.xid))
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1

    def test_open_is_the_default_and_decided_requests_leave_it(
            self, client, seed, admin):
        filed = _file(client, seed)
        client.patch(f"/api/v1/admin/takedowns/{filed['xid']}",
                     json={"status": "rejected", "outcome_note": "Not their work."},
                     headers=auth(admin.xid))
        assert client.get("/api/v1/admin/takedowns",
                          headers=auth(admin.xid)).json()["items"] == []
        everything = client.get("/api/v1/admin/takedowns?status=all",
                                headers=auth(admin.xid)).json()["items"]
        assert [i["status"] for i in everything] == ["rejected"]
        assert everything[0]["outcome_note"] == "Not their work."

    def test_oldest_first(self, client, seed, admin):
        """A queue sorted newest-first buries the three-week-old complaint."""
        for _ in range(3):
            _file(client, seed)
        received = [i["received_at"] for i in
                    client.get("/api/v1/admin/takedowns",
                               headers=auth(admin.xid)).json()["items"]]
        assert received == sorted(received)

    def test_a_teacher_cannot_read_the_queue(self, client, seed):
        """Allegations against a centre's material, readable by the centre's own
        staff, would be a notification service for "we are about to be caught"."""
        _file(client, seed)
        response = client.get("/api/v1/admin/takedowns",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 403, response.text


class TestTheAuthoringGrant:
    def test_an_author_can_play_back_what_they_uploaded(
            self, client, seed, with_audio):
        issued = client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid))
        assert issued.status_code == 200, issued.text
        body = issued.json()

        streamed = client.get(
            f"/api/v1/media/{body['media_xid']}/content?grant={body['grant']}")
        assert streamed.status_code in (200, 206, 302), streamed.text

    def test_the_grant_names_the_delivery_object_not_the_track(
            self, client, db, seed, with_audio):
        """A track owns a 400 MB master and a transcoded delivery file, and the
        media endpoint serves the latter. A grant naming the track's own xid
        would verify fine and then 404 at play time, with nothing in the console
        able to say why."""
        from sqlalchemy import text

        body = client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)).json()
        expected = db.scalar(text(
            "SELECT xid::text FROM media_assets WHERE id = :m"
        ).bindparams(m=seed["media_id"]))
        assert body["media_xid"] == expected
        assert body["media_xid"] != str(seed["audio_track"].xid)

    def test_a_student_at_the_same_centre_is_refused(
            self, client, db, seed, with_audio):
        """The reason this requires EDIT and not read-scope.

        `scoped()` admits every member of the owning organization. A read-scoped
        grant would hand a student the audio of the listening paper they are
        about to sit — which is the same mistake `GET /audio-tracks/{xid}/
        transcript` already had to be fixed for.
        """
        response = client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["student"].xid))
        assert response.status_code in (403, 404), response.text

    def test_a_track_with_no_media_says_so(self, client, db, seed):
        from app.modules.content.models import AudioTrack

        empty = AudioTrack(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                           title="Nothing uploaded yet", status="draft")
        db.add(empty)
        db.flush()
        response = client.post(f"/api/v1/audio-tracks/{empty.xid}/grant",
                               headers=auth(seed["author"].xid))
        assert response.status_code == 404
        assert response.json()["code"] == "track_has_no_media"

    def test_the_grant_does_not_burn_a_play_once_section(
            self, client, db, seed, with_audio):
        """An author previewing their own track must not consume the single play
        a student gets. This issues nothing against an attempt at all — the
        assertion is that no `attempt_sections` row was touched."""
        from sqlalchemy import text

        seed["section"].play_once = True
        db.flush()
        before = db.scalar(text(
            "SELECT count(*) FROM attempt_sections WHERE audio_locked_at IS NOT NULL"))
        client.post(f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
                    headers=auth(seed["author"].xid))
        after = db.scalar(text(
            "SELECT count(*) FROM attempt_sections WHERE audio_locked_at IS NOT NULL"))
        assert after == before
