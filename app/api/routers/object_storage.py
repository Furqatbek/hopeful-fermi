"""Object upload and delivery for the filesystem backend.

**This is a production route.** It was `dev_storage.py` and its docstring said
"Dev and test ONLY … never runs in production", which was true of the intended
deployment and is no longer true of this one: media lives on the disk of the box
the backend runs on, so `FileStorage` is the backend that ships and these are the
URLs it presigns.

That is not a rename. Three decisions were justified by "this never runs in
production" and each of them was wrong once it did:

  * `put_part` read the whole part into memory. Five megabytes per concurrent
    part upload is survivable; the 100 MB ceiling it enforced was not, on a box
    whose whole budget is fifty dollars a month. It streams to disk now.
  * `get_object` read the ENTIRE object into memory and returned it in one piece
    — a 14 MB listening section per request, with no range support at all. An
    `<audio>` element cannot seek without ranges and on iOS Safari will not play.
  * Neither had a coverage floor, because dev-only code does not need one.

What has not changed is why it is safe. Every request carries an HMAC signature
over the key and an expiry, checked by `platform.grants.verify_object` — the same
check an S3 presigned URL performs, for the same reason. And it stays out of the
OpenAPI document: it is not part of the product API, it is the object store's
own interface, and `scripts/check_api_coverage.py` would rightly flag it.

`media_delivery` decides whether `get_object` is in the request path at all. On
the default `proxy` it is not — bytes go through `GET /media/{xid}/content` with
a per-user grant and a full audit trail. `redirect` trades that binding for
cheaper egress, and then these signed URLs are what the client follows.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse

from app.platform import grants
from app.platform.errors import NotFound
from app.platform.storage import FileStorage, storage, storage_for

router = APIRouter(prefix="/internal/storage", include_in_schema=False)

# One part. `storage.PART_SIZE` is 5 MB and the client is told exactly that, so
# anything materially larger is a client that has stopped following the plan.
# Enforced from the CONTENT-LENGTH before a byte is read, because a limit checked
# after buffering is not a limit.
MAX_PART_BYTES = 8 * 1024 * 1024

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _backend(bucket: str | None = None) -> FileStorage:
    """These routes exist only for the file backend; with S3 the client talks to
    the object store directly and never reaches here.

    `bucket` names the one the URL asks for. It was a path parameter that nothing
    read, so every request was served out of the configured bucket whatever the
    URL said — the same defect as the delivery path, and the reason the bucket is
    now part of the signed material.
    """
    store = storage_for(bucket) if bucket else storage()
    if not isinstance(store, FileStorage):
        raise NotFound("Not found.")
    return store


@router.put("/parts/{upload_id}/{part_number}")
async def put_part(upload_id: str, part_number: int, sig: str,
                   request: Request) -> Response:
    """Streamed to disk, never assembled in memory.

    `await request.body()` is one line shorter and holds the whole part in RAM
    for as long as the slowest uploader takes. Five teachers on Uzbek upstream
    bandwidth is five parts resident at once, and the ceiling was set at 100 MB.
    """
    grants.verify_object(f"{upload_id}:{part_number}", sig)

    declared = request.headers.get("content-length")
    if declared is not None and int(declared) > MAX_PART_BYTES:
        return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)

    store = _backend()
    try:
        etag = await store.stream_part(upload_id, part_number, request.stream(),
                                       limit=MAX_PART_BYTES)
    except ValueError:
        # A client that lied in `Content-Length`, or sent none and kept going.
        # The partial file is already discarded by `stream_part`.
        return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
    # The ETag header is what the client echoes back in the complete call, which
    # is exactly the S3 contract this stands in for.
    return Response(status_code=status.HTTP_200_OK, headers={"ETag": etag})


@router.get("/{bucket}/{key:path}")
def get_object(bucket: str, key: str, sig: str, request: Request) -> Response:
    """A presigned GET, used when `media_delivery=redirect`.

    Streamed and range-aware. It used to be `b"".join(store.get(key))` — the
    whole object in memory, no ranges — which was defensible for a fixture and is
    not for a listening section: without `Range` an `<audio>` element cannot seek
    and iOS Safari will not play the file at all.
    """
    grants.verify_object(f"{bucket}/{key}", sig)
    store = _backend(bucket)
    stat = store.stat(key)
    if stat is None:
        raise NotFound("Object not found.")

    headers = {
        # `no-store`, not `private`: a shared device in a computer lab must not
        # keep an exam section in its disk cache after the student logs out.
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
    }
    span = _range(request.headers.get("range"), stat.bytes)
    if span is None:
        headers["Content-Length"] = str(stat.bytes)
        return StreamingResponse(store.get(key), media_type=stat.content_type,
                                 headers=headers)

    start, end = span
    if start >= stat.bytes:
        headers["Content-Range"] = f"bytes */{stat.bytes}"
        return Response(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                        headers=headers)
    headers["Content-Range"] = f"bytes {start}-{end}/{stat.bytes}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(store.get(key, start=start, end=end),
                             status_code=status.HTTP_206_PARTIAL_CONTENT,
                             media_type=stat.content_type, headers=headers)


def _range(header: str | None, size: int) -> tuple[int, int] | None:
    """`bytes=0-999`, `bytes=1000-` and `bytes=-500`, which is what real players
    send.

    MALFORMED input returns `None`, meaning "send the whole thing" — refusing
    would break a player over a header it did not have to send at all. An
    UNSATISFIABLE range is a different answer: `bytes=999999-` on a 300 KB file
    is a well-formed request for bytes that do not exist, and RFC 9110 says 416.
    The caller distinguishes them, so this must not collapse the second into the
    first — which it did, and a 416 test caught it.
    """
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:                       # bytes=-500 — the last 500 bytes
        length = min(int(last), size)
        return max(0, size - length), size - 1
    start = int(first)
    if last and int(last) < start:      # bytes=500-100 — nonsense, not a range
        return None
    # `end` is clamped to the object; `start` is NOT, so a request that begins
    # past the end reaches the caller and becomes a 416 rather than a whole file.
    return start, (min(int(last), size - 1) if last else size - 1)
