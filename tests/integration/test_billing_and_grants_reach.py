"""Three defects that all shared one shape: a row written and never read.

**Orders were billed to any organization.** `POST /orders` resolved `org_xid`
with no membership check, so any authenticated account could raise an order
against any centre. An xid matching nothing was worse than a refusal: `org_id`
stayed None and the order silently became a personal purchase — a bill the buyer
never agreed to rather than an error they could act on. And `user_id` was set to
None whenever an org was named, so a centre's order recorded no buyer at all.

**Paying granted nothing.** Both capture paths set `orders.status = 'paid'` and
stopped. No code in `app/` inserted an `entitlements` row, while migration 0012
builds `entitlements_source_idx` on `(source_kind, source_id)` — an index whose
only query is "what did this order grant". A centre could pay, watch the order
go green, and be refused every assignment with `no_seat`.

**`view` and `assign` grants reached nothing.** `filter_content` documents four
visibility routes and the fourth takes a `grant_ids` argument no caller passed.
The only permission any handler read was `copy`, for tests alone. So the
marketplace seam — "selling a test bank needs no schema change" — was a row that
listed correctly and did nothing.

**And then paying granted the WRONG THING.** `_grant_for_order` wrote
`source_kind = 'order'` for every purchase and never selected `products.kind`,
whose CHECK has allowed `seat_licence` since migration 0015. That one string is
the difference between a seat licence and an org-wide grant:
`Entitlements.check` meters an org entitlement per student only when it reads
`'seat'`. So a centre buying TEN seats entitled all four hundred of its
students, and the entire seat subsystem — `_seat_licence`, `assign_seats`,
`release_seat`, the seats screen — read a row nothing could ever write.
`entitlements.py` states the rule the code broke, in a comment two lines above
the branch: "without this, buying 10 seats would entitle a 400-student centre".

The fixture below has always sold a `seat_licence`, and the test asserted
`source_kind == 'order'` and passed.
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


def _user(db, name, *, org_id=None, role="student"):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, :n, DATE '1990-01-01', 'active') RETURNING id, xid
    """).bindparams(p=f"+9989{uuid.uuid4().int % 10**8:08d}", n=name)).mappings().one()
    if org_id:
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, :r, 'active')
        """).bindparams(o=org_id, u=row["id"], r=role))
    db.flush()
    return row


@pytest.fixture
def product(db):
    """A seat bundle, priced. `features` is jsonb because "adding a plan is a
    row, not a code change" — so what it grants comes from the row."""
    pid = db.scalar(text("""
        INSERT INTO products (code, kind, name, features, active)
        VALUES ('seats.term', 'seat_licence', 'Seat bundle',
                '["mock.unlimited"]'::jsonb, true)
        RETURNING id
    """))
    price = db.scalar(text("""
        INSERT INTO prices (product_id, currency, amount_minor, min_quantity, active)
        VALUES (:p, 'UZS', 4900000, 1, true) RETURNING id
    """).bindparams(p=pid))
    db.flush()
    return {"product_id": pid, "price_id": price}


class TestAnOrderNeedsAuthorityOverTheOrgItBills:
    def test_a_centre_admin_may_buy_for_their_centre(self, client, db, seed, product):
        boss = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        response = client.post("/api/v1/orders", headers=auth(boss["xid"]), json={
            "price_xid": product["price_id"], "quantity": 10,
            "provider": "click", "org_xid": str(seed["org"].xid)})
        assert response.status_code == 201, response.text

    def test_an_outsider_may_not(self, client, db, seed, product):
        """The bug. Any authenticated account could bill any centre."""
        outsider = _user(db, "Rival")
        response = client.post("/api/v1/orders", headers=auth(outsider["xid"]), json={
            "price_xid": product["price_id"], "quantity": 10,
            "provider": "click", "org_xid": str(seed["org"].xid)})
        assert response.status_code == 403, response.text

    def test_nor_may_a_student_of_that_centre(self, client, db, seed, product):
        """Membership is the wrong test. A student belongs to the centre and
        committing it to a seat bundle is not a student's decision."""
        response = client.post("/api/v1/orders",
                               headers=auth(seed["student"].xid), json={
            "price_xid": product["price_id"], "quantity": 10,
            "provider": "click", "org_xid": str(seed["org"].xid)})
        assert response.status_code == 403, response.text

    def test_an_unknown_org_is_refused_not_silently_billed_personally(
            self, client, db, seed, product):
        """It used to leave `org_id` None and charge the caller instead."""
        buyer = _user(db, "Anvar")
        response = client.post("/api/v1/orders", headers=auth(buyer["xid"]), json={
            "price_xid": product["price_id"], "quantity": 1,
            "provider": "click", "org_xid": str(uuid.uuid4())})
        assert response.status_code == 404, response.text
        assert db.scalar(text("SELECT count(*) FROM orders")) == 0

    def test_a_personal_order_still_works(self, client, db, product):
        buyer = _user(db, "Anvar")
        response = client.post("/api/v1/orders", headers=auth(buyer["xid"]), json={
            "price_xid": product["price_id"], "quantity": 1, "provider": "click"})
        assert response.status_code == 201, response.text

    def test_an_org_order_records_who_bought_it(self, client, db, seed, product):
        """`user_id` was None whenever an org was named, so "who committed us to
        this" — the first question asked about a bill — had no answer."""
        boss = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        client.post("/api/v1/orders", headers=auth(boss["xid"]), json={
            "price_xid": product["price_id"], "quantity": 10,
            "provider": "click", "org_xid": str(seed["org"].xid)})
        row = db.execute(text(
            "SELECT user_id, org_id FROM orders")).mappings().one()
        assert row["user_id"] == boss["id"]
        assert row["org_id"] == seed["org"].id


