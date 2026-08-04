"""Billing as the console drives it: catalogue, order, order status, entitlements.

The exact sequence `features/billing/Billing.tsx` sends, in order, with the same
shapes — `GET /orgs`, `GET /products`, `POST /orders` with an `Idempotency-Key`,
`GET /orders/{xid}`, `GET /me/entitlements`.

Three things here are worth more than the rest:

  * **A price identifier is an integer, not a uuid.** The contract declares
    `Product.prices[].xid` and `OrderCreate.price_xid` as `format: uuid`; the
    handler selects `pr.id` and the request model is `price_xid: int`. The screen
    therefore forwards the catalogue's value untouched and never rebuilds it,
    which is only safe if it really does round-trip. Pinned below.

  * **Amounts stay integer tiyin end to end.** 100 tiyin = 1 so'm, and it is what
    both providers transact in — Payme refuses a transaction whose `amount` is
    not equal to `orders.amount_minor`. A float anywhere in the chain is money
    going missing, so the type of the JSON value is asserted, not just its size.

  * **Paying an order grants nothing.** Nothing in this application ever writes an
    `entitlements` row: Click's Complete and Payme's PerformTransaction both set
    `orders.status = 'paid'` and stop, and `products.features` — the column that
    says what a product grants — is read by no code at all. The screen's copy
    says a paid order and a live licence are two records because of the test at
    the bottom of this file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token
from app.modules.billing.entitlements import SEAT_BUNDLE

# A realistic Uzbek seat bundle: 1 200 000 so'm, which is 120 000 000 tiyin. Big
# enough that a wrong scale is obvious and that thousands grouping matters.
SEAT_PRICE = 120_000_000

CLICK_SECRET = "test-click-secret"
CLICK_SERVICE_ID = "12345"

# What the browser is on when it places the order.
RETURN_URL = "http://localhost:5173/billing"


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


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def _user(db, org_id, role, name):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    if org_id is not None:
        db.add(OrgMembership(org_id=org_id, user_id=user.id, role=role,
                             status="active"))
    db.flush()
    return user


@pytest.fixture
def admin(db, seed):
    """The centre admin buying seats for their own centre."""
    return _user(db, seed["org"].id, "centre_admin", "Rustam")


@pytest.fixture
def catalogue(db):
    """One sellable seat bundle, and one product whose only price is inactive.

    The second is the empty branch of the catalogue table: `GET /products`
    left-joins prices on `active`, so an active product with no live price comes
    back with `prices: []` and nothing to send as `price_xid`.
    """
    seats = db.scalar(text("""
        INSERT INTO products (code, kind, name, description)
        VALUES ('seat_licence', 'seat_licence', 'Centre seats',
                'One seat covers one student for mock papers.') RETURNING id
    """))
    price_id = db.scalar(text("""
        INSERT INTO prices (product_id, currency, amount_minor)
        VALUES (:p, 'UZS', :a) RETURNING id
    """).bindparams(p=seats, a=SEAT_PRICE))
    withdrawn = db.scalar(text("""
        INSERT INTO products (code, kind, name)
        VALUES ('old_plan', 'subscription', 'Last year''s plan') RETURNING id
    """))
    db.execute(text("""
        INSERT INTO prices (product_id, currency, amount_minor, active)
        VALUES (:p, 'UZS', 9_900_000, false)
    """).bindparams(p=withdrawn))
    db.flush()
    return {"price_id": price_id, "seats_product_id": seats}


def _seat_bundle(products: list[dict]) -> dict:
    bundle = next(p for p in products if p["code"] == "seat_licence")
    return bundle["prices"][0]


def place_order(client, headers, price_xid, *, org_xid, quantity=1,
                provider="click", key=None):
    """The body the screen sends, with the header the contract requires."""
    return client.post(
        "/api/v1/orders",
        headers={**headers, "Idempotency-Key": key or str(_uuid.uuid4())},
        json={"price_xid": price_xid, "quantity": quantity, "provider": provider,
              "org_xid": org_xid, "return_url": RETURN_URL})


class TestTheCatalogue:
    def test_it_lists_a_product_with_its_live_price(self, client, admin, catalogue):
        products = _ok(client.get("/api/v1/products", headers=auth(admin.xid)))
        bundle = next(p for p in products if p["code"] == "seat_licence")
        assert bundle["kind"] == "seat_licence"
        assert bundle["prices"] == [
            {"xid": catalogue["price_id"], "currency": "UZS",
             "amount_minor": SEAT_PRICE, "interval": None}]

    def test_the_price_is_an_integer_number_of_tiyin(self, client, admin, catalogue):
        """Not a float, not a decimal string. A decimal round trip through a
        provider is how money goes missing, and JSON hides the difference from
        anything that only compares the value."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        assert isinstance(price["amount_minor"], int)
        assert not isinstance(price["amount_minor"], bool)
        assert price["amount_minor"] == SEAT_PRICE

    def test_the_price_identifier_is_not_a_uuid_whatever_the_contract_says(
            self, client, admin, catalogue):
        """`Product.prices[].xid` is declared `format: uuid` and is the price
        row's integer id. The screen forwards it untouched for exactly this
        reason — anything that parsed or reformatted it would send a value the
        server cannot match to a row."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        assert isinstance(price["xid"], int)

    def test_a_product_whose_prices_are_all_withdrawn_still_lists(
            self, client, admin, catalogue):
        products = _ok(client.get("/api/v1/products", headers=auth(admin.xid)))
        old = next(p for p in products if p["code"] == "old_plan")
        assert old["prices"] == []

    def test_it_answers_without_a_token(self, client, catalogue):
        """`security: []` in the contract, and no principal dependency in the
        handler. Noted rather than relied on: the console only ever asks for it
        signed in, but a catalogue that needed a token could not be a pricing
        page."""
        assert client.get("/api/v1/products").status_code == 200


class TestBuyingSeatsForTheCentre:
    def test_the_sequence_the_screen_sends(self, client, db, seed, admin, catalogue):
        """Org, catalogue, order — the three calls in the order the screen makes
        them, with the catalogue's own price identifier going back out."""
        orgs = _ok(client.get("/api/v1/orgs?limit=25", headers=auth(admin.xid)))
        org_xid = orgs["items"][0]["xid"]
        assert org_xid == str(seed["org"].xid)

        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        created = _ok(place_order(client, auth(admin.xid), price["xid"],
                                  org_xid=org_xid, quantity=10), 201)

        order = created["order"]
        assert order["reference"].startswith("ORD-")
        assert order["status"] == "awaiting_payment"
        assert order["currency"] == "UZS"
        assert order["paid_at"] is None
        # Ten seats at 1 200 000 so'm. Integer arithmetic in tiyin, both sides.
        assert order["amount_minor"] == SEAT_PRICE * 10
        assert isinstance(order["amount_minor"], int)

    def test_the_redirect_names_the_provider_the_admin_chose(
            self, client, seed, admin, catalogue):
        """The screen labels the button with the provider's own name — Click or
        Payme — rather than a generic one, so the page it opens has to be
        theirs."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        for provider in ("click", "payme"):
            created = _ok(place_order(client, auth(admin.xid), price["xid"],
                                      org_xid=str(seed["org"].xid),
                                      provider=provider), 201)
            assert provider in created["redirect_url"]
            assert created["order"]["reference"] in created["redirect_url"]
            assert created["order"]["provider"] == provider

    def test_the_order_is_billed_to_the_centre_and_names_the_admin(
            self, client, db, seed, admin, catalogue):
        """A centre's invoice has to survive the admin who placed it leaving,
        which is why the screen requires a centre before the button is live —
        and it also has to say who placed it, which `user_id` was being nulled
        to avoid answering."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        _ok(place_order(client, auth(admin.xid), price["xid"],
                        org_xid=str(seed["org"].xid), quantity=10), 201)
        row = db.execute(text("SELECT user_id, org_id, quantity, amount_minor "
                              "FROM orders")).mappings().one()
        assert row["org_id"] == seed["org"].id
        assert row["user_id"] is not None, "the order must name who placed it"
        assert (row["quantity"], row["amount_minor"]) == (10, SEAT_PRICE * 10)

    def test_where_the_buyer_started_is_kept_on_the_order(
            self, client, db, seed, admin, catalogue):
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        _ok(place_order(client, auth(admin.xid), price["xid"],
                        org_xid=str(seed["org"].xid)), 201)
        assert db.scalar(text(
            "SELECT metadata->>'return_url' FROM orders")) == RETURN_URL

    def test_a_double_tapped_button_is_one_order(self, client, db, seed, admin,
                                                 catalogue):
        """The whole reason the contract makes `Idempotency-Key` required. The
        screen holds one key per distinct body until that order exists, so a
        second tap replays rather than buys again."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        first = _ok(place_order(client, auth(admin.xid), price["xid"],
                                org_xid=str(seed["org"].xid), quantity=10,
                                key="one-tap"), 201)
        second = _ok(place_order(client, auth(admin.xid), price["xid"],
                                 org_xid=str(seed["org"].xid), quantity=10,
                                 key="one-tap"), 201)
        assert first["order"]["reference"] == second["order"]["reference"]
        assert db.scalar(text("SELECT count(*) FROM orders")) == 1

    def test_the_same_key_with_a_different_quantity_is_refused(
            self, client, seed, admin, catalogue):
        """Which is why the key is keyed on the body: editing the quantity and
        pressing Buy again must not arrive under the key that bought the last
        one, or the admin is told nothing and charged for the wrong number."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        _ok(place_order(client, auth(admin.xid), price["xid"],
                        org_xid=str(seed["org"].xid), quantity=10,
                        key="one-tap"), 201)
        refused = place_order(client, auth(admin.xid), price["xid"],
                              org_xid=str(seed["org"].xid), quantity=20,
                              key="one-tap")
        assert refused.status_code == 409
        assert refused.json()["code"] == "idempotency_key_reused"

    def test_a_uuid_price_identifier_is_refused(self, client, seed, admin,
                                                catalogue):
        """What the contract says to send. It is a 422, so a screen that built
        the identifier from anything other than the catalogue response would
        fail on every purchase."""
        refused = place_order(client, auth(admin.xid), str(_uuid.uuid4()),
                              org_xid=str(seed["org"].xid))
        assert refused.status_code == 422

    def test_a_withdrawn_price_cannot_be_bought(self, client, db, seed, admin,
                                                catalogue):
        """The catalogue never offers it, but a screen left open across a price
        change would still be holding it."""
        withdrawn = db.scalar(text(
            "SELECT id FROM prices WHERE NOT active"))
        refused = place_order(client, auth(admin.xid), withdrawn,
                              org_xid=str(seed["org"].xid))
        assert refused.status_code == 404


