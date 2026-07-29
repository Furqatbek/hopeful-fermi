"""HTTP -> domain -> database, end to end.

The gap Deliverable 4 §6 named. What is proven here that neither unit tests nor
the schema tests could prove: that the wiring holds — auth resolves, transactions
commit, domain errors become the right status codes, and idempotency replays.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import issue_access_token
from app.modules.exam.models import Attempt, AttemptAnswer


@pytest.fixture
def client(engine, db, monkeypatch):
    """A TestClient whose request-scoped session is the test's own session, so
    assertions see what the handler wrote without a commit race."""
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def auth(published):
    return {"Authorization": f"Bearer {issue_access_token(str(published['student'].xid))}"}


@pytest.fixture
def author_auth(seed):
    """Depends on `seed`, not `published`: the authoring tests need a DRAFT."""
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}


def start(client, auth, published, **kw) -> dict:
    body = {"test_version_xid": str(published["test_version"].xid), **kw}
    r = client.post("/api/v1/attempts", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


class TestAuth:
    def test_no_token_is_rejected(self, client):
        r = client.post("/api/v1/attempts", json={})
        assert r.status_code == 403
        assert r.json()["code"] == "unauthenticated"

    def test_a_garbage_token_is_rejected(self, client):
        r = client.get("/api/v1/attempts/" + str(uuid.uuid4()),
                       headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 403
        assert r.json()["code"] == "invalid_token"

    def test_errors_are_rfc9457_problem_documents(self, client):
        r = client.post("/api/v1/attempts", json={})
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert {"type", "title", "status", "code"} <= set(body)

    def test_every_response_carries_a_request_id(self, client, auth, published):
        r = client.post("/api/v1/attempts",
                        json={"test_version_xid": str(published["test_version"].xid)},
                        headers=auth)
        assert r.headers["X-Request-ID"]


class TestEntitlementGate:
    def test_without_an_entitlement_the_answer_is_402(self, client, auth, published):
        r = client.post("/api/v1/attempts",
                        json={"test_version_xid": str(published["test_version"].xid)},
                        headers=auth)
        assert r.status_code == 402
        assert r.json()["code"] == "payment_required"
        assert r.json()["feature"] == "mock.unlimited"

    def test_with_an_entitlement_the_attempt_starts(self, client, auth, entitled, published):
        r = client.post("/api/v1/attempts",
                        json={"test_version_xid": str(published["test_version"].xid)},
                        headers=auth)
        assert r.status_code == 201
        assert r.json()["status"] == "in_progress"

    def test_a_preview_needs_no_entitlement(self, client, author_auth, published):
        r = client.post("/api/v1/attempts",
                        json={"test_version_xid": str(published["test_version"].xid),
                              "mode": "preview"},
                        headers=author_auth)
        assert r.status_code == 201


class TestSittingAnExam:
    def test_the_whole_flow_over_http(self, client, auth, entitled, published, db):
        attempt = start(client, auth, published)
        xid = attempt["xid"]

        payload = client.get(f"/api/v1/attempts/{xid}/payload", headers=auth).json()
        assert payload["total_questions"] == 3

        qvs = [str(qv.xid) for qv in published["question_versions"]]
        r = client.post(f"/api/v1/attempts/{xid}/answers", headers=auth, json={
            "deltas": [
                {"question_version_xid": qvs[0], "slot_key": "s1",
                 "response": "bicycle", "client_seq": 1},
                {"question_version_xid": qvs[1], "slot_key": "s1",
                 "response": "library", "client_seq": 2},
            ]})
        assert r.status_code == 200
        saved = r.json()
        assert saved["accepted"] == 2 and saved["rejected"] == []
        assert saved["server_now"] and 3590 <= saved["seconds_remaining"] <= 3600

        result = client.post(f"/api/v1/attempts/{xid}/submit", headers=auth).json()
        assert result["raw_score"] == 2.0 and result["band"] == 6.0

        review = client.get(f"/api/v1/attempts/{xid}/review", headers=auth).json()
        assert len(review["items"]) == 3
        unanswered = [i for i in review["items"] if i["verdict"] == "unanswered"]
        assert len(unanswered) == 1

    def test_the_payload_never_contains_answer_keys(self, client, auth, entitled, published):
        attempt = start(client, auth, published)
        raw = client.get(f"/api/v1/attempts/{attempt['xid']}/payload", headers=auth).text
        for secret in ("bicycle", "library", "museum", "accept"):
            assert secret not in raw

    def test_another_users_attempt_is_404_not_403(self, client, auth, author_auth,
                                                  entitled, published):
        """Confirming an attempt exists tells a prober something they should not
        learn, so the answer is the same as for an id that never existed."""
        attempt = start(client, auth, published)
        r = client.get(f"/api/v1/attempts/{attempt['xid']}", headers=author_auth)
        assert r.status_code == 404

    def test_answers_after_submit_are_409(self, client, auth, entitled, published):
        attempt = start(client, auth, published)
        xid = attempt["xid"]
        client.post(f"/api/v1/attempts/{xid}/submit", headers=auth)
        r = client.post(f"/api/v1/attempts/{xid}/answers", headers=auth, json={
            "deltas": [{"question_version_xid": str(published["question_versions"][0].xid),
                        "slot_key": "s1", "response": "x", "client_seq": 9}]})
        assert r.status_code == 409
        assert r.json()["code"] == "attempt_frozen"

    def test_a_malformed_body_returns_every_field_error(self, client, auth,
                                                        entitled, published):
        attempt = start(client, auth, published)
        r = client.post(f"/api/v1/attempts/{attempt['xid']}/answers", headers=auth,
                        json={"deltas": [{"slot_key": "s1"}]})
        assert r.status_code == 422
        findings = r.json()["findings"]
        assert len(findings) >= 2
        assert all(f["path"] and f["fix_hint"] for f in findings)

    def test_play_once_is_enforced_over_http(self, client, auth, entitled, published):
        attempt = start(client, auth, published)
        first = client.post(
            f"/api/v1/attempts/{attempt['xid']}/sections/1/audio-grant", headers=auth)
        assert first.status_code == 200 and first.json()["grant"]
        second = client.post(
            f"/api/v1/attempts/{attempt['xid']}/sections/1/audio-grant", headers=auth)
        assert second.status_code == 409
        assert second.json()["code"] == "audio_already_played"


class TestIdempotency:
    def test_a_replayed_autosave_returns_the_stored_response(
            self, client, auth, entitled, published, db):
        attempt = start(client, auth, published)
        xid = attempt["xid"]
        body = {"deltas": [{"question_version_xid": str(published["question_versions"][0].xid),
                            "slot_key": "s1", "response": "bicycle", "client_seq": 1}]}
        headers = {**auth, "Idempotency-Key": "abc-123"}

        first = client.post(f"/api/v1/attempts/{xid}/answers", headers=headers, json=body)
        second = client.post(f"/api/v1/attempts/{xid}/answers", headers=headers, json=body)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()

        rows = db.scalars(select(AttemptAnswer)).all()
        assert len(rows) == 1 and rows[0].revision == 1   # not applied twice

    def test_the_same_key_with_a_different_body_is_a_client_bug(
            self, client, auth, entitled, published):
        attempt = start(client, auth, published)
        xid = attempt["xid"]
        headers = {**auth, "Idempotency-Key": "dup-key"}
        qv = str(published["question_versions"][0].xid)

        client.post(f"/api/v1/attempts/{xid}/answers", headers=headers, json={
            "deltas": [{"question_version_xid": qv, "slot_key": "s1",
                        "response": "one", "client_seq": 1}]})
        r = client.post(f"/api/v1/attempts/{xid}/answers", headers=headers, json={
            "deltas": [{"question_version_xid": qv, "slot_key": "s1",
                        "response": "two", "client_seq": 2}]})
        assert r.status_code == 409
        assert r.json()["code"] == "idempotency_key_reused"

    def test_a_replayed_start_does_not_create_a_second_attempt(
            self, client, auth, entitled, published, db):
        headers = {**auth, "Idempotency-Key": "start-1"}
        body = {"test_version_xid": str(published["test_version"].xid)}
        first = client.post("/api/v1/attempts", headers=headers, json=body)
        second = client.post("/api/v1/attempts", headers=headers, json=body)
        assert first.json()["xid"] == second.json()["xid"]
        assert len(db.scalars(select(Attempt)).all()) == 1


class TestAuthoringOverHttp:
    def test_publish_returns_every_gate_finding_at_once(
            self, client, author_auth, seed, db):
        """The whole promise of the gate, asserted through the API."""
        from app.modules.content.models import AnswerKeyVersion

        seed["org"].settings = {"teacher_can_publish": True}
        db.execute(AnswerKeyVersion.__table__.delete())
        db.flush()
        r = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=author_auth)
        assert r.status_code == 422
        findings = r.json()["findings"]
        assert len(findings) == 3                       # one per keyless question
        assert {f["code"] for f in findings} == {"KEY_MISSING"}
        assert all(f["path"] and f["fix_hint"] for f in findings)

    def test_a_teacher_cannot_publish_unless_the_centre_opted_in(
            self, client, author_auth, seed, db):
        r = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=author_auth)
        assert r.status_code == 403
        assert r.json()["code"] == "publish_not_permitted"

    def test_with_the_setting_on_the_teacher_can_publish(
            self, client, author_auth, seed, db):
        seed["org"].settings = {"teacher_can_publish": True}
        db.flush()
        r = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=author_auth)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "published"
        assert r.json()["total_questions"] == 3

    def test_validate_persists_the_report(self, client, author_auth, seed, db):
        from app.modules.content.models import TestVersionValidation

        r = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/validate",
                        headers=author_auth)
        assert r.status_code == 200 and r.json()["passed"] is True
        stored = db.scalars(select(TestVersionValidation)).all()
        assert len(stored) == 1 and stored[0].passed is True

    def test_fixing_a_key_supersedes_rather_than_edits(
            self, client, author_auth, published, db):
        from app.modules.content.models import AnswerKeyVersion

        qv = published["question_versions"][0]
        r = client.post(f"/api/v1/question-versions/{qv.xid}/keys", headers=author_auth,
                        json={"key": {"slots": {"s1": {"accept": ["bicycle", "bike"]}}},
                              "reason": "key_fix", "note": "students wrote bike"})
        assert r.status_code == 201, r.text
        assert r.json()["key_version"]["version_no"] == 2

        keys = db.scalars(select(AnswerKeyVersion)
                          .where(AnswerKeyVersion.question_version_id == qv.id)
                          .order_by(AnswerKeyVersion.version_no)).all()
        assert len(keys) == 2
        assert [k.is_current for k in keys] == [False, True]

    def test_a_key_fix_regrades_nothing_on_its_own(
            self, client, auth, author_auth, entitled, published, db):
        from app.modules.exam.models import ScoreRun

        attempt = start(client, auth, published)
        qvs = [str(q.xid) for q in published["question_versions"]]
        client.post(f"/api/v1/attempts/{attempt['xid']}/answers", headers=auth, json={
            "deltas": [{"question_version_xid": qvs[0], "slot_key": "s1",
                        "response": "bike", "client_seq": 1}]})
        client.post(f"/api/v1/attempts/{attempt['xid']}/submit", headers=auth)
        before = db.scalars(select(ScoreRun.raw_score)).all()

        r = client.post(
            f"/api/v1/question-versions/{published['question_versions'][0].xid}/keys",
            headers=author_auth,
            json={"key": {"slots": {"s1": {"accept": ["bicycle", "bike"]}}},
                  "reason": "key_fix"})
        assert r.status_code == 201
        assert r.json()["regrade_preview"]["attempts_total"] == 1
        assert "Nothing has been regraded" in r.json()["regrade_preview"]["note"]
        assert db.scalars(select(ScoreRun.raw_score)).all() == before


class TestImportOverHttp:
    DOC = {
        "canonical_version": 1,
        "title": "HTTP imported mock",
        "sections": [{
            "title": "Passage 1", "skill": "reading",
            "groups": [{
                "title": "Questions 1-2",
                "word_limit": {"max_words": 2, "allow_number": True},
                "questions": [
                    {"type_key": "sentence_completion", "text": "A {{s1}}.",
                     "accept": ["river"]},
                    {"type_key": "sentence_completion", "text": "B {{s1}}.",
                     "accept": ["lake"]},
                ]}]}],
    }

    def test_dry_run_then_commit(self, client, author_auth, seed, db):
        files = {"file": ("mock.json", json.dumps(self.DOC), "application/json")}
        r = client.post("/api/v1/imports", headers=author_auth, files=files,
                        data={"source_format": "json"})
        assert r.status_code == 202, r.text
        job = r.json()
        assert job["status"] == "validated"
        assert job["report"]["counts"]["questions"] == 2

        committed = client.post(f"/api/v1/imports/{job['xid']}/commit", headers=author_auth)
        assert committed.status_code == 200
        # A draft, never published: import must not be a publish bypass.
        assert committed.json()["test_version_status"] == "draft"

    def test_a_failed_parse_cannot_be_committed(self, client, author_auth, seed):
        files = {"file": ("mock.json", "{not json", "application/json")}
        r = client.post("/api/v1/imports", headers=author_auth, files=files,
                        data={"source_format": "json"})
        job = r.json()
        assert job["status"] == "failed"
        committed = client.post(f"/api/v1/imports/{job['xid']}/commit", headers=author_auth)
        assert committed.status_code == 409
        assert committed.json()["code"] == "import_not_validated"

    def test_another_users_import_job_is_invisible(self, client, auth, author_auth, seed):
        files = {"file": ("mock.json", json.dumps(self.DOC), "application/json")}
        job = client.post("/api/v1/imports", headers=author_auth, files=files,
                          data={"source_format": "json"}).json()
        assert client.get(f"/api/v1/imports/{job['xid']}", headers=auth).status_code == 404