class TestPayingGrantsWhatWasBought:
    def _order(self, db, seed, product, *, org: bool, buyer):
        return db.execute(text("""
            INSERT INTO orders (user_id, org_id, product_id, price_id, quantity,
                                amount_minor, currency, status, provider, reference)
            VALUES (:u, :o, :p, :pr, 10, 49000000, 'UZS', 'awaiting_payment',
                    'click', :ref)
            RETURNING id
        """).bindparams(u=buyer["id"], o=seed["org"].id if org else None,
                        p=product["product_id"], pr=product["price_id"],
                        ref=f"ORD-{uuid.uuid4().hex[:12].upper()}")).mappings().one()

    def test_capture_writes_the_entitlement(self, db, seed, product):
        from app.api.routers.platform_ops import _grant_for_order

        buyer = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        order = self._order(db, seed, product, org=True, buyer=buyer)
        assert _grant_for_order(db, order["id"]) == 1

        row = db.execute(text("""
            SELECT subject_kind, subject_id, feature, source_kind, source_id, quantity
            FROM entitlements
        """)).mappings().one()
        assert row["subject_kind"] == "org"
        assert row["subject_id"] == seed["org"].id
        assert row["feature"] == "mock.unlimited"
        # `seat`, not `order`, and the fixture is why: the product is a
        # `seat_licence`. This assertion used to read `order` and passed, which
        # is the whole fourth defect written down below.
        assert row["source_kind"] == "seat"
        assert row["source_id"] == order["id"]
        assert row["quantity"] == 10

    def test_a_personal_order_grants_to_the_person(self, db, seed, product):
        from app.api.routers.platform_ops import _grant_for_order

        buyer = _user(db, "Anvar")
        order = self._order(db, seed, product, org=False, buyer=buyer)
        _grant_for_order(db, order["id"])
        row = db.execute(text(
            "SELECT subject_kind, subject_id FROM entitlements")).mappings().one()
        assert row["subject_kind"] == "user"
        assert row["subject_id"] == buyer["id"]

    def test_granting_twice_does_not_double_the_seats(self, db, seed, product):
        """Both providers can deliver a capture more than once: Click retries
        `complete`, and Payme calls `PerformTransaction` again for a transaction
        it has already performed. Granting twice is free seats."""
        from app.api.routers.platform_ops import _grant_for_order

        buyer = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        order = self._order(db, seed, product, org=True, buyer=buyer)
        assert _grant_for_order(db, order["id"]) == 1
        assert _grant_for_order(db, order["id"]) == 0
        assert db.scalar(text("SELECT count(*) FROM entitlements")) == 1

    def test_the_entitlement_actually_satisfies_the_coverage_gate(
            self, db, seed, product):
        """The point of the whole thing. `SEAT_BUNDLE` is what
        `teaching._require_covered` asks about, and a grant that does not
        satisfy it is a purchase that changes nothing."""
        from app.api.routers.platform_ops import _grant_for_order
        from app.modules.billing.entitlements import SEAT_BUNDLE

        buyer = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        order = self._order(db, seed, product, org=True, buyer=buyer)
        _grant_for_order(db, order["id"])
        feature = db.scalar(text("SELECT feature FROM entitlements"))
        assert feature in SEAT_BUNDLE

    def test_a_product_with_no_features_grants_nothing_and_says_so(
            self, db, seed, product):
        from app.api.routers.platform_ops import _grant_for_order

        db.execute(text("UPDATE products SET features = '[]'::jsonb WHERE id = :p")
                   .bindparams(p=product["product_id"]))
        buyer = _user(db, "Anvar")
        order = self._order(db, seed, product, org=False, buyer=buyer)
        db.flush()
        assert _grant_for_order(db, order["id"]) == 0


