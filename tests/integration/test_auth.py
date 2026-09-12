"""The auth router: Telegram sign-in, SMS OTP, session rotation.

**None of this was tested.** 53 of 128 statements in `app/api/routers/auth.py`
were unexecuted — the whole of OTP request, OTP verify and refresh rotation, plus
the Telegram path. Writing the tests found two defects that a login endpoint
cannot have, both recorded below against the tests that pin them:

  * `POST /auth/telegram/verify {"contact_phone": "+998..."}` returned a working
    access token. `init_data` was optional; when absent, nothing was verified.
    Anyone who knew a phone number owned that account, minors included.
  * `max_attempts` could never fire, because `attempts` was never incremented —
    so an OTP challenge accepted unlimited guesses.

The refresh-rotation tests were not prompted by a defect; reuse detection is the
mechanism that turns a stolen refresh token from a 90-day credential into a
one-shot one, and it had no coverage either.

**Both of those guards were then found dead a second time, under the real
transaction boundary** — see `TestUnderTheRealUnitOfWork` at the end. Every
test above it runs the handler inside the suite's own session, which nothing
rolls back, so a write the handler makes and then raises past stays visible to
the assertion. In production `unit_of_work` rolls it back with the 4xx.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routers.auth import (
    OTP_PER_IP_PER_HOUR,
    OTP_PER_PHONE_PER_HOUR,
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
)
from app.platform.config import settings

BOT_TOKEN = "1234567:test-bot-token"
VICTIM = "+998901112233"
NEWCOMER = "+998907778899"


@pytest.fixture
def client(db):
    """The shared pattern: a TestClient whose request-scoped session is the
    test's own, so assertions see what the handler wrote without a commit race.

    What the override hides: the rollback. The real `deps.db` rolls the request
    back on any 4xx, and a handler that writes and then refuses keeps the write
    here and loses it there. `TestUnderTheRealUnitOfWork` below is where the
    attempt counter and the reuse revocation are proved to survive their own
    refusal; a test of either belongs there, not here.
    """
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def bot_token(monkeypatch):
    """Configure a bot token for the duration of a test.

    Without one the endpoint refuses outright — see
    `TestTelegramFailsClosed::test_it_refuses_when_no_bot_token_is_configured`.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    settings.cache_clear()
    yield BOT_TOKEN
    settings.cache_clear()