class TestCheckingAnOrder:
    def test_reading_it_back_by_id(self, client, seed, admin, catalogue):
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        created = _ok(place_order(client, auth(admin.xid), price["xid"],
                                  org_xid=str(seed["org"].xid), quantity=10), 201)
        order = _ok(client.get(f"/api/v1/orders/{created['order']['xid']}",
                               headers=auth(admin.xid)))
        assert order["status"] == "awaiting_payment"
        assert order["reference"] == created["order"]["reference"]
        # Still tiyin, still an integer, on the way back as well as out.
        assert order["amount_minor"] == SEAT_PRICE * 10
        assert isinstance(order["amount_minor"], int)

    def test_an_order_belonging_to_another_centre_is_not_found(
            self, client, db, seed, admin, catalogue):
        """A competitor centre must not be able to read what this one is paying,
        and the answer is 404 rather than 403 — the existence of the order is
        itself commercial information."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        created = _ok(place_order(client, auth(admin.xid), price["xid"],
                                  org_xid=str(seed["org"].xid)), 201)
        outsider = _user(db, None, None, "Another centre's admin")
        refused = client.get(f"/api/v1/orders/{created['order']['xid']}",
                             headers=auth(outsider.xid))
        assert refused.status_code == 404

    def test_an_unknown_order_is_a_404(self, client, admin):
        assert client.get(f"/api/v1/orders/{_uuid.uuid4()}",
                          headers=auth(admin.xid)).status_code == 404

    def test_any_member_of_the_centre_can_read_its_orders(
            self, client, db, seed, admin, catalogue):
        """Recorded because it is wider than the neighbouring rule, not because
        it is right: `GET /orgs/{xid}/seats` requires MANAGE_ORG on the grounds
        that seat counts are commercial information about a school, while this
        endpoint accepts bare org membership. A student who has the order id can
        read what their centre paid."""
        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        created = _ok(place_order(client, auth(admin.xid), price["xid"],
                                  org_xid=str(seed["org"].xid)), 201)
        student = _user(db, seed["org"].id, "student", "Aziza")
        allowed = client.get(f"/api/v1/orders/{created['order']['xid']}",
                             headers=auth(student.xid))
        assert allowed.status_code == 200


@pytest.fixture
def licence(db, seed):
    """The centre's seat licence, for the feature the coverage gate reads."""
    from app.modules.billing.models import EntitlementRow

    row = EntitlementRow(
        subject_kind="org", subject_id=seed["org"].id,
        feature=sorted(SEAT_BUNDLE)[0], source_kind="seat", quantity=10,
        consumed=4, starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=300))
    db.add(row)
    db.flush()
    return row