class TestWhatAPurchaseActuallyBuys:
    """`products.kind` and `products.features` were both selected for display and
    read by no decision.

    `kind` decided nothing at all, so every purchase — subscription, one-off,
    seat licence — produced the same org-wide `source_kind='order'` row. And
    `features` was passed through `str()`, so the shape migration 0015 writes
    down IN THE TABLE DEFINITION granted a feature literally named
    `{'feature': 'mock.unlimited'}`. Both are the same fault: a column the
    schema documents and the code never interprets.
    """

    def _product(self, db, kind, features, code=None):
        pid = db.scalar(text("""
            INSERT INTO products (code, kind, name, features, active)
            VALUES (:c, :k, 'A plan', CAST(:f AS jsonb), true) RETURNING id
        """).bindparams(c=code or f"p-{uuid.uuid4().hex[:8]}", k=kind, f=features))
        price = db.scalar(text("""
            INSERT INTO prices (product_id, currency, amount_minor, interval, active)
            VALUES (:p, 'UZS', 4900000, NULL, true) RETURNING id
        """).bindparams(p=pid))
        db.flush()
        return {"product_id": pid, "price_id": price}

    def _paid(self, db, product, buyer, *, org_id=None, quantity=1):
        from app.api.routers.platform_ops import _grant_for_order

        order = db.execute(text("""
            INSERT INTO orders (user_id, org_id, product_id, price_id, quantity,
                                amount_minor, currency, status, provider, reference)
            VALUES (:u, :o, :p, :pr, :q, 4900000, 'UZS', 'awaiting_payment',
                    'click', :ref) RETURNING id
        """).bindparams(u=buyer["id"], o=org_id, p=product["product_id"],
                        pr=product["price_id"], q=quantity,
                        ref=f"ORD-{uuid.uuid4().hex[:12].upper()}")).mappings().one()
        written = _grant_for_order(db, order["id"])
        return written, db.execute(text("""
            SELECT feature, source_kind, quantity FROM entitlements
            WHERE source_id = :o ORDER BY feature
        """).bindparams(o=order["id"])).mappings().all()

    def test_the_documented_feature_shape_grants_the_feature_it_names(
            self, db, seed):
        """The shape is in migration 0015's own comment. `str()` on that dict
        produces `{'feature': 'mock.unlimited'}` — a valid row, a green order,
        and a feature name `check()` can never match."""
        product = self._product(db, "one_off", """
            [{"feature": "mock.unlimited"}, {"feature": "competition.entry", "quantity": 4}]
        """)
        written, rows = self._paid(db, product, _user(db, "Anvar"), quantity=2)
        assert written == 2
        assert [r["feature"] for r in rows] == ["competition.entry", "mock.unlimited"]
        # Four entries per pack, two packs bought.
        assert [r["quantity"] for r in rows] == [8, 2]

    def test_a_seat_licence_is_seat_metered_and_a_one_off_is_not(self, db, seed):
        """The fault, stated as the pair it is. Identical features, identical
        quantity, and the only difference is the column that decided nothing."""
        buyer = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        seats = self._product(db, "seat_licence", '["mock.unlimited"]')
        one_off = self._product(db, "one_off", '["mock.unlimited"]')

        _, seat_rows = self._paid(db, seats, buyer, org_id=seed["org"].id, quantity=10)
        _, off_rows = self._paid(db, one_off, buyer, org_id=seed["org"].id, quantity=10)
        assert seat_rows[0]["source_kind"] == "seat"
        assert off_rows[0]["source_kind"] == "order"

    def test_a_subscription_is_bounded_by_its_period_not_by_a_count(self, db, seed):
        """`quantity` means "a consumable balance" — migration 0015 says so. A
        subscription's bound is its expiry, and writing `1` there made every
        monthly subscriber's `mock.unlimited` a single use, waiting for
        `Entitlements.consume` to acquire a caller and start refusing people who
        had paid."""
        product = self._product(db, "subscription", '["mock.unlimited"]')
        _, rows = self._paid(db, product, _user(db, "Anvar"))
        assert rows[0]["quantity"] is None

    def test_a_malformed_feature_entry_is_skipped_not_coerced(self, db, seed):
        """An entitlement for a feature nobody can spell is worse than none: the
        order still reads `paid` and the row still looks like proof of it."""
        product = self._product(db, "one_off",
                                '[{"name": "mock.unlimited"}, 7, null, {}, "  "]')
        written, rows = self._paid(db, product, _user(db, "Anvar"))
        assert written == 0
        assert rows == []

    def test_a_seat_purchase_satisfies_the_gate_only_for_a_seated_student(
            self, db, seed):
        """End to end, through the real rule. This is what a centre buys seats
        FOR, and until now buying them changed nothing about who was covered —
        in the generous direction, which is why nobody would have complained."""
        from app.api.deps import entitlements
        from app.modules.billing.models import SeatAssignment

        buyer = _user(db, "Gulnora", org_id=seed["org"].id, role="centre_admin")
        product = self._product(db, "seat_licence", '["mock.unlimited"]')
        self._paid(db, product, buyer, org_id=seed["org"].id, quantity=1)

        # Internal ids, not xids. `EntitlementStore`'s parameters are NAMED
        # `user_xid`/`org_xids` and every caller in `app/` passes `str(user.id)`
        # — `_EntitlementStore` does `int(user_xid)` on the way in. The adapter
        # is at least consistent with itself, so the names are a lie rather than
        # a bug, but they are the reason to pin this here rather than trust it.
        ents = entitlements(db)
        args = {"features": ["mock.unlimited"], "org_xids": [str(seed["org"].id)]}
        assert not ents.check_any(user_xid=str(seed["student"].id), **args).allowed

        entitlement_id = db.scalar(text("SELECT id FROM entitlements"))
        db.add(SeatAssignment(entitlement_id=entitlement_id,
                              user_id=seed["student"].id, assigned_by=buyer["id"]))
        db.flush()
        assert ents.check_any(user_xid=str(seed["student"].id), **args).allowed


