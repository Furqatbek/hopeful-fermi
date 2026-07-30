"""Object signatures: the three ways `verify_object` says no.

`sign_object` stands in for an S3 presigned URL when the file backend is in use,
and `api/routers/dev_storage.py` is the route it points at — an **upload**
endpoint as well as a download one. Its only gate is this function.

A coverage run found that all three rejection branches were unexecuted: the happy
path was tested through the resumable-upload flow and nothing ever presented a
bad signature. A verifier whose refusals have never run is a verifier nobody has
checked, and here that is the difference between a dev backend and an open write
endpoint on anything that happens to boot with `STORAGE_BACKEND=file`.

The media-grant half of this module (`issue`/`verify`, which binds user, object,
expiry and purpose) is covered by `tests/integration/test_media.py`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.platform import grants
from app.platform.errors import Forbidden

KEY = "media/2026/07/master.wav"
NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC)


class TestObjectSignatures:
    def test_a_fresh_signature_verifies(self):
        sig = grants.sign_object(KEY, ttl_seconds=300, now=NOW)
        assert grants.verify_object(KEY, sig, now=NOW) is None

    def test_a_signature_for_another_key_is_refused(self):
        """The one that matters most. Without it, a signature handed out for a
        practice track opens every object in the bucket."""
        sig = grants.sign_object(KEY, ttl_seconds=300, now=NOW)
        with pytest.raises(Forbidden) as refused:
            grants.verify_object("media/2026/07/exam-section-1.wav", sig, now=NOW)
        assert refused.value.code == "grant_bad_signature"

    def test_a_tampered_mac_is_refused(self):
        sig = grants.sign_object(KEY, ttl_seconds=300, now=NOW)
        expires, mac = sig.split(".")
        forged = f"{expires}.{'A' * len(mac)}"
        with pytest.raises(Forbidden) as refused:
            grants.verify_object(KEY, forged, now=NOW)
        assert refused.value.code == "grant_bad_signature"

    def test_extending_the_expiry_invalidates_the_mac(self):
        """The expiry is inside the MAC, so a client cannot buy itself another
        hour by editing the query string."""
        sig = grants.sign_object(KEY, ttl_seconds=60, now=NOW)
        expires, mac = sig.split(".")
        with pytest.raises(Forbidden) as refused:
            grants.verify_object(KEY, f"{int(expires) + 3600}.{mac}", now=NOW)
        assert refused.value.code == "grant_bad_signature"

    def test_an_expired_signature_is_refused(self):
        sig = grants.sign_object(KEY, ttl_seconds=60, now=NOW)
        later = NOW + dt.timedelta(seconds=61)
        with pytest.raises(Forbidden) as refused:
            grants.verify_object(KEY, sig, now=later)
        assert refused.value.code == "grant_expired"

    def test_it_is_still_valid_one_second_before_expiry(self):
        sig = grants.sign_object(KEY, ttl_seconds=60, now=NOW)
        assert grants.verify_object(KEY, sig, now=NOW + dt.timedelta(seconds=59)) is None

    @pytest.mark.parametrize("bad", ["", "nonsense", "no-dot-here",
                                     "notanumber.abc", ".abc"])
    def test_a_malformed_signature_is_refused_rather_than_crashing(self, bad):
        """An unparseable `?sig=` must be a 403, not a 500. A stack trace from a
        query parameter is both an availability bug and a disclosure one."""
        with pytest.raises(Forbidden) as refused:
            grants.verify_object(KEY, bad, now=NOW)
        assert refused.value.code == "grant_malformed"

    def test_it_raises_rather_than_returning_false(self):
        """Same reasoning as `verify` for media grants: a function returning
        False is one an exhausted caller wraps in `if not verify(...)` and
        inverts. There is no falsy return to misread."""
        sig = grants.sign_object(KEY, ttl_seconds=300, now=NOW)
        assert grants.verify_object(KEY, sig, now=NOW) is None

    def test_a_media_grant_that_is_not_a_grant_at_all_is_refused(self):
        """`verify` for media grants, not object signatures: the malformed branch
        maps a junk token to a 403 rather than letting a decode error escape as a
        500 from a query parameter."""
        for junk in ("", "not.a.grant", "aaaa", "x" * 200):
            with pytest.raises(Forbidden):
                grants.verify(junk, user_xid="u", media_xid="m", now=NOW)

    def test_signatures_differ_by_key(self):
        a = grants.sign_object("a.wav", ttl_seconds=300, now=NOW)
        b = grants.sign_object("b.wav", ttl_seconds=300, now=NOW)
        assert a != b
