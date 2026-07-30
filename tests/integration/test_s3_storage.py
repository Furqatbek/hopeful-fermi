"""`S3Storage` against a real S3-compatible server.

Until now this class was covered by a signature comparison and nothing else, so
**the first real S3 deploy would have been its first real test** — noted as an
open item since `0009` §7. The gap mattered more than it sounds: `FileStorage`
implements multipart by writing part files to a directory, which is not remotely
how S3 behaves. Minimum part sizes, ETags, `NoSuchUpload`, presigned SigV4 query
strings and path-style addressing all exist only on the S3 side.

Most of what runs here is `StorageContract`, the same assertions
`tests/platform/test_storage.py` runs against the local backend. That is the
point: the two backends are interchangeable by *behaviour*, not by having the
same method names.

The rest is S3-specific, and the important one is `TestPresignedUploadOverHttp`.
Deliverable 3 promises a teacher's 40 MB WAV goes from their browser to the
object store without touching the app server — the whole reason `presign_parts`
exists. Nothing had ever PUT to one of those URLs.

Needs an endpoint:

    S3_ENDPOINT=http://localhost:9000 S3_ACCESS_KEY=minioadmin \\
    S3_SECRET_KEY=minioadmin python3 -m pytest tests/integration/test_s3_storage.py

Skipped without one — and under `CI` a skip is a failure, so the runner has to
provide the service. `.github/workflows/ci.yml` runs `minio/minio`.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from app.platform.config import settings
from app.platform.storage import PART_SIZE, S3Storage, Storage, StorageError
from tests.storage_contract import PART_A, PART_C, StorageContract

pytestmark = pytest.mark.skipif(
    not os.environ.get("S3_ENDPOINT"),
    reason="set S3_ENDPOINT to run the S3 storage tests (CI runs MinIO)")


@pytest.fixture
def s3_store():
    """A brand-new bucket per test, emptied and removed afterwards.

    Per test rather than per session because multipart leaves uploads behind on
    failure, and a leaked in-progress upload in a shared bucket makes the next
    test's failure someone else's fault.
    """
    endpoint = os.environ["S3_ENDPOINT"]
    bucket = f"ielts-test-{uuid.uuid4().hex[:12]}"

    previous = {k: os.environ.get(k) for k in
                ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY",
                 "S3_REGION", "STORAGE_BACKEND")}
    os.environ.update({
        "S3_ENDPOINT": endpoint,
        "S3_BUCKET": bucket,
        "S3_ACCESS_KEY": os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        "S3_SECRET_KEY": os.environ.get("S3_SECRET_KEY", "minioadmin"),
        "S3_REGION": os.environ.get("S3_REGION", "us-east-1"),
        "STORAGE_BACKEND": "s3",
    })
    settings.cache_clear()

    store = S3Storage(bucket=bucket, endpoint=endpoint)
    store._client.create_bucket(Bucket=bucket)
    try:
        yield store
    finally:
        _empty(store, bucket)
        store._client.delete_bucket(Bucket=bucket)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        settings.cache_clear()


def _empty(store, bucket: str) -> None:
    """Objects AND unfinished multipart uploads. A bucket with a dangling upload
    refuses to delete, which would turn one failing test into a leaked bucket
    every run."""
    pages = store._client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
    for page in pages:
        for obj in page.get("Contents", []):
            store._client.delete_object(Bucket=bucket, Key=obj["Key"])
    uploads = store._client.list_multipart_uploads(Bucket=bucket)
    for upload in uploads.get("Uploads", []):
        store._client.abort_multipart_upload(
            Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])


def _put_part_over_http(store, key, upload_id, n, data) -> str:
    """Upload a part the way the product does: PUT to a presigned URL.

    Not `_client.upload_part`. The bytes are supposed to bypass this process
    entirely, and a test that calls the SDK directly proves the SDK works rather
    than that our presigned URL does.
    """
    url = store.presign_parts(key, upload_id, count=n, ttl_seconds=600)[n - 1].url
    response = httpx.put(url, content=data, timeout=60.0)
    response.raise_for_status()
    return response.headers["ETag"]


class TestS3StorageContract(StorageContract):
    """The same assertions the local backend satisfies."""

    @pytest.fixture
    def store(self, s3_store):
        return s3_store

    @pytest.fixture
    def stored(self, s3_store):
        s3_store.put("r/data.bin", b"0123456789" * 30,
                     content_type="application/octet-stream")
        return s3_store

    @pytest.fixture
    def upload_part(self):
        return _put_part_over_http


class TestPresignedUploadOverHttp:
    """The promise in Deliverable 3: bytes never touch the app server.

    On a 4 vCPU box shared with the exam endpoints, proxying a 40 MB WAV through
    gunicorn during a mock is how one teacher's upload becomes forty students'
    timeouts. The presigned-URL path is what prevents that, and this is the only
    place it is exercised end to end.
    """

    def test_a_presigned_part_url_accepts_a_put_from_outside_the_app(self, s3_store):
        key = "u/direct.wav"
        upload_id = s3_store.create_multipart(key, content_type="audio/wav")
        etag = _put_part_over_http(s3_store, key, upload_id, 1, PART_A)
        assert etag
        s3_store.complete_multipart(key, upload_id, [{"n": 1, "etag": etag}])
        assert s3_store.stat(key).bytes == PART_SIZE

    def test_a_presigned_get_url_serves_the_bytes(self, s3_store):
        s3_store.put("u/readable.bin", b"payload", content_type="text/plain")
        url = s3_store.presign_get("u/readable.bin", ttl_seconds=60)
        assert httpx.get(url, timeout=30.0).content == b"payload"

    def test_an_expired_presigned_url_is_refused(self, s3_store):
        """"Short-TTL signed URLs" is an anti-scrape commitment from Deliverable
        3, not a comment. A URL that outlives its TTL is a permanent download
        link pasted into a group chat."""
        s3_store.put("u/expiring.bin", b"payload", content_type="text/plain")
        url = s3_store.presign_get("u/expiring.bin", ttl_seconds=1)
        import time

        time.sleep(2)
        assert httpx.get(url, timeout=30.0).status_code == 403

    def test_a_tampered_presigned_url_is_refused(self, s3_store):
        s3_store.put("u/one.bin", b"one", content_type="text/plain")
        s3_store.put("u/two.bin", b"two", content_type="text/plain")
        url = s3_store.presign_get("u/one.bin", ttl_seconds=300)
        assert httpx.get(url.replace("one.bin", "two.bin"), timeout=30.0).status_code == 403


class TestS3Specifics:
    def test_it_uses_path_style_addressing(self, s3_store):
        """Virtual-host buckets need wildcard DNS, which a self-hosted MinIO or a
        Tashkent IDC gateway rarely has. Portability is the entire reason this
        class exists (`storage.py` module docstring)."""
        url = s3_store.presign_get("anything.bin", ttl_seconds=60)
        assert f"/{s3_store.bucket}/" in url

    def test_the_completed_object_has_no_content_checksum(self, s3_store):
        """S3's ETag for a multipart object is a hash of hashes, not of content,
        so it cannot be the integrity value on the row. The ingest worker
        computes SHA-256 after download; `complete_multipart` returns an empty
        checksum and must keep doing so rather than quietly returning the ETag."""
        key = "s/multi.bin"
        upload_id = s3_store.create_multipart(key, content_type="audio/wav")
        parts = [{"n": 1, "etag": _put_part_over_http(s3_store, key, upload_id, 1, PART_A)},
                 {"n": 2, "etag": _put_part_over_http(s3_store, key, upload_id, 2, PART_C)}]
        ref = s3_store.complete_multipart(key, upload_id, parts)
        assert ref.checksum_sha256 == ""
        assert ref.bytes == PART_SIZE + len(PART_C)

    def test_a_part_below_the_minimum_is_refused_by_the_server(self, s3_store):
        """Not our rule — S3's. Worth pinning because `FileStorage` happily
        accepts a 10-byte part, so a change that started chunking small would
        pass every local test and fail on the first real upload."""
        key = "s/tiny.bin"
        upload_id = s3_store.create_multipart(key, content_type="audio/wav")
        first = _put_part_over_http(s3_store, key, upload_id, 1, b"tiny")
        second = _put_part_over_http(s3_store, key, upload_id, 2, b"also tiny")
        with pytest.raises(StorageError, match="EntityTooSmall"):
            s3_store.complete_multipart(key, upload_id, [
                {"n": 1, "etag": first}, {"n": 2, "etag": second}])
        s3_store.abort_multipart(key, upload_id)

    def test_it_satisfies_the_protocol(self, s3_store):
        assert isinstance(s3_store, Storage)

    def test_a_missing_bucket_surfaces_as_a_storage_error(self, s3_store):
        """A typo'd bucket name in config should say so, not raise a botocore
        class the caller cannot import."""
        rogue = S3Storage(bucket="definitely-not-a-bucket-9f3a",
                          endpoint=os.environ["S3_ENDPOINT"])
        with pytest.raises(StorageError):
            list(rogue.get("anything.bin"))

    def test_an_unreachable_endpoint_raises_rather_than_reporting_missing(self):
        """The dangerous half of the `stat` fix.

        `stat()` returning None means "no such object", and callers act on it —
        `content/media.py` treats it as "the upload did not land" and marks the
        asset failed. The old implementation was `except Exception: return None`,
        so during an outage every object in the system would have reported itself
        missing and the ingest path would have recorded data loss that had not
        happened.

        Port 1 is reserved and never listens.
        """
        dead = S3Storage(bucket="anything", endpoint="http://127.0.0.1:1")
        with pytest.raises(StorageError):
            dead.stat("some/key.bin")

    def test_a_genuinely_missing_object_still_stats_as_none(self, s3_store):
        """The other half: narrowing must not turn "not found" into an error."""
        assert s3_store.stat("definitely/not/here.bin") is None
