"""Delivery, against a real database and a fake socket.

Three gaps met here at once, and they only mean anything together:

  * `otp_request` generated a six-digit code, hashed it into `otp_challenges` and
    dropped the plaintext. Nothing sent it, so SMS and Telegram sign-in were both
    unreachable — and so was `phone_verified_at`, which `routers/identity.py`
    requires before a student may accept an invitation to a prep centre.
  * There was no message text in the repository at all.
  * `Transport.send` logged and returned, so every notification reached
    `status = 'sent'` having been written to a log file.

The end-to-end test at the bottom is the one that would have caught all three:
it takes the code out of the text the transport was handed and signs in with it.
Nothing short of a real code, really rendered, really passed to a transport,
makes that pass.

No test in this file opens a socket. `send_http` is injected and the fake records
what would have gone out.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.modules.identity import notify
from app.modules.identity.transport import HttpResponse, TelegramTransport

PHONE = "+998901234567"
# For rows this file queues directly. 12:00 Tashkent, so quiet hours do not
# defer anything and the test does not pass by day and fail at night.
NOON = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


class FakeSender:
    def __init__(self, status: int = 200, body: str | None = None):
        self.status = status
        self.body = body if body is not None else json.dumps(
            {"ok": True, "result": {"message_id": 42}})
        self.calls: list[dict] = []

    def __call__(self, url: str, payload: bytes, timeout: float) -> HttpResponse:
        self.calls.append(json.loads(payload.decode()))
        return HttpResponse(status=self.status, body=self.body)


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def user(db):
    """An active account with a phone and no Telegram link — the SMS case."""
    db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status, locale)
        VALUES (:p, 'Dilnoza', '2003-04-05', 'active', 'uz-Latn')
    """).bindparams(p=PHONE))
    db.flush()
    return db.scalar(text("SELECT id FROM users WHERE phone = :p").bindparams(p=PHONE))


@pytest.fixture
def telegram_user(db, user):
    db.execute(text("UPDATE users SET telegram_user_id = 771122 WHERE id = :u")
               .bindparams(u=user))
    db.flush()
    return user


def transport(sender: FakeSender) -> TelegramTransport:
    return TelegramTransport("botsecret", send_http=sender, api_base="https://tg.test")


def row(db, user_id: int) -> dict:
    return db.execute(text("""
        SELECT status, attempts, failed_reason, params, cost_minor, channel
        FROM notifications WHERE user_id = :u ORDER BY id DESC LIMIT 1
    """).bindparams(u=user_id)).mappings().one()


def request_code(client, phone: str = PHONE):
    return client.post("/api/v1/auth/otp/request", json={"phone": phone})


def due(offset_hours: int = 0) -> dt.datetime:
    """The delivery clock for anything queued through the HTTP handler.

    `otp_request` is a request handler, not a domain function taking `now`, so
    it stamps `scheduled_at` from the real clock. A delivery run pinned to a
    fixed date finds nothing due and every assertion below it becomes vacuous.
    """
    return dt.datetime.now(dt.UTC) + dt.timedelta(hours=offset_hours)


