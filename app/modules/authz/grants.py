"""Which shared content an actor may actually see.

`policy.filter_content` documents four visibility routes and the fourth —
"anything explicitly shared with them via `content_grants`" — takes a
`grant_ids` argument that **no caller in the application ever passed**. So a
centre could create a grant, the grantee could list it through
`GET /content-grants`, and the material stayed invisible: `view` and `assign`
did nothing at all, and the only permission any handler read was `copy`, in
`tests_authoring._require_copy_grant`, for tests alone.

That is the same defect this codebase keeps producing — a table written by one
side and read by neither — and it is the one that matters commercially, because
the grant row is the marketplace seam. Selling a test bank is supposed to be
this row and no schema change.

This module is the reader. It lives beside `policy.py` rather than in a router
because sharing authority is authorization, and the rule that decides what a
grant reaches has to be in one place or it will be in several.

**Permission ordering is a hierarchy, not a set.** `copy` implies `assign`
implies `view`: a centre that may take a copy may obviously look at it. So a
listing asks for `view` and gets everything, while `_require_copy_grant` asks
for `copy` and gets only the strongest. Modelled here rather than at each call
site, because "which permissions count as read access" is exactly the question
that drifts when three routers answer it separately.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Strongest first. A grant satisfies a requirement when it appears at or before
#: the requirement's position — `copy` satisfies a `view` requirement, never the
#: other way round.
HIERARCHY: tuple[str, ...] = ("copy", "assign", "view")

#: `content_grants.subject_type` to the table its `subject_id` points at. The
#: same vocabulary `platform_ops._SUBJECT_TABLES` uses; kept here as well
#: because this module must not import a router, and asserted equal by
#: `tests/integration/test_content_grants_reach.py` so the two cannot drift.
SUBJECT_TABLES: dict[str, str] = {
    "test": "tests",
    "passage": "passages",
    "audio_track": "audio_tracks",
    "question_group": "question_groups",
    "question": "questions",
    "cue_card_set": "cue_card_sets",
    "band_map": "band_maps",
}


class Actor(Protocol):
    user_id: int
    org_ids: tuple[int, ...]

    @property
    def is_platform_admin(self) -> bool: ...


def satisfying(permission: str) -> tuple[str, ...]:
    """Every stored permission that satisfies a requirement for `permission`."""
    if permission not in HIERARCHY:
        return (permission,)
    return HIERARCHY[: HIERARCHY.index(permission) + 1]


def granted_ids(session: Session, actor: Actor, subject_type: str, *,
                permission: str = "view") -> set[int]:
    """Internal ids of `subject_type` rows shared with this actor.

    Internal ids because that is what `filter_content` puts in its `IN` clause,
    and it never leaves the process — the same reason every other listing works
    on `model.id`.

    A platform admin gets an empty set on purpose rather than everything:
    `filter_content` returns the query unfiltered for them well before this is
    reached, so computing a set here would be a query whose result is discarded.

    Expiry and revocation are applied in SQL rather than filtered afterwards, so
    an expired grant costs nothing to carry — a centre that shared a bank for a
    term should not slow every listing for the year after it lapses.
    """
    if actor.is_platform_admin:
        return set()
    table = SUBJECT_TABLES.get(subject_type)
    if table is None:
        return set()

    rows = session.execute(text(f"""
        SELECT g.subject_id
        FROM content_grants g
        JOIN {table} t ON t.id = g.subject_id
        WHERE g.subject_type = :kind
          AND g.permission = ANY(:perms)
          AND g.revoked_at IS NULL
          AND (g.expires_at IS NULL OR g.expires_at > now())
          AND ((g.grantee_kind = 'org' AND g.grantee_id = ANY(:orgs))
               OR (g.grantee_kind = 'user' AND g.grantee_id = :uid)
               OR g.grantee_kind = 'public')
    """).bindparams(kind=subject_type, perms=list(satisfying(permission)),
                    orgs=list(actor.org_ids) or [0], uid=actor.user_id))
    return {row[0] for row in rows}


def subject_type_of(model: Any) -> str | None:
    """The `content_grants.subject_type` for an ORM model, or None.

    Derived from `__tablename__` rather than a second hand-written map, so a
    model that gains a table cannot quietly fall out of the sharing rules with
    nothing to notice.
    """
    table = getattr(model, "__tablename__", None)
    for kind, name in SUBJECT_TABLES.items():
        if name == table:
            return kind
    return None