def init_data(telegram_id: int = 5551234, username: str = "aziza",
              auth_date: dt.datetime | None = None, token: str = BOT_TOKEN) -> str:
    """A correctly signed Telegram Mini App payload."""
    moment = auth_date or dt.datetime.now(dt.UTC)
    pairs = {
        "auth_date": str(int(moment.timestamp())),
        "query_id": "AAF",
        "user": json.dumps({"id": telegram_id, "first_name": "Aziza",
                            "username": username}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


@pytest.fixture
def victim(db):
    """An existing account. The one an attacker would want."""
    db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status, phone_verified_at)
        VALUES (:p, 'Victim', '1995-01-01', 'active', now())
    """).bindparams(p=VICTIM))
    db.flush()
    return VICTIM


def verify(client, **body):
    return client.post("/api/v1/auth/telegram/verify", json=body)


# ── the bypass ───────────────────────────────────────────────────────

class TestTelegramSignInIsNotABypass:
    """`telegram_verify` accepted `contact_phone` with no `init_data` at all and
    returned a session for whatever account used that number.

    Not a subtle branch — the main path. `if body.init_data:` made verification
    conditional on the caller choosing to be verified.
    """

    def test_a_bare_phone_number_is_refused(self, client, victim, bot_token):
        response = verify(client, contact_phone=victim)
        assert response.status_code == 403
        assert response.json()["code"] == "init_data_required"

    def test_a_bare_phone_number_mints_no_token(self, client, victim, bot_token):
        assert "access_token" not in verify(client, contact_phone=victim).json()

    def test_a_forged_signature_is_refused(self, client, bot_token):
        forged = init_data(token="not-the-bot-token")
        assert verify(client, init_data=forged).json()["code"] == "invalid_init_data"

    def test_tampering_with_the_signed_user_is_refused(self, client, bot_token):
        """The `user` field is inside the check string, so swapping the id
        invalidates the hash."""
        good = init_data(telegram_id=1)
        tampered = good.replace("%22id%22%3A1", "%22id%22%3A2")
        assert verify(client, init_data=tampered).status_code == 403

    def test_valid_telegram_plus_someone_elses_phone_does_not_take_over(
            self, client, victim, bot_token):
        """The second bypass, and the subtler one. `initData` does not carry a
        phone number — `contact_phone` comes from `requestContact` in the client,
        and a client can send anything. So a real Telegram account presenting a
        victim's number must not become that victim."""
        response = verify(client, init_data=init_data(telegram_id=999),
                          contact_phone=victim, date_of_birth="2000-01-01")
        assert response.status_code == 403
        assert response.json()["code"] == "phone_already_registered"

    def test_and_it_does_not_quietly_relink_the_account(self, client, db, victim,
                                                        bot_token):
        verify(client, init_data=init_data(telegram_id=999), contact_phone=victim)
        linked = db.scalar(text(
            "SELECT telegram_user_id FROM users WHERE phone = :p").bindparams(p=victim))
        assert linked is None


class TestTelegramFailsClosed:
    def test_it_refuses_when_no_bot_token_is_configured(self, client, monkeypatch):
        """An authentication path that degrades to "allow" when a secret is
        missing is worse than one that is switched off, because the missing
        secret is invisible until somebody goes looking."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        settings.cache_clear()
        try:
            response = verify(client, init_data=init_data())
            assert response.status_code == 403
            assert response.json()["code"] == "telegram_not_configured"
        finally:
            settings.cache_clear()

    def test_stale_init_data_is_refused(self, client, bot_token):
        """Telegram documents rejecting an old `auth_date`. Without it, one
        payload captured from a shared screen is a permanent credential."""
        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
        response = verify(client, init_data=init_data(auth_date=old))
        assert response.json()["code"] == "init_data_expired"

    def test_init_data_from_the_future_is_refused(self, client, bot_token):
        ahead = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        assert verify(client, init_data=init_data(auth_date=ahead)).status_code == 403

    def test_a_payload_with_no_auth_date_is_refused(self, client, bot_token):
        """No `auth_date` means no replay window, so there is nothing to expire.
        Signed-but-undateable has to be refused rather than treated as fresh."""
        pairs = {"user": json.dumps({"id": 7}), "query_id": "AAF"}
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        response = verify(client, init_data=urlencode(pairs))
        assert response.json()["code"] == "invalid_init_data"

    def test_a_payload_with_no_user_is_refused(self, client, bot_token):
        pairs = {"auth_date": str(int(dt.datetime.now(dt.UTC).timestamp()))}
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        assert verify(client, init_data=urlencode(pairs)).status_code == 403


class TestTelegramRegistrationAndSignIn:
    def test_a_new_telegram_user_registers(self, client, db, bot_token):
        response = verify(client, init_data=init_data(telegram_id=4242),
                          contact_phone=NEWCOMER, date_of_birth="2004-05-05",
                          given_name="Bekzod")
        assert response.status_code == 200
        assert response.json()["principal"]["user"]["given_name"] == "Bekzod"

    def test_registration_requires_a_date_of_birth(self, client, bot_token):
        """The 18 boundary drives the matching safety rule: minors are never
        paired 1:1 with adults. A nullable DOB makes that unenforceable."""
        response = verify(client, init_data=init_data(telegram_id=4243),
                          contact_phone=NEWCOMER)
        assert response.json()["code"] == "date_of_birth_required"

    def test_the_phone_is_stored_unverified(self, client, db, bot_token):
        """Telegram vouched for the Telegram account, not for the number. Marking
        it verified would let the OTP path treat an unproven number as proven."""
        verify(client, init_data=init_data(telegram_id=4244), contact_phone=NEWCOMER,
               date_of_birth="2004-05-05")
        row = db.execute(text("""
            SELECT phone_verified_at, telegram_user_id FROM users WHERE phone = :p
        """).bindparams(p=NEWCOMER)).mappings().one()
        assert row["phone_verified_at"] is None
        assert row["telegram_user_id"] == 4244

    def test_a_returning_user_signs_in_by_telegram_id_alone(self, client, bot_token):
        verify(client, init_data=init_data(telegram_id=4245), contact_phone=NEWCOMER,
               date_of_birth="2004-05-05")
        again = verify(client, init_data=init_data(telegram_id=4245))
        assert again.status_code == 200
        assert again.json()["access_token"]

    def test_an_unknown_telegram_account_with_no_phone_is_refused(self, client,
                                                                   bot_token):
        """Signed in as a real Telegram user we have never seen, and no number
        offered. There is nothing to create an account from."""
        response = verify(client, init_data=init_data(telegram_id=777777))
        assert response.status_code == 403
        assert response.json()["code"] == "no_phone"

    def test_a_changed_username_is_picked_up(self, client, db, bot_token):
        verify(client, init_data=init_data(telegram_id=4246, username="old"),
               contact_phone=NEWCOMER, date_of_birth="2004-05-05")
        verify(client, init_data=init_data(telegram_id=4246, username="new"))
        assert db.scalar(text("""
            SELECT telegram_username FROM users WHERE telegram_user_id = 4246
        """)) == "new"


# ── OTP ──────────────────────────────────────────────────────────────

def request_code(client, phone: str = VICTIM):
    return client.post("/api/v1/auth/otp/request", json={"phone": phone})


def code_for(db, challenge_xid: str) -> str:
    """Brute-force the 6-digit code from its stored hash.

    The plaintext is never persisted — that is the point of `code_hash` — so a
    test that needs to enter the right code has to reverse it. A million hashes
    is under a second, which is also a fair statement of what the attempt limit
    is protecting against.
    """
    stored = db.scalar(text(
        "SELECT code_hash FROM otp_challenges WHERE xid = CAST(:x AS uuid)"
    ).bindparams(x=challenge_xid))
    for n in range(1_000_000):
        candidate = f"{n:06d}"
        if hashlib.sha256(f"{challenge_xid}:{candidate}".encode()).hexdigest() == stored:
            return candidate
    raise AssertionError("no code matched the stored hash")


class TestOtpRequest:
    def test_it_returns_202_and_a_challenge(self, client):
        response = request_code(client)
        assert response.status_code == 202
        assert response.json()["challenge_xid"]

    def test_it_returns_202_for_a_number_with_no_account(self, client):
        """Anything else turns the endpoint into a phone-number oracle: try a
        number, learn whether it belongs to a student at your competitor."""
        assert request_code(client, "+998900000000").status_code == 202

    def test_the_plaintext_code_is_never_stored(self, client, db):
        xid = request_code(client).json()["challenge_xid"]
        row = db.execute(text("""
            SELECT * FROM otp_challenges WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=xid)).mappings().one()
        assert len(row["code_hash"]) == 64
        assert not any(isinstance(v, str) and v.isdigit() and len(v) == 6
                       for v in row.values())

    def test_the_challenge_is_addressable(self, client, db):
        """It was not. The xid went into the code hash and nowhere else, so the
        server could not find its own challenge without knowing the code — which
        is why the attempt counter could not be incremented."""
        xid = request_code(client).json()["challenge_xid"]
        assert db.scalar(text("""
            SELECT count(*) FROM otp_challenges WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=xid)) == 1

    def test_a_sixth_code_in_an_hour_is_refused(self, client):
        """SMS is the only user-linear line on the infrastructure bill, so this
        is a budget control before it is an abuse control.

        **429, and this asserted 400.** The contract has declared
        `'429': RateLimited` on this operation since it was drafted; the handler
        raised a bare `DomainError`, whose status is 400. So the test agreed with
        the code and both disagreed with the contract — and 400 is the one answer
        that makes a well-behaved client stop retrying for good, because it means
        "your request is wrong" rather than "come back later".
        """
        for _ in range(5):
            assert request_code(client).status_code == 202
        refused = request_code(client)
        assert refused.status_code == 429
        assert refused.json()["code"] == "rate_limited"

    def test_and_it_says_when_to_come_back(self, client):
        """`Retry-After` in the header, not just the body. Generic client
        middleware and every proxy in between read the header; none of them parse
        a problem document. It counts down to when the OLDEST of the five ages
        out of the hour, which is when a sixth actually becomes available."""
        for _ in range(5):
            request_code(client)
        refused = request_code(client)
        assert 0 < int(refused.headers["Retry-After"]) <= 3600
        assert refused.json()["retry_after"] == int(refused.headers["Retry-After"])

    def test_the_limit_is_per_number(self, client):
        for _ in range(5):
            request_code(client)
        assert request_code(client, "+998905554433").status_code == 202

    def test_a_malformed_number_is_rejected_before_anything_is_spent(self, client):
        for bad in ("998901234567", "+7901234567", "+99890123456", "", "+998abcdefghi"):
            assert client.post("/api/v1/auth/otp/request",
                               json={"phone": bad}).status_code == 422

    def test_a_purpose_outside_the_contract_is_a_422(self, client, db):
        """`purpose: str` carried `admin` straight into `otp_challenges`, whose
        CHECK answered with a 500 — after the code had been generated. A 422
        with the field named, and nothing written, is what a wrong enum value
        earns; the same for `channel`."""
        refused = client.post("/api/v1/auth/otp/request",
                              json={"phone": VICTIM, "purpose": "admin"})
        assert refused.status_code == 422, refused.text
        assert refused.json()["findings"][0]["code"] == "REQUEST_INVALID"
        assert client.post("/api/v1/auth/otp/request",
                           json={"phone": VICTIM, "channel": "carrier_pigeon"}
                           ).status_code == 422
        assert db.scalar(text("SELECT count(*) FROM otp_challenges")) == 0


@pytest.fixture
def addressed(db):
    """A client factory whose requests arrive FROM a given address.

    The default `TestClient` presents `"testclient"`, which `_as_inet` drops, so
    every test above runs with no address at all and the per-IP count is never
    consulted — which is what let it not exist. Starlette's `client=` is the
    `(host, port)` the ASGI scope carries, i.e. what `request.client.host` reads.
    """
    from app.api import deps
    from app.api.main import create_app

    opened = []

    def make(host: str) -> TestClient:
        app = create_app()
        app.dependency_overrides[deps.db] = lambda: db
        c = TestClient(app, raise_server_exceptions=False, client=(host, 1))
        opened.append(c.__enter__())
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


class TestTheAddressBudget:
    """ADR-0001 §5.2: "hard rate limits per phone and per IP". The contract says
    it, the compose file's `--forwarded-allow-ips` comment says it, migration
    0003 built `otp_challenges_ip_idx` for it — and `otp_request` counted only
    the phone. One address could ask for a code to every registered number, five
    times an hour each, and the per-phone budget would agree to every one.

    The ceiling is generous on purpose: forty students in one classroom behind
    one NAT request pilot codes in the same hour, and a refused student is the
    failure `api/limits.py` says is worse than any abuse.
    """

    ATTACKER = "203.0.113.9"
    CLASSROOM = "198.51.100.7"

    @staticmethod
    def _phone(n: int) -> str:
        return f"+9989000{n:05d}"

    def test_the_hundred_and_first_number_from_one_address_is_refused(self, addressed):
        client = addressed(self.ATTACKER)
        for n in range(OTP_PER_IP_PER_HOUR):
            assert request_code(client, self._phone(n)).status_code == 202, n
        refused = request_code(client, self._phone(OTP_PER_IP_PER_HOUR))
        assert refused.status_code == 429
        assert refused.json()["code"] == "rate_limited"
        # The same shape as the per-phone refusal: a header any proxy or client
        # middleware reads, counting down to when the oldest send ages out.
        assert 0 < int(refused.headers["Retry-After"]) <= 3600

    def test_and_another_address_is_unaffected(self, addressed):
        """Per address, not global — the misconfigured-proxy failure the compose
        comment names, where every request appears to come from Caddy, is a
        different bug and would show up here as a 429 for the classroom."""
        attacker, classroom = addressed(self.ATTACKER), addressed(self.CLASSROOM)
        for n in range(OTP_PER_IP_PER_HOUR):
            request_code(attacker, self._phone(n))
        assert request_code(attacker, self._phone(OTP_PER_IP_PER_HOUR)).status_code == 429
        assert request_code(classroom, self._phone(OTP_PER_IP_PER_HOUR)).status_code == 202

    def test_a_classroom_is_never_refused(self, addressed):
        """The sizing claim, pinned: forty students, one NAT, one hour."""
        classroom = addressed(self.CLASSROOM)
        for n in range(40):
            assert request_code(classroom, self._phone(n)).status_code == 202

    def test_the_phone_budget_still_fires_first(self, addressed):
        """The two are independent AND-gates: a sixth code for one number is
        refused long before the address has spent anything like its allowance."""
        client = addressed(self.CLASSROOM)
        for _ in range(OTP_PER_PHONE_PER_HOUR):
            assert request_code(client).status_code == 202
        assert request_code(client).status_code == 429
        assert request_code(client, self._phone(1)).status_code == 202

    def test_an_address_nobody_can_attribute_is_not_counted(self, client, db):
        """`"testclient"` does not parse, and a proxy that puts junk in the
        header is the same case. The per-phone budget holds; refusing on an
        address nobody can name would refuse everyone behind that proxy at once.
        Mirrors the INSERT's own call about the column."""
        for n in range(OTP_PER_IP_PER_HOUR + 1):
            assert request_code(client, self._phone(n)).status_code == 202
        assert db.scalar(text(
            "SELECT count(*) FROM otp_challenges WHERE request_ip IS NULL")) == \
            OTP_PER_IP_PER_HOUR + 1


class TestTheRequestAddressIsContextNotThePoint:
    def test_an_unparseable_client_address_does_not_lose_the_challenge(self, client, db):
        """`request_ip` is `inet`, and the address can be `"testclient"` here or
        anything a proxy puts in a header in production. Same failure as
        `content_attestations.ip` in `0009` §6.4: the insert died and took the
        row with it. The challenge is the point; the address is context."""
        from app.api.routers.auth import _as_inet

        assert _as_inet("testclient") is None
        assert _as_inet("") is None
        assert _as_inet(None) is None
        assert _as_inet("203.0.113.7") == "203.0.113.7"

        response = request_code(client)
        assert response.status_code == 202
        assert db.scalar(text("""
            SELECT count(*) FROM otp_challenges WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=response.json()["challenge_xid"])) == 1


class TestOtpVerify:
    def test_the_right_code_opens_a_session(self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": xid, "code": code_for(db, xid)})
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_a_wrong_code_is_refused(self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        wrong = f"{(int(code_for(db, xid)) + 1) % 1_000_000:06d}"
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": xid, "code": wrong})
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_code"

    def test_a_wrong_code_costs_an_attempt(self, client, db, victim):
        """The defect. `attempts` was never incremented anywhere, so the
        `max_attempts` check below it could not fire and a challenge accepted
        unlimited guesses."""
        xid = request_code(client).json()["challenge_xid"]
        before = db.scalar(text(
            "SELECT attempts FROM otp_challenges WHERE xid = CAST(:x AS uuid)"
        ).bindparams(x=xid))
        client.post("/api/v1/auth/otp/verify",
                    json={"challenge_xid": xid, "code": "000000"})
        db.expire_all()
        after = db.scalar(text(
            "SELECT attempts FROM otp_challenges WHERE xid = CAST(:x AS uuid)"
        ).bindparams(x=xid))
        assert after == before + 1

    def test_the_challenge_dies_after_five_wrong_guesses(self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        right = code_for(db, xid)
        for n in range(5):
            client.post("/api/v1/auth/otp/verify",
                        json={"challenge_xid": xid, "code": f"{n:06d}" if
                              f"{n:06d}" != right else "999999"})
        # Even the CORRECT code is refused now: the budget was spent guessing.
        exhausted = client.post("/api/v1/auth/otp/verify",
                                json={"challenge_xid": xid, "code": right})
        assert exhausted.status_code == 410

    def test_a_code_cannot_be_used_twice(self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        code = code_for(db, xid)
        assert client.post("/api/v1/auth/otp/verify",
                           json={"challenge_xid": xid, "code": code}).status_code == 200
        again = client.post("/api/v1/auth/otp/verify",
                            json={"challenge_xid": xid, "code": code})
        assert again.status_code == 401

    def test_an_expired_code_is_refused(self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        code = code_for(db, xid)
        db.execute(text("""
            UPDATE otp_challenges SET expires_at = now() - interval '1 minute'
            WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=xid))
        db.flush()
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": xid, "code": code})
        assert response.status_code == 410

    def test_an_unknown_challenge_is_refused(self, client, victim):
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": "0198f4f0-0000-7000-8000-000000000000",
                                     "code": "123456"})
        assert response.status_code == 401

    def test_a_code_for_a_number_with_no_account_is_a_404(self, client, db):
        xid = request_code(client, "+998900000000").json()["challenge_xid"]
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": xid, "code": code_for(db, xid)})
        assert response.status_code == 404

    def test_a_malformed_code_is_rejected_by_the_schema(self, client):
        for bad in ("12345", "1234567", "abcdef", ""):
            assert client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": "x", "code": bad}).status_code == 422


