"""Object storage. The ONLY place an S3 SDK appears (ADR-0001 §5.5).

Every media reference in this system is `(bucket, key, content_type, bytes,
checksum)` — never a URL and never a provider-specific handle. That is not
tidiness: **data residency is a legal question here**, and "we may be required to
store this in-country" is a requirement that only survives if swapping the
provider is a config change. A URL stored in a row is a provider you cannot leave.

Two backends behind one protocol:

  * `S3Storage`   — any S3-compatible endpoint (MinIO in dev, Hetzner or a
    Tashkent IDC in production). Multipart upload parts are PRESIGNED, so a
    40 MB file goes from the teacher's browser to the object store and never
    touches the app server. On a 4 vCPU box that is the difference between an
    upload being free and an upload starving the exam endpoints.
  * `FileStorage` — the local disk. For tests and for a laptop with nothing
    installed. It implements the same protocol including multipart, so the code
    under test is the code that ships.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from app.platform.config import settings

log = structlog.get_logger()

# S3 requires every part except the last to be at least 5 MiB. Also roughly the
# right unit of retry on a dropping 4G connection: small enough to re-send
# cheaply, large enough that a 40 MB file is eight parts and not four hundred.
PART_SIZE = 5 * 1024 * 1024


class StorageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """What goes in the database. No URL, by design."""

    bucket: str
    key: str
    content_type: str
    bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class ObjectStat:
    bytes: int
    content_type: str
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class PartUpload:
    part_number: int
    url: str
    offset: int
    length: int


@runtime_checkable
class Storage(Protocol):
    """The port from ADR-0001 §5.4, plus the multipart half a resumable upload
    needs. Nothing here mentions a provider.

    `runtime_checkable` so a test can assert a backend satisfies it. That is a
    shape check only — Python cannot verify signatures at runtime — which is why
    `TestProtocolConformance` compares the two backends' parameters explicitly.
    """

    bucket: str

    def put(self, key: str, data: bytes, *, content_type: str) -> ObjectRef: ...
    def get(self, key: str, *, start: int | None = None,
            end: int | None = None) -> Iterator[bytes]: ...
    def stat(self, key: str) -> ObjectStat | None: ...
    def delete(self, key: str) -> None: ...
    def presign_get(self, key: str, *, ttl_seconds: int) -> str: ...
    def download(self, key: str, destination: Path) -> Path: ...
    def upload_file(self, key: str, source: Path, *, content_type: str) -> ObjectRef: ...

    def create_multipart(self, key: str, *, content_type: str) -> str: ...
    def presign_parts(self, key: str, upload_id: str, *, count: int,
                      ttl_seconds: int) -> list[PartUpload]: ...
    def complete_multipart(self, key: str, upload_id: str,
                           parts: list[dict[str, Any]]) -> ObjectRef: ...
    def abort_multipart(self, key: str, upload_id: str) -> None: ...


# ── S3 ───────────────────────────────────────────────────────────────

# The error codes an S3-compatible server uses for "it is not there". Anything
# else — a refused connection, expired credentials, a 500 from the provider — is
# an outage, and conflating the two is how "the object is missing" gets logged
# during an incident in which every object is present.
_NOT_FOUND = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


@contextmanager
def _translated(operation: str) -> Iterator[None]:
    """Turn every boto3 failure into a `StorageError`.

    "The ONLY place an S3 SDK appears" is the promise at the top of this module,
    and it is broken the moment a `botocore.errorfactory.NoSuchKey` escapes: a
    caller cannot catch that without importing botocore, which is the one thing
    this port exists to prevent. `FileStorage` has always raised `StorageError`
    here; `S3Storage` raised whatever boto3 felt like, and the two were never
    compared because nothing ran the same tests against both.

    The original is kept as `__cause__` and in the message — the provider's error
    code is the useful part of a storage failure, and losing it to tidiness would
    make the next incident harder, not easier.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        yield
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "?")
        raise StorageError(f"{operation} failed: {code}") from exc
    except BotoCoreError as exc:
        raise StorageError(f"{operation} failed: {type(exc).__name__}") from exc


