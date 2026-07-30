"""Every assertion an implementation of the `Storage` port must satisfy.

`tests/platform/test_storage.py` said it "tests the contract rather than the
implementation — every assertion here is one an S3 backend must also satisfy",
and then only ever ran against `FileStorage`. This module makes that literally
true: both backends subclass it and run the same assertions.

The shape check that used to stand in for this (`TestProtocolConformance`,
comparing method signatures) stays, because it is cheap and it runs without a
container. But a signature is not a contract, and running this against MinIO
proved it: `S3Storage.get` and `.stat` had signatures identical to the local
backend and raised `botocore.errorfactory.NoSuchKey` where the local backend
raised `StorageError` — a provider exception escaping the one module that is
supposed to contain it.

Not collected directly — the filename does not match `test_*.py`.

Subclasses provide two fixtures:

  `store`        an instance of the backend
  `upload_part`  a callable (store, key, upload_id, n, data) -> etag

The second exists because uploading a part is the one operation that is
deliberately NOT on the port: in production the client PUTs to a presigned URL
and the app server never sees the bytes. For S3 the fixture really does PUT over
HTTP to a presigned URL, which is the only place that path is exercised.
"""

from __future__ import annotations

import hashlib

import pytest

from app.platform.storage import PART_SIZE, ObjectRef, StorageError

# Two full parts and a short tail. S3 rejects any part but the last below 5 MiB,
# so a contract that both backends can satisfy has to use the real size — which
# is also the only way the tail-shorter-than-a-part case gets covered at all.
PART_A = b"A" * PART_SIZE
PART_B = b"B" * PART_SIZE
PART_C = b"C" * 1024


