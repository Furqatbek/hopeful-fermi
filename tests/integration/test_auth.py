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
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.platform.config import settings

BOT_TOKEN = "1234567:test-bot-token"
VICTIM = "+998901112233"
NEWCOMER = "+998907778899"


@pytest.fixture
def client(db):
    """The shared pattern: a TestClient whose request-scoped session is the
    test's own, so assertions see what the handler wrote without a commit race."""
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
        is a budget control before it is an abuse control."""
        for _ in range(5):
            assert request_code(client).status_code == 202
        refused = request_code(client)
        assert refused.status_code == 400
        assert refused.json()["code"] == "rate_limited"

    def test_the_limit_is_per_number(self, client):
        for _ in range(5):
            request_code(client)
        assert request_code(client, "+998905554433").status_code == 202

    def test_a_malformed_number_is_rejected_before_anything_is_spent(self, client):
        for bad in ("998901234567", "+7901234567", "+99890123456", "", "+998abcdefghi"):
            assert client.post("/api/v1/auth/otp/request",
                               json={"phone": bad}).status_code == 422


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
        assert response.status_code == 403
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
        assert again.status_code == 403

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
        assert response.status_code == 403

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
    xid = request_code(client, phone).json()["challenge_xid"]
    return client.post("/api/v1/auth/otp/verify",
                       json={"challenge_xid": xid, "code": code_for(db, xid)}).json()


class TestRefreshRotation:
    def test_a_refresh_returns_a_new_pair(self, client, db, victim):
        first = sign_in(client, db)
        rotated = client.post("/api/v1/auth/refresh",
                              json={"refresh_token": first["refresh_token"]})
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != first["refresh_token"]

    def test_the_old_token_stops_working(self, client, db, victim):
        first = sign_in(client, db)
        client.post("/api/v1/auth/refresh",
                    json={"refresh_token": first["refresh_token"]})
        reused = client.post("/api/v1/auth/refresh",
                             json={"refresh_token": first["refresh_token"]})
        assert reused.status_code == 403
        assert reused.json()["code"] == "token_reuse_detected"

    def test_reuse_revokes_the_whole_chain(self, client, db, victim):
        """The mechanism that turns a stolen refresh token from a 90-day
        credential into a one-shot one. Whoever presents the old token second
        loses — and so does the thief, because both sessions die."""
        first = sign_in(client, db)
        second = client.post("/api/v1/auth/refresh",
                             json={"refresh_token": first["refresh_token"]}).json()

        client.post("/api/v1/auth/refresh",
                    json={"refresh_token": first["refresh_token"]})

        still_live = client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": second["refresh_token"]})
        assert still_live.status_code == 403, "the current token survived a reuse alarm"

    def test_the_revocation_reason_is_recorded(self, client, db, victim):
        first = sign_in(client, db)
        client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        db.expire_all()
        reasons = set(db.scalars(text("SELECT revoked_reason FROM auth_sessions")).all())
        assert "reuse_detected" in reasons

    def test_an_unknown_token_is_refused(self, client, victim):
        response = client.post("/api/v1/auth/refresh",
                               json={"refresh_token": "not-a-real-token"})
        assert response.status_code == 403
        assert response.json()["code"] == "invalid_token"

    def test_an_expired_session_is_refused(self, client, db, victim):
        first = sign_in(client, db)
        db.execute(text("UPDATE auth_sessions SET expires_at = now() - interval '1 day'"))
        db.flush()
        response = client.post("/api/v1/auth/refresh",
                               json={"refresh_token": first["refresh_token"]})
        assert response.json()["code"] == "session_expired"

    def test_the_raw_token_is_never_stored(self, client, db, victim):
        """A database dump must not be a set of live sessions."""
        first = sign_in(client, db)
        stored = db.scalars(text("SELECT token_hash FROM auth_sessions")).all()
        assert first["refresh_token"] not in stored
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
        client.post("/api/v1/auth/logout",
                    headers={"Authorization": f"Bearer {first['access_token']}"})
        assert client.post("/api/v1/auth/refresh",
                           json={"refresh_token": first["refresh_token"]}).status_code == 403

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
