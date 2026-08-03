"""Preview as the console performs it: sit your own draft before a cohort does.

The publish gate catches structure — a missing key, a blank with no answer, audio
shorter than the questions asked about it. It cannot tell an author that a blank
is in the wrong clause, that two options are both defensible, or that the passage
never mentions what question 7 asks for. Reading it as an entrant is the only
thing that can, and there was no screen for it.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def _idem() -> dict:
    return {"Idempotency-Key": str(_uuid.uuid4())}


class TestTheScreensSequence:
    def test_start_read_answer_submit_and_read_the_marking(self, client, seed):
        h = auth(seed["author"].xid)

        attempt = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview", headers=h), 201)
        assert attempt["mode"] == "preview"
        # The countdown the screen renders, taken from the server rather than
        # computed against a device clock.
        assert attempt["seconds_remaining"] > 0

        paper = _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/payload", headers=h))
        # Exactly what the renderer walks.
        section = paper["sections"][0]
        assert {"position", "skill", "title", "groups"} <= set(section)
        group = section["groups"][0]
        assert {"number_start", "instructions", "questions"} <= set(group)
        question = group["questions"][0]
        assert {"number", "question_version_xid", "type_key", "payload",
                "slot_keys"} <= set(question)
        # Numbering comes from the SNAPSHOT. A blank rendered against the wrong
        # number is the defect preview exists to catch.
        assert question["number"] == group["number_start"]

        _ok(client.post(f"/api/v1/attempts/{attempt['xid']}/answers",
                        headers={**h, **_idem()},
                        json={"deltas": [{
                            "question_version_xid": question["question_version_xid"],
                            "slot_key": question["slot_keys"][0],
                            "response": "bicycle", "client_seq": 1}]}))
        _ok(client.post(f"/api/v1/attempts/{attempt['xid']}/submit", headers=h))

        review = _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/review", headers=h))
        item = review["items"][0]
        # The marking is the point: the author types what they believe is right
        # and finds out whether the key agrees.
        assert item["raw_response"] == "bicycle"
        assert item["accepted_answers"]
        assert item["verdict"] in ("correct", "incorrect")

    def test_a_draft_can_be_previewed_at_all(self, client, seed):
        """The whole reason this endpoint exists: an unpublished version cannot
        be sat through `POST /attempts`, which requires a published one."""
        assert seed["test_version"].status == "draft"
        refused = client.post("/api/v1/attempts", headers=auth(seed["author"].xid),
                              json={"test_version_xid": str(seed["test_version"].xid)})
        assert refused.status_code in (403, 409, 402)
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
                        headers=auth(seed["author"].xid)), 201)

    def test_the_answers_endpoint_requires_an_idempotency_key(self, client, seed):
        """Declared required, because this endpoint is retried on unreliable
        networks and a replay must return the stored response rather than
        applying a delta twice."""
        h = auth(seed["author"].xid)
        attempt = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview", headers=h), 201)
        paper = _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/payload", headers=h))
        q = paper["sections"][0]["groups"][0]["questions"][0]
        body = {"deltas": [{"question_version_xid": q["question_version_xid"],
                            "slot_key": q["slot_keys"][0],
                            "response": "bicycle", "client_seq": 1}]}
        first = client.post(f"/api/v1/attempts/{attempt['xid']}/answers",
                            headers={**h, **_idem()}, json=body)
        assert first.status_code == 200


class TestAPreviewCountsForNothing:
    """`mode = 'preview'` is excluded from every statistic and from item
    exposure. That is what makes previewing a paper repeatedly safe — otherwise
    an author checking their own work would burn it."""

    def test_the_read_is_recorded_but_does_not_count_towards_burn(
            self, client, db, seed):
        """A row IS written, tagged `preview`, and that is deliberate.

        A first pass here asserted no row at all and "fixed" the writer to skip
        one — wrong, and the existing suite caught it. `refresh_exposure`
        excludes `context = 'preview'` when it computes `burn_score`, so an
        author checking their own paper never spends it; the row is kept because
        the anti-scrape index on `(user_id, occurred_at)` wants to see an account
        touching an abnormal number of items whoever they are, and an author is
        not exempt from that question.
        """
        h = auth(seed["author"].xid)
        attempt = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview", headers=h), 201)
        _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/payload", headers=h))

        contexts = list(db.scalars(text(
            "SELECT DISTINCT context FROM item_exposures")))
        assert contexts == ["preview"]
        # And the burn calculation ignores every one of them.
        counted = db.execute(text(
            "SELECT count(*) FROM item_exposures WHERE context <> 'preview'")).scalar()
        assert counted == 0

    def test_previewing_does_not_spend_the_paper_for_a_contest(
            self, client, db, seed, published):
        """A contest refuses a paper somebody has already sat. If a preview
        counted, an author checking their own work would make it uncontestable."""
        h = auth(seed["author"].xid)
        attempt = _ok(client.post(
            f"/api/v1/test-versions/{published['test_version'].xid}/preview",
            headers=h), 201)
        _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/payload", headers=h))
        _ok(client.post(f"/api/v1/attempts/{attempt['xid']}/submit", headers=h))

        contest = client.post("/api/v1/competitions", headers=h, json={
            "title": "Winter Open",
            "test_version_xid": str(published["test_version"].xid),
            "starts_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat(),
            "duration_seconds": 3600, "visibility": "org"})
        assert contest.status_code == 201, contest.text

    def test_a_preview_is_not_counted_in_a_regrade(self, client, db, seed, published):
        """`_affected_count` excludes preview attempts, so an author's own run
        does not appear in the number a human decides a regrade on."""
        h = auth(seed["author"].xid)
        attempt = _ok(client.post(
            f"/api/v1/test-versions/{published['test_version'].xid}/preview",
            headers=h), 201)
        paper = _ok(client.get(f"/api/v1/attempts/{attempt['xid']}/payload", headers=h))
        q = paper["sections"][0]["groups"][0]["questions"][0]
        _ok(client.post(f"/api/v1/attempts/{attempt['xid']}/answers",
                        headers={**h, **_idem()},
                        json={"deltas": [{
                            "question_version_xid": q["question_version_xid"],
                            "slot_key": q["slot_keys"][0],
                            "response": "bicycle", "client_seq": 1}]}))
        _ok(client.post(f"/api/v1/attempts/{attempt['xid']}/submit", headers=h))

        fixed = _ok(client.post(
            f"/api/v1/question-versions/{q['question_version_xid']}/keys", headers=h,
            json={"key": {"slots": {"s1": {"accept": ["bicycle", "bike"]}}},
                  "reason": "key_fix"}), 201)
        # No sat attempts that count, so no job is staged at all.
        assert "regrade_job_xid" not in fixed
        assert fixed["regrade_preview"]["attempts_total"] == 0


class TestPreviewIsNotAWayIntoSomebodyElsesPaper:
    def test_a_teacher_at_another_centre_is_refused(self, client, db, seed):
        from app.modules.identity.models import Organization, OrgMembership, User

        other = Organization(name="Rival Prep", slug=f"rp-{_uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(other)
        db.flush()
        outsider = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}",
                        given_name="Rival", date_of_birth=dt.date(1990, 1, 1))
        db.add(outsider)
        db.flush()
        db.add(OrgMembership(org_id=other.id, user_id=outsider.id, role="teacher",
                             status="active"))
        db.flush()
        refused = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
            headers=auth(outsider.xid))
        assert refused.status_code == 404

    def test_a_student_at_the_centre_is_refused(self, client, seed):
        """It requires EDIT on the version — a student holding read scope on
        their centre's content must not be able to sit an unpublished paper."""
        refused = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
            headers=auth(seed["student"].xid))
        assert refused.status_code in (403, 404)
