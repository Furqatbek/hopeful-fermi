"""The copyright chain, which was broken at both ends.

The brief's instruction is blunt: "Assume some centres WILL try to upload
published Cambridge papers, and design so that liability and evidence are
handled." Two independent defects meant neither half worked for a reading
passage, which is the route by which a typed or pasted paper arrives.

**No evidence was recorded.** `PassageCreate` did not declare `attestation`,
although the contract did and although `content_attestations.subject_type` has
listed `'passage'` in its CHECK constraint since the migration that created the
table. Pydantic drops an undeclared field silently, so the console's copyright
form collected a claim, posted it, and it went nowhere.

**And the gate could not refuse anything.** `PassageRef.has_attestation`
defaults to `True` and `under_takedown` to `False`, and `load_composition` set
neither — so `publish_gate` checks 17 and 18 were unreachable code. A passage
with no attestation published cleanly. So did one under an open takedown, which
also means the "soft-hide pending review" a filing performs stopped nothing.

Each default is the safe direction for a dataclass and the wrong direction for a
gate. That is why it read as working: every part was individually correct.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token

ORIGINAL = {"claim": "original", "statement_version": "1"}


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


def _passage(client, seed, attestation=ORIGINAL, title="The history of glass"):
    body = {"title": title,
            "blocks": [{"type": "paragraph",
                        "runs": [{"t": "text", "v": "Glass is old."}]}]}
    if attestation is not None:
        body["attestation"] = attestation
    return client.post("/api/v1/passages", json=body,
                       headers=auth(seed["author"].xid))


class TestTheEvidenceIsRecorded:
    def test_creating_a_passage_writes_an_attestation_row(
            self, client, db, seed):
        response = _passage(client, seed)
        assert response.status_code == 201, response.text
        row = db.execute(text("""
            SELECT a.claim, a.statement_hash, a.user_id
            FROM content_attestations a
            JOIN passages p ON p.id = a.subject_id
            WHERE a.subject_type = 'passage' AND p.xid = CAST(:x AS uuid)
        """).bindparams(x=response.json()["xid"])).mappings().one()
        assert row["claim"] == "original"
        assert row["user_id"] == seed["author"].id
        # The HASH of the statement, not a boolean. What the uploader actually
        # affirmed is the part a court would ask about.
        assert len(row["statement_hash"]) == 64

    def test_a_passage_with_no_attestation_is_refused(self, client, seed):
        """Refused, not defaulted. An attestation that quietly becomes
        "original" manufactures a claim the uploader never made, which is the
        opposite of evidence — the same reasoning `media.validate` already
        applies to an upload."""
        response = _passage(client, seed, attestation=None)
        assert response.status_code == 422, response.text

    def test_a_licensed_passage_must_say_what_the_licence_is(self, client, seed):
        response = _passage(client, seed,
                            attestation={"claim": "licensed",
                                         "statement_version": "1"})
        assert response.status_code == 422, response.text
        assert "LICENCE_NOTE_REQUIRED" in response.text

    def test_an_invented_claim_is_refused(self, client, seed):
        response = _passage(client, seed,
                            attestation={"claim": "i_found_it",
                                         "statement_version": "1"})
        assert response.status_code == 422, response.text


class TestThePublishGateCanRefuse:
    @pytest.fixture
    def unattested(self, db, seed):
        """Strip the seed's attestation, explicitly.

        The fixture carries one because an ordinary centre's passage does. The
        case under test is the passage that does NOT — imported before this rule
        existed, or inserted straight into the database — and spelling that out
        here is better than a fixture that is quietly invalid for everyone else.
        """
        db.execute(text("""
            DELETE FROM content_attestations
            WHERE subject_type = 'passage' AND subject_id = (
                SELECT passage_id FROM passage_versions WHERE id = :v)
        """).bindparams(v=seed["passage_version"].id))
        db.flush()
        return seed

    def _publish(self, client, seed, who=None):
        return client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
            headers=auth(who or seed["author"].xid))

    @pytest.fixture
    def centre_admin(self, db, seed):
        """PUBLISH is {CENTRE_ADMIN, PLATFORM_ADMIN} — the seeded author is a
        teacher and would be refused for the wrong reason."""
        import datetime as dt
        import uuid as _uuid

        from app.modules.identity.models import OrgMembership, User

        boss = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}",
                    given_name="Gulnora", date_of_birth=dt.date(1980, 1, 1),
                    status="active")
        db.add(boss)
        db.flush()
        db.add(OrgMembership(org_id=seed["org"].id, user_id=boss.id,
                             role="centre_admin", status="active"))
        db.flush()
        return boss

    def test_a_passage_with_no_attestation_blocks_the_publish(
            self, client, seed, unattested, centre_admin):
        """Check 17, which had never fired for any test in the system."""
        response = self._publish(client, seed, who=centre_admin.xid)
        assert response.status_code == 422, response.text
        assert "ATTESTATION_MISSING" in response.text

    def test_with_the_attestation_that_check_passes(
            self, client, seed, centre_admin):
        response = self._publish(client, seed, who=centre_admin.xid)
        assert "ATTESTATION_MISSING" not in response.text

    def test_an_open_takedown_blocks_the_publish(
            self, client, db, seed, centre_admin):
        """Check 18. Filing soft-hides the subject, and the soft-hide stopped
        nothing at all: the material stayed publishable while a rights holder's
        claim sat unreviewed."""
        passage_id = db.scalar(text(
            "SELECT passage_id FROM passage_versions WHERE id = :v"
        ).bindparams(v=seed["passage_version"].id))
        db.execute(text("""
            INSERT INTO takedown_requests (claimant_name, claimant_email,
                                           rights_basis, sworn_statement,
                                           subject_type, subject_id, description,
                                           hidden_at)
            VALUES ('Cambridge University Press', 'rights@example.org',
                    'Copyright owner', true, 'passage', :s,
                    'Reading Passage 1 from Cambridge IELTS 17.', now())
        """).bindparams(s=passage_id))
        db.flush()
        response = self._publish(client, seed, who=centre_admin.xid)
        assert response.status_code == 422, response.text
        assert "TAKEDOWN_OPEN" in response.text

    def test_a_rejected_takedown_does_not_block_it(
            self, client, db, seed, centre_admin):
        """`hidden_at` stays set after a rejection — deleting the record would
        destroy the evidence — so the STATUS is the test, not the hide."""
        passage_id = db.scalar(text(
            "SELECT passage_id FROM passage_versions WHERE id = :v"
        ).bindparams(v=seed["passage_version"].id))
        db.execute(text("""
            INSERT INTO takedown_requests (claimant_name, claimant_email,
                                           rights_basis, sworn_statement,
                                           subject_type, subject_id, description,
                                           hidden_at, status)
            VALUES ('Confused Claimant', 'x@example.org', 'None, as it turns out',
                    true, 'passage', :s, 'Mistaken.', now(), 'rejected')
        """).bindparams(s=passage_id))
        db.flush()
        response = self._publish(client, seed, who=centre_admin.xid)
        assert "TAKEDOWN_OPEN" not in response.text

    def test_the_claim_follows_the_passage_not_the_version(
            self, client, db, seed, centre_admin):
        """Cutting a new version does not make somebody else's work yours, and
        it does not clear a claim against it either. The attestation is keyed on
        the passage for that reason, and this fails if it is ever keyed on the
        version instead."""
        assert db.scalar(text("""
            SELECT count(*) FROM content_attestations a
            JOIN passage_versions v ON v.passage_id = a.subject_id
            WHERE a.subject_type = 'passage' AND v.id = :v
        """).bindparams(v=seed["passage_version"].id)) == 1
        response = self._publish(client, seed, who=centre_admin.xid)
        assert "ATTESTATION_MISSING" not in response.text
