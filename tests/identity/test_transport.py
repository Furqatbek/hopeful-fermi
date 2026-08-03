"""The Telegram transport, with the socket replaced by a list.

Nothing here touches the network, and that is a design constraint rather than a
test convenience: `send_http` is a constructor argument precisely so CI can
assert on the exact `chat_id` and the exact rendered text that WOULD have gone
out. A transport whose tests need a bot account is a transport CI never runs.

The classification tests are the point. `notify.deliver` already counts attempts
and gives up at `MAX_ATTEMPTS`; what this transport owes it is the distinction
between "come back" and "stop", because retrying a user who has blocked the bot
three times a night is the failure mode that makes the notify queue useless.
"""

from __future__ import annotations

import io
import json

import pytest

from app.modules.identity.notify import PermanentDeliveryError, Recipient, TransientDeliveryError
from app.modules.identity.transport import HttpResponse, TelegramTransport

TOKEN = "1234567:test-bot-token"
CODE = "428913"


class FakeSender:
    """Records the call and returns a canned response. Three lines of behaviour,
    because a fake with logic is a second implementation to debug."""

    def __init__(self, status: int = 200, body: str | None = None):
        self.status = status
        self.body = body if body is not None else json.dumps(
            {"ok": True, "result": {"message_id": 7788}})
        self.calls: list[tuple[str, dict, float]] = []

    def __call__(self, url: str, payload: bytes, timeout: float) -> HttpResponse:
        self.calls.append((url, json.loads(payload.decode()), timeout))
        return HttpResponse(status=self.status, body=self.body)


def linked(**overrides) -> Recipient:
    fields = {"user_id": 1, "locale": "uz-Latn", "timezone": "Asia/Tashkent",
              "phone": "+998901112233", "telegram_user_id": 99001122}
    return Recipient(**{**fields, **overrides})


def transport(sender: FakeSender, token: str = TOKEN) -> TelegramTransport:
    return TelegramTransport(token, send_http=sender, api_base="https://tg.test")


def send_otp(sender: FakeSender, recipient: Recipient | None = None, **kwargs):
    return transport(sender).send(channel="telegram",
                                  recipient=recipient or linked(),
                                  template="auth.otp", params={"code": CODE},
                                  locale=kwargs.pop("locale", "uz-Latn"), **kwargs)


class TestWhatIsActuallySent:
    def test_the_chat_id_is_the_users_telegram_id(self):
        """`users.telegram_user_id` IS the private-chat `chat_id`. Getting this
        wrong sends a login code to a stranger."""
        sender = FakeSender()
        send_otp(sender, linked(telegram_user_id=555000111))
        assert sender.calls[0][1]["chat_id"] == 555000111

    def test_the_text_is_the_rendered_message_not_the_template_key(self):
        sender = FakeSender()
        send_otp(sender)
        text = sender.calls[0][1]["text"]
        assert CODE in text
        assert "auth.otp" not in text and "{" not in text

    def test_the_text_is_in_the_recipients_locale(self):
        russian = FakeSender()
        uzbek = FakeSender()
        send_otp(russian, locale="ru")
        send_otp(uzbek, locale="uz-Latn")
        assert russian.calls[0][1]["text"] != uzbek.calls[0][1]["text"]
        assert CODE in russian.calls[0][1]["text"]

    def test_it_posts_to_sendmessage_with_the_bot_token(self):
        sender = FakeSender()
        send_otp(sender)
        assert sender.calls[0][0] == f"https://tg.test/bot{TOKEN}/sendMessage"

    def test_every_call_carries_a_timeout(self):
        """A worker blocked on a hung socket stops the whole notify queue —
        including the login codes people are waiting on."""
        sender = FakeSender()
        send_otp(sender)
        assert 0 < sender.calls[0][2] <= 30

    def test_a_successful_send_returns_the_message_id(self):
        assert send_otp(FakeSender()) == "7788"

    def test_an_unparseable_success_body_is_still_a_success(self):
        """A response shape we failed to parse does not un-send the message."""
        assert send_otp(FakeSender(body="not json")) is None