class TestTheCodeLeavesTheHandler:
    def test_requesting_a_code_queues_a_notification(self, client, db, user):
        """It did not. The code was hashed into `otp_challenges` and the
        plaintext was dropped on the floor — `_ = func  # delivery is a worker
        concern` stood where the send should have been."""
        assert request_code(client).status_code == 202
        assert row(db, user)["status"] == "queued"

    def test_the_queued_notification_carries_the_code(self, client, db, user):
        challenge = request_code(client).json()["challenge_xid"]
        code = row(db, user)["params"]["code"]
        assert re.fullmatch(r"[0-9]{6}", code)
        # And it is the code the server will accept, not a second one.
        assert client.post("/api/v1/auth/otp/verify",
                           json={"challenge_xid": challenge,
                                 "code": code}).status_code == 200

    def test_an_unknown_number_queues_nothing_and_still_answers_202(self, client, db):
        """Silence rather than a refusal: anything else turns this endpoint into
        a phone-number oracle."""
        assert request_code(client, "+998900000000").status_code == 202
        assert db.scalar(text("SELECT count(*) FROM notifications")) == 0

    def test_a_suspended_account_is_not_messaged(self, client, db, user):
        """`notify.queue` only accepts an active recipient, so a suspended
        account cannot be handed a working login code. Still 202: the endpoint
        must not become an account-status oracle either."""
        db.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u")
                   .bindparams(u=user))
        db.flush()
        assert request_code(client).status_code == 202
        assert db.scalar(text("SELECT count(*) FROM notifications")) == 0

    def test_a_linked_account_is_messaged_on_telegram_not_charged_sms(
            self, client, db, telegram_user):
        """The cost model, at the one endpoint that spends money."""
        request_code(client)
        assert row(db, telegram_user)["channel"] == "telegram"

    def test_the_client_cannot_force_the_paid_channel(self, client, db, telegram_user):
        """`OtpRequest.channel` is a request, not an instruction. Honouring it
        would let anyone spend the SMS budget on an account that Telegram
        reaches for free."""
        client.post("/api/v1/auth/otp/request",
                    json={"phone": PHONE, "channel": "sms"})
        assert row(db, telegram_user)["channel"] == "telegram"


class TestWhatTelegramWouldHaveReceived:
    def test_the_exact_chat_id_and_the_exact_text(self, client, db, telegram_user):
        sender = FakeSender()
        request_code(client)
        code = row(db, telegram_user)["params"]["code"]
        sent, failed = notify.deliver(db, transport(sender), now=due())

        assert (sent, failed) == (1, 0)
        assert sender.calls == [{
            "chat_id": 771122,
            "text": f"IELTS Hub kodi: {code}. 5 daqiqa amal qiladi. "
                    "Hech kimga aytmang.",
        }]

    def test_the_message_is_in_the_users_locale(self, client, db, telegram_user):
        db.execute(text("UPDATE users SET locale = 'ru' WHERE id = :u")
                   .bindparams(u=telegram_user))
        db.flush()
        sender = FakeSender()
        request_code(client)
        notify.deliver(db, transport(sender), now=due())
        assert sender.calls[0]["text"].startswith("Код IELTS Hub:")

    def test_a_locale_with_no_body_still_gets_a_message(self, client, db, telegram_user):
        """`uz-Cyrl` is not hypothetical: migration 0003 permits it on
        `users.locale` and there is no Cyrillic-script body. Falling back to the
        same language in Latin script beats not sending a login code."""
        db.execute(text("UPDATE users SET locale = 'uz-Cyrl' WHERE id = :u")
                   .bindparams(u=telegram_user))
        db.flush()
        sender = FakeSender()
        request_code(client)
        notify.deliver(db, transport(sender), now=due())
        assert sender.calls[0]["text"].startswith("IELTS Hub kodi:")

    def test_a_delivered_row_is_marked_sent(self, client, db, telegram_user):
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        assert row(db, telegram_user)["status"] == "sent"


