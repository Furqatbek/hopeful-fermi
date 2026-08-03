"""The whole authoring chain the admin console performs, end to end.

Every request below is one a screen makes, with the body that screen sends. It
exists because each link was individually covered and the CHAIN was not, and two
defects lived exactly in the joins:

* `GET /question-groups` returned `current_version: null` on every row, so the
  picker that fills a section with a group had nothing to offer, whatever the
  centre had authored. The console's "no question groups have a version yet" was
  not a rare fallback, it was the only branch that ever ran.
* `POST /questions` stored the whole `AnswerKeyCreate` wrapper as the answer key,
  so every question authored with its key through the contract's own shape failed
  the publish gate with `'slots' is a required property`.

Neither is visible from either end. You only see them by walking the whole way.
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
    """A centre admin. Publishing is a centre-admin act unless the org turns on
    `teacher_can_publish`, which defaults off."""
    import datetime as _dt
    import uuid as _uuid

    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=_dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


class TestTheConsoleCanAuthorAPublishableTest:
    def test_from_nothing_to_published(self, client, db, seed, admin):
        h = auth(seed["author"].xid)

        # ── Questions (QuestionLibrary) ──────────────────────────────
        question_versions = []
        for n in (1, 2, 3):
            body = _ok(client.post("/api/v1/questions", headers=h, json={
                "type_key": "sentence_completion", "type_version": 1,
                "skill": "reading", "points": 1,
                "payload": {"text": f"Bridge {n} opened in {{{{s1}}}}.",
                            "slots": ["s1"]},
                # The wrapper the contract declares — `AnswerKeyCreate`, not a
                # bare key. `reason` is the half that used to be dropped, and
                # `initial` versus `key_fix` is what the regrade flow keys off.
                "key": {"key": {"slots": {"s1": {"accept": [f"19{n}0"]}}},
                        "reason": "initial"}}), 201)
            question_versions.append(body["current_version"]["xid"])

        # The key is stored UNWRAPPED, which is what the publish gate validates.
        keys = _ok(client.get(
            f"/api/v1/question-versions/{question_versions[0]}/keys", headers=h))
        assert keys[0]["key"] == {"slots": {"s1": {"accept": ["1910"]}}}
        assert keys[0]["is_current"] is True

        # ── Passage (PassageLibrary) ─────────────────────────────────
        passage = _ok(client.post("/api/v1/passages", headers=h, json={
            "title": "The first bridge", "genre": "article",
            "body": [{"text": "A paragraph about bridges."}],
            "attestation": {"claim": "original", "statement_version": "1"}}), 201)

        # ── Group (GroupLibrary) ─────────────────────────────────────
        group = _ok(client.post("/api/v1/question-groups", headers=h, json={
            "title": "Questions 1-3", "skill": "reading",
            "instructions": {"en": "Complete the sentences. ONE WORD ONLY."},
            "word_limit": {"max_words": 1, "allow_number": True,
                           "hyphen_counts_as_one": True,
                           "on_violation": "mark_incorrect"}}), 201)
        group_version = group["current_version"]["xid"]

        for qv in question_versions:
            _ok(client.post(f"/api/v1/question-group-versions/{group_version}/items",
                            headers=h, json={"question_version_xid": qv}), 201)
        detail = _ok(client.get(
            f"/api/v1/question-group-versions/{group_version}", headers=h))
        # The server appends; the screen sends no position because an order the
        # author never chose is not one to assert.
        assert [i["position"] for i in detail["items"]] == [1, 2, 3]

        # ── The picker (AttachGroup) ─────────────────────────────────
        listing = _ok(client.get("/api/v1/question-groups?limit=100", headers=h))
        # Exactly the filter the picker applies. It matched nothing, always.
        placeable = [g for g in listing["items"]
                     if (g.get("current_version") or {}).get("xid")]
        assert any(g["current_version"]["xid"] == group_version for g in placeable)

        # ── Test, version, section (TestLibrary / AddSection) ────────
        test = _ok(client.post("/api/v1/tests", headers=h, json={
            "title": "Console Mock 1", "kind": "mock", "variant": "academic",
            "skills": ["reading"]}), 201)
        version = _ok(client.post(f"/api/v1/tests/{test['xid']}/versions",
                                  headers=h, json={}), 201)
        section = _ok(client.post(
            f"/api/v1/test-versions/{version['xid']}/sections", headers=h, json={
                "position": 1, "skill": "reading", "title": "Reading 1",
                "time_limit_seconds": 3600,
                "passage_version_xid": passage["current_version"]["xid"]}), 201)

        placed = _ok(client.post(f"/api/v1/sections/{section['xid']}/groups",
                                 headers=h,
                                 json={"group_version_xid": group_version,
                                       "position": 1}), 201)
        assert placed["number_start"] == 1

        # ── Band map (Composition) ───────────────────────────────────
        # The raw→band curve. Without it the gate refuses with BAND_MAP_MISSING,
        # and nothing in the console set one — so a test authored here stopped
        # one step short of publishable, naming a thing the author could not
        # reach. `If-Match` carries the ETag from the read, so a concurrent edit
        # is refused rather than silently overwritten.
        maps = _ok(client.get("/api/v1/band-maps", headers=h))
        assert any(m["is_platform_default"] for m in maps), \
            "the console offers platform defaults; a centre should not have to author one"
        curve = next(m for m in maps if m["current_version"])
        read = client.get(f"/api/v1/test-versions/{version['xid']}", headers=h)
        tag = read.headers["ETag"]
        _ok(client.patch(
            f"/api/v1/test-versions/{version['xid']}",
            headers={**h, "If-Match": tag},
            json={"band_map_version_xid": curve["current_version"]["xid"]}))

        # A stale tag is refused rather than silently winning — the whole point
        # of the lock the contract declares.
        stale = client.patch(
            f"/api/v1/test-versions/{version['xid']}",
            headers={**h, "If-Match": '"0-deadbeef"'},
            json={"title": "Sneaked in"})
        assert stale.status_code == 412
        assert stale.json()["code"] == "stale_version"

        # ── Validate, then publish (Composition) ─────────────────────
        report = _ok(client.post(
            f"/api/v1/test-versions/{version['xid']}/validate", headers=h))
        errors = [f["code"] for f in report["findings"]
                  if f["severity"] == "error"]
        assert errors == [], errors
        assert report["passed"] is True

        # A teacher may not publish unless the centre says so — the Roster
        # screen's `teacher_can_publish`, off by default.
        refused = client.post(f"/api/v1/test-versions/{version['xid']}/publish",
                              headers=h)
        assert refused.status_code == 403
        assert refused.json()["reason"] == "role_teacher_cannot_publish"

        published = _ok(client.post(
            f"/api/v1/test-versions/{version['xid']}/publish", headers=admin))
        assert published["status"] == "published"
        assert published["total_questions"] == 3

        # And it is assignable, which is what publishing is FOR: the Assignments
        # screen offers only tests carrying a published version.
        listed = _ok(client.get("/api/v1/tests?limit=50", headers=h))
        mine = next(t for t in listed["items"] if t["xid"] == test["xid"])
        assert mine["current_published_version_xid"] == version["xid"]
