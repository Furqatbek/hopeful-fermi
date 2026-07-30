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