# ── sessions ─────────────────────────────────────────────────────────

def sign_in(client, db, phone: str = VICTIM) -> dict:
    """Sign in and return the response BODY.

    The body no longer carries the refresh token — that arrives in the
    `ielts_refresh` cookie, which the client's jar picks up by itself. Use
    `held_token` to read what the browser ended up holding.
    """
    xid = request_code(client, phone).json()["challenge_xid"]
    return client.post("/api/v1/auth/otp/verify",
                       json={"challenge_xid": xid, "code": code_for(db, xid)}).json()


def held_token(client) -> str:
    """The refresh token the browser is holding, read out of its cookie jar."""
    return client.cookies.get(REFRESH_COOKIE)


def present(client, token: str):
    """`POST /auth/refresh` presenting a SPECIFIC token.

    A real browser cannot do this once rotation has overwritten its cookie — but
    a thief holding a stolen copy can, and that is what every reuse test below is
    about. Writing the jar entry directly is how the test plays the thief.
    """
    client.cookies.set(REFRESH_COOKIE, token, path=REFRESH_COOKIE_PATH)
    return client.post("/api/v1/auth/refresh")


class TestTheRefreshTokenIsNotReachableByScript:
    """ADR-0002 §6: the refresh token moved out of the response body into an
    httpOnly cookie, so an XSS steals at most a 15-minute access token rather
    than a 90-day session.

    These assert the PROPERTY, not the plumbing. Without them a later refactor
    could put the token back in the body and every other test in this file would
    still pass — which is precisely how the thing a change was made for gets
    quietly undone.
    """

    def test_the_body_does_not_contain_the_refresh_token(self, client, db, victim):
        body = sign_in(client, db)
        assert "refresh_token" not in body
        assert sorted(body) == ["access_token", "expires_in", "principal"]
        # Nor anywhere else in the payload under a different name.
        assert held_token(client) not in json.dumps(body)

    def test_the_cookie_is_httponly_scoped_and_samesite(self, client, db, victim):
        xid = request_code(client, VICTIM).json()["challenge_xid"]
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": xid, "code": code_for(db, xid)})
        header = response.headers["set-cookie"]
        # The one that matters: it is what makes the token unreadable from
        # `document.cookie`.
        assert "HttpOnly" in header
        # What lets this be a cookie at all without CSRF tokens on every mutating
        # route — see the comment on REFRESH_COOKIE.
        assert "samesite=strict" in header.lower()
        # Scoped, so it rides on the auth routes and not the hundreds of calls
        # that never need it.
        assert f"Path={REFRESH_COOKIE_PATH}" in header

    def test_refresh_needs_no_request_body_at_all(self, client, db, victim):
        sign_in(client, db)
        assert client.post("/api/v1/auth/refresh").status_code == 200

    def test_a_request_with_no_cookie_says_so(self, client, victim):
        response = client.post("/api/v1/auth/refresh")
        assert response.status_code == 401
        # NOT `invalid_token`. A browser that has simply never signed in must not
        # be sent down the chain-revoked branch of the client's error handling.
        assert response.json()["code"] == "no_session"

    def test_logout_clears_the_cookie(self, client, db, victim):
        first = sign_in(client, db)
        response = client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {first['access_token']}"})
        assert response.status_code == 204
        # Cleared at the SAME path it was set on; a delete on another path is a
        # silent no-op that leaves the credential in the browser.
        assert f"Path={REFRESH_COOKIE_PATH}" in response.headers["set-cookie"]
        assert not client.cookies.get(REFRESH_COOKIE)