class TestTelegramFailureClassification:
    def test_403_is_permanent(self):
        """The user blocked the bot or deleted the account. Three more attempts
        reach the same answer fifteen minutes later."""
        sender = FakeSender(403, json.dumps(
            {"ok": False, "error_code": 403,
             "description": "Forbidden: bot was blocked by the user"}))
        with pytest.raises(PermanentDeliveryError, match="blocked"):
            send_otp(sender)

    def test_429_is_transient(self):
        sender = FakeSender(429, json.dumps(
            {"ok": False, "description": "Too Many Requests: retry after 30"}))
        with pytest.raises(TransientDeliveryError):
            send_otp(sender)

    def test_a_server_error_is_transient(self):
        with pytest.raises(TransientDeliveryError, match="502"):
            send_otp(FakeSender(502, "<html>bad gateway</html>"))

    def test_400_is_permanent(self):
        """`chat not found` — the account was never actually reachable."""
        sender = FakeSender(400, json.dumps(
            {"ok": False, "description": "Bad Request: chat not found"}))
        with pytest.raises(PermanentDeliveryError, match="chat not found"):
            send_otp(sender)

    def test_a_bad_bot_token_is_permanent_not_a_hiccup(self):
        """401 needs a deployment change. Retrying is the one thing that cannot
        fix it, and the reason on the row is what tells an operator so."""
        with pytest.raises(PermanentDeliveryError, match="401"):
            send_otp(FakeSender(401, json.dumps({"ok": False})))

    def test_permanent_and_transient_are_different_classes(self):
        """`deliver` branches on exactly this. If one derived from the other the
        `except` order would decide the retry policy."""
        assert not issubclass(TransientDeliveryError, PermanentDeliveryError)
        assert not issubclass(PermanentDeliveryError, TransientDeliveryError)


class TestNothingLeaksTheCode:
    def test_an_error_body_echoing_the_code_is_redacted(self):
        """`failed_reason` is persisted and logged. What a provider echoes back
        is the provider's choice, so the value is stripped rather than trusted
        not to appear."""
        sender = FakeSender(400, json.dumps(
            {"ok": False, "description": f"Bad Request: text was {CODE}"}))
        with pytest.raises(PermanentDeliveryError) as raised:
            send_otp(sender)
        assert CODE not in str(raised.value)
        assert "[redacted]" in str(raised.value)

    def test_a_transient_error_is_redacted_too(self):
        sender = FakeSender(500, json.dumps({"ok": False, "description": CODE}))
        with pytest.raises(TransientDeliveryError) as raised:
            send_otp(sender)
        assert CODE not in str(raised.value)

    def test_a_missing_telegram_link_does_not_name_the_code(self):
        with pytest.raises(PermanentDeliveryError) as raised:
            send_otp(FakeSender(), linked(telegram_user_id=None))
        assert CODE not in str(raised.value)


class TestFailingClosed:
    def test_sms_is_never_sent_and_names_the_missing_provider(self):
        """No provider is contracted. A transport that shrugged and returned
        would mark the row `sent` and charge `COST_MINOR` for a message that does
        not exist."""
        sender = FakeSender()
        with pytest.raises(PermanentDeliveryError, match="no provider is contracted"):
            transport(sender).send(channel="sms", recipient=linked(),
                                   template="auth.otp", params={"code": CODE},
                                   locale="uz-Latn")
        assert sender.calls == []

    def test_the_sms_reason_names_a_real_gateway_to_go_and_contract(self):
        with pytest.raises(PermanentDeliveryError, match="Eskiz"):
            transport(FakeSender()).send(
                channel="sms", recipient=linked(telegram_user_id=None),
                template="auth.otp", params={"code": CODE}, locale="uz-Latn")

    def test_sms_is_permanent_so_it_cannot_reach_sent_by_retrying(self):
        with pytest.raises(PermanentDeliveryError):
            transport(FakeSender()).send(channel="sms", recipient=linked(),
                                         template="attempt.scored",
                                         params={"attempt_xid": "a", "band": 6.0},
                                         locale="en")

    def test_no_bot_token_refuses_rather_than_calling_telegram(self):
        """Same rule as `_verify_telegram`: a missing secret makes the feature
        refuse loudly instead of degrading into something that looks like it
        works."""
        sender = FakeSender()
        with pytest.raises(PermanentDeliveryError, match="no bot token"):
            transport(sender, token="").send(
                channel="telegram", recipient=linked(), template="auth.otp",
                params={"code": CODE}, locale="uz-Latn")
        assert sender.calls == []

    def test_a_recipient_with_no_telegram_link_is_permanent(self):
        """Reachable in production: the link can be dropped between queueing and
        sending."""
        with pytest.raises(PermanentDeliveryError, match="no telegram_user_id"):
            send_otp(FakeSender(), linked(telegram_user_id=None))

    def test_an_unimplemented_channel_is_refused_by_name(self):
        with pytest.raises(PermanentDeliveryError, match="'push'"):
            transport(FakeSender()).send(channel="push", recipient=linked(),
                                         template="attempt.scored",
                                         params={"attempt_xid": "a", "band": 6.0},
                                         locale="en")


