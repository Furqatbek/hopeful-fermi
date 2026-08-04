"""A minor cannot be stranger-matched without a parent having agreed.

The contract has said so since it was written: booking "refuses a minor booking
a public stranger-matched slot without a `stranger_matching` consent granted by
a parent — a general terms acceptance does not cover voice calls with strangers,
and a regulator will not read it that way." Nothing checked it.

`POST /me/consents` enforced the RECORDING side correctly — a minor's
`stranger_matching` consent is refused without `granted_by_kind: parent` and a
parent phone — so the evidence was being collected and never consulted. The same
defect shape as the lexicon and the registry: written by one side, read by
neither.

What already held, and still does: the adult/minor invariant. `_permitted_bands`
means a minor only ever sees and books a minor-banded slot, so "a minor matched
1:1 with an adult" was and is prevented server-side. This is the narrower case —
two minors, matched as strangers, with no parent in the loop.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token

MINOR = dt.date.today() - dt.timedelta(days=365 * 15)
ADULT = dt.date.today() - dt.timedelta(days=365 * 30)


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


def _user(db, name, dob):
    return db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, :n, CAST(:d AS date), 'active') RETURNING id, xid
    """).bindparams(p=f"+9989{uuid.uuid4().int % 10**8:08d}", n=name,
                    d=dob.isoformat())).mappings().one()


def _slot(db, seed, *, audience: str, age_band: str = "minor"):
    return db.execute(text("""
        INSERT INTO speaking_slots (org_id, starts_at, duration_minutes, capacity,
                                    status, audience, age_band, language, created_by)
        VALUES (:o, now() + interval '1 hour', 15, 8, 'scheduled', :aud, :band,
                'en', :by)
        RETURNING id, xid
    """).bindparams(o=seed["org"].id, aud=audience, band=age_band,
                    by=seed["author"].id)).mappings().one()


def _consent(db, user_id, *, by: str = "parent", revoked: bool = False):
    db.execute(text("""
        INSERT INTO consents (user_id, kind, doc_version, doc_hash, granted_by_kind,
                              parent_name, parent_phone, channel, revoked_at)
        VALUES (:u, 'stranger_matching', '1', repeat('c', 64), :by,
                'A Parent', '+998900000001', 'web',
                CASE WHEN :rev THEN now() ELSE NULL END)
    """).bindparams(u=user_id, by=by, rev=revoked))
    db.flush()


def _book(client, slot_xid, user_xid):
    return client.post(f"/api/v1/speaking/slots/{slot_xid}/book",
                       headers=auth(user_xid))


class TestAMinorNeedsAParentForStrangerMatching:
    def test_a_public_slot_is_refused_without_consent(self, client, db, seed):
        minor = _user(db, "Aziza", MINOR)
        slot = _slot(db, seed, audience="public")
        db.flush()

        response = _book(client, slot["xid"], minor["xid"])
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "parental_consent_required"

    def test_and_permitted_with_it(self, client, db, seed):
        minor = _user(db, "Aziza", MINOR)
        _consent(db, minor["id"])
        slot = _slot(db, seed, audience="public")
        db.flush()

        response = _book(client, slot["xid"], minor["xid"])
        assert response.status_code == 201, response.text

    def test_a_consent_the_student_granted_themselves_does_not_count(
            self, client, db, seed):
        """The whole point. `POST /me/consents` refuses to record this one for a
        minor, so it can only arrive by a direct insert or a change of birth
        date — and the check is on `granted_by_kind`, not on the row existing."""
        minor = _user(db, "Aziza", MINOR)
        _consent(db, minor["id"], by="self")
        slot = _slot(db, seed, audience="public")
        db.flush()

        response = _book(client, slot["xid"], minor["xid"])
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "parental_consent_required"

    def test_a_revoked_consent_does_not_count(self, client, db, seed):
        """A parent who withdraws must be able to. Nothing writes `revoked_at`
        today — there is no withdrawal endpoint, which is its own gap — but the
        read side must honour it the moment one exists, or the withdrawal would
        be recorded and ignored, which is this same bug again."""
        minor = _user(db, "Aziza", MINOR)
        _consent(db, minor["id"], revoked=True)
        slot = _slot(db, seed, audience="public")
        db.flush()

        response = _book(client, slot["xid"], minor["xid"])
        assert response.status_code == 403, response.text


class TestWhatItDoesNotChange:
    def test_a_cohort_slot_needs_no_parental_consent(self, client, db, seed):
        """A centre's own session is not stranger matching. The people in it are
        the student's classmates and a teacher, which is what the parent already
        agreed to by enrolling them."""
        minor = _user(db, "Aziza", MINOR)
        slot = _slot(db, seed, audience="cohort")
        db.flush()

        assert _book(client, slot["xid"], minor["xid"]).status_code == 201

    def test_an_org_slot_needs_no_parental_consent(self, client, db, seed):
        minor = _user(db, "Aziza", MINOR)
        slot = _slot(db, seed, audience="org")
        db.flush()

        assert _book(client, slot["xid"], minor["xid"]).status_code == 201

    def test_an_adult_needs_no_parental_consent(self, client, db, seed):
        adult = _user(db, "Bek", ADULT)
        slot = _slot(db, seed, audience="public", age_band="adult")
        db.flush()

        assert _book(client, slot["xid"], adult["xid"]).status_code == 201

    def test_the_age_band_is_still_refused_first(self, client, db, seed):
        """Order matters for what a child is told. A minor probing an ADULT
        public slot must get the child-safety answer, not a remark about
        paperwork — and must get it whether or not a consent exists."""
        minor = _user(db, "Aziza", MINOR)
        slot = _slot(db, seed, audience="public", age_band="adult")
        db.flush()

        response = _book(client, slot["xid"], minor["xid"])
        assert response.status_code == 403
        assert response.json()["code"] == "age_band_mismatch"

    def test_the_invariant_that_already_held_still_holds(self, client, db, seed):
        """The one the brief names: minors are never matched 1:1 with adults.
        Enforced at booking against the account's own date of birth, not
        anything the client sent, and a consent does not buy past it."""
        minor = _user(db, "Aziza", MINOR)
        _consent(db, minor["id"])
        adult_slot = _slot(db, seed, audience="public", age_band="adult")
        db.flush()

        response = _book(client, adult_slot["xid"], minor["xid"])
        assert response.status_code == 403
        assert response.json()["code"] == "age_band_mismatch"
