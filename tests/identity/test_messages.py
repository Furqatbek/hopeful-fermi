"""The message bodies, and the two failures that must be loud.

There was no text anywhere in this repository. `notifications` stored a template
key, a params jsonb and a locale, and nothing turned them into a sentence — so
the interesting cases are not "does Uzbek come out" but the two ways a renderer
quietly ships nonsense: a key with no body, and a body whose placeholder was
never queued. Both are asserted here against the real catalogue rather than a
fixture, because a fixture would let the shipped bodies drift away from the tests
that claim to check them.
"""

from __future__ import annotations

import pytest

from app.modules.identity import messages
from app.modules.identity.notify import PermanentDeliveryError

# Every template key queued anywhere in `app/`, with the call site. Found by
# grepping `notify.queue`, `template=` and `INSERT INTO notifications`; the last
# two are why `competition.rank_changed` and `speaking.no_partner` are easy to
# miss — both are written as SQL literals, not as a keyword argument.
#
# Pinned as an equality so both directions fail: a new caller with no body, and a
# body for a message nothing sends.
QUEUED = {
    "auth.otp": {"code": "123456"},                        # api/routers/auth.py
    "assignment.set": {"assignment_xid": "a-1",            # workers/actors.py
                       "test_title": "Cambridge 17 Test 1",
                       "closes_at": "2026-08-10T12:00:00+00:00"},
    "attempt.scored": {"attempt_xid": "at-1", "band": 6.5},         # workers/actors.py
    "regrade.band_changed": {"attempt_xid": "at-1", "old_band": 6.0,  # exam/regrade.py
                             "new_band": 6.5, "direction": "up",
                             "regrade_job_xid": "rj-1"},
    "competition.rank_changed": {"competition_xid": "c-1",  # competitions/service.py
                                 "old_rank": 4, "new_rank": 2,
                                 "notice": "Item 7 was rekeyed."},
    "speaking.no_partner": {"slot_xid": "s-1"},             # speaking/service.py
}


class TestTheCatalogueCoversWhatIsQueued:
    def test_every_queued_template_has_a_body(self):
        assert set(messages.CATALOGUE) == set(QUEUED)

    @pytest.mark.parametrize("template", sorted(QUEUED))
    def test_every_template_renders_in_every_locale(self, template):
        """A locale missing from one entry falls back silently at runtime, which
        is the right behaviour for a user and the wrong one for a release: the
        student gets Uzbek and nobody learns the translation was never written."""
        for locale in messages.LOCALES:
            rendered = messages.render(template, QUEUED[template], locale)
            assert rendered and "{" not in rendered

    @pytest.mark.parametrize("template", sorted(QUEUED))
    def test_the_uzbek_body_differs_from_the_english_one(self, template):
        """Guards against an entry added with the English string pasted into all
        three slots, which passes every other test in this file."""
        assert (messages.render(template, QUEUED[template], "uz-Latn")
                != messages.render(template, QUEUED[template], "en"))

    def test_the_latin_bodies_stay_inside_gsm_7(self):
        """One non-ASCII character switches an SMS from 160 characters to 70 and
        turns one paid segment into two. Russian pays that unavoidably; Uzbek
        Latin and English do not have to."""
        offenders = [(template, locale, character)
                     for template, bodies in messages.CATALOGUE.items()
                     for locale in ("uz-Latn", "en")
                     for character in bodies[locale] if ord(character) > 127]
        assert offenders == []


class TestAnUnknownTemplateIsLoud:
    def test_it_raises(self):
        with pytest.raises(messages.UnknownTemplate):
            messages.render("invoice.overdue", {}, "en")

    def test_the_message_names_the_key(self):
        with pytest.raises(messages.UnknownTemplate, match="invoice.overdue"):
            messages.render("invoice.overdue", {}, "en")

    def test_it_is_permanent_so_deliver_does_not_retry_it(self):
        """No number of attempts invents a template body. Classifying it as
        permanent is what puts the reason on the row now instead of after three
        attempts and fifteen minutes."""
        assert issubclass(messages.UnknownTemplate, PermanentDeliveryError)


