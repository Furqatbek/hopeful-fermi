"""The words. Every message this product sends to a person is in this file.

There were none. `notifications` has carried a `template` key, a `params` jsonb
and a `locale` column since migration 0002, and nothing in the repository ever
turned those three into a sentence — so a working transport would have had
nothing to say, and the first symptom in production would have been a student
receiving an empty SMS that had already been paid for.

## Plain data, not a template engine

A dict of format strings and one function. Jinja would be a runtime dependency,
a sandbox question and a second place for logic to hide, to buy loops and
conditionals that six one-line notices do not need. `str.format_map` already
fails on a missing placeholder, which is the only guard that matters here.

## Uzbek first

`uz-Latn` is the default and the fallback, because it is what the user base
speaks — not `en`, which is merely what the code is written in. `ru` and `en`
exist because Tashkent is genuinely trilingual.

An unrecognised locale falls back rather than raising, and that path is not
hypothetical: migration 0003 constrains `users.locale` to
`('uz-Latn','uz-Cyrl','ru','en')`, so **`uz-Cyrl` is a locale a real account can
hold and this file has no body for it.** Such a user gets Uzbek in Latin script
— the same language, a script most of them read — rather than nothing. Adding a
fourth column here is the fix if that is ever judged insufficient; failing to
send a login code over it would not be.

The `uz-Latn` and `en` bodies are deliberately ASCII, `o'` rather than `oʻ`. An
SMS that stays inside GSM-7 is 160 characters; one non-ASCII character switches
the whole message to UCS-2 and halves that to 70, which turns one paid segment
into two. Russian is Cyrillic and pays that cost unavoidably.

## Two loud failures

An unknown template key and a missing parameter both raise, and both are
`PermanentDeliveryError` — no number of retries invents a template body or a
parameter that was never queued, so the notification is marked `failed` with the
reason on the row rather than burning three attempts to reach the same place.

The alternative — rendering what we can and shipping the rest — is what produces
"Your code is {code}" on somebody's lock screen, or a blank message. A message
nobody can act on is worse than a `failed` row somebody can see.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.identity.notify import PermanentDeliveryError

DEFAULT_LOCALE = "uz-Latn"
LOCALES = (DEFAULT_LOCALE, "ru", "en")

# A `params` value that is present but null. `band` is null for an unscored
# attempt and `old_rank` is null for someone who was not previously ranked, and
# both reach this file — so without a substitution the student reads
# "Ball: None", which is a bug report written in their own notification.
NULL_PLACEHOLDER = "-"


class UnknownTemplate(PermanentDeliveryError):
    """A template key with no body. Names the key; never the params."""


class MissingParam(PermanentDeliveryError):
    """A body needs a placeholder the queued `params` does not carry."""


# template key -> locale -> body.
#
# Every key here is queued somewhere in `app/`, and every key queued in `app/` is
# here. As of this commit that is:
#
#     auth.otp                 api/routers/auth.py       otp_request
#     assignment.set           workers/actors.py         notify_assignment
#     attempt.scored           workers/actors.py         notify_scored
#     regrade.band_changed     modules/exam/regrade.py   notifications_for
#     competition.rank_changed modules/competitions/service.py  republish
#     speaking.no_partner      modules/speaking/service.py      match_slot
#
# `tests/identity/test_messages.py` pins that list, so adding a seventh caller
# without a body here fails a test rather than a student's phone.
CATALOGUE: dict[str, dict[str, str]] = {
    # The only message in this table that is a credential. Kept short and
    # front-loaded: on a locked screen the code has to be readable from the
    # notification preview, which is about forty characters.
    "auth.otp": {
        "uz-Latn": "IELTS Hub kodi: {code}. 5 daqiqa amal qiladi. "
                   "Hech kimga aytmang.",
        "ru": "Код IELTS Hub: {code}. Действителен 5 минут. "
              "Никому его не сообщайте.",
        "en": "IELTS Hub code: {code}. Valid for 5 minutes. Do not share it.",
    },
    # `closes_at` is an ISO-8601 timestamp WITH its offset, and it is printed as
    # it arrives. Rendering has no timezone — `Recipient.timezone` is known to
    # the transport and not to this function — and printing a UTC instant as if
    # it were Tashkent local time would state a deadline five hours off. An
    # unambiguous timestamp beats a friendly wrong one for something a student
    # plans their evening around.
    "assignment.set": {
        "uz-Latn": "Yangi topshiriq: {test_title}. Muddat: {closes_at}.",
        "ru": "Новое задание: {test_title}. Срок: {closes_at}.",
        "en": "New assignment: {test_title}. Due: {closes_at}.",
    },
    "attempt.scored": {
        "uz-Latn": "Ishingiz tekshirildi. Ball: {band}.",
        "ru": "Ваша работа проверена. Балл: {band}.",
        "en": "Your attempt has been scored. Band: {band}.",
    },
    # Says the KEY was corrected, not that the student's answer was. A band that
    # moves without a stated cause reads as the server having been wrong about
    # them, which is the complaint the whole regrade audit trail exists to avoid.
    "regrade.band_changed": {
        "uz-Latn": "Javob kaliti to'g'rilandi. Ballingiz {old_band} dan "
                   "{new_band} ga o'zgardi.",
        "ru": "Ключ ответов был исправлен. Ваш балл изменился с {old_band} "
              "на {new_band}.",
        "en": "An answer key was corrected. Your band changed from {old_band} "
              "to {new_band}.",
    },
    # `{notice}` is the admin's public explanation and is last on purpose: it is
    # the only free text in this file, it is often empty, and `render` strips the
    # result so an empty one leaves no trailing space.
    "competition.rank_changed": {
        "uz-Latn": "Musobaqa natijalari qayta hisoblandi. O'rningiz {old_rank} "
                   "dan {new_rank} ga o'zgardi. {notice}",
        "ru": "Результаты соревнования пересчитаны. Ваше место изменилось "
              "с {old_rank} на {new_rank}. {notice}",
        "en": "The contest results were recomputed. Your place changed from "
              "{old_rank} to {new_rank}. {notice}",
    },
    # Says whose fault it was not. Someone who booked a slot, showed up and got
    # nobody will otherwise read the silence as being about them.
    "speaking.no_partner": {
        "uz-Latn": "Speaking mashg'ulotiga juftlik topilmadi - navbatda "
                   "ishtirokchi yetarli bo'lmadi. Boshqa vaqtni tanlang.",
        "ru": "Для разговорной практики не нашлось пары - в очереди не хватило "
              "участников. Выберите другое время.",
        "en": "No partner was found for your speaking session - the queue was "
              "too small. Please pick another slot.",
    },
}


def resolve_locale(locale: str) -> str:
    """`ru-RU` is Russian. `uz-Cyrl` has no body here, so it is Uzbek Latin.

    The region-stripping step is not decoration: Telegram and every browser send
    a full tag, so an exact-match-only lookup would send Uzbek to every Russian
    speaker whose client happened to say `ru-RU` — a silent, plausible-looking
    wrong answer.
    """
    if locale in LOCALES:
        return locale
    base = locale.split("-", 1)[0]
    return base if base in LOCALES else DEFAULT_LOCALE


def render(template: str, params: Mapping[str, object], locale: str) -> str:
    """The queued row, as a sentence. Raises rather than guessing."""
    bodies = CATALOGUE.get(template)
    if bodies is None:
        raise UnknownTemplate(
            f"no message body for template {template!r}; add one to "
            "app/modules/identity/messages.py")
    # `.get(..., default)` rather than `[...]`: a body that has not been
    # translated yet should still reach the user in Uzbek. The completeness test
    # is what stops that fallback from being invisible.
    body = bodies.get(resolve_locale(locale), bodies[DEFAULT_LOCALE])
    values = {key: (NULL_PLACEHOLDER if value is None else value)
              for key, value in params.items()}
    try:
        return body.format_map(values).strip()
    except KeyError as missing:
        # `from None`, and only the placeholder NAME in the message. This string
        # is written to `notifications.failed_reason` and logged, and `params`
        # for `auth.otp` holds a live login code — a chained traceback or a repr
        # of the mapping would put it in both.
        raise MissingParam(
            f"template {template!r} needs parameter {missing.args[0]!r}, "
            "which was not queued with the notification") from None