def _code(exc: Exception) -> str | None:
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None


def _is_missing(exc: Exception) -> bool:
    from botocore.exceptions import ClientError

    return (isinstance(exc, ClientError)
            and exc.response.get("Error", {}).get("Code") in _NOT_FOUND)


class S3Storage:
    """Any S3-compatible endpoint. `boto3` appears here and nowhere else."""

    def __init__(self, *, bucket: str | None = None, endpoint: str | None = None,
                 region: str | None = None) -> None:
        import boto3
        from botocore.config import Config

        cfg = settings()
        self.bucket = bucket or cfg.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint or cfg.s3_endpoint,
            region_name=region or cfg.s3_region,
            aws_access_key_id=cfg.s3_access_key or None,
            aws_secret_access_key=cfg.s3_secret_key or None,
            # Path style, because a self-hosted MinIO or a local IDC gateway
            # rarely has wildcard DNS for virtual-host buckets — and portability
            # is the entire reason this class exists.
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path"},
                          retries={"max_attempts": 3, "mode": "standard"}),
        )

    def put(self, key: str, data: bytes, *, content_type: str) -> ObjectRef:
        digest = hashlib.sha256(data).hexdigest()
        with _translated(f"put {key}"):
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                    ContentType=content_type)
        return ObjectRef(self.bucket, key, content_type, len(data), digest)

    def upload_file(self, key: str, source: Path, *, content_type: str) -> ObjectRef:
        digest = _sha256_file(source)
        with _translated(f"upload {key}"):
            self._client.upload_file(str(source), self.bucket, key,
                                     ExtraArgs={"ContentType": content_type})
        return ObjectRef(self.bucket, key, content_type, source.stat().st_size, digest)

    def get(self, key: str, *, start: int | None = None,
            end: int | None = None) -> Iterator[bytes]:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if start is not None:
            kwargs["Range"] = f"bytes={start}-{'' if end is None else end}"
        with _translated(f"get {key}"):
            body = self._client.get_object(**kwargs)["Body"]
        # Outside the context manager: a mid-stream failure is a read error, and
        # wrapping the loop would also swallow whatever the consumer raises.
        while chunk := body.read(64 * 1024):
            yield chunk

    def download(self, key: str, destination: Path) -> Path:
        with _translated(f"download {key}"):
            self._client.download_file(self.bucket, key, str(destination))
        return destination

    def stat(self, key: str) -> ObjectStat | None:
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            # NOT a bare `return None`. `stat() is None` means "no such object",
            # and callers act on it -- `media.py` treats it as "the upload did
            # not land". Reporting a refused connection as a missing object turns
            # an outage into a silent data-loss report.
            if _is_missing(exc):
                return None
            raise StorageError(f"stat {key} failed") from exc
        return ObjectStat(bytes=head["ContentLength"],
                          content_type=head.get("ContentType", "application/octet-stream"),
                          etag=head.get("ETag"))

    def delete(self, key: str) -> None:
        with _translated(f"delete {key}"):
            self._client.delete_object(Bucket=self.bucket, Key=key)

    def presign_get(self, key: str, *, ttl_seconds: int) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds)

    def create_multipart(self, key: str, *, content_type: str) -> str:
        with _translated(f"create multipart {key}"):
            return self._client.create_multipart_upload(
                Bucket=self.bucket, Key=key, ContentType=content_type)["UploadId"]

    def presign_parts(self, key: str, upload_id: str, *, count: int,
                      ttl_seconds: int) -> list[PartUpload]:
        return [
            PartUpload(
                part_number=n,
                url=self._client.generate_presigned_url(
                    "upload_part",
                    Params={"Bucket": self.bucket, "Key": key,
                            "UploadId": upload_id, "PartNumber": n},
                    ExpiresIn=ttl_seconds),
                offset=(n - 1) * PART_SIZE, length=PART_SIZE)
            for n in range(1, count + 1)
        ]

    def complete_multipart(self, key: str, upload_id: str,
                           parts: list[dict[str, Any]]) -> ObjectRef:
        with _translated(f"complete multipart {key}"):
            self._client.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": [
                    {"PartNumber": int(p["n"]), "ETag": p["etag"]}
                    for p in sorted(parts, key=lambda p: int(p["n"]))]})
        head = self.stat(key)
        # The checksum is computed by the ingest worker after download, not here:
        # S3's ETag for a multipart object is a hash of hashes, not of content, so
        # it cannot serve as the integrity value stored on the row.
        return ObjectRef(self.bucket, key, head.content_type if head else "",
                         head.bytes if head else 0, "")

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Idempotent, like the local backend: a client retrying a cancel on a
        flaky connection must not turn a tidy-up into a 500."""
        try:
            self._client.abort_multipart_upload(Bucket=self.bucket, Key=key,
                                                UploadId=upload_id)
        except Exception as exc:
            if _is_missing(exc) or _code(exc) == "NoSuchUpload":
                return
            raise StorageError(f"abort multipart {key} failed") from exc


# ── local disk ───────────────────────────────────────────────────────

class FileStorage:
    """The same protocol over a directory on the machine the backend runs on.

    **This is the deployed backend**, not a laptop stand-in. Media lives on the
    server's own disk: one box, one filesystem, no object store to pay for or to
    place in another jurisdiction. Data residency is a legal question here —
    "assume I may be required to store personal data in-country" — and a
    directory on a Tashkent VPS answers it without a migration.

    What that costs, stated rather than discovered: the media disk is the same
    disk as everything else, backups must now include it, and there is no
    replication behind it. `storage_backend = "s3"` remains the seam for the day
    those matter (ADR-0001 §5.4), and `S3Storage` implements the same protocol so
    moving is configuration.

    It implements multipart properly — parts land as separate files and are
    concatenated on complete — so the resumable-upload code exercised by the test
    suite is the code that runs against S3, rather than a simplified path that
    happens to pass.

    `presign_*` returns a URL to a dev-only route on this application, because
    there is no object store to PUT at. That route is mounted only in debug and
    is deliberately absent from the OpenAPI contract: it is not part of the
    product API.
    """

    def __init__(self, root: Path | str | None = None,
                 bucket: str | None = None) -> None:
        cfg = settings()
        self.bucket = bucket or cfg.s3_bucket
        self.root = Path(root or cfg.storage_root) / self.bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # A key is server-generated, but treating it as a path is one traversal
        # bug away from writing outside the root, so it is resolved and checked.
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise StorageError(f"key escapes the storage root: {key!r}")
        return target

    def put(self, key: str, data: bytes, *, content_type: str) -> ObjectRef:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _write_meta(path, content_type)
        return ObjectRef(self.bucket, key, content_type, len(data),
                         hashlib.sha256(data).hexdigest())

    def upload_file(self, key: str, source: Path, *, content_type: str) -> ObjectRef:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        _write_meta(path, content_type)
        return ObjectRef(self.bucket, key, content_type, path.stat().st_size,
                         _sha256_file(path))

    def get(self, key: str, *, start: int | None = None,
            end: int | None = None) -> Iterator[bytes]:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"no such object: {key}")
        with path.open("rb") as handle:
            if start:
                handle.seek(start)
            remaining = None if end is None else (end - (start or 0) + 1)
            while True:
                size = 64 * 1024 if remaining is None else min(64 * 1024, remaining)
                if size <= 0:
                    return
                chunk = handle.read(size)
                if not chunk:
                    return
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    def download(self, key: str, destination: Path) -> Path:
        shutil.copyfile(self._path(key), destination)
        return destination

    def stat(self, key: str) -> ObjectStat | None:
        path = self._path(key)
        if not path.exists():
            return None
        return ObjectStat(bytes=path.stat().st_size, content_type=_read_meta(path))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presign_get(self, key: str, *, ttl_seconds: int) -> str:
        from app.platform.grants import sign_object

        return (f"{settings().public_base_url}/internal/storage/{self.bucket}/{key}"
                f"?sig={sign_object(key, ttl_seconds=ttl_seconds)}")

    def create_multipart(self, key: str, *, content_type: str) -> str:
        upload_id = hashlib.sha256(f"{key}:{os.urandom(8).hex()}".encode()).hexdigest()[:24]
        (self.root / ".parts" / upload_id).mkdir(parents=True, exist_ok=True)
        return upload_id

    def presign_parts(self, key: str, upload_id: str, *, count: int,
                      ttl_seconds: int) -> list[PartUpload]:
        from app.platform.grants import sign_object

        base = settings().public_base_url
        return [
            PartUpload(part_number=n,
                       url=(f"{base}/internal/storage/parts/{upload_id}/{n}"
                            f"?sig={sign_object(f'{upload_id}:{n}', ttl_seconds=ttl_seconds)}"),
                       offset=(n - 1) * PART_SIZE, length=PART_SIZE)
            for n in range(1, count + 1)
        ]

    def put_part(self, upload_id: str, part_number: int, data: bytes) -> str:
        """The target of a presigned part URL, for a caller that already holds
        the bytes. `stream_part` is what the HTTP route uses."""
        directory = self.root / ".parts" / upload_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{part_number:05d}").write_bytes(data)
        return hashlib.md5(data).hexdigest()          # noqa: S324 — S3 ETag shape

    async def stream_part(self, upload_id: str, part_number: int, chunks,
                          *, limit: int) -> str:
        """The same thing without holding the part in memory.

        The ETag is computed as the bytes go past, so the file is never read back
        to hash it — on a box where the media disk and the database disk are the
        same disk, halving the I/O of every upload is not a micro-optimisation.

        Over-long input raises `ValueError` and leaves nothing behind. A limit
        enforced after buffering is not a limit, and the partial file is deleted
        rather than left for the completion step to concatenate into a corrupt
        object.
        """
        directory = self.root / ".parts" / upload_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{part_number:05d}"
        digest, written = hashlib.md5(), 0                    # noqa: S324
        try:
            with path.open("wb") as out:
                async for chunk in chunks:
                    written += len(chunk)
                    if written > limit:
                        raise ValueError("part exceeds the permitted size")
                    digest.update(chunk)
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    def complete_multipart(self, key: str, upload_id: str,
                           parts: list[dict[str, Any]]) -> ObjectRef:
        directory = self.root / ".parts" / upload_id
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        with path.open("wb") as out:
            for part in sorted(directory.glob("*")):
                # Chunked rather than `part.read_bytes()`. Parts are 5 MB today,
                # so the old form was survivable — but it made peak memory a
                # function of a constant somebody else owns.
                with part.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        out.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
        shutil.rmtree(directory, ignore_errors=True)
        content_type = _read_meta(path)
        return ObjectRef(self.bucket, key, content_type, total, digest.hexdigest())

    def abort_multipart(self, key: str, upload_id: str) -> None:
        shutil.rmtree(self.root / ".parts" / upload_id, ignore_errors=True)


def _write_meta(path: Path, content_type: str) -> None:
    path.with_suffix(path.suffix + ".ct").write_text(content_type)


def _read_meta(path: Path) -> str:
    meta = path.with_suffix(path.suffix + ".ct")
    return meta.read_text() if meta.exists() else "application/octet-stream"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


# ── the process-wide instance ────────────────────────────────────────

_storage: Storage | None = None


def storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = (S3Storage() if settings().storage_backend == "s3"
                    else FileStorage())
        log.info("storage_configured", backend=settings().storage_backend,
                 bucket=_storage.bucket)
    return _storage


def set_storage(instance: Storage | None) -> None:
    """Tests only."""
    global _storage
    _storage = instance


def scratch_dir() -> Path:
    """Where the transcode worker unpacks a master before probing it.

    Configurable because a 40 MB WAV plus its transcoded output on a container
    with a small writable layer is exactly the surprise that takes a box down at
    3 a.m., and pointing it at a mounted volume must not need a code change.
    """
    path = Path(settings().media_scratch_dir or tempfile.gettempdir())
    path.mkdir(parents=True, exist_ok=True)
    return path
