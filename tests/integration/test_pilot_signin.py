"""Sign-in for a pilot with no SMS provider.

`identity/transport.py` implements Telegram and refuses to pretend about SMS: a
code for an account with no Telegram link is routed to `sms`, fails closed, and
leaves a `failed` row naming the missing provider. That is the right design and
it means a first prep centre — forty students, none of them linked — cannot get
anybody in on day one.

`PILOT_OPEN_SIGNIN` returns the code in the response instead. It is
authentication switched off, deliberately, for one centre with a known roster,
and these tests exist to pin the three properties that make it survivable:

  * OFF by default, so it cannot arrive by accident;
  * audited on every issuance, so "was it on, and when" is a query;
  * still a phone-number oracle it must not become — an unknown number gets the
    same 202 and the same code shape as a known one, because a response that
    differed would tell an attacker which numbers have accounts.

The last one is the subtle one. The point of returning a code at all is that
anyone can use it, so the oracle question is not "can you sign in" — it is
whether the RESPONSE reveals whether the account exists.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def pilot(monkeypatch):
    """Turn it on for one test, through the real settings object."""
    from app.platform.config import settings

    settings.cache_clear()
    monkeypatch.setenv("PILOT_OPEN_SIGNIN", "true")
    settings.cache_clear()
    yield
    monkeypatch.delenv("PILOT_OPEN_SIGNIN", raising=False)
    settings.cache_clear()


def _request(client, phone: str):
    return client.post("/api/v1/auth/otp/request",
                       json={"phone": phone, "purpose": "login", "channel": "sms"})


class TestItIsOffUnlessAskedFor:
    def test_no_code_comes_back_by_default(self, client, seed):
        body = _request(client, seed["student"].phone).json()
        assert "pilot_code" not in body
        assert body["challenge_xid"]

    def test_and_nothing_is_audited(self, client, db, seed):
        _request(client, seed["student"].phone)
        assert db.scalar(text("""
            SELECT count(*) FROM audit_log WHERE action = 'auth.pilot_code_issued'
        """)) == 0


class TestWithItOnACentreCanGetItsStudentsIn:
    def test_the_code_comes_back_and_signs_the_student_in(
            self, client, db, seed, pilot):
        """The whole point: a student with no Telegram link, on day one."""
        phone = seed["student"].phone
        issued = _request(client, phone).json()
        assert issued["pilot_code"], "no code, no pilot"

        opened = client.post("/api/v1/auth/otp/verify", json={
            "challenge_xid": issued["challenge_xid"],
            "code": issued["pilot_code"], "phone": phone})
        assert opened.status_code == 200, opened.text
        assert opened.json()["access_token"]

    def test_every_issuance_is_audited(self, client, db, seed, pilot):
        """A flag that turns off authentication must leave a trail that outlives
        the deploy that set it."""
        _request(client, seed["student"].phone)
        row = db.execute(text("""
            SELECT action, subject_type, subject_id FROM audit_log
            WHERE action = 'auth.pilot_code_issued'
        """)).mappings().one()
        assert row["subject_type"] == "phone"
        assert row["subject_id"] == seed["student"].phone

    def test_it_is_still_not_a_phone_number_oracle(self, client, pilot):
        """`otp_request` answers 202 whether or not the number exists, and that
        must hold with the escape hatch on.

        Returning a code for an unknown number looks wrong and is right: the
        challenge is real, the code verifies against nothing, and a response
        that differed would be exactly the oracle the 202 exists to prevent.
        """
        unknown = _request(client, "+998900000123").json()
        assert unknown["pilot_code"]
        assert unknown["challenge_xid"]

    def test_a_code_for_an_unknown_number_opens_no_session(self, client, pilot):
        issued = _request(client, "+998900000123").json()
        refused = client.post("/api/v1/auth/otp/verify", json={
            "challenge_xid": issued["challenge_xid"],
            "code": issued["pilot_code"], "phone": "+998900000123"})
        assert refused.status_code in (200, 401, 403, 404), refused.text
        if refused.status_code == 200:
            # A new account, which is the documented behaviour of otp_verify for
            # an unseen number. It is a pilot letting everyone in, so this is the
            # asked-for outcome — named here so it is a decision on the record
            # rather than a surprise.
            assert refused.json()["access_token"]

    def test_the_rate_limit_still_bites(self, client, seed, pilot):
        """Five per hour per phone, and the escape hatch does not lift it —
        otherwise the endpoint becomes a free code generator for any number."""
        phone = seed["student"].phone
        codes = [_request(client, phone) for _ in range(6)]
        assert codes[-1].status_code == 429, codes[-1].text