class TestTheCodeDoesNotOutliveDelivery:
    def test_a_sent_row_no_longer_holds_the_code(self, client, db, telegram_user):
        """`notifications` is the only place a plaintext code is ever persisted —
        `otp_challenges` keeps a hash — and nothing prunes this table."""
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        assert "code" not in row(db, telegram_user)["params"]

    def test_a_permanently_failed_row_no_longer_holds_the_code(self, client, db, user):
        """The SMS case, which is every unlinked account today. Without the
        scrub the code would sit in this row forever."""
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        assert row(db, user)["status"] == "failed"
        assert "code" not in row(db, user)["params"]

    def test_a_row_still_waiting_to_retry_keeps_the_code(self, client, db, telegram_user):
        """Scrubbing on a transient failure would leave a queued row that renders
        to `MissingParam` on its next attempt — a retry guaranteed to fail."""
        request_code(client)
        notify.deliver(db, transport(FakeSender(429)), now=due())
        pending = row(db, telegram_user)
        assert pending["status"] == "queued"
        assert re.fullmatch(r"[0-9]{6}", pending["params"]["code"])

    def test_the_code_is_gone_once_the_retries_are_spent(self, client, db, telegram_user):
        request_code(client)
        for attempt in range(notify.MAX_ATTEMPTS):
            notify.deliver(db, transport(FakeSender(500)),
                           now=due(attempt))
        assert row(db, telegram_user)["status"] == "failed"
        assert "code" not in row(db, telegram_user)["params"]

    def test_a_successful_delivery_logs_nothing_containing_the_code(
            self, client, db, telegram_user):
        """Logs outlive the row and go somewhere the database's access rules do
        not. `capture_logs` reads the structured event dicts rather than stdout,
        so this does not depend on which test happened to bind the stream first.
        """
        from structlog.testing import capture_logs

        request_code(client)
        code = row(db, telegram_user)["params"]["code"]
        with capture_logs() as events:
            notify.deliver(db, transport(FakeSender()), now=due())
        assert events, "delivery logged nothing at all — the assertion is vacuous"
        assert not any(code in str(value) for event in events
                       for value in event.values())

    def test_a_failed_delivery_logs_nothing_containing_the_code(
            self, client, db, user):
        """The SMS path, which is where every unlinked account ends up."""
        from structlog.testing import capture_logs

        request_code(client)
        code = row(db, user)["params"]["code"]
        with capture_logs() as events:
            notify.deliver(db, transport(FakeSender()), now=due())
        assert events
        assert not any(code in str(value) for event in events
                       for value in event.values())

    def test_no_failure_reason_ever_contains_the_code(self, client, db, telegram_user):
        """`failed_reason` is read in a listing and printed in logs."""
        request_code(client)
        code = row(db, telegram_user)["params"]["code"]
        echoed = FakeSender(400, json.dumps(
            {"ok": False, "description": f"Bad Request: message text is {code}"}))
        notify.deliver(db, transport(echoed), now=due())
        assert code not in row(db, telegram_user)["failed_reason"]


class TestSmsFailsClosed:
    def test_an_unlinked_account_ends_failed_never_sent(self, client, db, user):
        """The consequence, made visible in the data rather than hidden: a user
        with no Telegram link cannot receive a login code today."""
        request_code(client)
        sent, failed = notify.deliver(db, transport(FakeSender()), now=due())
        assert (sent, failed) == (0, 1)
        assert row(db, user)["status"] == "failed"

    def test_the_reason_names_the_missing_provider(self, client, db, user):
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        reason = row(db, user)["failed_reason"]
        assert "no provider is contracted" in reason
        assert "Eskiz" in reason

    def test_it_gives_up_on_the_first_attempt(self, client, db, user):
        """Permanent, so it does not burn three attempts over fifteen minutes to
        arrive at the same answer."""
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        assert row(db, user)["attempts"] == 1

    def test_nothing_is_charged_for_a_message_that_was_not_sent(self, client, db, user):
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        assert row(db, user)["cost_minor"] is None
        assert notify.monthly_sms_cost(db) == 0

    def test_it_is_countable_in_one_query(self, client, db, user):
        """How big the gap is has to be answerable without reading code."""
        request_code(client)
        notify.deliver(db, transport(FakeSender()), now=due())
        assert db.scalar(text("""
            SELECT count(*) FROM notifications
            WHERE channel = 'sms' AND status = 'failed'
              AND failed_reason LIKE '%no provider is contracted%'
        """)) == 1