class TestRefreshRotation:
    def test_a_refresh_returns_a_new_token(self, client, db, victim):
        sign_in(client, db)
        first = held_token(client)
        rotated = client.post("/api/v1/auth/refresh")
        assert rotated.status_code == 200
        assert held_token(client) != first

    def test_the_old_token_stops_working(self, client, db, victim):
        sign_in(client, db)
        first = held_token(client)
        client.post("/api/v1/auth/refresh")
        reused = present(client, first)
        assert reused.status_code == 401
        assert reused.json()["code"] == "token_reuse_detected"

    def test_reuse_revokes_the_whole_chain(self, client, db, victim):
        """The mechanism that turns a stolen refresh token from a 90-day
        credential into a one-shot one. Whoever presents the old token second
        loses — and so does the thief, because both sessions die."""
        sign_in(client, db)
        first = held_token(client)
        client.post("/api/v1/auth/refresh")
        second = held_token(client)

        present(client, first)

        still_live = present(client, second)
        assert still_live.status_code == 401, "the current token survived a reuse alarm"

    def test_the_revocation_reason_is_recorded(self, client, db, victim):
        sign_in(client, db)
        first = held_token(client)
        client.post("/api/v1/auth/refresh")
        present(client, first)
        db.expire_all()
        reasons = set(db.scalars(text("SELECT revoked_reason FROM auth_sessions")).all())
        assert "reuse_detected" in reasons

    def test_an_unknown_token_is_refused(self, client, victim):
        response = present(client, "not-a-real-token")
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_token"

    def test_an_expired_session_is_refused(self, client, db, victim):
        sign_in(client, db)
        db.execute(text("UPDATE auth_sessions SET expires_at = now() - interval '1 day'"))
        db.flush()
        assert client.post("/api/v1/auth/refresh").json()["code"] == "session_expired"

    def test_the_raw_token_is_never_stored(self, client, db, victim):
        """A database dump must not be a set of live sessions."""
        sign_in(client, db)
        stored = db.scalars(text("SELECT token_hash FROM auth_sessions")).all()
        assert held_token(client) not in stored
        assert all(len(h) == 64 for h in stored)


