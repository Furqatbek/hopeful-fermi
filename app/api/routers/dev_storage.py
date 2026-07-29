"""The target of `FileStorage`'s presigned URLs. Dev and test ONLY.

Against S3 the client PUTs parts straight to the object store and these routes do
not exist in the request path at all. On a laptop there is no object store, so
the file backend presigns URLs pointing here instead.

Three things keep this from being a hole:

  * it is mounted only when `storage_backend == "file"`, so a production deploy
    against S3 never serves it;
  * every request carries an HMAC signature over the key and an expiry, checked
    by `platform.grants.verify_object` — the same check an S3 presigned URL
    performs, for the same reason;
  * it is absent from the OpenAPI document. It is not part of the product API,
    and `scripts/check_api_coverage.py` would rightly flag it if it were.

It exists so the resumable-upload code exercised by the test suite is the code
that ships, rather than a simplified path that happens to pass.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.platform import grants
from app.platform.errors import NotFound
from app.platform.storage import FileStorage, storage

router = APIRouter(prefix="/dev/storage", include_in_schema=False)

# 100 MB. Generous for a test fixture, small enough that a mistake here cannot
# exhaust a laptop's memory — this route reads the part into RAM, which is
# acceptable precisely because it never runs in production.
MAX_PART_BYTES = 100 * 1024 * 1024


def _backend() -> FileStorage:
    store = storage()
    if not isinstance(store, FileStorage):
        raise NotFound("Not found.")
    return store


@router.put("/parts/{upload_id}/{part_number}")
async def put_part(upload_id: str, part_number: int, sig: str,
                   request: Request) -> Response:
    grants.verify_object(f"{upload_id}:{part_number}", sig)
    data = await request.body()
    if len(data) > MAX_PART_BYTES:
        return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    etag = _backend().put_part(upload_id, part_number, data)
    # The ETag header is what the client echoes back in the complete call, which
    # is exactly the S3 contract this stands in for.
    return Response(status_code=status.HTTP_200_OK, headers={"ETag": etag})


@router.get("/{bucket}/{key:path}")
def get_object(bucket: str, key: str, sig: str) -> Response:
    """The stand-in for a presigned GET, used when `media_delivery=redirect`."""
    grants.verify_object(key, sig)
    store = _backend()
    stat = store.stat(key)
    if stat is None:
        raise NotFound("Object not found.")
    return Response(content=b"".join(store.get(key)),
                    media_type=stat.content_type,
                    headers={"Cache-Control": "private, no-store"})
