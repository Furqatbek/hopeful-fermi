"""Orders, seat licences, and the registry admin endpoints.

The last of `platform_ops`. Nothing here was a security hole on the scale of the
unauthenticated Payme callback next door (`test_payments.py`), but two of them
carry commercial promises worth pinning:

  * **A seat licence only covers users who hold a seat.** Ten seats must not
    entitle a four-hundred-student centre, and the check that stops it is a
    single `assigned + len(users) > quantity`.
  * **Seat information is centre-admin only**, not org-member. Who holds your
    centre's seats and how many remain is commercial information about that
    school.

The registry admin endpoints are the ones that make "adding a question type
needs no migration and no redeploy" true, which is the claim
`scripts/acceptance_new_question_type.py` verifies end to end and these verify at
the HTTP boundary.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token

SEAT_PRICE = 1_200_000


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
def centre_admin(db, seed):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES ('+998901000001', 'Centre Admin', '1985-01-01', 'active')
        RETURNING id, xid
    """)).mappings().one()
    db.execute(text("""
        INSERT INTO org_memberships (org_id, user_id, role, status)
        VALUES (:o, :u, 'centre_admin', 'active')
    """).bindparams(o=seed["org"].id, u=row["id"]))
    db.flush()
    return row


@pytest.fixture
def platform_admin(db):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES ('+998901000002', 'Platform Admin', '1985-01-01', 'active')
        RETURNING id, xid
    """)).mappings().one()
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return row


@pytest.fixture
def price(db):
    product_id = db.scalar(text("""
        INSERT INTO products (code, kind, name)
        VALUES ('seat_licence', 'seat_licence', 'Centre seats') RETURNING id
    """))
    price_id = db.scalar(text("""
        INSERT INTO prices (product_id, currency, amount_minor)
        VALUES (:p, 'UZS', :a) RETURNING id
    """).bindparams(p=product_id, a=SEAT_PRICE))
    db.flush()
    return price_id


class TestOrders:
    def test_creating_one_returns_a_reference_and_a_redirect(self, client, seed,
                                                             price):
        response = client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                               json={"price_xid": price, "quantity": 1,
                                     "provider": "payme"})
        assert response.status_code == 201
        order = response.json()["order"]
        assert order["reference"].startswith("ORD-")
        assert order["status"] == "awaiting_payment"
        assert response.json()["redirect_url"]

    def test_the_amount_is_quantity_times_price_in_tiyin(self, client, seed, price):
        """Tiyin throughout, because that is what Click and Payme transact in. A
        decimal round trip through a provider is how money goes missing."""
        response = client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                               json={"price_xid": price, "quantity": 10,
                                     "provider": "click"})
        assert response.json()["order"]["amount_minor"] == SEAT_PRICE * 10

    def test_an_unknown_price_is_a_404(self, client, seed):
        assert client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                           json={"price_xid": 999999, "quantity": 1,
                                 "provider": "payme"}).status_code == 404

    def test_an_inactive_price_cannot_be_bought(self, client, db, seed, price):
        db.execute(text("UPDATE prices SET active = false WHERE id = :p")
                   .bindparams(p=price))
        db.flush()
        assert client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                           json={"price_xid": price, "quantity": 1,
                                 "provider": "payme"}).status_code == 404

    def test_an_org_order_belongs_to_the_org_not_the_buyer(self, client, db, seed,
                                                           price, centre_admin):
        """A centre's invoice must survive the admin who placed it leaving."""
        client.post("/api/v1/orders", headers=auth(centre_admin["xid"]),
                    json={"price_xid": price, "quantity": 5, "provider": "payme",
                          "org_xid": str(seed["org"].xid)})
        row = db.execute(text("SELECT user_id, org_id FROM orders")).mappings().one()
        assert row["user_id"] is None
        assert row["org_id"] == seed["org"].id

    def test_a_replayed_request_returns_the_same_order(self, client, seed, price):
        """Idempotency-Key: a double-tapped Pay button must not create two
        orders, because the student will pay whichever one the app shows them."""
        headers = {**auth(seed["student"].xid), "Idempotency-Key": "buy-once"}
        body = {"price_xid": price, "quantity": 1, "provider": "payme"}
        first = client.post("/api/v1/orders", headers=headers, json=body).json()
        second = client.post("/api/v1/orders", headers=headers, json=body).json()
        assert first["order"]["reference"] == second["order"]["reference"]

    def test_without_the_header_two_calls_are_two_orders(self, client, seed, price):
        body = {"price_xid": price, "quantity": 1, "provider": "payme"}
        first = client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                            json=body).json()
        second = client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                             json=body).json()
        assert first["order"]["reference"] != second["order"]["reference"]

    def test_reading_one_back(self, client, seed, price):
        xid = client.post("/api/v1/orders", headers=auth(seed["student"].xid),
                          json={"price_xid": price, "quantity": 1,
                                "provider": "payme"}).json()["order"]["xid"]
        response = client.get(f"/api/v1/orders/{xid}", headers=auth(seed["student"].xid))
        assert response.status_code == 200
        assert response.json()["status"] == "awaiting_payment"

    def test_reading_an_unknown_order_is_a_404(self, client, seed):
        assert client.get(f"/api/v1/orders/{uuid.uuid4()}",
                          headers=auth(seed["student"].xid)).status_code == 404


