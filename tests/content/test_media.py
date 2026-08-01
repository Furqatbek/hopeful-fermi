"""What a file has to be before this platform will store it.

`tests/integration/test_media.py` runs the upload against a real object store and
a real ffmpeg. This is the pure half — `_validate_request` is a function over a
kind, a content type and a size, and the whole of it can be exercised without a
service.

It exists because of the line it replaced:

    allowed = ALLOWED_AUDIO if kind == "audio" else ALLOWED_IMAGE

Five kinds, two branches.
"""

from __future__ import annotations

import pytest


class TestEveryKindIsValidatedAsItsOwnKind:
    """`allowed = ALLOWED_AUDIO if kind == "audio" else ALLOWED_IMAGE`.

    `media_assets.kind` permits five values. A binary either/or over a column
    with five is a bug waiting for the second caller: a `document` upload was
    checked against the IMAGE allowlist and refused with "application/pdf is not
    a supported document format. Supported: image/jpeg, image/png…".

    Only audio has a caller today, which is exactly why nobody had met it.
    """

    def test_a_pdf_is_a_document_not_a_bad_image(self):
        from app.modules.content.media import _validate_request

        _validate_request("document", "application/pdf", 1000,
                          {"claim": "original", "statement_version": "1"})

    def test_a_zip_is_an_archive(self):
        from app.modules.content.media import _validate_request

        _validate_request("archive", "application/zip", 1000,
                          {"claim": "original", "statement_version": "1"})

    def test_a_video_is_stored(self):
        """Media lives on the backend's own disk now, and video is one of the
        things that goes there. Stored and served as uploaded — there is no
        transcode, because the audio pipeline exists for loudness normalisation
        and play-once, which are properties of a listening section rather than of
        a file."""
        from app.modules.content.media import _validate_request

        _validate_request("video", "video/mp4", 1000,
                          {"claim": "original", "statement_version": "1"})

    def test_a_video_in_an_audio_container_is_refused(self):
        from app.modules.content.media import _validate_request
        from app.platform.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            _validate_request("video", "audio/mpeg", 1000,
                              {"claim": "original", "statement_version": "1"})

    def test_each_kind_has_its_own_ceiling(self):
        """A 40 MB image is a mistake; a 40 MB video is a phone recording."""
        from app.modules.content.media import KINDS

        assert KINDS["video"][1] > KINDS["audio"][1] > KINDS["image"][1]

    def test_an_unknown_kind_is_refused_at_the_door(self):
        """`media_assets.kind` has a CHECK constraint, so this would fail at
        INSERT anyway — as a 500, after the upload was opened, having told the
        teacher nothing."""
        from app.modules.content.media import _validate_request
        from app.platform.errors import ValidationFailed

        with pytest.raises(ValidationFailed) as raised:
            _validate_request("hologram", "video/mp4", 1000,
                              {"claim": "original", "statement_version": "1"})
        assert any(f["code"] == "MEDIA_KIND_UNSUPPORTED"
                   for f in raised.value.extra["findings"])

    def test_the_table_covers_every_kind_the_database_allows(self):
        """The two lists drifting apart is how `document` came to be checked
        against the image allowlist. Read from the migration rather than
        restated, so adding a sixth kind to one and not the other fails here."""
        import pathlib
        import re

        from app.modules.content.media import KINDS

        sql = pathlib.Path("migrations/versions/0024_video_media_kind.py").read_text()
        declared = set(re.findall(r"'(\w+)'",
                                  re.findall(r"CHECK \(kind IN \(([^)]+)\)",
                                             sql)[0]))
        assert declared == set(KINDS)
