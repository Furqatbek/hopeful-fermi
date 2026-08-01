"""The object store, when the object store is this machine's disk.

`FileStorage` was framed throughout as "tests and laptops" and its HTTP surface
as "Dev and test ONLY … never runs in production". Media now lives on the disk of
the box the backend runs on, so both are the shipping path — and three decisions
that "never runs in production" had justified were wrong the moment it did:

  * `put_part` read the whole part into memory, with a 100 MB ceiling;
  * `get_object` read the ENTIRE object into memory and supported no ranges,
    which an `<audio>` element needs to seek and iOS Safari needs to play at all;
  * neither had a coverage floor.

What has not changed is the signature check. Every request here carries an HMAC
over the key and an expiry — the same thing an S3 presigned URL is.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.platform import grants
from app.platform.storage import FileStorage, storage


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def store():
    backend = storage()
    if not isinstance(backend, FileStorage):
        pytest.skip("the file backend is not configured here")
    return backend


@pytest.fixture
def stored(store):
    """A 300 KB object, big enough that a range is a different answer."""
    body = bytes(range(256)) * 1200
    store.put("ranges/section.m4a", body, content_type="audio/mp4")
    return body


def _url(key: str) -> str:
    return (f"/internal/storage/{storage().bucket}/{key}"
            f"?sig={grants.sign_object(key, ttl_seconds=120)}")


class TestTheRouteIsNotCalledDevAnyMore:
    def test_the_prefix_says_what_it_is(self, client, stored):
        """`/dev/storage` in a production URL is a lie a reader believes. The
        presigned URLs `FileStorage` mints point here."""
        assert client.get(_url("ranges/section.m4a")).status_code == 200

    def test_it_stays_out_of_the_contract(self, client):
        """Not part of the product API — it is the object store's own interface —
        and `check_api_coverage` would rightly flag an undeclared path."""
        from app.api.main import create_app

        paths = create_app().openapi()["paths"]
        assert not [p for p in paths if p.startswith("/internal/storage")]


class TestSignatures:
    def test_an_unsigned_read_is_refused(self, client, stored):
        assert client.get(
            f"/internal/storage/{storage().bucket}/ranges/section.m4a?sig=nope"
        ).status_code in (403, 422)

    def test_a_signature_for_another_key_does_not_transfer(self, client, store,
                                                           stored):
        """The signature covers the KEY. Without that it is a bearer token for
        the whole bucket."""
        store.put("ranges/other.m4a", b"x", content_type="audio/mp4")
        borrowed = grants.sign_object("ranges/other.m4a", ttl_seconds=120)
        assert client.get(
            f"/internal/storage/{storage().bucket}/ranges/section.m4a?sig={borrowed}"
        ).status_code == 403

    def test_an_expired_signature_is_refused(self, client, stored):
        stale = grants.sign_object("ranges/section.m4a", ttl_seconds=-1)
        assert client.get(
            f"/internal/storage/{storage().bucket}/ranges/section.m4a?sig={stale}"
        ).status_code == 403


class TestRanges:
    """"Without them an `<audio>` element cannot seek, and on iOS Safari it will
    not play at all." The old handler returned the whole object every time."""

    def test_the_whole_object_when_no_range_is_asked_for(self, client, stored):
        response = client.get(_url("ranges/section.m4a"))
        assert response.status_code == 200
        assert response.content == stored
        assert response.headers["Accept-Ranges"] == "bytes"

    def test_a_closed_range(self, client, stored):
        response = client.get(_url("ranges/section.m4a"),
                              headers={"Range": "bytes=0-99"})
        assert response.status_code == 206
        assert response.content == stored[:100]
        assert response.headers["Content-Range"] == f"bytes 0-99/{len(stored)}"
        assert response.headers["Content-Length"] == "100"

    def test_an_open_ended_range(self, client, stored):
        """`bytes=1000-` is what a player sends when it seeks."""
        response = client.get(_url("ranges/section.m4a"),
                              headers={"Range": "bytes=1000-"})
        assert response.status_code == 206
        assert response.content == stored[1000:]

    def test_a_suffix_range(self, client, stored):
        """`bytes=-500` — the last 500 bytes. Players use it to read a trailing
        atom before deciding how to stream the rest."""
        response = client.get(_url("ranges/section.m4a"),
                              headers={"Range": "bytes=-500"})
        assert response.status_code == 206
        assert response.content == stored[-500:]

    def test_a_range_past_the_end_is_a_416(self, client, stored):
        response = client.get(_url("ranges/section.m4a"),
                              headers={"Range": f"bytes={len(stored) + 10}-"})
        assert response.status_code == 416
        assert response.headers["Content-Range"] == f"bytes */{len(stored)}"

    def test_a_range_that_overruns_the_end_is_clamped(self, client, stored):
        """A player that asks for more than there is gets what there is, not a
        416 — this is the ordinary last-chunk request."""
        response = client.get(_url("ranges/section.m4a"),
                              headers={"Range": f"bytes=0-{len(stored) + 500}"})
        assert response.status_code == 206
        assert response.content == stored

    @pytest.mark.parametrize("header", ["bytes=abc", "items=0-10", "bytes=-", "",
                                        "bytes=500-100"])
    def test_a_malformed_range_sends_the_whole_thing(self, client, stored, header):
        """Ignored rather than refused. A malformed Range means "send the whole
        thing"; refusing would break a player over a header it did not have to
        send."""
        response = client.get(_url("ranges/section.m4a"),
                              headers={"Range": header} if header else {})
        assert response.status_code == 200
        assert response.content == stored