class TestInApp:
    def test_it_sends_nothing_outbound(self):
        """The row is the message; the client reads it on the next open."""
        sender = FakeSender()
        result = transport(sender).send(
            channel="in_app", recipient=linked(), template="attempt.scored",
            params={"attempt_xid": "a", "band": 6.0}, locale="en")
        assert result is None
        assert sender.calls == []

    def test_an_unknown_template_still_fails_on_in_app(self):
        """The channel most notifications use, and the one where a silently empty
        message would never be reported — nobody is waiting for it."""
        with pytest.raises(PermanentDeliveryError, match="no message body"):
            transport(FakeSender()).send(channel="in_app", recipient=linked(),
                                         template="invoice.overdue", params={},
                                         locale="en")

    def test_a_missing_param_still_fails_on_in_app(self):
        with pytest.raises(PermanentDeliveryError, match="band"):
            transport(FakeSender()).send(channel="in_app", recipient=linked(),
                                         template="attempt.scored",
                                         params={"attempt_xid": "a"}, locale="en")


class TestRenderFailuresReachTheCaller:
    def test_an_unknown_template_is_not_sent(self):
        sender = FakeSender()
        with pytest.raises(PermanentDeliveryError):
            transport(sender).send(channel="telegram", recipient=linked(),
                                   template="invoice.overdue", params={},
                                   locale="en")
        assert sender.calls == []

    def test_a_missing_param_is_not_sent(self):
        sender = FakeSender()
        with pytest.raises(PermanentDeliveryError):
            transport(sender).send(channel="telegram", recipient=linked(),
                                   template="auth.otp", params={}, locale="en")
        assert sender.calls == []


class TestTheDefaultSender:
    """`urlopen_sender` itself, with `urllib.request.urlopen` replaced.

    It is the default the constructor installs, so it is the code that runs in
    production and none of the tests above touch it. Still no socket: the point
    of the seam is that CI never opens one.
    """

    def test_the_constructor_defaults_to_it(self):
        """Pinned so nobody 'simplifies' the default to a fake and leaves CI
        green while production sends nothing."""
        from app.modules.identity import transport as module

        assert TelegramTransport("t")._send_http is module.urlopen_sender

    def test_a_network_failure_is_transient(self):
        """A refused connection is a socket problem, not a statement about the
        recipient — it must not spend an attempt as though the bot were
        blocked."""
        import urllib.error
        import urllib.request

        from app.modules.identity import transport as module

        def refuse(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib.request, "urlopen", refuse)
            with pytest.raises(TransientDeliveryError, match="URLError"):
                module.urlopen_sender("https://tg.test/x", b"{}", 1.0)

    def test_a_timeout_is_transient(self):
        import urllib.request

        from app.modules.identity import transport as module

        def hang(request, timeout=None):
            raise TimeoutError("timed out")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib.request, "urlopen", hang)
            with pytest.raises(TransientDeliveryError):
                module.urlopen_sender("https://tg.test/x", b"{}", 1.0)

    def test_an_http_error_is_returned_as_a_response_not_raised(self):
        """A 403 is an ANSWER. Letting `HTTPError` propagate would classify every
        blocked bot as a network blip and retry it."""
        import urllib.error
        import urllib.request

        from app.modules.identity import transport as module

        def forbid(request, timeout=None):
            raise urllib.error.HTTPError(
                "https://tg.test/x", 403, "Forbidden", {},
                io.BytesIO(b'{"ok": false, "description": "blocked"}'))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib.request, "urlopen", forbid)
            response = module.urlopen_sender("https://tg.test/x", b"{}", 1.0)
        assert response.status == 403
        assert "blocked" in response.body

    def test_a_success_carries_the_body_through(self):
        import urllib.request

        from app.modules.identity import transport as module

        class Response:
            status = 200

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib.request, "urlopen", lambda request, timeout=None: Response())
            response = module.urlopen_sender("https://tg.test/x", b"{}", 1.0)
        assert (response.status, response.body) == (200, '{"ok": true}')

    def test_it_posts_json(self):
        import urllib.request

        from app.modules.identity import transport as module

        seen = {}

        class Response:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def capture(request, timeout=None):
            seen["method"] = request.get_method()
            seen["type"] = request.get_header("Content-type")
            seen["timeout"] = timeout
            return Response()

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib.request, "urlopen", capture)
            module.urlopen_sender("https://tg.test/x", b'{"a": 1}', 4.5)
        assert seen == {"method": "POST", "type": "application/json", "timeout": 4.5}