class TestSessionEndpoints:
    def test_logout_revokes_every_live_session(self, client, db, victim):
        first = sign_in(client, db)
        headers = {"Authorization": f"Bearer {first['access_token']}"}
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        db.expire_all()
        assert db.scalar(text(
            "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL")) == 0

    def test_a_logged_out_refresh_token_is_dead(self, client, db, victim):
        first = sign_in(client, db)
        token = held_token(client)
        client.post("/api/v1/auth/logout",
                    headers={"Authorization": f"Bearer {first['access_token']}"})
        # Presented explicitly, because logout also clears the jar: the point is
        # that the token is dead SERVER-side, not merely forgotten by the client.
        assert present(client, token).status_code == 401

    def test_the_session_endpoint_reports_the_principal(self, client, db, victim):
        first = sign_in(client, db)
        response = client.get("/api/v1/auth/session",
                              headers={"Authorization": f"Bearer {first['access_token']}"})
        assert response.status_code == 200
        assert response.json()["user"]["phone"] == VICTIM
        assert "entitlements" in response.json()

    def test_it_requires_a_token(self, client):
        assert client.get("/api/v1/auth/session").status_code in (401, 403)

    def test_the_date_of_birth_is_never_returned(self, client, db, victim):
        """Deliberately absent from every response. `is_minor` is the derived
        fact the client needs; the date itself is a minor's personal data with
        no client-side use."""
        first = sign_in(client, db)
        body = client.get("/api/v1/auth/session",
                          headers={"Authorization": f"Bearer {first['access_token']}"}).text
        assert "date_of_birth" not in body
        assert "1995-01-01" not in body

    def test_it_reports_whether_the_account_is_a_minor(self, client, db):
        """The flag the matching layer's age banding depends on."""
        db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998903334455', 'Minor', CAST(:dob AS date), 'active')
        """).bindparams(dob=(dt.date.today() - dt.timedelta(days=365 * 15)).isoformat()))
        db.flush()
        first = sign_in(client, db, "+998903334455")
        assert first["principal"]["is_minor"] is True


# ── the device label ─────────────────────────────────────────────────

def _devices(client, body: dict) -> list[dict]:
    return client.get("/api/v1/me/devices",
                      headers={"Authorization": f"Bearer {body['access_token']}"}).json()


class TestTheDeviceLabel:
    """Both sign-in bodies declare `device: DeviceInfo` in the contract, and both
    Pydantic models lacked the field, so it was dropped on the floor —
    `auth_sessions.device_label` was read by `GET /me/devices` and written by
    nothing, and the console's "Where you are signed in" table showed "Unnamed
    session" for every row, which is no help choosing which one to forget.
    """

    def test_a_label_sent_at_sign_in_is_what_the_devices_screen_shows(
            self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        opened = client.post("/api/v1/auth/otp/verify",
                             json={"challenge_xid": xid, "code": code_for(db, xid),
                                   "device": {"label": "Mom's phone"}})
        assert opened.status_code == 200, opened.text
        assert [d["label"] for d in _devices(client, opened.json())] == ["Mom's phone"]

    def test_the_telegram_path_records_it_too(self, client, db, bot_token):
        opened = verify(client, init_data=init_data(telegram_id=4250),
                        contact_phone=NEWCOMER, date_of_birth="2004-05-05",
                        device={"label": "Redmi Note 12", "platform": "android"})
        assert opened.status_code == 200, opened.text
        assert [d["label"] for d in _devices(client, opened.json())] == ["Redmi Note 12"]

    def test_no_device_means_no_label(self, client, db, victim):
        """The two clients send nothing yet, and a session opened by refresh or
        by invite redemption carries nothing either. Null, not `""`, so the
        screen renders its placeholder rather than an empty cell."""
        opened = sign_in(client, db)
        assert [d["label"] for d in _devices(client, opened)] == [None]

    def test_a_blank_label_is_stored_as_null(self, client, db, victim):
        xid = request_code(client).json()["challenge_xid"]
        opened = client.post("/api/v1/auth/otp/verify",
                             json={"challenge_xid": xid, "code": code_for(db, xid),
                                   "device": {"label": "   "}})
        assert [d["label"] for d in _devices(client, opened.json())] == [None]

    def test_an_overlong_label_is_refused_at_the_door(self, client, db, victim):
        """Untrusted text rendered in the console. Capped, and refused rather
        than truncated, because the schema is the place a client learns the cap."""
        xid = request_code(client).json()["challenge_xid"]
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": xid, "code": code_for(db, xid),
                                     "device": {"label": "x" * 200}})
        assert response.status_code == 422

    def test_platform_is_still_not_invented(self, client, db, victim):
        """`auth_sessions` has no column for it, and the conformance check's
        note says: do not invent a value. The full contract `DeviceInfo` is not
        refused — the undeclared halves are dropped — and `platform` stays null."""
        xid = request_code(client).json()["challenge_xid"]
        opened = client.post("/api/v1/auth/otp/verify",
                             json={"challenge_xid": xid, "code": code_for(db, xid),
                                   "device": {"label": "Lab PC", "platform": "web",
                                              "fingerprint": "abc"}})
        assert [d["platform"] for d in _devices(client, opened.json())] == [None]


# ── under the real unit of work ──────────────────────────────────────

@pytest.fixture
def live_client(database_url, db, victim):
    """A client that runs the REAL `deps.db`, against the scratch database.

    The pattern from `test_transaction_boundary.py`, which is where the request
    transaction was first executed under test at all. Depends on `victim` so the
    account exists before the suite's session is committed: the request runs in
    a DIFFERENT session and would otherwise see none of the flushed rows.
    """
    from app.api.main import create_app
    from app.platform import db as platform_db

    db.commit()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    settings.cache_clear()
    platform_db.reset_engine()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        settings.cache_clear()
        platform_db.reset_engine()
        db.rollback()


def _committed(db, sql: str, **params):
    """What the request's transaction actually left behind. `rollback` first, so
    the suite's session is not reading through a snapshot older than the
    request."""
    db.rollback()
    return db.scalar(text(sql).bindparams(**params))


class TestUnderTheRealUnitOfWork:
    """The two guards this file records as fixed, run inside the transaction
    boundary production runs them in — where both were still dead.

    `unit_of_work` rolls back on ANY exception, a `DomainError` becoming a 4xx
    included. That is the right rule for a partial write behind a 409. It is the
    wrong rule for a write that IS the refusal's point: the attempt charge that
    a wrong guess must leave behind, and the chain revocation that a reuse
    alarm must leave behind. Both were issued and then raised past, so both
    were undone — and every test above passed, because the `client` fixture's
    session is never rolled back.
    """

    def test_a_wrong_guess_still_costs_an_attempt_after_the_401(self, live_client, db):
        """Reverting the commit in `_consume_challenge` reads 0 here: the
        increment went out with the rollback and the challenge was as fresh as
        before the guess."""
        xid = request_code(live_client).json()["challenge_xid"]
        refused = live_client.post("/api/v1/auth/otp/verify",
                                   json={"challenge_xid": xid, "code": "000000"})
        assert refused.status_code == 401
        assert _committed(db, "SELECT attempts FROM otp_challenges "
                              "WHERE xid = CAST(:x AS uuid)", x=xid) == 1

    def test_the_sixth_guess_is_refused_even_when_it_is_right(self, live_client, db):
        """ADR-0001 §5.2, "max 5 attempts", for real. Without the commit the
        sixth guess — the correct code — answered 200."""
        xid = request_code(live_client).json()["challenge_xid"]
        right = code_for(db, xid)
        for n in range(5):
            guess = f"{n:06d}" if f"{n:06d}" != right else "999999"
            assert live_client.post("/api/v1/auth/otp/verify",
                                    json={"challenge_xid": xid,
                                          "code": guess}).status_code == 401
        exhausted = live_client.post("/api/v1/auth/otp/verify",
                                     json={"challenge_xid": xid, "code": right})
        assert exhausted.status_code == 410

    def test_a_correct_code_still_opens_a_session(self, live_client, db):
        """The commit must not have cost the success path anything: the code is
        consumed and the session opened in the request's own transaction, and
        both land."""
        xid = request_code(live_client).json()["challenge_xid"]
        opened = live_client.post("/api/v1/auth/otp/verify",
                                  json={"challenge_xid": xid, "code": code_for(db, xid)})
        assert opened.status_code == 200, opened.text
        assert _committed(db, "SELECT consumed_at FROM otp_challenges "
                              "WHERE xid = CAST(:x AS uuid)", x=xid) is not None
        assert _committed(db, "SELECT count(*) FROM auth_sessions "
                              "WHERE revoked_at IS NULL") == 1

    def test_reuse_kills_the_current_token_too(self, live_client, db):
        """The thief's scenario. They stole R1 and refreshed first, so they hold
        R2; the victim's browser presents R1 later and trips the alarm. Without
        the commit, the alarm rolled back with its own 401 and R2 stayed a
        working ninety-day credential — the test above named "reuse revokes the
        whole chain" passed only under the un-rolled-back session."""
        sign_in(live_client, db)
        first = held_token(live_client)
        assert live_client.post("/api/v1/auth/refresh").status_code == 200
        current = held_token(live_client)

        alarm = present(live_client, first)
        assert alarm.status_code == 401
        assert alarm.json()["code"] == "token_reuse_detected"

        assert present(live_client, current).status_code == 401, \
            "the current token survived a reuse alarm"

    def test_every_session_in_the_chain_is_revoked_on_disk(self, live_client, db):
        sign_in(live_client, db)
        first = held_token(live_client)
        live_client.post("/api/v1/auth/refresh")
        present(live_client, first)
        assert _committed(db, "SELECT count(*) FROM auth_sessions "
                              "WHERE revoked_at IS NULL") == 0
        assert _committed(db, "SELECT count(*) FROM auth_sessions "
                              "WHERE revoked_reason = 'reuse_detected'") == 1

    def test_a_refused_verify_leaves_no_session_behind(self, live_client, db):
        """The other half of the rule still holds: the commit is the charge and
        nothing more. A wrong guess must not have opened a session on its way
        to the 401."""
        xid = request_code(live_client).json()["challenge_xid"]
        live_client.post("/api/v1/auth/otp/verify",
                         json={"challenge_xid": xid, "code": "000000"})
        assert _committed(db, "SELECT count(*) FROM auth_sessions") == 0
