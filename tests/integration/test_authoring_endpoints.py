"""Publish, the key fix, and import — the three authoring acts that are not CRUD.

The publish gate itself is covered by `tests/content/test_publish_gate.py` and the
round trip by `test_authoring_flow.py`. This covers the 5% no test executed, which
turned out to be mostly the import path — and the import path is the one that
matters most here, because it is how a whole test arrives at once.

**A bulk import captured no copyright attestation.** The brief is explicit:
*"any uploader must affirm the material is original or licensed, and that
attestation is logged with the upload... Assume some centres WILL try to upload
published Cambridge papers, and design so that liability and evidence are
handled."* Every piece was already in place — `content_attestations` lists
`'import_job'` in its `subject_type` CHECK, `media.record_attestation` is written
and working, the OpenAPI form declares an `attestation` field — and
`POST /imports` asked for none of it. Uploading a DOCX of a Cambridge paper is the
single likeliest route to that liability, and it was the one route with no
evidence attached.

**The import endpoints had no authorization at all.** `Action.IMPORT` is in the
policy matrix, granted to teacher and above, and called from nowhere in the
codebase. Any authenticated user — a student — could import a test and commit it
into their centre's org.

Three smaller ones: the documented form field is `format` and the handler read
`source_format`, so a client following the contract got a 422;
`committed_test_version_xid` is declared in the `ImportJob` schema and was
hardcoded `None`, so an author could never learn what their import produced; and
a superseded answer key recorded `superseded_at` as
`SELECT created_at FROM test_versions LIMIT 1` — an unrelated row's timestamp.
"""

from __future__ import annotations

import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token

ATTEST = {"claim": "original", "statement_version": "1"}


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
    """The seeded author, a `teacher` at the centre."""
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
    from app.modules.identity.models import OrgMembership, User
    import datetime as dt

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Gulnora",
                date_of_birth=dt.date(1980, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def student(db, seed):
    return auth(seed["student"].xid)


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201, 202)), response.text
    return response.json()


IMPORT_DOC = {
    "format": "ielts-hub-import/1",
    "title": "Imported mock",
    "variant": "academic",
    "sections": [{
        "title": "Section 1", "skill": "reading",
        "passage": {"title": "Passage", "blocks": [
            {"type": "paragraph", "runs": [{"t": "text", "v": "Some text."}]}]},
        "groups": [{
            "title": "Questions 1-1",
            "instructions": {"en": "Answer the question."},
            "questions": [{"type_key": "short_answer", "type_version": 1,
                           "text": "What did they ride?", "accept": ["bike"]}],
        }],
    }],
}


def _upload(client, headers, doc=None, *, attestation=ATTEST, field="format",
            fmt="json"):
    files = {"file": ("import.json",
                      io.BytesIO(json.dumps(doc or IMPORT_DOC).encode()),
                      "application/json")}
    data = {field: fmt}
    if attestation is not None:
        data["attestation"] = json.dumps(attestation)
    return client.post("/api/v1/imports", headers=headers, files=files, data=data)


# ── the attestation ──────────────────────────────────────────────────

