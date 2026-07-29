"""The storage port, tested against the backend that ships on a laptop.

`FileStorage` exists so the resumable-upload code exercised everywhere else is
the code that runs against S3, rather than a simplified path that happens to
pass. That only holds if it implements the SAME contract, so this file tests the
contract rather than the implementation — every assertion here is one an S3
backend must also satisfy.

`S3Storage` is not tested here: exercising it needs a MinIO container, which is a
dependency the unit suite should not have. The protocol conformance test at the
bottom is what keeps the two from diverging in shape.
"""

from __future__ import annotations

import hashlib

import pytest

from app.platform.storage import (
    PART_SIZE, FileStorage, ObjectRef, S3Storage, Storage, StorageError,
)


@pytest.fixture
def store(tmp_path) -> FileStorage:
    return FileStorage(root=tmp_path, bucket="test-bucket")


class TestBasicObjects:
    def test_put_then_get_round_trips(self, store):
        ref = store.put("a/b.txt", b"hello", content_type="text/plain")
        assert b"".join(store.get("a/b.txt")) == b"hello"
        assert ref.bytes == 5
        assert ref.checksum_sha256 == hashlib.sha256(b"hello").hexdigest()
        assert ref.bucket == "test-bucket"

    def test_the_reference_carries_no_url(self, store):
        """A URL in a row is a provider you cannot leave, and data residency
        here is a legal question rather than a preference (ADR-0001 §5.4)."""
        ref = store.put("k", b"x", content_type="text/plain")
        assert set(ObjectRef.__slots__) == {
            "bucket", "key", "content_type", "bytes", "checksum_sha256"}
        assert "http" not in str(ref)

    def test_stat_reports_size_and_type(self, store):
        store.put("k.m4a", b"0123456789", content_type="audio/mp4")
        stat = store.stat("k.m4a")
        assert stat.bytes == 10 and stat.content_type == "audio/mp4"

    def test_stat_of_a_missing_object_is_none_not_an_error(self, store):
        assert store.stat("nope") is None

    def test_delete_is_idempotent(self, store):
        store.put("k", b"x", content_type="text/plain")
        store.delete("k")
        store.delete("k")
        assert store.stat("k") is None

    def test_getting_a_missing_object_raises(self, store):
        with pytest.raises(StorageError):
            list(store.get("missing"))

    def test_a_key_cannot_escape_the_root(self, store):
        """Keys are server-generated, but treating one as a path is one bug away
        from writing outside the root."""
        with pytest.raises(StorageError):
            store.put("../../etc/passwd", b"x", content_type="text/plain")


class TestRangeReads:
    """Range support is not optional: without it an `<audio>` element cannot seek,
    and on iOS Safari it will not play at all."""

    @pytest.fixture
    def stored(self, store):
        store.put("audio.m4a", bytes(range(256)), content_type="audio/mp4")
        return store

    def test_a_mid_range_returns_exactly_that_slice(self, stored):
        assert b"".join(stored.get("audio.m4a", start=10, end=19)) == bytes(range(10, 20))

    def test_an_open_ended_range_runs_to_the_end(self, stored):
        assert b"".join(stored.get("audio.m4a", start=250)) == bytes(range(250, 256))

    def test_no_range_returns_everything(self, stored):
        assert len(b"".join(stored.get("audio.m4a"))) == 256

    def test_a_single_byte_range(self, stored):
        assert b"".join(stored.get("audio.m4a", start=7, end=7)) == bytes([7])


class TestMultipart:
    def test_parts_assemble_in_order_regardless_of_arrival(self, store):
        """A client on a flaky connection re-sends parts out of order. The object
        must not depend on the order they landed in."""
        upload_id = store.create_multipart("big.bin", content_type="audio/mp4")
        store.put_part(upload_id, 3, b"CCC")
        store.put_part(upload_id, 1, b"AAA")
        store.put_part(upload_id, 2, b"BBB")

        ref = store.complete_multipart("big.bin", upload_id,
                                       [{"n": n, "etag": "x"} for n in (1, 2, 3)])
        assert b"".join(store.get("big.bin")) == b"AAABBBCCC"
        assert ref.bytes == 9
        assert ref.checksum_sha256 == hashlib.sha256(b"AAABBBCCC").hexdigest()

    def test_a_re_sent_part_replaces_rather_than_duplicates(self, store):
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        store.put_part(upload_id, 1, b"first-try")
        store.put_part(upload_id, 1, b"retry")
        ref = store.complete_multipart("k.bin", upload_id, [{"n": 1, "etag": "x"}])
        assert b"".join(store.get("k.bin")) == b"retry"
        assert ref.bytes == 5

    def test_presigned_part_urls_cover_the_whole_file(self, store):
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        parts = store.presign_parts("k.bin", upload_id, count=3, ttl_seconds=60)
        assert [p.part_number for p in parts] == [1, 2, 3]
        assert [p.offset for p in parts] == [0, PART_SIZE, 2 * PART_SIZE]
        assert all(p.url.startswith("http") for p in parts)

    def test_a_presigned_part_url_carries_a_signature(self, store):
        """It stands in for an S3 presigned URL and must gate the same way:
        without the signature the dev route is an open write endpoint."""
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        url = store.presign_parts("k.bin", upload_id, count=1, ttl_seconds=60)[0].url
        assert "sig=" in url

    def test_aborting_discards_the_parts(self, store):
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        store.put_part(upload_id, 1, b"data")
        store.abort_multipart("k.bin", upload_id)
        assert store.stat("k.bin") is None
        assert not (store.root / ".parts" / upload_id).exists()

    def test_aborting_twice_is_not_an_error(self, store):
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        store.abort_multipart("k.bin", upload_id)
        store.abort_multipart("k.bin", upload_id)


class TestFileTransfer:
    def test_upload_file_then_download_round_trips(self, store, tmp_path):
        """The transcode worker's path: download the master, encode, upload the
        result."""
        source = tmp_path / "in.bin"
        source.write_bytes(b"x" * 4096)

        ref = store.upload_file("m/master.wav", source, content_type="audio/wav")
        assert ref.bytes == 4096
        assert ref.checksum_sha256 == hashlib.sha256(b"x" * 4096).hexdigest()

        out = tmp_path / "out.bin"
        store.download("m/master.wav", out)
        assert out.read_bytes() == b"x" * 4096


class TestProtocolConformance:
    def test_both_backends_implement_the_same_surface(self):
        """The guard that keeps the tested backend and the shipped one aligned.

        S3Storage is not exercised here — that needs a MinIO container the unit
        suite should not depend on — so this checks it at least presents the same
        methods with the same signatures. A method added to one and not the other
        fails here rather than in production.
        """
        import inspect

        required = [name for name in dir(Storage)
                    if not name.startswith("_") and callable(getattr(Storage, name, None))]
        assert "presign_parts" in required and "download" in required

        for name in required:
            s3 = inspect.signature(getattr(S3Storage, name))
            local = inspect.signature(getattr(FileStorage, name))
            assert list(s3.parameters) == list(local.parameters), (
                f"{name} differs between backends: {s3} vs {local}")

    def test_the_file_backend_satisfies_the_protocol(self, store):
        assert isinstance(store, Storage)
