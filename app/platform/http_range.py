"""One `Range` header parser, for both delivery paths.

`GET /media/{xid}/content` (the per-user grant) and `GET /internal/storage`
(the presigned URL) each carried their own, and they disagreed: the media one
clamped a request that began past the end into a one-byte 206, and answered a
malformed header with a 206 and a `Content-Range` too, while the storage one
sent the whole object for the first and 416 for the second — with the tests to
prove it. `bytes=999999-` on a 300 KB listening section was therefore a
different answer depending on which URL the player happened to be given, which
is decided by `media_delivery`, a config knob. This is the one with the RFC 9110
contract and the tests; both routes read it now.
"""

from __future__ import annotations

import re

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
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