class TestAViewGrantMakesContentVisible:
    @pytest.fixture
    def rival(self, db):
        org = db.execute(text("""
            INSERT INTO organizations (name, slug) VALUES ('Rival Centre', :s)
            RETURNING id, xid
        """).bindparams(s=f"rival-{uuid.uuid4().hex[:8]}")).mappings().one()
        admin = _user(db, "Rival Admin", org_id=org["id"], role="centre_admin")
        return {"org": org, "admin": admin}

    def _share(self, db, seed, rival, permission="view", subject="passage"):
        subject_id = (seed["passage_version"].passage_id if subject == "passage"
                      else seed["test"].id)
        db.execute(text("""
            INSERT INTO content_grants (subject_type, subject_id, grantee_kind,
                                        grantee_id, permission, granted_by)
            VALUES (:kind, :sid, 'org', :g, :perm, :by)
        """).bindparams(kind=subject, sid=subject_id, g=rival["org"]["id"],
                        perm=permission, by=seed["author"].id))
        db.flush()

    def test_without_a_grant_the_rival_sees_nothing(self, client, seed, rival):
        body = client.get("/api/v1/passages",
                          headers=auth(rival["admin"]["xid"])).json()
        assert body["items"] == []

    def test_a_view_grant_makes_it_visible(self, client, db, seed, rival):
        """The defect: this listed empty however many grants existed."""
        self._share(db, seed, rival)
        titles = [p["title"] for p in client.get(
            "/api/v1/passages", headers=auth(rival["admin"]["xid"])).json()["items"]]
        assert titles == ["Cartography"]

    def test_copy_implies_view(self, client, db, seed, rival):
        """A hierarchy, not a set. A centre that may take a copy may obviously
        look at it, and modelling that once is what stops three routers
        answering it differently."""
        self._share(db, seed, rival, permission="copy")
        assert client.get("/api/v1/passages",
                          headers=auth(rival["admin"]["xid"])).json()["items"]

    def test_a_revoked_grant_takes_it_away_again(self, client, db, seed, rival):
        self._share(db, seed, rival)
        db.execute(text("UPDATE content_grants SET revoked_at = now()"))
        db.flush()
        assert client.get("/api/v1/passages",
                          headers=auth(rival["admin"]["xid"])).json()["items"] == []

    def test_an_expired_grant_does_not_count(self, client, db, seed, rival):
        self._share(db, seed, rival)
        db.execute(text("UPDATE content_grants SET expires_at = now() - interval '1 day'"))
        db.flush()
        assert client.get("/api/v1/passages",
                          headers=auth(rival["admin"]["xid"])).json()["items"] == []

    def test_a_grant_on_a_test_does_not_leak_passages(self, client, db, seed, rival):
        """Grants are per subject. Sharing a paper is not sharing the library it
        was built from, and a subject_type mix-up would be exactly the leak the
        org-private default exists to prevent."""
        self._share(db, seed, rival, subject="test")
        assert client.get("/api/v1/passages",
                          headers=auth(rival["admin"]["xid"])).json()["items"] == []

    def test_the_owning_centre_is_unaffected(self, client, db, seed, rival):
        self._share(db, seed, rival)
        assert client.get("/api/v1/passages",
                          headers=auth(seed["author"].xid)).json()["items"]


def test_the_subject_vocabulary_matches_the_governance_router():
    """Two copies of the same vocabulary, in modules that must not import each
    other. Asserted equal so they cannot drift into a grant that lists but does
    not resolve."""
    from app.api.routers.platform_ops import _SUBJECT_TABLES
    from app.modules.authz.grants import SUBJECT_TABLES

    assert SUBJECT_TABLES == _SUBJECT_TABLES
