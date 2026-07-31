"""The review gate: publishing checks that somebody approved *this* content.

`content_reviews`, `submit-review` and `review` have existed since migration
0008, and `POST /test-versions/{xid}/publish` never read any of them. Every part
of the workflow worked and none of it was load-bearing:

  * a draft nobody had looked at published;
  * a version a reviewer had explicitly rejected published, unchanged;
  * the submitter could approve their own request, and the row named them as the
    reviewer.

ADR-0007 §3.7 argues that approval must not itself publish — "otherwise the
reviewer's click is also a deploy and there is no moment at which to stop it".
That is right, and it presumes the converse: that publish is downstream of
approval. Nothing connected the two ends, so the review step was decoration.

**Three decisions, in order of how much they cost to get wrong.**

1. *Review is per-centre, off by default.* Most centres here are one or two
   people. A universal requirement plus a self-approval bar is a one-teacher
   centre that cannot publish at all — the product broken for its commonest
   customer. `settings.require_review` joins `teacher_can_publish` and
   `content_edit_others`, and like them it is the centre's call. Unlike them it
   RESTRICTS rather than widens, so it defaults off; a school that wants sign-off
   turns it on and thereby asserts it has two people.

2. *An approval is over content, not over a row id.* This is the whole gate. A
   version stays editable while `in_review`, so approve-then-edit-then-publish
   was available to a single actor, and the answer key is the field most worth
   changing after somebody else has looked at it. The approval therefore records
   `fingerprint()` of the composition it was granted over, and publish refuses
   when the version has moved since.

3. *Nobody approves their own submission.* Refused even when the centre does not
   require review, because the point of the row is the evidence, not the
   permission: "approved by the person who wrote it" answers "who signed off?"
   with a name that means nothing. `decide_review`'s own docstring names the
   failure — "the review becomes theatre" — for the case one rank down.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform.errors import Conflict

from .composition import TestComposition

SETTING = "require_review"


def required_by(org_settings: dict[str, Any] | None) -> bool:
    return bool((org_settings or {}).get(SETTING))


def fingerprint(composition: TestComposition) -> str:
    """A stable digest of everything the gate reads, answer keys included.

    Deliberately NOT `repo.build_snapshot()`, which is the other checksum in this
    codebase and the obvious thing to reuse. The snapshot is the student-facing
    document and excludes answer keys by design — so a fingerprint over it would
    be blind to the single edit this gate most needs to catch. "Bad keys are the
    fastest way to lose a school client"; a key change after approval must
    invalidate the approval.

    Over-broad rather than under-broad on purpose. Every field in the composition
    is one the publish gate reads, which makes it material to whether this test
    is fit to publish: a takedown landing on the passage, or an audio track that
    stopped being `ready`, moves the fingerprint and costs a second look. That is
    the correct outcome in both cases.
    """
    return hashlib.sha256(
        json.dumps(dataclasses.asdict(composition), sort_keys=True,
                   separators=(",", ":"), default=str).encode()
    ).hexdigest()


class Approval(NamedTuple):
    id: int
    reviewer_id: int | None
    content_checksum: str | None
    decided_at: Any


def latest_approval(session: Session, test_version_id: int) -> Approval | None:
    """The most recent `approved` decision for this version, if any."""
    row = session.execute(text("""
        SELECT id, reviewer_id, content_checksum, decided_at
        FROM content_reviews
        WHERE test_version_id = :tv AND state = 'approved'
        ORDER BY decided_at DESC NULLS LAST, id DESC
        LIMIT 1
    """).bindparams(tv=test_version_id)).mappings().first()
    return Approval(**row) if row else None


def require_approval(session: Session, test_version_id: int,
                     composition: TestComposition,
                     *, org_settings: dict[str, Any] | None) -> Approval | None:
    """Refuse to publish content this centre's reviewer has not approved.

    Returns the approval so the caller can name it in the audit record, or None
    when the centre does not require one — the audit row then says so, which is
    the honest thing to write for a centre that has opted out.
    """
    if not required_by(org_settings):
        return None

    approval = latest_approval(session, test_version_id)
    if approval is None:
        raise Conflict(
            "This centre requires a content review before publishing, and this "
            "version has not been approved.",
            code="review_required")

    current = fingerprint(composition)
    if approval.content_checksum != current:
        # Covers the pre-0022 NULL as well: an approval that recorded no
        # fingerprint cannot demonstrate what it was over, so it does not carry.
        raise Conflict(
            "This version has changed since it was approved. Submit it for "
            "review again.",
            code="review_stale")
    return approval
