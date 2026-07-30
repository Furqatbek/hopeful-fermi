"""The payment callbacks: Payme JSON-RPC and Click's two-phase form POST.

These were the largest untested block in `platform_ops.py` — 66 of its 179
uncovered lines — and they are the two endpoints in the system that turn an
unauthenticated HTTP request into an entitlement.

**`payme_rpc` had no authentication at all.** `payme_merchant_key` sat in the
config unread by anything, while the OpenAPI document declared
`security: [paymeBasic]`. Verified against a real database before the fix: an
unauthenticated `PerformTransaction` naming a known order reference returned
`state: 2` and left `orders.status = 'paid'`.

Two more holes sat behind it, both reachable once authentication is granted —
i.e. by the provider having a bad day, or by anyone who obtains the key:

  * `CreateTransaction` never checked the amount, so a transaction could be
    created for any sum against any order.
  * `PerformTransaction` captured with `UPDATE ... WHERE provider_txn_id = :txn`
    and then marked the order paid **whether or not that UPDATE matched
    anything** — a Perform naming a transaction that never existed granted the
    entitlement with no payment row behind it.

Click was in better shape: it verifies a signature and replays from
`payment_events`. It had no tests either.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.platform.config import settings

MERCHANT_KEY = "test-payme-merchant-key"
CLICK_SECRET = "test-click-secret"
SERVICE_ID = "12345"
AMOUNT = 5_000_000          # tiyin. 50,000 soum.


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("PAYME_MERCHANT_KEY", MERCHANT_KEY)
    monkeypatch.setenv("CLICK_SECRET_KEY", CLICK_SECRET)
    monkeypatch.setenv("CLICK_SERVICE_ID", SERVICE_ID)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def order(db):
    """An unpaid order. The thing an attacker wants marked paid."""
    user_id = db.scalar(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES ('+998901234599', 'Buyer', '1990-01-01', 'active') RETURNING id
    """))
    product_id = db.scalar(text("""
        INSERT INTO products (code, kind, name)
        VALUES ('mock_pack', 'one_off', 'Ten mock exams') RETURNING id
    """))
    price_id = db.scalar(text("""
        INSERT INTO prices (product_id, currency, amount_minor)
        VALUES (:p, 'UZS', :a) RETURNING id
    """).bindparams(p=product_id, a=AMOUNT))
    db.execute(text("""
        INSERT INTO orders (user_id, product_id, price_id, quantity, amount_minor,
                            currency, status, provider, reference)
        VALUES (:u, :p, :pr, 1, :a, 'UZS', 'awaiting_payment', 'payme', 'ORD-TEST-1')
    """).bindparams(u=user_id, p=product_id, pr=price_id, a=AMOUNT))
    db.flush()
    return "ORD-TEST-1"