class TestTelegramFailuresOnTheRow:
    def test_a_blocked_bot_fails_once_and_stops(self, client, db, telegram_user):
        """403. Retrying a user who blocked the bot three times a night is what
        makes a notify queue useless."""
        blocked = FakeSender(403, json.dumps(
            {"ok": False, "description": "Forbidden: bot was blocked by the user"}))
        request_code(client)
        notify.deliver(db, transport(blocked), now=due())
        after = row(db, telegram_user)
        assert (after["status"], after["attempts"]) == ("failed", 1)
        assert "blocked" in after["failed_reason"]

    def test_a_rate_limit_stays_queued_for_another_attempt(self, client, db,
                                                           telegram_user):
        """429 is Telegram asking us to slow down, not a statement about the
        recipient."""
        request_code(client)
        notify.deliver(db, transport(FakeSender(429)), now=due())
        after = row(db, telegram_user)
        assert (after["status"], after["attempts"]) == ("queued", 1)

    def test_a_rate_limited_message_is_delivered_on_the_retry(self, client, db,
                                                              telegram_user):
        request_code(client)
        notify.deliver(db, transport(FakeSender(429)), now=due())
        recovered = FakeSender()
        notify.deliver(db, transport(recovered), now=due(1))
        assert len(recovered.calls) == 1
        assert row(db, telegram_user)["status"] == "sent"

    def test_a_server_error_is_retried_until_the_attempts_run_out(self, client, db,
                                                                  telegram_user):
        request_code(client)
        for attempt in range(notify.MAX_ATTEMPTS):
            notify.deliver(db, transport(FakeSender(503)),
                           now=due(attempt))
        after = row(db, telegram_user)
        assert (after["status"], after["attempts"]) == ("failed", notify.MAX_ATTEMPTS)

    def test_a_transient_failure_does_not_charge_or_mark_sent(self, client, db,
                                                              telegram_user):
        request_code(client)
        notify.deliver(db, transport(FakeSender(500)), now=due())
        assert row(db, telegram_user)["cost_minor"] is None


class TestRenderFailuresAreVisible:
    def test_an_unknown_template_fails_the_row_rather_than_sending_nothing(
            self, db, telegram_user):
        """A key with no body used to be impossible to notice: the stub transport
        logged and the row said `sent`."""
        sender = FakeSender()
        notify.queue(db, user_id=telegram_user, template="invoice.overdue",
                     params={}, now=NOON)
        notify.deliver(db, transport(sender), now=due())
        after = row(db, telegram_user)
        assert (after["status"], after["attempts"]) == ("failed", 1)
        assert "no message body" in after["failed_reason"]
        assert sender.calls == []

    def test_a_missing_param_fails_the_row(self, db, telegram_user):
        """Better than delivering "Ball: {band}" to a student."""
        sender = FakeSender()
        notify.queue(db, user_id=telegram_user, template="attempt.scored",
                     params={"attempt_xid": "a"}, now=NOON)
        notify.deliver(db, transport(sender), now=due())
        after = row(db, telegram_user)
        assert after["status"] == "failed"
        assert "band" in after["failed_reason"]
        assert sender.calls == []


class TestTheWorkerIsWiredToTheRealTransport:
    def test_the_actor_builds_a_telegram_transport(self):
        """`_transport()` returned the logging stub, which is why nothing in
        production had ever sent a message."""
        from app.workers import actors

        assert isinstance(actors._transport(), TelegramTransport)

    def test_it_is_not_the_logging_stub(self):
        from app.workers import actors

        assert type(actors._transport()) is not notify.Transport


class TestSignInEndToEnd:
    def test_the_code_telegram_receives_is_the_code_that_signs_you_in(
            self, client, db, telegram_user):
        """The whole point, in one test.

        Generate a code, render it, hand it to a transport, read it back out of
        the text that transport was given, and use it. It fails if the code is
        never queued, if there is no message body, if the transport is a stub, or
        if the rendered digits are not the digits the server hashed.
        """
        sender = FakeSender()
        challenge = request_code(client).json()["challenge_xid"]
        notify.deliver(db, transport(sender), now=due())

        delivered = re.search(r"\b([0-9]{6})\b", sender.calls[0]["text"]).group(1)
        response = client.post("/api/v1/auth/otp/verify",
                               json={"challenge_xid": challenge, "code": delivered})
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_signing_in_that_way_verifies_the_phone(self, client, db, telegram_user):
        """`phone_verified_at` is the gate on accepting an invitation to a prep
        centre, and no code had ever reached a handset to set it."""
        sender = FakeSender()
        challenge = request_code(client).json()["challenge_xid"]
        notify.deliver(db, transport(sender), now=due())
        delivered = re.search(r"\b([0-9]{6})\b", sender.calls[0]["text"]).group(1)
        client.post("/api/v1/auth/otp/verify",
                    json={"challenge_xid": challenge, "code": delivered})
        assert db.scalar(text("SELECT phone_verified_at FROM users WHERE id = :u")
                         .bindparams(u=telegram_user)) is not None