@pytest.fixture
def seat_licence(db, seed):
    """Three seats for the centre."""
    db.execute(text("""
        INSERT INTO entitlements (subject_kind, subject_id, feature, source_kind,
                                  quantity)
        VALUES ('org', :o, 'mock_exams', 'seat', 3)
    """).bindparams(o=seed["org"].id))
    db.flush()


class TestSeats:
    def test_an_org_with_no_licence_reports_zero(self, client, seed, centre_admin):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/seats",
                              headers=auth(centre_admin["xid"]))
        assert response.json() == {"entitlement_xid": None, "total": 0, "assigned": 0,
                                   "remaining": 0, "expires_at": None, "members": []}

    def test_assigning_a_seat_consumes_one(self, client, seed, centre_admin,
                                           seat_licence):
        response = client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(seed["student"].xid)]})
        assert response.status_code == 200
        assert response.json()["assigned"] == 1
        assert response.json()["remaining"] == 2

    def test_the_holder_is_named(self, client, seed, centre_admin, seat_licence):
        response = client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(seed["student"].xid)]})
        assert [m["xid"] for m in response.json()["members"]] == [
            str(seed["student"].xid)]

    def test_assigning_the_same_person_twice_does_not_burn_two(
            self, client, seed, centre_admin, seat_licence):
        for _ in range(2):
            response = client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                                   headers=auth(centre_admin["xid"]),
                                   json={"user_xids": [str(seed["student"].xid)]})
        assert response.json()["assigned"] == 1

    def test_you_cannot_assign_more_seats_than_you_bought(
            self, client, db, seed, centre_admin, seat_licence):
        """The whole point of a seat licence. Without this, ten seats entitle a
        four-hundred-student centre."""
        extra = [str(db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES (:p, 'Student', '2005-01-01', 'active') RETURNING xid
        """).bindparams(p=f"+99890200{n:04d}")).scalar()) for n in range(4)]
        response = client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": extra})
        assert response.status_code == 409
        assert response.json()["code"] == "not_enough_seats"

    def test_and_nothing_is_assigned_when_it_refuses(self, client, db, seed,
                                                     centre_admin, seat_licence):
        extra = [str(db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES (:p, 'Student', '2005-01-01', 'active') RETURNING xid
        """).bindparams(p=f"+99890300{n:04d}")).scalar()) for n in range(4)]
        client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                    headers=auth(centre_admin["xid"]), json={"user_xids": extra})
        assert db.scalar(text("SELECT count(*) FROM seat_assignments")) == 0

    def test_assigning_against_no_licence_is_a_404(self, client, seed, centre_admin):
        response = client.post(f"/api/v1/orgs/{seed['org'].xid}/seats",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(seed["student"].xid)]})
        assert response.status_code == 404

    def test_a_student_cannot_read_the_centres_seats(self, client, seed,
                                                     seat_licence):
        """Org membership is not centre administration. How many seats remain is
        commercial information about their school."""
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/seats",
                              headers=auth(seed["student"].xid))
        assert response.status_code == 403

    def test_a_teacher_cannot_either(self, client, seed, seat_licence):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/seats",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 403

    def test_a_rival_centre_gets_a_404_not_a_403(self, client, db, seed,
                                                 seat_licence):
        """404, so the endpoint does not confirm the organization exists."""
        rival = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998904000001', 'Rival', '1985-01-01', 'active')
            RETURNING id, xid
        """)).mappings().one()
        org = db.execute(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival Centre', 'rival-centre', 'active') RETURNING id
        """)).scalar()
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, 'centre_admin', 'active')
        """).bindparams(o=org, u=rival["id"]))
        db.flush()
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/seats",
                              headers=auth(rival["xid"]))
        assert response.status_code == 404


NEW_TYPE = {
    "key": "true_false_not_given_v2", "version": 1, "status": "active",
    "title": "True / False / Not Given", "description": "Claim verification.",
    "skills": ["reading"],
    "payload_schema": {"type": "object", "properties": {"stem": {"type": "string"}}},
    "key_schema": {"type": "object"},
    "response_schema": {"type": "object"},
    # A real primitive from the closed set of three. Inventing a fourth is code
    # plus a deploy, which is exactly what the registry exists to avoid.
    "scoring": {"primitive": "choice_per_slot",
                "options": {"option_source": "payload.options",
                            "aggregate": "per_slot", "points_per_slot": 1}},
    "validation": {}, "authoring": {},
}


class TestRegistryAdmin:
    """"Adding a question type to a RUNNING production system, with no migration
    and no redeploy." """

    def test_an_admin_registers_a_new_type(self, client, platform_admin):
        response = client.post("/api/v1/admin/question-types",
                               headers=auth(platform_admin["xid"]), json=NEW_TYPE)
        assert response.status_code == 201
        assert response.json()["key"] == "true_false_not_given_v2"

    def test_it_is_readable_immediately_with_no_restart(self, client, platform_admin):
        client.post("/api/v1/admin/question-types",
                    headers=auth(platform_admin["xid"]), json=NEW_TYPE)
        response = client.get("/api/v1/question-types/true_false_not_given_v2/1")
        assert response.status_code == 200
        assert response.json()["title"] == "True / False / Not Given"

    def test_registering_the_same_version_twice_is_refused(self, client,
                                                           platform_admin):
        """"Definitions are never edited in place." A published question scored
        by v1 must keep meaning what it meant."""
        client.post("/api/v1/admin/question-types",
                    headers=auth(platform_admin["xid"]), json=NEW_TYPE)
        again = client.post("/api/v1/admin/question-types",
                            headers=auth(platform_admin["xid"]), json=NEW_TYPE)
        assert again.status_code == 409
        assert again.json()["code"] == "type_version_exists"

    def test_an_invalid_definition_reports_what_is_wrong(self, client,
                                                         platform_admin):
        broken = {**NEW_TYPE, "scoring": {"primitive": "telepathy"}}
        response = client.post("/api/v1/admin/question-types",
                               headers=auth(platform_admin["xid"]), json=broken)
        assert response.status_code == 422
        assert response.json()["findings"][0]["code"] == "DEFINITION_INVALID"

    def test_the_dry_run_validates_without_writing(self, client, db,
                                                   platform_admin):
        before = db.scalar(text("SELECT count(*) FROM question_type_defs"))
        response = client.post("/api/v1/admin/question-types/validate",
                               headers=auth(platform_admin["xid"]), json=NEW_TYPE)
        assert response.json()["passed"] is True
        assert db.scalar(text("SELECT count(*) FROM question_type_defs")) == before

    def test_the_dry_run_reports_a_bad_one(self, client, platform_admin):
        response = client.post("/api/v1/admin/question-types/validate",
                               headers=auth(platform_admin["xid"]),
                               json={**NEW_TYPE, "scoring": {"primitive": "nope"}})
        assert response.json()["passed"] is False

    def test_a_non_admin_cannot_register(self, client, seed):
        response = client.post("/api/v1/admin/question-types",
                               headers=auth(seed["author"].xid), json=NEW_TYPE)
        assert response.status_code == 403

    def test_reading_an_unregistered_type_is_a_404(self, client):
        assert client.get("/api/v1/question-types/not_a_type/1").status_code == 404

    def test_the_lexicon_lists_and_filters(self, client, db, platform_admin):
        """A missing UK/US pair is one row, not a deploy."""
        added = client.post("/api/v1/admin/lexicon", headers=auth(platform_admin["xid"]),
                            json={"kind": "spelling_variant", "a": "colour",
                                  "b": "color"})
        assert added.status_code == 201
        everything = client.get("/api/v1/admin/lexicon",
                                headers=auth(platform_admin["xid"])).json()
        variants = client.get("/api/v1/admin/lexicon?kind=spelling_variant",
                              headers=auth(platform_admin["xid"])).json()
        assert any(e["a"] == "colour" for e in variants)
        assert len(variants) < len(everything), "the filter did not narrow anything"

    def test_an_unknown_lexicon_kind_is_a_422_not_a_500(self, client,
                                                        platform_admin):
        """It was a 500. `body: dict` let an unrecognised `kind` reach the CHECK
        constraint, and the caller got "Something went wrong on our side" for
        typing `spelling` instead of `spelling_variant`. The aborted transaction
        then failed every later query on that session, so the real cause was
        several errors back by the time anyone looked."""
        response = client.post("/api/v1/admin/lexicon",
                               headers=auth(platform_admin["xid"]),
                               json={"kind": "spelling", "a": "colour", "b": "color"})
        assert response.status_code == 422
        assert response.json()["findings"][0]["path"] == "body.kind"

    def test_the_session_still_works_afterwards(self, client, platform_admin):
        """The half that made it hard to diagnose."""
        client.post("/api/v1/admin/lexicon", headers=auth(platform_admin["xid"]),
                    json={"kind": "spelling", "a": "colour", "b": "color"})
        assert client.get("/api/v1/admin/lexicon",
                          headers=auth(platform_admin["xid"])).status_code == 200

    def test_a_non_admin_cannot_read_the_lexicon(self, client, seed):
        assert client.get("/api/v1/admin/lexicon",
                          headers=auth(seed["author"].xid)).status_code == 403
