"""Three defects on the child-safety surface, each individually invisible.

  * The queue served its most urgent reports LAST. `priority` is text checked
    against ('normal','high','critical') and the query said `ORDER BY priority
    DESC`, which over those three strings is descending ALPHABETICAL order:
    normal, high, critical. `critical` is what a minor plus a grooming or
    sexual-content report is set to. The partial index was declared the same
    way, so storage and query agreed with each other and both disagreed with
    what the column means.

  * `safety_reports.status` was read by the minors filter and written by no code
    path at all, so the queue with its own response SLA could never be emptied,
    and nothing distinguished "not looked at" from "looked at, nothing in it".

  * `GET /content/flagged-items` had no authorization check — only
    `Depends(principal)` and an org-membership filter — so a student read
    `common_wrong`, which is what their classmates typed, for every flagged item
    in their school's bank, before sitting the paper.

The first two were found by an agent building the moderation screen; the third
by one building item analysis. None is subtle once you look at it, and none was
findable by reading a diff.
"""

from __future__ import annotations

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
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return seed["author"]


def _report(db, seed, *, priority: str, category: str = "harassment",
            minor: bool = False, evidence: bool = False, minutes_ago: int = 0):
    media_id = None
    if evidence:
        media_id = db.scalar(text("""
            INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                      content_type, bytes, checksum_sha256, status)
            VALUES (:u, 'audio', 'test-media', :k, 'audio/mp4', 512, 'x', 'ready')
            RETURNING id
        """).bindparams(u=seed["student"].id, k=f"evidence/{uuid.uuid4()}.m4a"))
    return db.execute(text("""
        INSERT INTO safety_reports (reporter_user_id, subject_kind, subject_user_id,
                                    category, description, priority, involves_minor,
                                    evidence_media_id, created_at)
        VALUES (:r, 'user', :s, :c, 'x', :p, :m, :e, now() - (:ago * interval '1 minute'))
        RETURNING xid, id
    """).bindparams(r=seed["student"].id, s=seed["author"].id, c=category,
                    p=priority, m=minor, e=media_id,
                    ago=minutes_ago)).mappings().one()


class TestTheQueueOrder:
    def test_critical_comes_first(self, client, db, seed, admin):
        """`ORDER BY priority DESC` put it last. A moderator working down the
        page reached the grooming report after the spam."""
        _report(db, seed, priority="normal", minutes_ago=30)
        _report(db, seed, priority="critical", category="grooming", minutes_ago=20)
        _report(db, seed, priority="high", minutes_ago=10)
        db.flush()
        got = [r["priority"] for r in client.get(
            "/api/v1/admin/reports", headers=auth(admin.xid)).json()["items"]]
        assert got == ["critical", "high", "normal"]

    def test_within_a_priority_the_oldest_is_first(self, client, db, seed, admin):
        """The SLA clock starts when the report is filed, so at equal urgency
        the one that has been waiting longest is the one to answer."""
        _report(db, seed, priority="critical", minutes_ago=5)
        _report(db, seed, priority="critical", minutes_ago=90)
        db.flush()
        created = [r["created_at"] for r in client.get(
            "/api/v1/admin/reports", headers=auth(admin.xid)).json()["items"]]
        assert created == sorted(created)

    def test_the_index_still_covers_the_query(self, db):
        """The rank is written twice — in the handler and in the index — so the
        pair can drift and the only symptom would be a slow queue. Asserting the
        index expression keeps them together."""
        definition = db.scalar(text("""
            SELECT indexdef FROM pg_indexes
            WHERE indexname = 'safety_reports_queue_idx'
        """))
        assert "'critical'" in definition and "created_at" in definition
        assert "priority DESC" not in definition


