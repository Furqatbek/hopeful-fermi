"""The outbound side. Telegram is implemented; SMS is deliberately not.

Until this file existed the product could not send a message to a human at all:
`Transport.send` wrote a log line and returned, and every notification reached
`status = 'sent'` having been logged and nothing else. The queue, the retry
counter and the cost column were all real and all measuring a send that never
happened.

## Telegram, because it is free

`notify._channel` prefers Telegram for cost reasons argued in that module: SMS is
the only line on the infrastructure bill that grows with the user count, and at
1,500 users a careless notification design is the difference between $0 and $60 a
month against a ceiling of $50. So the channel that is free is the one that is
built, and it needs no new credential — `telegram_bot_token` is already
configured and already verifies Mini App `initData` in `routers/auth.py`.

## SMS is a seam, not an implementation, and it fails CLOSED

**There is no SMS provider and this module will not pretend otherwise.** The
Uzbek gateways — Eskiz, Play Mobile — need a signed commercial agreement and a
registered sender ID before a single message leaves. That is the product owner's
decision and their money; writing a plausible-looking `EskizTransport` against an
account that does not exist would produce code nobody can run and a `sent` status
nobody can trust.

So `sms` raises `PermanentDeliveryError` naming the missing provider. The row
ends `failed` with that reason, never `sent`, and `monthly_sms_cost` stays honest
because nothing is charged for a message that was not sent.

**The consequence, stated rather than hidden:** a user with no Telegram link who
requests a login code cannot receive it today. `notify._channel` routes them to
`sms`, `sms` fails closed, and the `failed` row with its reason is the product
telling the truth about itself. That is a real gap in the product, and one
`SELECT status, failed_reason FROM notifications WHERE channel = 'sms'` away
from being counted. The seam for closing it is `_send_sms` below.

## No new runtime dependency

`urllib.request` from the standard library, not `requests` and not `httpx`.
`httpx` is a dev dependency (the Starlette test client) and promoting it to a
runtime one would ship an HTTP stack, its `h11`/`httpcore`/`certifi` tail and a
version to keep current, to make one POST of a JSON object. If this file ever
needs connection pooling or HTTP/2 that trade changes; it does not today.

## Tests never touch the network

`send_http` is injected. Every test passes a fake and asserts on the exact
`chat_id` and the exact rendered text that would have gone out. A transport whose
tests need an account is a transport nobody runs in CI.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from app.modules.identity import messages
from app.modules.identity.notify import (
    SECRET_PARAMS,
    PermanentDeliveryError,
    Recipient,
    TransientDeliveryError,
    Transport,
)

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"

# Every outbound call is bounded. Without a timeout `urlopen` inherits the socket
# default, which is no timeout at all: one hung connection to api.telegram.org
# holds the notify worker forever and the whole queue stops behind it — including
# the login codes, which are the notifications people are actively waiting for.
TIMEOUT_SECONDS = 10.0

# Telegram statuses that will never succeed on a retry. 403 is the user having
# blocked the bot or deleted their account; 400 is a malformed request or a chat
# that does not exist; 401 is a bot token this deployment has wrong, which a
# retry cannot fix either — all three need a person, not a queue.
PERMANENT_STATUS = frozenset({400, 401, 403, 404})


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str


# url, JSON request body, timeout in seconds -> response. Narrow on purpose: the
# fake a test writes should be three lines, or it is testing the fake.
HttpSender = Callable[[str, bytes, float], HttpResponse]


def urlopen_sender(url: str, payload: bytes, timeout: float) -> HttpResponse:
    """The default sender. One POST, no session, no retry of its own.

    Retrying lives in `notify.deliver`, which counts attempts on the row and
    stops at `MAX_ATTEMPTS`. A second retry loop here would multiply with that
    one and turn three attempts into nine.
    """
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(status=response.status,
                                body=response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is an ANSWER, not a failure to reach anyone: Telegram puts
        # the reason in the body and `send` needs it to decide permanent versus
        # transient. Letting this propagate would classify every 403 as a network
        # blip and retry a blocked bot three times.
        return HttpResponse(status=exc.code,
                            body=exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientDeliveryError(f"telegram: {type(exc).__name__}") from None


class TelegramTransport(Transport):
    """Bot API `sendMessage`, keyed on `users.telegram_user_id`.

    That column IS the `chat_id` for a private chat, which is why the Mini App
    sign-in path records it: a bot cannot open a conversation with someone who
    has not started one, so the Telegram link established at registration is the
    entire permission to message them.
    """

    def __init__(self, bot_token: str, *, send_http: HttpSender = urlopen_sender,
                 api_base: str = TELEGRAM_API,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self._token = bot_token
        self._send_http = send_http
        self._api_base = api_base
        self._timeout = timeout

    def send(self, *, channel: str, recipient: Recipient, template: str,
             params: dict, locale: str) -> str | None:
        # Rendered before the channel is looked at, so an unknown template key or
        # a missing parameter is caught on `in_app` too — the channel most
        # notifications use, and the one where a silently empty message would
        # never be reported because nobody is waiting for it.
        body = messages.render(template, params, locale)
        if channel == "in_app":
            # Nothing outbound: the row IS the message, read the next time the
            # student opens the app. Rendering it here is still the check.
            return None
        if channel == "telegram":
            return self._send_telegram(recipient, body, params)
        if channel == "sms":
            return self._send_sms(recipient, body, params)
        raise PermanentDeliveryError(
            f"channel {channel!r} has no transport; only telegram and in_app "
            "are implemented")

    def _send_telegram(self, recipient: Recipient, body: str, params: dict) -> str | None:
        if not self._token:
            # Same fail-closed rule as `_verify_telegram`: a missing secret makes
            # the feature refuse, loudly, rather than degrade into something that
            # looks like it works.
            raise PermanentDeliveryError(
                "telegram: no bot token configured (TELEGRAM_BOT_TOKEN)")
        if not recipient.telegram_user_id:
            # Reachable even though `_channel` only picks telegram for a linked
            # account: the link can be dropped between queueing and sending, and
            # `queue(channel=...)` lets a caller name the channel outright.
            raise PermanentDeliveryError(
                f"telegram: user {recipient.user_id} has no telegram_user_id")

        payload = json.dumps({"chat_id": recipient.telegram_user_id, "text": body})
        response = self._send_http(
            f"{self._api_base}/bot{self._token}/sendMessage",
            payload.encode("utf-8"), self._timeout)

        if response.status == 200:
            log.info("telegram_sent", user_id=recipient.user_id)
            return _message_id(response.body)
        detail = _redact(_describe(response), params)
        if response.status in PERMANENT_STATUS:
            raise PermanentDeliveryError(f"telegram: {detail}")
        # 429 and every 5xx. Telegram's own `retry_after` is not honoured
        # directly: `deliver` reschedules on its own escalating backoff, and two
        # schedules for one row is the sort of thing that sends nothing at all.
        raise TransientDeliveryError(f"telegram: {detail}")

    def _send_sms(self, recipient: Recipient, body: str, params: dict) -> str | None:
        """The documented seam. Read the module docstring before filling it in.

        A provider goes here: sign the request with the contracted credentials,
        POST the rendered `body` to `recipient.phone`, and classify the response
        the way `_send_telegram` does — an unknown number is permanent, a gateway
        5xx is transient. Add the per-message price to `notify.COST_MINOR` at the
        same time, because that column is the only warning before the bill.
        """
        raise PermanentDeliveryError(
            "sms: no provider is contracted, so this message cannot be "
            "delivered. Uzbek gateways (Eskiz, Play Mobile) require a "
            "commercial agreement and a registered sender ID.")


def _describe(response: HttpResponse) -> str:
    """Telegram's own explanation, or the bare status if it did not give one.

    `{"ok": false, "error_code": 403, "description": "Forbidden: bot was blocked
    by the user"}` is what makes a `failed` row actionable; "HTTP 403" alone
    leaves the reader guessing between a blocked bot and a broken token.
    """
    try:
        described = json.loads(response.body).get("description")
    except (ValueError, AttributeError):
        described = None
    return f"HTTP {response.status}: {described}" if described else f"HTTP {response.status}"


def _redact(detail: str, params: dict) -> str:
    """Remove secret parameter values from anything that becomes an error string.

    `failed_reason` is persisted and logged, and for `auth.otp` the params hold a
    live login code. What a provider echoes back in an error body is the
    provider's choice, not ours, so the value is stripped here rather than
    trusted not to appear.
    """
    for key in SECRET_PARAMS:
        value = params.get(key)
        if value:
            detail = detail.replace(str(value), "[redacted]")
    return detail


def _message_id(body: str) -> str | None:
    """Telegram's id for the sent message. Best effort — a send that succeeded is
    not undone by a response shape we failed to parse."""
    try:
        return str(json.loads(body)["result"]["message_id"])
    except (ValueError, KeyError, TypeError):
        return None