class StorageContract:
    """Inherited by one subclass per backend."""

    # ── objects ──────────────────────────────────────────────────────

    def test_put_then_get_round_trips(self, store):
        store.put("a/one.txt", b"hello", content_type="text/plain")
        assert b"".join(store.get("a/one.txt")) == b"hello"

    def test_the_reference_carries_no_url(self, store):
        """Data residency (`storage.py` module docstring): a URL in a row is a
        provider you cannot leave."""
        ref = store.put("a/two.bin", b"xyz", content_type="application/octet-stream")
        assert isinstance(ref, ObjectRef)
        assert ref.checksum_sha256 == hashlib.sha256(b"xyz").hexdigest()
        assert ref.bytes == 3
        for value in (ref.bucket, ref.key, ref.content_type):
            assert "://" not in value

    def test_stat_reports_size_and_type(self, store):
        store.put("a/three.wav", b"z" * 100, content_type="audio/wav")
        stat = store.stat("a/three.wav")
        assert stat.bytes == 100
        assert stat.content_type == "audio/wav"

    def test_stat_of_a_missing_object_is_none_not_an_error(self, store):
        assert store.stat("nope/missing.bin") is None

    def test_delete_removes_and_is_idempotent(self, store):
        store.put("a/gone.txt", b"bye", content_type="text/plain")
        store.delete("a/gone.txt")
        assert store.stat("a/gone.txt") is None
        store.delete("a/gone.txt")

    def test_getting_a_missing_object_raises_storage_error(self, store):
        """A NAMED exception, not "raises something".

        Callers of the port cannot catch `botocore.exceptions.ClientError`
        without importing boto3, which is the one thing this module exists to
        keep out of the rest of the codebase.
        """
        with pytest.raises(StorageError):
            list(store.get("nope/missing.bin"))

    # ── range reads ──────────────────────────────────────────────────
    #
    # An <audio> element cannot seek without these, and iOS Safari will not play
    # at all (docs/design/0009-media.md section 4).

    def test_a_mid_range_returns_exactly_that_slice(self, stored):
        assert b"".join(stored.get("r/data.bin", start=10, end=19)) == b"0123456789"

    def test_an_open_ended_range_runs_to_the_end(self, stored):
        assert b"".join(stored.get("r/data.bin", start=250)) == b"0123456789" * 5

    def test_no_range_returns_everything(self, stored):
        assert len(b"".join(stored.get("r/data.bin"))) == 300

    def test_a_single_byte_range(self, stored):
        assert b"".join(stored.get("r/data.bin", start=0, end=0)) == b"0"

    # ── multipart ────────────────────────────────────────────────────

    def test_parts_assemble_in_order_regardless_of_arrival(self, store, upload_part):
        """The client uploads parts concurrently, so they finish out of order.
        Assembly is by part number, not by arrival."""
        key = "m/out-of-order.bin"
        upload_id = store.create_multipart(key, content_type="audio/wav")
        etags = {}
        for n, data in ((3, PART_C), (1, PART_A), (2, PART_B)):
            etags[n] = upload_part(store, key, upload_id, n, data)

        store.complete_multipart(key, upload_id, [
            {"n": n, "etag": etags[n]} for n in (2, 3, 1)
        ])
        assert b"".join(store.get(key)) == PART_A + PART_B + PART_C

    def test_the_assembled_object_reports_its_full_size(self, store, upload_part):
        key = "m/sized.bin"
        upload_id = store.create_multipart(key, content_type="audio/wav")
        parts = [{"n": n, "etag": upload_part(store, key, upload_id, n, data)}
                 for n, data in ((1, PART_A), (2, PART_C))]
        store.complete_multipart(key, upload_id, parts)
        assert store.stat(key).bytes == PART_SIZE + 1024

    def test_presigned_part_urls_cover_the_whole_file(self, store):
        key = "m/presigned.bin"
        upload_id = store.create_multipart(key, content_type="audio/wav")
        parts = store.presign_parts(key, upload_id, count=4, ttl_seconds=600)

        assert [p.part_number for p in parts] == [1, 2, 3, 4]
        assert [p.offset for p in parts] == [0, PART_SIZE, 2 * PART_SIZE, 3 * PART_SIZE]
        assert all(p.length == PART_SIZE for p in parts)
        store.abort_multipart(key, upload_id)

    def test_a_presigned_part_url_is_signed_and_expiring(self, store):
        key = "m/signed.bin"
        upload_id = store.create_multipart(key, content_type="audio/wav")
        url = store.presign_parts(key, upload_id, count=1, ttl_seconds=600)[0].url
        assert url.startswith("http")
        # Either an S3 SigV4 query string or this app's own HMAC. An unsigned URL
        # is a world-writable object.
        assert "sig=" in url or "X-Amz-Signature=" in url
        store.abort_multipart(key, upload_id)

    def test_a_presigned_get_is_signed(self, store):
        store.put("m/readable.bin", b"data", content_type="application/octet-stream")
        url = store.presign_get("m/readable.bin", ttl_seconds=60)
        assert url.startswith("http")
        assert "sig=" in url or "X-Amz-Signature=" in url

    def test_aborting_discards_the_parts(self, store, upload_part):
        key = "m/aborted.bin"
        upload_id = store.create_multipart(key, content_type="audio/wav")
        upload_part(store, key, upload_id, 1, PART_A)
        store.abort_multipart(key, upload_id)
        assert store.stat(key) is None

    def test_aborting_twice_is_not_an_error(self, store):
        """The client retries a cancel on a flaky connection. A second abort
        must not turn a tidy-up into a 500."""
        key = "m/aborted-twice.bin"
        upload_id = store.create_multipart(key, content_type="audio/wav")
        store.abort_multipart(key, upload_id)
        store.abort_multipart(key, upload_id)

    # ── files ────────────────────────────────────────────────────────

    def test_upload_file_then_download_round_trips(self, store, tmp_path):
        source = tmp_path / "master.wav"
        source.write_bytes(b"x" * 4096)
        ref = store.upload_file("f/master.wav", source, content_type="audio/wav")
        assert ref.bytes == 4096
        assert ref.checksum_sha256 == hashlib.sha256(b"x" * 4096).hexdigest()

        out = tmp_path / "back.wav"
        store.download("f/master.wav", out)
        assert out.read_bytes() == b"x" * 4096
