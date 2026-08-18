"""Keyset paging, in one place because nine listings promised it and one did it.

`{items, next_cursor}` is the envelope this contract declares for every paged
listing. Exactly one handler ever issued a cursor — content grants, which had to
because its policy filter runs in Python and truncation there hides who can see
a centre's material. The other nine returned `next_cursor: null` unconditionally
under a `limit` of 25, so a centre with four hundred students had twenty-five,
a library of two hundred passages had twenty-five, and nothing in the response
said the rest existed.

Two things are wrong with an offset and both are why this is keyset:

  * an offset shifts under you. Delete a row on page one and page two skips one;
    insert and it repeats one. On a roster being edited while it is read, that
    is a student who silently does not appear.
  * `OFFSET n` makes the database walk and discard n rows. At page forty that is
    a thousand rows read to return twenty-five.

The cursor is therefore a POSITION — the last key seen — and the query resumes
strictly past it.

**An ordering is not optional.** Half the listings this replaces had no ORDER BY
at all, so "the first 25" was whatever the plan happened to emit, and could
differ between two calls a second apart. A keyset cursor forces the question, and
the ordering key has to be unique or the resume point is ambiguous — `id` for
everything here, which is monotonic and never reused.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any, Protocol, TypeVar

#: Cannot occur in a timestamp, an integer or a rank — the only things in a key.
SEP = "\x1f"


def encode_cursor(key: Any) -> str:
    """Opaque and URL-safe.

    The obvious cursor is the key itself, and it is a trap: an ISO offset
    contains `+`, which a query string decodes as a space, so the next page
    fails with `invalid input syntax for type timestamp` unless every client
    remembers to encode it. Base64url removes the class of bug rather than
    documenting it — and an opaque token also stops clients constructing their
    own, which is the other reason cursors are opaque.
    """
    raw = key.isoformat() if isinstance(key, dt.datetime) else str(key)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    """Back to the key, or None for anything we did not mint.

    A malformed cursor is a client bug or a hand-edited URL. Starting from the
    top is both safe and obvious; raising would turn a typo in a URL into a 500
    on a screen someone is trying to read.
    """
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.urlsafe_b64decode(padded).decode()
    except Exception:
        return None


def decode_id(cursor: str | None) -> int | None:
    """The same, for the `id`-ordered listings — which is most of them."""
    raw = decode_cursor(cursor)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def encode_key(*parts: Any) -> str:
    """A cursor for a COMPOUND ordering, which most non-trivial orderings are.

    `ORDER BY closes_at, id` cannot be resumed with `id > last_id`: the rows are
    ordered by the timestamp first, so an id comparison has nothing to do with
    the position in the sequence. It would skip and repeat rows more or less at
    random, which is worse than not paging at all — a listing that quietly drops
    an assignment is harder to notice than one that stops at 25.

    So the cursor carries every part of the ordering, and SQL compares it as a
    ROW VALUE, which PostgreSQL supports directly and can drive from a composite
    index. The unit separator is the delimiter because it cannot occur in a
    timestamp, an integer or a rank — the three things that are ever in here.
    """
    return encode_cursor(SEP.join(
        p.isoformat() if isinstance(p, dt.datetime) else str(p) for p in parts))


def decode_key(cursor: str | None, arity: int) -> tuple[Any, ...] | None:
    """Back to the parts, or None for anything unusable.

    The LAST part is always the tie-breaking id and is returned as an int; the
    rest are strings, which PostgreSQL casts against the column it compares
    them to. Wrong arity means a cursor minted for a different listing, and
    starting from the top is the safe answer to that.
    """
    raw = decode_cursor(cursor)
    if raw is None:
        return None
    parts = raw.split(SEP)
    if len(parts) != arity:
        return None
    try:
        return (*parts[:-1], int(parts[-1]))
    except ValueError:
        return None


class HasId(Protocol):
    id: int


T = TypeVar("T", bound=HasId)


def page(rows: list[T], limit: int) -> tuple[list[T], str | None]:
    """Split a batch of `limit + 1` rows into a page and the cursor after it.

    Fetching one MORE than asked for is what makes the answer exact. Comparing
    `len(rows) == limit` instead guesses: a listing whose total is exactly 25
    would hand back a cursor, and the client would fetch an empty page to
    discover there was nothing there. That empty page is what a "Show more"
    button turning into nothing after a click looks like, and it is worse than
    no button.
    """
    if len(rows) <= limit:
        return rows, None
    kept = rows[:limit]
    return kept, encode_cursor(kept[-1].id)
