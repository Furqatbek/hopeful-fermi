"""The Import screen's exact call sequence: template → upload → report → commit.

The pipeline existed — three adapters, a canonical document, a dry run, a diff, a
commit — and no screen reached it, so a centre arriving with forty papers already
written had to retype them. Every request below is one the screen makes, with the
body it sends.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token

ATTESTATION = json.dumps({"claim": "original", "statement_version": "1"})


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
    assert response.status_code in (expected or (200, 201, 202)), response.text
    return response.json()


class TestTheTemplateRoundTrips:
    """A template that does not import is worse than no template: it teaches the
    centre the feature is broken on their first contact with it."""

    @pytest.mark.parametrize("fmt", ["csv", "json"])
    def test_the_downloaded_template_imports(self, client, seed, fmt):
        template = client.get(f"/api/v1/imports/template?format={fmt}",
                              headers=auth(seed["author"].xid))
        assert template.status_code == 200
        body = template.content
        assert body, "the screen offers this as a download"

        job = _ok(client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                              files={"file": (f"t.{fmt}", body)},
                              data={"format": fmt, "attestation": ATTESTATION}), 202)
        assert job["status"] == "validated", job["report"]
        counts = job["report"]["counts"]
        assert counts["questions"] >= 1 and counts["keys"] == counts["questions"]


class TestTheDryRunIsTheProduct:
    def test_nothing_is_written_until_commit(self, client, db, seed):
        from app.modules.content.models import Test

        before = db.query(Test).count()
        template = client.get("/api/v1/imports/template?format=json",
                              headers=auth(seed["author"].xid)).content
        job = _ok(client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                              files={"file": ("t.json", template)},
                              data={"format": "json", "attestation": ATTESTATION}), 202)
        assert db.query(Test).count() == before, "the upload must write no content"

        # Exactly what the report panel reads.
        report = _ok(client.get(f"/api/v1/imports/{job['xid']}",
                                headers=auth(seed["author"].xid)))
        assert report["status"] == "validated"
        assert report["committed_test_version_xid"] is None
        assert set(report["report"]["counts"]) >= {"sections", "groups",
                                                   "questions", "keys"}

    def test_commit_lands_a_draft_and_names_it(self, client, seed):
        template = client.get("/api/v1/imports/template?format=json",
                              headers=auth(seed["author"].xid)).content
        job = _ok(client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                              files={"file": ("t.json", template)},
                              data={"format": "json", "attestation": ATTESTATION}), 202)
        done = _ok(client.post(f"/api/v1/imports/{job['xid']}/commit",
                               headers=auth(seed["author"].xid)))
        assert done["status"] == "committed"
        version_xid = done["committed_test_version_xid"]
        assert version_xid, "the screen links straight to it"

        # A DRAFT: import must not become a way around the publish gate.
        version = _ok(client.get(f"/api/v1/test-versions/{version_xid}",
                                 headers=auth(seed["author"].xid)))
        assert version["status"] == "draft"
        assert version["sections"], "an imported version arrives composed"


class TestWhatTheScreenRefuses:
    def test_an_import_with_no_attestation_is_refused(self, client, seed):
        """The affirmation is a required field on the upload, not a setting
        somebody accepted at signup. A bulk import is the likeliest single route
        to a published exam paper entering this system."""
        template = client.get("/api/v1/imports/template?format=json",
                              headers=auth(seed["author"].xid)).content
        refused = client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                              files={"file": ("t.json", template)},
                              data={"format": "json"})
        assert refused.status_code == 422

    def test_a_broken_file_reports_rather_than_crashing(self, client, seed):
        job = _ok(client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                              files={"file": ("t.json", b"{not json at all")},
                              data={"format": "json", "attestation": ATTESTATION}), 202)
        assert job["status"] == "failed"
        # And it cannot be committed — the screen offers no button in this state.
        refused = client.post(f"/api/v1/imports/{job['xid']}/commit",
                              headers=auth(seed["author"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "import_not_validated"

    def test_the_attestation_is_recorded_even_when_the_parse_fails(
            self, client, db, seed):
        """Handing the file over is when the claim was made; a failed parse does
        not un-make it. A centre that repeatedly uploads material it cannot
        attest to is exactly the pattern an investigation looks for."""
        from sqlalchemy import text

        _ok(client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                        files={"file": ("t.json", b"{broken")},
                        data={"format": "json", "attestation": ATTESTATION}), 202)
        rows = db.execute(text(
            "SELECT count(*) FROM content_attestations WHERE subject_type = 'import_job'"
        )).scalar()
        assert rows == 1

    def test_a_student_cannot_import(self, client, seed):
        """Import was the one door into the content library with no lock on it."""
        template = client.get("/api/v1/imports/template?format=json",
                              headers=auth(seed["author"].xid)).content
        refused = client.post("/api/v1/imports", headers=auth(seed["student"].xid),
                              files={"file": ("t.json", template)},
                              data={"format": "json", "attestation": ATTESTATION})
        assert refused.status_code == 403


class TestTheOfflineRoundTrip:
    def test_importing_as_a_new_version_of_an_existing_test(self, client, seed):
        """Export, edit in a spreadsheet, bring it back as a new VERSION of the
        same test rather than as an unrelated copy."""
        # `include_keys` is not optional for a ROUND TRIP. Export defaults to
        # omitting them, and the importer refuses a row with no answer
        # (`CSV_NO_ANSWER`) — so the default export produces a file that cannot
        # come back, and a screen offering export must say so.
        exported = client.get(
            f"/api/v1/test-versions/{seed['test_version'].xid}"
            "/export?format=csv&include_keys=true",
            headers=auth(seed["author"].xid))
        assert exported.status_code == 200, exported.text

        job = _ok(client.post("/api/v1/imports", headers=auth(seed["author"].xid),
                              files={"file": ("t.csv", exported.content)},
                              data={"format": "csv", "attestation": ATTESTATION,
                                    "target_test_xid": str(seed["test"].xid)}), 202)
        assert job["status"] == "validated", job["report"]
        done = _ok(client.post(f"/api/v1/imports/{job['xid']}/commit",
                               headers=auth(seed["author"].xid)))

        # The same test, a new version — not a second test with the same name.
        detail = _ok(client.get(f"/api/v1/tests/{seed['test'].xid}",
                                headers=auth(seed["author"].xid)))
        assert done["committed_test_version_xid"] in {
            v["xid"] for v in detail["versions"]}


class TestTheWordTemplateSaysItDoesNotExist:
    def test_asking_for_docx_is_refused_not_quietly_given_json(self, client, seed):
        """The enum offers `docx` and this returned the JSON template for it — so
        a centre asking for the Word template got a `.json` file and no
        explanation. There is no Word template: `from_docx` reads a locked
        document carrying the canonical JSON in a file property, and nothing here
        produces one."""
        response = client.get("/api/v1/imports/template?format=docx",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 422
        codes = [f["code"] for f in response.json()["findings"]]
        assert codes == ["TEMPLATE_UNAVAILABLE"]
        assert b"IELTS-IMPORT" not in response.content