class TestImportRequiresACopyrightAttestation:
    """"Any uploader must affirm the material is original or licensed, and that
    attestation is logged with the upload... Assume some centres WILL try to
    upload published Cambridge papers."

    A bulk import is that upload. Every part of the machinery existed —
    `content_attestations` even lists `'import_job'` as a subject type — and this
    endpoint asked for nothing.
    """

    def test_an_import_without_one_is_refused(self, client, author):
        refused = _upload(client, author, attestation=None)
        assert refused.status_code == 422, refused.text
        assert "attestation" in refused.text

    def test_an_unknown_claim_is_refused(self, client, author):
        """"Refused, not defaulted. A missing attestation that quietly becomes
        'original' is worse than no attestation at all." """
        refused = _upload(client, author,
                          attestation={"claim": "probably fine",
                                       "statement_version": "1"})
        assert refused.status_code == 422, refused.text

    def test_a_licensed_claim_must_say_what_the_licence_is(self, client, author):
        refused = _upload(client, author,
                          attestation={"claim": "licensed", "statement_version": "1"})
        assert refused.status_code == 422, refused.text
        assert "licence" in refused.text.lower()

    def test_a_licensed_claim_with_a_note_is_accepted(self, client, author):
        assert _upload(client, author,
                       attestation={"claim": "licensed", "statement_version": "1",
                                    "licence_note": "Cambridge licence #4471"}
                       ).status_code == 202

    def test_the_attestation_is_recorded_against_the_import_job(self, client, db,
                                                                author, seed):
        """"Show me the attestation for this asset" is the first question in a
        takedown, and an import job has to be able to answer it."""
        job = _ok(_upload(client, author), 202)
        row = db.execute(text("""
            SELECT a.claim, a.statement_key, a.statement_hash, a.user_id, a.org_id
            FROM content_attestations a
            JOIN import_jobs j ON j.id = a.subject_id
            WHERE a.subject_type = 'import_job' AND j.xid = CAST(:x AS uuid)
        """).bindparams(x=job["xid"])).mappings().one()
        assert row["claim"] == "original"
        assert row["statement_key"] == "upload"
        assert row["user_id"] == seed["author"].id
        assert row["org_id"] == seed["org"].id

    def test_it_stores_the_statement_hash_not_a_boolean(self, client, db, author):
        """"'They ticked a box' is not a defence; 'they ticked THIS box, whose text
        hashed to X, from this IP, at this time' is." """
        import hashlib

        from app.modules.content.media import ATTESTATION_STATEMENTS

        _ok(_upload(client, author), 202)
        row = db.execute(text("""
            SELECT statement_hash, statement_version, affirmed_at
            FROM content_attestations WHERE subject_type = 'import_job'
        """)).mappings().one()
        expected = hashlib.sha256(
            ATTESTATION_STATEMENTS["upload"]["text"].encode()).hexdigest()
        assert row["statement_hash"] == expected
        assert row["affirmed_at"]

    def test_a_failed_parse_still_records_the_attestation(self, client, db, author):
        """The affirmation was made when the file was handed over. A parse failure
        does not un-make it, and a centre that repeatedly uploads material it
        cannot attest to is exactly the pattern an investigation looks for."""
        _ok(_upload(client, author, {"format": "nonsense"}), 202)
        assert db.scalar(text("SELECT count(*) FROM content_attestations "
                              "WHERE subject_type = 'import_job'")) == 1


# ── authorization ────────────────────────────────────────────────────

class TestImportNeedsAuthoringRights:
    """`Action.IMPORT` is in the policy matrix — teacher, centre admin, platform
    admin — and was called from nowhere in the codebase. Import was the one door
    into the content library with no lock on it."""

    def test_a_student_cannot_import(self, client, student):
        refused = _upload(client, student)
        assert refused.status_code == 403, refused.text

    def test_a_teacher_can(self, client, author):
        assert _upload(client, author).status_code == 202

    def test_a_centre_admin_can(self, client, centre_admin):
        assert _upload(client, centre_admin).status_code == 202

    def test_a_student_cannot_commit_someone_elses_import(self, client, db, author,
                                                          student):
        job = _ok(_upload(client, author), 202)
        assert client.post(f"/api/v1/imports/{job['xid']}/commit",
                           headers=student).status_code == 404

    def test_a_student_cannot_read_someone_elses_import(self, client, author,
                                                        student):
        job = _ok(_upload(client, author), 202)
        assert client.get(f"/api/v1/imports/{job['xid']}",
                          headers=student).status_code == 404


# ── the documented request shape ─────────────────────────────────────

class TestTheImportRequest:
    def test_the_documented_field_name_works(self, client, author):
        """The schema says `required: [file, format]`. The handler read
        `source_format`, so a client written against the contract got a 422 for a
        missing field the contract never mentions."""
        assert _upload(client, author, field="format").status_code == 202

    def test_an_unknown_format_is_refused(self, client, author):
        refused = _upload(client, author, fmt="wingdings")
        assert refused.status_code in (409, 422), refused.text

    def test_a_missing_format_is_refused(self, client, author):
        files = {"file": ("import.json", io.BytesIO(b"{}"), "application/json")}
        refused = client.post("/api/v1/imports", headers=author, files=files,
                              data={"attestation": json.dumps(ATTEST)})
        assert refused.status_code == 422, refused.text

    def test_an_oversized_file_is_refused(self, client, author):
        """`file.read()` had no ceiling, so an authenticated client could put an
        arbitrary file into memory on a 4 vCPU box."""
        from app.api.routers.authoring import MAX_IMPORT_BYTES

        files = {"file": ("big.json", io.BytesIO(b"x" * (MAX_IMPORT_BYTES + 1)),
                          "application/json")}
        refused = client.post("/api/v1/imports", headers=author, files=files,
                              data={"format": "json",
                                    "attestation": json.dumps(ATTEST)})
        assert refused.status_code == 422, refused.text
        assert "IMPORT_TOO_LARGE" in refused.text

    @pytest.mark.parametrize("value", ["not json at all", '"a string"', "[1, 2]",
                                       "null"])
    def test_a_malformed_attestation_is_refused_not_ignored(self, client, author,
                                                            value):
        """Anything that is not an object with a valid claim is a missing
        attestation, and a missing attestation is a refusal. Falling back to a
        default here would manufacture a claim the uploader never made."""
        files = {"file": ("import.json", io.BytesIO(b"{}"), "application/json")}
        refused = client.post("/api/v1/imports", headers=author, files=files,
                              data={"format": "json", "attestation": value})
        assert refused.status_code == 422, refused.text
        assert "ATTESTATION_REQUIRED" in refused.text


