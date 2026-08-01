"""The secrets in `config.py` are published, so nothing may deploy holding one.

`jwt_secret` defaulted to `dev-only-change-me`, eighteen characters, committed to
this repository. It signs every access token; it derives the media-grant key in
`platform/grants.py` and the competition payload key in `routers/competitions.py`.
A deployment that never set `JWT_SECRET` was one `git clone` away from an access
token for any user id — including a platform admin — and from opening any signed
media URL.

The argument was already written in this same file, for the payment keys: "a
callback that marks orders paid cannot have a default credential." Nothing in it
was specific to payments.

It surfaced through the warnings gate, which is the point of having one: PyJWT
had been saying `the HMAC key is 18 bytes` on every token operation, roughly
4,900 times per test run, into output no human or CI job read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.platform.config import DEV_PLACEHOLDER, PUBLISHED_DEFAULTS, Settings

REAL = "u7Qk2mB9xLpR4vT6yN8cW3zA5dF1gH0jK2lM4nP6qS8t"


def _settings(**overrides) -> Settings:
    """`Settings` reads `.env` and the process environment, either of which can
    carry a real secret and hide the very default under test."""
    base = {"jwt_secret": REAL, "turn_secret": REAL, "environment": "production",
            "_env_file": None}
    return Settings(**{**base, **overrides})


class TestADeploymentCannotHoldAPublishedSecret:
    @pytest.mark.parametrize("field", ["jwt_secret", "turn_secret"])
    def test_the_default_is_refused_outside_development(self, field):
        with pytest.raises(ValidationError) as exc:
            _settings(**{field: DEV_PLACEHOLDER})
        assert field in str(exc.value)

    @pytest.mark.parametrize("value", sorted(PUBLISHED_DEFAULTS))
    def test_including_the_one_it_used_to_be(self, value):
        """`dev-only-change-me` stays on the list after being replaced. Anyone
        who copied it into a `.env` a year ago still has it there, and the
        replacement would otherwise let exactly that deployment through."""
        with pytest.raises(ValidationError):
            _settings(jwt_secret=value)

    def test_a_short_secret_is_refused_even_if_it_is_original(self):
        """32 bytes is the floor RFC 7518 §3.2 sets for HS256. A key shorter than
        its own hash output is brute-forceable offline, and nobody is watching
        the log of an offline attack."""
        with pytest.raises(ValidationError):
            _settings(jwt_secret="short-but-mine")

    def test_the_message_names_every_offending_field_at_once(self):
        """Two deploys, two failures, two guesses is how a config error becomes
        a forty-minute outage. Report both."""
        with pytest.raises(ValidationError) as exc:
            _settings(jwt_secret=DEV_PLACEHOLDER, turn_secret=DEV_PLACEHOLDER)
        assert "jwt_secret" in str(exc.value) and "turn_secret" in str(exc.value)

    def test_and_says_how_to_generate_one(self):
        """The reader is holding a broken deploy at some unsociable hour."""
        with pytest.raises(ValidationError) as exc:
            _settings(jwt_secret=DEV_PLACEHOLDER)
        assert "secrets.token_urlsafe" in str(exc.value)

    def test_a_real_secret_is_accepted(self):
        """The other half. A validator that refused everything would pass every
        test above."""
        assert _settings().jwt_secret == REAL


class TestDevelopmentStillJustRuns:
    """`git clone && make test` has to work without a `.env`. The check is
    keyed on `environment` because it is the one setting a deployment cannot
    forget — nothing else about a non-development deployment works while it
    still says `development`."""

    def test_the_placeholder_is_fine_in_development(self):
        assert _settings(jwt_secret=DEV_PLACEHOLDER,
                         environment="development").jwt_secret == DEV_PLACEHOLDER

    def test_and_that_is_what_the_defaults_are(self):
        defaults = Settings(_env_file=None)
        assert defaults.jwt_secret == DEV_PLACEHOLDER
        assert defaults.turn_secret == DEV_PLACEHOLDER
        assert defaults.environment == "development"

    def test_the_placeholder_is_long_enough_not_to_warn(self):
        """It is 32+ characters for a reason beyond the check: PyJWT warns below
        that on every single token operation, and a suite that emits ~4,900
        warnings is a suite whose warnings nobody reads. `pyproject.toml` now
        turns warnings into errors, which only holds while the ordinary path is
        quiet."""
        assert len(DEV_PLACEHOLDER) >= 32

    def test_and_still_says_what_it_is(self):
        assert "not-a-real-secret" in DEV_PLACEHOLDER