class TestTheQueueCanBeEmptied:
    def test_an_action_closes_the_report_it_answers(self, client, db, seed, admin):
        row = _report(db, seed, priority="high", minor=True)
        db.flush()
        response = client.post("/api/v1/admin/moderation-actions", json={
            "action": "warn", "reason": "First offence, warned.",
            "target_user_xid": str(seed["student"].xid),
            "report_xid": str(row["xid"]),
        }, headers=auth(admin.xid))
        assert response.status_code == 201, response.text
        assert response.json()["report_status"] == "actioned"
        assert db.scalar(text("SELECT status FROM safety_reports WHERE id = :i")
                         .bindparams(i=row["id"])) == "actioned"

    def test_dismiss_closes_it_without_acting_against_anybody(
            self, client, db, seed, admin):
        """The outcome most reports deserve, and the only one that was
        unavailable — so the minors queue could never reach empty."""
        row = _report(db, seed, priority="critical", minor=True)
        db.flush()
        response = client.post("/api/v1/admin/moderation-actions", json={
            "action": "dismiss", "reason": "Reviewed; the audio shows no incident.",
            "report_xid": str(row["xid"]),
        }, headers=auth(admin.xid))
        assert response.status_code == 201, response.text
        assert response.json()["report_status"] == "dismissed"
        assert client.get("/api/v1/admin/reports?queue=minors",
                          headers=auth(admin.xid)).json()["items"] == []

    def test_an_investigation_can_be_opened_rather_than_closed(
            self, client, db, seed, admin):
        row = _report(db, seed, priority="critical")
        db.flush()
        client.post("/api/v1/admin/moderation-actions", json={
            "action": "warn", "reason": "Opening an investigation.",
            "target_user_xid": str(seed["student"].xid),
            "report_xid": str(row["xid"]), "report_status": "investigating",
        }, headers=auth(admin.xid))
        assert db.scalar(text("SELECT status FROM safety_reports WHERE id = :i")
                         .bindparams(i=row["id"])) == "investigating"

    def test_an_action_naming_no_report_changes_nothing(self, client, seed, admin):
        response = client.post("/api/v1/admin/moderation-actions", json={
            "action": "warn", "reason": "Spoken to directly.",
            "target_user_xid": str(seed["student"].xid),
        }, headers=auth(admin.xid))
        assert response.status_code == 201
        assert response.json()["report_status"] is None

    def test_banning_nobody_is_refused(self, client, seed, admin):
        """It used to answer 201 with sessions_revoked 0 and write an
        immutable-log row saying the user had been banned — the outcome the
        model's own docstring calls the worst of the three."""
        response = client.post("/api/v1/admin/moderation-actions", json={
            "action": "ban", "reason": "Repeated grooming reports.",
            "target_user_xid": str(uuid.uuid4()),
        }, headers=auth(admin.xid))
        assert response.status_code == 404, response.text


class TestTheQueueSaysWhatItKnows:
    def test_it_names_the_person_the_report_is_about(self, client, db, seed, admin):
        """`subject_user_id` is written on every report and was dropped by the
        DTO, so the moderator had no way to learn the xid the action endpoint
        needs."""
        _report(db, seed, priority="high")
        db.flush()
        row = client.get("/api/v1/admin/reports",
                         headers=auth(admin.xid)).json()["items"][0]
        assert row["subject_user_xid"] == str(seed["author"].xid)
        assert row["subject_kind"] == "user"

    def test_evidence_is_reported_when_there_is_evidence(
            self, client, db, seed, admin):
        """`has_evidence` was the literal False, including for the one report
        type that attaches a recording."""
        _report(db, seed, priority="critical", evidence=True)
        _report(db, seed, priority="normal", evidence=False)
        db.flush()
        flags = {r["priority"]: r["has_evidence"] for r in client.get(
            "/api/v1/admin/reports", headers=auth(admin.xid)).json()["items"]}
        assert flags == {"critical": True, "normal": False}


class TestFlaggedItemsIsNotReadableByStudents:
    @pytest.fixture
    def flagged(self, db, seed):
        """One flagged item in the seeded centre's bank, with common_wrong."""
        qv = seed["question_versions"][0]
        question_id = db.scalar(text(
            "SELECT question_id FROM question_versions WHERE id = :v"
        ).bindparams(v=qv.id))
        db.execute(text("""
            INSERT INTO item_stats (question_id, question_version_id, org_id,
                                    window_start, window_end, n_responses, n_correct,
                                    p_value, discrimination, flagged, flag_reasons,
                                    common_wrong, option_distribution)
            VALUES (:q, :v, :o, current_date - 90, current_date, 24, 3,
                    0.1200, -0.3100, true,
                    ARRAY['negative_discrimination'],
                    CAST('[{"value": "bicycle", "count": 18}]' AS jsonb),
                    CAST('{}' AS jsonb))
        """).bindparams(q=question_id, v=qv.id, o=seed["org"].id))
        db.flush()
        return seed

    def test_a_teacher_may_read_it(self, client, seed, flagged):
        response = client.get("/api/v1/content/flagged-items",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 200, response.text
        assert len(response.json()) == 1

    def test_a_student_at_the_same_centre_may_not(self, client, seed, flagged):
        """The leak. `common_wrong` is a list of what classmates typed and a
        p-value tells a student which questions to spend their time on — which
        is the reason the sibling endpoint refuses them, in its own docstring."""
        response = client.get("/api/v1/content/flagged-items",
                              headers=auth(seed["student"].xid))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "view_exposure_not_permitted"

    def test_it_agrees_with_the_endpoint_beside_it(self, client, seed, flagged):
        """Two endpoints over one dataset. Whatever they answer a student, they
        must answer the same thing — that is the property that broke."""
        version_xid = seed["test_version"].xid
        both = {
            client.get("/api/v1/content/flagged-items",
                       headers=auth(seed["student"].xid)).status_code,
            client.get(f"/api/v1/test-versions/{version_xid}/item-analysis",
                       headers=auth(seed["student"].xid)).status_code,
        }
        assert both == {403}