# ── the job record ───────────────────────────────────────────────────

class TestReadingAnImportJob:
    def test_a_validated_job_reports_its_findings(self, client, author):
        job = _ok(_upload(client, author), 202)
        body = _ok(client.get(f"/api/v1/imports/{job['xid']}", headers=author))
        assert body["status"] == "validated"
        assert body["report"]

    def test_a_committed_job_names_the_version_it_produced(self, client, author):
        """`committed_test_version_xid` is declared in the ImportJob schema and was
        hardcoded `None`, so an author who committed an import could never find
        out what it had created."""
        job = _ok(_upload(client, author), 202)
        committed = _ok(client.post(f"/api/v1/imports/{job['xid']}/commit",
                                    headers=author))
        body = _ok(client.get(f"/api/v1/imports/{job['xid']}", headers=author))
        assert body["status"] == "committed"
        assert body["committed_test_version_xid"] \
            == committed["committed_test_version_xid"]

    def test_an_uncommitted_job_reports_null(self, client, author):
        job = _ok(_upload(client, author), 202)
        body = _ok(client.get(f"/api/v1/imports/{job['xid']}", headers=author))
        assert body["committed_test_version_xid"] is None

    def test_an_unknown_job_is_a_404(self, client, author):
        assert client.get(f"/api/v1/imports/{uuid.uuid4()}",
                          headers=author).status_code == 404

    def test_committing_twice_replays_rather_than_importing_twice(self, client, db,
                                                                  author):
        job = _ok(_upload(client, author), 202)
        headers = {**author, "Idempotency-Key": "commit-once"}
        first = _ok(client.post(f"/api/v1/imports/{job['xid']}/commit",
                                headers=headers))
        again = _ok(client.post(f"/api/v1/imports/{job['xid']}/commit",
                                headers=headers))
        assert first == again
        assert db.scalar(text("SELECT count(*) FROM tests WHERE title = "
                              "'Imported mock'")) == 1

    def test_a_committed_import_lands_as_a_draft(self, client, author):
        """"Import must not become a publish bypass." """
        job = _ok(_upload(client, author), 202)
        body = _ok(client.post(f"/api/v1/imports/{job['xid']}/commit",
                               headers=author))
        assert body["test_version_status"] == "draft"


# ── publish ──────────────────────────────────────────────────────────

class TestPublishing:
    def test_a_centre_admin_can_publish(self, client, db, seed, centre_admin):
        body = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=centre_admin))
        assert body["status"] == "published"
        assert body["published_at"]

    def test_a_teacher_cannot_unless_the_centre_opted_in(self, client, seed, author):
        """"A centre's reputation rides on its published material, so the default
        is off." """
        refused = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=author)
        assert refused.status_code == 403
        assert refused.json()["code"] == "publish_not_permitted"

    def test_a_teacher_can_once_the_centre_has(self, client, db, seed, author):
        seed["org"].settings = {"teacher_can_publish": True}
        db.flush()
        assert client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=author).status_code == 200

    def test_publishing_twice_is_refused(self, client, seed, centre_admin):
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=centre_admin))
        refused = client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=centre_admin)
        assert refused.status_code == 409
        assert refused.json()["code"] == "already_published"

    def test_publishing_points_the_test_at_the_version(self, client, db, seed,
                                                       centre_admin):
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=centre_admin))
        db.expire_all()
        assert db.scalar(text("SELECT current_published_version_id FROM tests "
                              "WHERE id = :t").bindparams(t=seed["test"].id)) \
            == seed["test_version"].id

    def test_an_unknown_version_is_a_404(self, client, centre_admin):
        assert client.post(f"/api/v1/test-versions/{uuid.uuid4()}/publish",
                           headers=centre_admin).status_code == 404

    def test_validating_persists_the_findings(self, client, db, seed, author):
        """"Persists them so an author can close the tab and come back to the
        list." """
        _ok(client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/validate",
                        headers=author))
        assert db.scalar(text("SELECT count(*) FROM test_version_validations")) == 1

    def test_validating_an_unknown_version_is_a_404(self, client, author):
        assert client.post(f"/api/v1/test-versions/{uuid.uuid4()}/validate",
                           headers=author).status_code == 404


# ── the key fix ──────────────────────────────────────────────────────