class TestAMissingParamIsLoud:
    def test_it_raises_rather_than_delivering_the_placeholder(self):
        """"Your code is {code}" delivered literally is worse than no message:
        the student cannot act on it and the code is spent."""
        with pytest.raises(messages.MissingParam):
            messages.render("auth.otp", {}, "uz-Latn")

    def test_the_message_names_the_placeholder(self):
        with pytest.raises(messages.MissingParam, match="code"):
            messages.render("auth.otp", {}, "uz-Latn")

    def test_a_partially_supplied_body_still_raises(self):
        with pytest.raises(messages.MissingParam, match="new_band"):
            messages.render("regrade.band_changed", {"old_band": 6.0}, "en")

    def test_it_is_permanent(self):
        assert issubclass(messages.MissingParam, PermanentDeliveryError)

    def test_the_failure_does_not_carry_the_other_params(self):
        """`MissingParam` becomes `notifications.failed_reason`, which is stored
        and logged. An `auth.otp` row that is missing some OTHER placeholder must
        not put the live code in there on its way out."""
        with pytest.raises(messages.MissingParam) as raised:
            messages.render("assignment.set", {"code": "424242"}, "en")
        assert "424242" not in str(raised.value)
        # `raise ... from None`. The chained `KeyError` would otherwise be
        # printed under "During handling of the above exception", and a traceback
        # of `format_map` carries the mapping it was called with.
        assert raised.value.__suppress_context__


class TestLocaleFallback:
    def test_uz_cyrl_falls_back_rather_than_failing_to_send(self):
        """A real value, not a hypothetical one: migration 0003 constrains
        `users.locale` to `('uz-Latn','uz-Cyrl','ru','en')` and there is no
        Cyrillic-script body, so this fallback runs for real accounts."""
        assert (messages.render("auth.otp", {"code": "123456"}, "uz-Cyrl")
                == messages.render("auth.otp", {"code": "123456"}, "uz-Latn"))

    def test_a_locale_from_outside_the_schema_also_falls_back(self):
        assert (messages.render("auth.otp", {"code": "123456"}, "tr")
                == messages.render("auth.otp", {"code": "123456"}, "uz-Latn"))

    def test_a_regional_tag_resolves_to_its_language(self):
        """Telegram and every browser send `ru-RU`, not `ru`. Exact-match-only
        would send Uzbek to every Russian speaker and look like it worked."""
        assert (messages.render("auth.otp", {"code": "123456"}, "ru-RU")
                == messages.render("auth.otp", {"code": "123456"}, "ru"))

    def test_an_empty_locale_falls_back(self):
        assert messages.resolve_locale("") == messages.DEFAULT_LOCALE

    def test_the_default_is_uzbek_not_english(self):
        """The user base is Uzbek. English is what the code is written in, which
        is not the same fact."""
        assert messages.DEFAULT_LOCALE == "uz-Latn"
        assert messages.resolve_locale("de") == "uz-Latn"


class TestValues:
    def test_the_code_appears_verbatim(self):
        """The one message whose entire content is a credential. If the digits
        are transformed on the way through, the student types something the
        server will not accept."""
        assert "428913" in messages.render("auth.otp", {"code": "428913"}, "uz-Latn")

    def test_a_null_value_is_not_rendered_as_none(self):
        """`band` is null for an unscored attempt and `old_rank` is null for
        somebody not previously ranked. Both reach this renderer."""
        rendered = messages.render("attempt.scored",
                                   {"attempt_xid": "a", "band": None}, "en")
        assert "None" not in rendered
        assert messages.NULL_PLACEHOLDER in rendered

    def test_an_empty_notice_leaves_no_trailing_space(self):
        rendered = messages.render(
            "competition.rank_changed",
            {"competition_xid": "c", "old_rank": 3, "new_rank": 1, "notice": ""},
            "en")
        assert rendered == rendered.strip()
        assert not rendered.endswith(" .")

    def test_extra_params_are_ignored(self):
        """Callers queue more than the body uses — `assignment_xid` is a deep
        link, not text — and adding one must not break delivery."""
        assert messages.render("speaking.no_partner",
                               {"slot_xid": "s", "unused": "x"}, "en")

    def test_braces_in_a_value_are_not_re_expanded(self):
        """A test title is centre-authored text. If it were re-formatted, a title
        containing `{code}` would read another row's parameters."""
        rendered = messages.render(
            "assignment.set",
            {"test_title": "{code} practice", "closes_at": "2026-08-10",
             "assignment_xid": "a"}, "en")
        assert "{code} practice" in rendered