class TestWhatTheCentreHolds:
    def test_nothing_bought_is_an_empty_list_not_an_error(self, client, admin):
        assert _ok(client.get("/api/v1/me/entitlements",
                              headers=auth(admin.xid))) == []

    def test_it_answers_what_the_centre_holds_not_only_the_admins_own(
            self, client, seed, admin, licence):
        """The question the screen exists to answer. `subject_kind = 'org'` rows
        for every organization the actor belongs to come back alongside their
        personal ones, so a centre admin does see their centre's licence."""
        rows = _ok(client.get("/api/v1/me/entitlements", headers=auth(admin.xid)))
        assert len(rows) == 1
        assert rows[0]["subject_kind"] == "org"
        assert rows[0]["source_kind"] == "seat"
        assert rows[0]["feature"] in SEAT_BUNDLE
        # Ten seats, four spent: the number the screen renders as "6 of 10".
        assert (rows[0]["quantity"], rows[0]["remaining"]) == (10, 6)

    def test_it_does_not_say_which_centre_holds_it(self, client, seed, admin,
                                                   licence):
        """So a person in two centres cannot be told whose licence this is. The
        screen says "a centre you belong to" rather than naming one, because
        naming one would be a guess."""
        rows = _ok(client.get("/api/v1/me/entitlements", headers=auth(admin.xid)))
        assert set(rows[0]) == {"feature", "subject_kind", "source_kind", "quantity",
                                "remaining", "starts_at", "expires_at"}

    def test_a_lapsed_licence_is_still_listed(self, client, db, seed, admin):
        """The handler filters on `revoked_at IS NULL` and nothing else — it does
        not go through `Entitlements.check()`, whatever the contract says. So the
        list is what the centre BOUGHT, and the screen works out from the dates
        what each row is worth today."""
        from app.modules.billing.models import EntitlementRow

        now = dt.datetime.now(dt.UTC)
        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature="org.assignments", source_kind="order",
                              starts_at=now - dt.timedelta(days=400),
                              expires_at=now - dt.timedelta(days=35)))
        db.flush()
        rows = _ok(client.get("/api/v1/me/entitlements", headers=auth(admin.xid)))
        assert len(rows) == 1
        assert dt.datetime.fromisoformat(rows[0]["expires_at"]) < now

    def test_an_exhausted_grant_is_listed_with_nothing_left(
            self, client, db, seed, admin):
        from app.modules.billing.models import EntitlementRow

        db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                              feature="competition.entry", source_kind="order",
                              quantity=4, consumed=4,
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.flush()
        rows = _ok(client.get("/api/v1/me/entitlements", headers=auth(admin.xid)))
        assert rows[0]["remaining"] == 0

    def test_a_revoked_licence_is_gone(self, client, db, seed, admin, licence):
        """A refund and a ban both need this, and it has to be immediate — an
        expiry date in the future is not what makes a grant live."""
        licence.revoked_at = dt.datetime.now(dt.UTC)
        licence.revoked_reason = "refunded"
        db.flush()
        assert _ok(client.get("/api/v1/me/entitlements",
                              headers=auth(admin.xid))) == []

    def test_another_centres_licence_is_not_listed(self, client, db, admin, licence):
        """The contractual promise. An admin at one centre must never see what a
        competitor holds."""
        from app.modules.identity.models import Organization

        rival = Organization(name="Rival Prep", slug=f"rp-{_uuid.uuid4().hex[:6]}",
                             status="active")
        db.add(rival)
        db.flush()
        stranger = _user(db, rival.id, "centre_admin", "Someone else")
        assert _ok(client.get("/api/v1/me/entitlements",
                              headers=auth(stranger.xid))) == []

    def test_an_unlimited_grant_reports_no_balance(self, client, db, seed, admin):
        """NULL quantity means unlimited, and the screen prints "unlimited"
        rather than a zero — which would read as a licence with nothing left."""
        from app.modules.billing.models import EntitlementRow

        db.add(EntitlementRow(subject_kind="user", subject_id=admin.id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.flush()
        rows = _ok(client.get("/api/v1/me/entitlements", headers=auth(admin.xid)))
        assert rows[0]["quantity"] is None and rows[0]["remaining"] is None
        assert rows[0]["subject_kind"] == "user"


def click_form(phase: str, reference: str, *, txn: str = "click-1",
               amount: str = "1200000.00") -> dict:
    """Click's documented concatenation, MD5-hashed. Their spec, not ours."""
    action = "0" if phase == "prepare" else "1"
    raw = (f"{txn}{CLICK_SERVICE_ID}{CLICK_SECRET}{reference}{amount}{action}"
           f"1700000000")
    return {"click_trans_id": txn, "service_id": CLICK_SERVICE_ID,
            "merchant_trans_id": reference, "merchant_prepare_id": "",
            "amount": amount, "action": action, "sign_time": "1700000000",
            "sign_string": hashlib.md5(raw.encode()).hexdigest()}  # noqa: S324


class TestPayingIsNotBeingLicensed:
    def test_a_paid_order_grants_nothing(self, client, db, seed, admin, catalogue,
                                         monkeypatch):
        """**Nothing in this application ever writes an `entitlements` row.**

        Click's Complete and Payme's PerformTransaction both set
        `orders.status = 'paid'` and stop; `products.features` — the column that
        records what a product grants, and the basis of "adding a plan is a row,
        not a code change" — is read by no code in `app/`. So a centre can buy
        seats, be marked paid, and hold nothing.

        This is why the screen says a paid order and a live licence are two
        records, and tells the admin to send the reference rather than buy it
        again. It is a backend gap, reported and not fixed here.
        """
        from app.platform.config import settings

        monkeypatch.setenv("CLICK_SECRET_KEY", CLICK_SECRET)
        monkeypatch.setenv("CLICK_SERVICE_ID", CLICK_SERVICE_ID)
        settings.cache_clear()

        price = _seat_bundle(_ok(client.get("/api/v1/products",
                                            headers=auth(admin.xid))))
        created = _ok(place_order(client, auth(admin.xid), price["xid"],
                                  org_xid=str(seed["org"].xid), quantity=10), 201)
        reference = created["order"]["reference"]

        assert _ok(client.post("/api/v1/payments/click/prepare",
                               data=click_form("prepare", reference)))["error"] == 0
        assert _ok(client.post("/api/v1/payments/click/complete",
                               data=click_form("complete", reference)))["error"] == 0

        paid = _ok(client.get(f"/api/v1/orders/{created['order']['xid']}",
                              headers=auth(admin.xid)))
        assert paid["status"] == "paid"
        assert paid["paid_at"] is not None

        assert _ok(client.get("/api/v1/me/entitlements",
                              headers=auth(admin.xid))) == []
        settings.cache_clear()