class TestCaching:
    def test_an_exam_section_is_never_stored_by_a_cache(self, client, stored):
        """"A shared device in a computer lab must not keep an exam section in
        its disk cache after the student logs out." """
        directives = {d.strip() for d in
                      client.get(_url("ranges/section.m4a")
                                 ).headers["Cache-Control"].split(",")}
        assert directives == {"private", "no-store"}


class TestPartUploadsAreStreamed:
    """`await request.body()` held the whole part in RAM for as long as the
    slowest uploader took, with a 100 MB ceiling, on a box whose entire budget is
    fifty dollars a month."""

    def _sig(self, upload_id: str, part: int) -> str:
        return grants.sign_object(f"{upload_id}:{part}", ttl_seconds=120)

    def test_a_part_lands_on_disk_and_returns_its_etag(self, client, store):
        upload_id = store.create_multipart("k/part.m4a", content_type="audio/mp4")
        body = b"a" * 4096
        response = client.put(
            f"/internal/storage/parts/{upload_id}/1?sig={self._sig(upload_id, 1)}",
            content=body)
        assert response.status_code == 200
        assert response.headers["ETag"]
        assert (store.root / ".parts" / upload_id / "00001").read_bytes() == body

    def test_the_etag_matches_the_unstreamed_path(self, client, store):
        """`stream_part` hashes as the bytes go past rather than reading the file
        back. It must agree with `put_part`, which the completion step and every
        S3 client compare against."""
        body = b"b" * 8192
        one = store.create_multipart("k/a", content_type="audio/mp4")
        two = store.create_multipart("k/b", content_type="audio/mp4")
        client.put(f"/internal/storage/parts/{one}/1?sig={self._sig(one, 1)}",
                   content=body)
        streamed = (store.root / ".parts" / one / "00001").read_bytes()
        assert store.put_part(two, 1, body)
        assert streamed == body

    def test_a_declared_oversize_is_refused_without_reading_the_body(
            self, client, store, monkeypatch):
        """The header check is an EARLY-OUT, and its only observable property is
        that the body is never read.

        Deleting it leaves every other test here passing, because the streaming
        limit catches the same request a few megabytes later — the two are
        redundant on outcome and differ only in work done. So the assertion has
        to be about the work: a 2 GB upload that announces itself should cost one
        response, not 8 MB of disk writes first.
        """
        from app.api.routers.object_storage import MAX_PART_BYTES
        from app.platform.storage import FileStorage

        reached = []
        original = FileStorage.stream_part

        async def spy(self, *args, **kwargs):
            reached.append(1)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(FileStorage, "stream_part", spy)
        upload_id = store.create_multipart("k/big", content_type="audio/mp4")
        response = client.put(
            f"/internal/storage/parts/{upload_id}/1?sig={self._sig(upload_id, 1)}",
            content=b"x" * (MAX_PART_BYTES + 1))
        assert response.status_code == 413
        assert reached == [], "the body was read before the size was checked"

    def test_a_part_that_declares_no_length_is_still_bounded(self, client, store):
        """**Two guards, and only one was being tested.**

        A chunked upload carries no `Content-Length`, so the header check cannot
        fire and the limit inside `stream_part` is the only thing standing
        between a client and the disk. Removing the header check left every test
        here passing, because the streaming limit caught it — and removing the
        streaming limit would have done the same in reverse.
        """
        from app.api.routers.object_storage import MAX_PART_BYTES

        upload_id = store.create_multipart("k/chunked", content_type="audio/mp4")
        oversized = (b"x" * 65536 for _ in range((MAX_PART_BYTES // 65536) + 2))
        response = client.put(
            f"/internal/storage/parts/{upload_id}/1?sig={self._sig(upload_id, 1)}",
            content=oversized)
        assert response.status_code == 413

    def test_and_leaves_nothing_behind(self, client, store):
        """Otherwise the completion step concatenates a truncated part into a
        corrupt object and the checksum mismatch surfaces in a worker.

        Chunked, for the same reason: with a declared length the request is
        refused before `stream_part` opens a file, so a test using one asserts
        that nothing was cleaned up because nothing was ever written.
        """
        from app.api.routers.object_storage import MAX_PART_BYTES

        upload_id = store.create_multipart("k/big2", content_type="audio/mp4")
        oversized = (b"x" * 65536 for _ in range((MAX_PART_BYTES // 65536) + 2))
        client.put(
            f"/internal/storage/parts/{upload_id}/1?sig={self._sig(upload_id, 1)}",
            content=oversized)
        assert not (store.root / ".parts" / upload_id / "00001").exists()

    def test_an_unsigned_part_upload_is_refused(self, client, store):
        upload_id = store.create_multipart("k/unsigned", content_type="audio/mp4")
        assert client.put(
            f"/internal/storage/parts/{upload_id}/1?sig=nope", content=b"x"
        ).status_code in (403, 422)


class TestCompletionStreams:
    def test_parts_are_concatenated_without_reading_one_whole(self, client, store):
        """`part.read_bytes()` made peak memory a function of `PART_SIZE`, a
        constant this file does not own."""
        upload_id = store.create_multipart("k/whole.m4a", content_type="audio/mp4")
        store.put_part(upload_id, 1, b"1" * 1000)
        store.put_part(upload_id, 2, b"2" * 1000)
        ref = store.complete_multipart("k/whole.m4a", upload_id,
                                       [{"part_number": 1}, {"part_number": 2}])
        assert ref.bytes == 2000
        assert b"".join(store.get("k/whole.m4a")) == b"1" * 1000 + b"2" * 1000


class TestWhatIsNotThere:
    def test_a_signed_url_for_a_missing_object_is_a_404(self, client, store):
        """Signatures are minted over a key, not over the existence of one — a
        presign for an object a worker has since removed is well-formed and
        points at nothing."""
        assert client.get(_url("ranges/deleted.m4a")).status_code == 404

    def test_the_route_refuses_to_serve_an_s3_backend(self, client, monkeypatch):
        """`main` mounts this only when `storage_backend == "file"`, so the guard
        is against a half-applied config change rather than a live path — a
        `_backend()` that returned an `S3Storage` would reach for `.root` and
        answer 500. It is defensive, but it is one line and it is reachable by
        exactly the mistake it names."""
        from app.api.routers import object_storage
        from app.platform.storage import S3Storage

        monkeypatch.setattr(object_storage, "storage",
                            lambda: object.__new__(S3Storage))
        assert client.get(_url("ranges/section.m4a")).status_code == 404