def basic(key: str = MERCHANT_KEY) -> dict:
    token = base64.b64encode(f"Paycom:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def rpc(client, method: str, headers: dict | None = None, **params):
    return client.post("/api/v1/payments/payme",
                       json={"id": 42, "method": method, "params": params},
                       headers=headers if headers is not None else basic())


def status_of(db, reference: str = "ORD-TEST-1") -> str:
    db.expire_all()
    return db.scalar(text("SELECT status FROM orders WHERE reference = :r")
                     .bindparams(r=reference))


# ── the bypass ───────────────────────────────────────────────────────

class TestPaymeRequiresTheMerchantKey:
    """It required nothing. Anyone who could reach the endpoint and name an order
    reference could mark it paid and collect the entitlement."""

    def test_an_unauthenticated_call_is_refused(self, client, order, keys):
        response = rpc(client, "PerformTransaction", headers={},
                       id="attacker-1", account={"order": order})
        assert response.json()["error"]["code"] == -32504

    def test_and_the_order_is_untouched(self, client, db, order, keys):
        rpc(client, "PerformTransaction", headers={},
            id="attacker-1", account={"order": order})
        assert status_of(db) == "awaiting_payment"

    def test_a_wrong_key_is_refused(self, client, order, keys):
        response = rpc(client, "CheckPerformTransaction",
                       headers=basic("not-the-merchant-key"),
                       amount=AMOUNT, account={"order": order})
        assert response.json()["error"]["code"] == -32504

    def test_a_wrong_login_is_refused(self, client, order, keys):
        """Payme's login is the literal string `Paycom`. Accepting any login with
        the right password would let a compromised unrelated credential in."""
        token = base64.b64encode(f"admin:{MERCHANT_KEY}".encode()).decode()
        response = rpc(client, "CheckPerformTransaction",
                       headers={"Authorization": f"Basic {token}"},
                       amount=AMOUNT, account={"order": order})
        assert response.json()["error"]["code"] == -32504

    @pytest.mark.parametrize("header", [
        "Bearer abc", "Basic", "Basic !!!not-base64!!!", "Basic " + "z" * 8, "",
    ])
    def test_a_malformed_authorization_header_is_refused_not_a_500(
            self, client, order, keys, header):
        response = rpc(client, "CheckPerformTransaction",
                       headers={"Authorization": header},
                       amount=AMOUNT, account={"order": order})
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32504

    def test_it_refuses_when_no_merchant_key_is_configured(self, client, order,
                                                           monkeypatch):
        """The default used to be the string `dev-only-change-me`, published in
        this repository — so a deployment that had not overridden it accepted a
        Basic header anyone could compute. Empty now, and empty means refuse."""
        monkeypatch.setenv("PAYME_MERCHANT_KEY", "")
        settings.cache_clear()
        try:
            response = rpc(client, "CheckPerformTransaction", headers=basic(""),
                           amount=AMOUNT, account={"order": order})
            assert response.json()["error"]["code"] == -32504
        finally:
            settings.cache_clear()


class TestPaymeStateMachine:
    def test_check_allows_a_matching_amount(self, client, order, keys):
        response = rpc(client, "CheckPerformTransaction",
                       amount=AMOUNT, account={"order": order})
        assert response.json()["result"] == {"allow": True}

    def test_check_refuses_an_unknown_order(self, client, keys):
        response = rpc(client, "CheckPerformTransaction",
                       amount=AMOUNT, account={"order": "ORD-NOPE"})
        assert response.json()["error"]["code"] == -31050

    def test_check_refuses_a_wrong_amount(self, client, order, keys):
        response = rpc(client, "CheckPerformTransaction",
                       amount=100, account={"order": order})
        assert response.json()["error"]["code"] == -31001

    def test_create_refuses_a_wrong_amount_too(self, client, db, order, keys):
        """`CreateTransaction` did not check. The provider is supposed to call
        Check first, but "the other side always calls the methods in order" is an
        assumption, and the one that is wrong is the one that books a 50,000 soum
        pack for one soum."""
        response = rpc(client, "CreateTransaction", id="t1", amount=100,
                       account={"order": order})
        assert response.json()["error"]["code"] == -31001
        assert db.scalar(text("SELECT count(*) FROM payments")) == 0

    def test_create_refuses_an_unknown_order(self, client, keys):
        response = rpc(client, "CreateTransaction", id="t1", amount=AMOUNT,
                       account={"order": "ORD-NOPE"})
        assert response.json()["error"]["code"] == -31050

    def test_create_authorizes(self, client, db, order, keys):
        response = rpc(client, "CreateTransaction", id="t1", amount=AMOUNT,
                       account={"order": order})
        assert response.json()["result"]["state"] == 1
        assert db.scalar(text("SELECT state FROM payments WHERE provider_txn_id = 't1'")
                         ) == "authorized"

    def test_create_is_idempotent(self, client, db, order, keys):
        for _ in range(3):
            rpc(client, "CreateTransaction", id="t1", amount=AMOUNT,
                account={"order": order})
        assert db.scalar(text("SELECT count(*) FROM payments")) == 1

    def test_perform_captures_and_pays_the_order(self, client, db, order, keys):
        rpc(client, "CreateTransaction", id="t1", amount=AMOUNT, account={"order": order})
        response = rpc(client, "PerformTransaction", id="t1", account={"order": order})
        assert response.json()["result"]["state"] == 2
        assert status_of(db) == "paid"

    def test_perform_refuses_a_transaction_that_was_never_created(self, client, db,
                                                                  order, keys):
        """The second hole. The UPDATE matched nothing and the code marked the
        ORDER paid anyway — an entitlement with no payment row behind it, and
        nothing for reconciliation to find."""
        response = rpc(client, "PerformTransaction", id="never-created",
                       account={"order": order})
        assert response.json()["error"]["code"] == -31003
        assert status_of(db) == "awaiting_payment"

    def test_perform_is_idempotent(self, client, db, order, keys):
        """Webhooks arrive twice. The second Perform must not move `paid_at`."""
        rpc(client, "CreateTransaction", id="t1", amount=AMOUNT, account={"order": order})
        rpc(client, "PerformTransaction", id="t1", account={"order": order})
        first = db.scalar(text("SELECT paid_at FROM orders WHERE reference = 'ORD-TEST-1'"))
        rpc(client, "PerformTransaction", id="t1", account={"order": order})
        db.expire_all()
        assert db.scalar(text(
            "SELECT paid_at FROM orders WHERE reference = 'ORD-TEST-1'")) == first

    def test_cancel_records_the_reason(self, client, db, order, keys):
        rpc(client, "CreateTransaction", id="t1", amount=AMOUNT, account={"order": order})
        response = rpc(client, "CancelTransaction", id="t1", reason=5,
                       account={"order": order})
        assert response.json()["result"]["state"] == -1
        row = db.execute(text("""
            SELECT state, cancel_reason FROM payments WHERE provider_txn_id = 't1'
        """)).mappings().one()
        assert row["state"] == "cancelled" and row["cancel_reason"] == "5"

    def test_check_transaction_reports_state(self, client, order, keys):
        rpc(client, "CreateTransaction", id="t1", amount=AMOUNT, account={"order": order})
        assert rpc(client, "CheckTransaction", id="t1").json()["result"]["state"] == 1
        rpc(client, "PerformTransaction", id="t1", account={"order": order})
        assert rpc(client, "CheckTransaction", id="t1").json()["result"]["state"] == 2

    def test_check_transaction_on_an_unknown_id(self, client, keys):
        assert rpc(client, "CheckTransaction", id="nope").json()["error"]["code"] == -31003

    def test_get_statement_lists_captured_payments_only(self, client, order, keys):
        """The reconciliation pair: Payme asks what we think happened, and the
        daily job asks Payme the same in reverse."""
        rpc(client, "CreateTransaction", id="t1", amount=AMOUNT, account={"order": order})
        assert rpc(client, "GetStatement").json()["result"]["transactions"] == []
        rpc(client, "PerformTransaction", id="t1", account={"order": order})
        listed = rpc(client, "GetStatement").json()["result"]["transactions"]
        assert listed == [{"id": "t1", "amount": AMOUNT, "account": {"order": "ORD-TEST-1"}}]

    def test_an_unknown_method_is_a_named_error(self, client, keys):
        assert rpc(client, "DropTables").json()["error"]["code"] == -32601

    def test_the_rpc_id_is_echoed(self, client, order, keys):
        """Payme correlates by it. Losing it makes their retries unmatchable."""
        assert rpc(client, "CheckPerformTransaction", amount=AMOUNT,
                   account={"order": order}).json()["id"] == 42


# ── Click ────────────────────────────────────────────────────────────

def click_form(phase: str, *, txn: str = "click-1", reference: str = "ORD-TEST-1",
               amount: str = "50000.00", secret: str = CLICK_SECRET,
               prepare_id: str = "") -> dict:
    action = "0" if phase == "prepare" else "1"
    raw = (f"{txn}{SERVICE_ID}{secret}{reference}{prepare_id}{amount}{action}"
           f"1700000000")
    return {
        "click_trans_id": txn, "service_id": SERVICE_ID,
        "merchant_trans_id": reference, "merchant_prepare_id": prepare_id,
        "amount": amount, "action": action, "sign_time": "1700000000",
        "sign_string": hashlib.md5(raw.encode()).hexdigest(),  # noqa: S324 their spec
    }


class TestClickCallbacks:
    def test_a_valid_prepare_authorizes(self, client, db, order, keys):
        response = client.post("/api/v1/payments/click/prepare",
                               data=click_form("prepare"))
        assert response.json()["error"] == 0
        assert db.scalar(text(
            "SELECT state FROM payments WHERE provider = 'click'")) == "authorized"

    def test_a_valid_complete_captures_and_pays(self, client, db, order, keys):
        client.post("/api/v1/payments/click/prepare", data=click_form("prepare"))
        response = client.post("/api/v1/payments/click/complete",
                               data=click_form("complete"))
        assert response.json()["error"] == 0
        assert status_of(db) == "paid"

    def test_a_bad_signature_is_refused(self, client, db, order, keys):
        response = client.post("/api/v1/payments/click/prepare",
                               data=click_form("prepare", secret="wrong-secret"))
        assert response.json()["error"] == -1
        assert db.scalar(text("SELECT count(*) FROM payments")) == 0

    def test_it_refuses_when_no_click_secret_is_configured(self, client, db, order,
                                                           monkeypatch):
        """An unconfigured secret is not a weak secret, it is a published one:
        the signature becomes computable by anyone who has read the repository."""
        monkeypatch.setenv("CLICK_SECRET_KEY", "")
        monkeypatch.setenv("CLICK_SERVICE_ID", SERVICE_ID)
        settings.cache_clear()
        try:
            response = client.post("/api/v1/payments/click/prepare",
                                   data=click_form("prepare", secret=""))
            assert response.json()["error"] == -1
            assert db.scalar(text("SELECT count(*) FROM payments")) == 0
        finally:
            settings.cache_clear()

    def test_an_unknown_order_is_refused(self, client, keys):
        response = client.post("/api/v1/payments/click/prepare",
                               data=click_form("prepare", reference="ORD-NOPE"))
        assert response.json()["error"] == -5

    def test_a_replay_is_answered_from_the_stored_response(self, client, db, order,
                                                           keys):
        """Not re-executed. The recorded answer is returned verbatim, which is
        what stops a duplicate webhook from double-capturing."""
        first = client.post("/api/v1/payments/click/complete",
                            data=click_form("complete")).json()
        second = client.post("/api/v1/payments/click/complete",
                             data=click_form("complete")).json()
        assert first == second
        assert db.scalar(text("SELECT count(*) FROM payments")) == 1

    def test_every_callback_is_recorded_including_rejected_ones(self, client, db,
                                                                order, keys):
        """A provider dispute is settled by showing exactly what they sent and
        exactly what we answered — only possible if failures are stored too."""
        client.post("/api/v1/payments/click/prepare",
                    data=click_form("prepare", secret="wrong-secret"))
        row = db.execute(text("""
            SELECT signature_ok, result, response FROM payment_events
            WHERE provider = 'click'
        """)).mappings().one()
        assert row["signature_ok"] is False
        assert row["result"] == "signature_failed"
        assert row["response"]["error"] == -1

    def test_the_raw_payload_is_kept(self, client, db, order, keys):
        client.post("/api/v1/payments/click/prepare", data=click_form("prepare"))
        payload = db.scalar(text(
            "SELECT payload FROM payment_events WHERE provider = 'click'"))
        assert payload["click_trans_id"] == "click-1"
        assert payload["sign_string"]

    def test_a_callback_missing_its_transaction_id_is_a_422(self, client, keys):
        """`click_trans_id` is the only required field; without it there is
        nothing to be idempotent on."""
        assert client.post("/api/v1/payments/click/prepare",
                           data={"service_id": SERVICE_ID}).status_code == 422