class TestTheKeyFix:
    """"A bad key must be fixable without invalidating what students already
    sat." """

    def test_a_new_key_supersedes_the_old_one(self, client, db, seed, author):
        qv = seed["question_versions"][0]
        body = _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                               json={"key": {"slots": {"s1": {"accept": ["bicycle"]}}},
                                     "reason": "key_fix",
                                     "note": "bike was the only accepted form"},
                               headers=author), 201)
        assert body["key_version"]["version_no"] == 2
        assert body["key_version"]["is_current"] is True
        assert db.scalar(text("""
            SELECT count(*) FROM answer_key_versions
            WHERE question_version_id = :q AND is_current
        """).bindparams(q=qv.id)) == 1

    def test_the_superseded_key_is_stamped_with_when_it_happened(self, client, db,
                                                                 seed, author):
        """`superseded_at` was set to `SELECT created_at FROM test_versions LIMIT 1`
        — an unrelated row's timestamp, whichever the planner happened to return.
        "When did this key change" is the first question of any regrade dispute.
        """
        import datetime as dt

        qv = seed["question_versions"][0]
        before = dt.datetime.now(dt.UTC)
        _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                        json={"key": {"slots": {"s1": {"accept": ["bicycle"]}}}},
                        headers=author), 201)
        stamped = db.scalar(text("""
            SELECT superseded_at FROM answer_key_versions
            WHERE question_version_id = :q AND NOT is_current
        """).bindparams(q=qv.id))
        assert stamped is not None
        assert stamped >= before

    def test_the_question_version_itself_is_untouched(self, client, db, seed,
                                                      author):
        """"The question version stays frozen; a new key version supersedes the
        old one." A student's paper must still say what it said."""
        qv = seed["question_versions"][0]
        before = db.scalar(text("SELECT checksum FROM question_versions WHERE id = :q")
                           .bindparams(q=qv.id))
        _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                        json={"key": {"slots": {"s1": {"accept": ["bicycle"]}}}},
                        headers=author), 201)
        db.expire_all()
        assert db.scalar(text("SELECT checksum FROM question_versions WHERE id = :q")
                         .bindparams(q=qv.id)) == before

    def test_nothing_is_regraded_here(self, client, db, seed, author):
        """"Nothing is regraded here — a dry-run job is staged and the impact
        returned for a human to confirm." """
        qv = seed["question_versions"][0]
        body = _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                               json={"key": {"slots": {"s1": {"accept": ["x"]}}}},
                               headers=author), 201)
        assert body["regrade_preview"]["attempts_total"] == 0
        assert "No sat attempts" in body["regrade_preview"]["note"]
        assert db.scalar(text("SELECT count(*) FROM score_runs")) == 0

    def test_the_preview_counts_attempts_not_slots(self, client, db, seed, author):
        """"One attempt can hold several slots of the same question, and '42
        attempts affected' must not read as 126 because it was a three-blank
        sentence completion." """
        qv = seed["question_versions"][0]
        attempt = db.scalar(text("""
            INSERT INTO attempts (user_id, test_version_id, mode, status, started_at,
                                  submitted_at)
            VALUES (:u, :tv, 'exam', 'scored', now(), now()) RETURNING id
        """).bindparams(u=seed["student"].id, tv=seed["test_version"].id))
        run = db.scalar(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, is_current)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 3, 3, true) RETURNING id
        """).bindparams(a=attempt))
        for slot in ("s1", "s2", "s3"):
            db.execute(text("""
                INSERT INTO item_scores (score_run_id, question_id,
                                         question_version_id, slot_key, awarded,
                                         max_points, verdict)
                VALUES (:r, (SELECT question_id FROM question_versions WHERE id = :q),
                        :q, :k, 1, 1, 'correct')
            """).bindparams(r=run, q=qv.id, k=slot))
        db.flush()
        body = _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                               json={"key": {"slots": {"s1": {"accept": ["x"]}}}},
                               headers=author), 201)
        assert body["regrade_preview"]["attempts_total"] == 1

    def test_a_retry_returns_the_stored_response(self, client, db, seed, author):
        qv = seed["question_versions"][0]
        headers = {**author, "Idempotency-Key": "fix-once"}
        body = {"key": {"slots": {"s1": {"accept": ["bicycle"]}}}}
        first = _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                                json=body, headers=headers), 201)
        again = _ok(client.post(f"/api/v1/question-versions/{qv.xid}/keys",
                                json=body, headers=headers), 201)
        assert first == again
        assert db.scalar(text("""
            SELECT count(*) FROM answer_key_versions WHERE question_version_id = :q
        """).bindparams(q=qv.id)) == 2

    def test_an_unknown_question_version_is_a_404(self, client, author):
        assert client.post(f"/api/v1/question-versions/{uuid.uuid4()}/keys",
                           json={"key": {}}, headers=author).status_code == 404
