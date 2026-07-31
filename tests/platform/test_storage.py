"""The storage port, tested against the backend that ships on a laptop.

`FileStorage` exists so the resumable-upload code exercised everywhere else is
the code that runs against S3, rather than a simplified path that happens to
pass. That only holds if it implements the SAME contract.

Which is why the contract now lives in `tests/storage_contract.py` and runs
against both backends — here against the local disk with no services, and in
`tests/integration/test_s3_storage.py` against a real MinIO. This file used to
claim "every assertion here is one an S3 backend must also satisfy" while never
running any of them against S3, and two of them turned out to be false.

What stays here is what is specific to the local backend: path traversal (an S3
key is not a path), the part directory, and the signature comparison — which is
cheap, runs without a container, and is still worth having even now that
behaviour is checked, because it fails at import rather than at connect.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.platform.storage import FileStorage, S3Storage, Storage, StorageError
from tests.storage_contract import StorageContract


@pytest.fixture
def store(tmp_path) -> FileStorage:
    return FileStorage(root=tmp_path, bucket="test-bucket")


class TestFileStorageContract(StorageContract):
    """The shared contract, on the local backend."""

    @pytest.fixture
    def stored(self, store):
        store.put("r/data.bin", b"0123456789" * 30,
                  content_type="application/octet-stream")
        return store

    @pytest.fixture
    def upload_part(self):
        """`put_part` is the dev-only stand-in for a client PUT to a presigned
        URL. The S3 subclass really does PUT over HTTP."""
        return lambda store, key, upload_id, n, data: store.put_part(upload_id, n, data)


class TestLocalBackendSpecifics:
    """Behaviour that only the disk backend has, or can have."""

    def test_a_key_cannot_escape_the_root(self, store):
        """Keys are server-generated, but treating one as a path is one bug away
        from writing outside the root. An S3 key is not a path, so there is
        nothing to check on that side."""
        with pytest.raises(StorageError):
            store.put("../../etc/passwd", b"x", content_type="text/plain")

    def test_a_re_sent_part_replaces_rather_than_duplicates(self, store):
        """Local only: on S3 a re-PUT part is replaced by the server, and the
        client sends the newest ETag. Here the part file has to be overwritten."""
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        store.put_part(upload_id, 1, b"first-try")
        store.put_part(upload_id, 1, b"retry")
        ref = store.complete_multipart("k.bin", upload_id, [{"n": 1, "etag": "x"}])
        assert b"".join(store.get("k.bin")) == b"retry"
        assert ref.bytes == 5

    def test_the_part_directory_is_removed_on_abort(self, store):
        upload_id = store.create_multipart("k.bin", content_type="audio/mp4")
        store.put_part(upload_id, 1, b"data")
        store.abort_multipart("k.bin", upload_id)
        assert not (store.root / ".parts" / upload_id).exists()

    def test_the_assembled_object_carries_a_content_checksum(self, store):
        """Local only, and the reason the two backends differ here is real: S3's
        ETag for a multipart object is a hash of hashes, so `S3Storage` returns
        an empty checksum and the ingest worker computes it after download."""
        upload_id = store.create_multipart("big.bin", content_type="audio/mp4")
        for n, chunk in ((1, b"AAA"), (2, b"BBB")):
            store.put_part(upload_id, n, chunk)
        ref = store.complete_multipart("big.bin", upload_id,
                                       [{"n": n, "etag": "x"} for n in (1, 2)])
        assert ref.checksum_sha256 == hashlib.sha256(b"AAABBB").hexdigest()


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


class TestTheProviderErrorHelpers:
    """`_code` and `_is_missing` decide whether a failure means "no such object"
    or "the provider is down", and `content/media.py` acts on the difference: a
    missing object marks an asset FAILED and tells the author their upload did
    not land.

    The old `stat()` was `except Exception: return None`, so during an outage
    every object in the system would have reported itself missing and the ingest
    path would have recorded data loss that had not happened. These two functions
    are what narrowed it, and they are pure functions over an exception's shape —
    so they are checked against real `botocore` exceptions here rather than
    against whatever a particular provider happens to return today.
    """

    @staticmethod
    def _client_error(code: str):
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")

    @pytest.mark.parametrize("code", ["404", "NoSuchKey", "NoSuchBucket", "NotFound"])
    def test_the_not_found_codes_read_as_missing(self, code):
        from app.platform.storage import _is_missing

        assert _is_missing(self._client_error(code)) is True

    @pytest.mark.parametrize("code", ["AccessDenied", "SlowDown", "InternalError",
                                      "RequestTimeout"])
    def test_everything_else_is_an_outage_not_an_absence(self, code):
        """The half that matters. `AccessDenied` on a misconfigured deployment
        must not read as "the file is gone"."""
        from app.platform.storage import _is_missing

        assert _is_missing(self._client_error(code)) is False

    def test_a_connection_error_is_not_a_missing_object(self):
        """Nothing answered, so nothing said the object was absent."""
        from botocore.exceptions import EndpointConnectionError

        from app.platform.storage import _is_missing

        assert _is_missing(EndpointConnectionError(endpoint_url="http://x")) is False

    def test_the_code_is_readable_for_an_operator(self):
        from app.platform.storage import _code

        assert _code(self._client_error("AccessDenied")) == "AccessDenied"

    def test_an_exception_with_no_code_has_none(self):
        """A `BotoCoreError` carries no HTTP response, so there is no code to
        read — and `None` is what the caller compares against `"NoSuchUpload"`."""
        from botocore.exceptions import EndpointConnectionError

        from app.platform.storage import _code

        assert _code(EndpointConnectionError(endpoint_url="http://x")) is None


class TestTheProcessWideInstance:
    def test_it_is_built_once_and_reused(self):
        """`storage()` memoizes because every router and every actor resolves it
        independently — that is the point of the singleton, and two instances
        would mean two `FileStorage` roots or two boto3 clients."""
        from app.platform.storage import set_storage, storage

        set_storage(None)
        try:
            assert storage() is storage()
        finally:
            set_storage(None)

    def test_the_backend_follows_the_configuration(self, monkeypatch):
        """"`file` needs nothing and is the default; `s3` points at any
        S3-compatible endpoint — MinIO locally, Hetzner or a Tashkent IDC in
        production." Data residency is a stated constraint, and this one setting
        is what makes moving providers a deployment change."""
        from app.platform import storage as storage_module
        from app.platform.config import settings

        for backend, expected in (("file", FileStorage), ("s3", S3Storage)):
            monkeypatch.setenv("STORAGE_BACKEND", backend)
            settings.cache_clear()
            storage_module.set_storage(None)
            try:
                assert isinstance(storage_module.storage(), expected)
            finally:
                storage_module.set_storage(None)
                settings.cache_clear()


class TestScratchDir:
    """Where the transcode worker unpacks a master before probing it.

    "A 40 MB WAV plus its transcoded output on a container with a small writable
    layer is exactly the surprise that takes a box down at 3 a.m., and pointing it
    at a mounted volume must not need a code change."
    """

    def test_it_defaults_to_the_system_temp_dir(self, monkeypatch):
        import tempfile

        from app.platform.config import settings
        from app.platform.storage import scratch_dir

        monkeypatch.delenv("MEDIA_SCRATCH_DIR", raising=False)
        settings.cache_clear()
        try:
            assert scratch_dir() == Path(tempfile.gettempdir())
        finally:
            settings.cache_clear()

    def test_a_configured_directory_is_created_if_absent(self, monkeypatch, tmp_path):
        """Created, not required to exist. A mounted volume on a fresh container
        is empty, and a worker that refused to start until somebody mkdir'd it
        would be down for the length of one deploy."""
        from app.platform.config import settings
        from app.platform.storage import scratch_dir

        target = tmp_path / "media" / "scratch"
        monkeypatch.setenv("MEDIA_SCRATCH_DIR", str(target))
        settings.cache_clear()
        try:
            assert scratch_dir() == target
            assert target.is_dir()
        finally:
            settings.cache_clear()
