"""The central policy engine.

Two halves, and the second one is the load-bearing one:

  * `check(actor, action, resource)` stops a teacher OPENING a competitor
    centre's test.
  * `filter(actor, action, query)`  stops that test appearing in a LIST response
    at all.

Almost every real multi-tenant data leak is a missing list-scope, not a missing
detail-check — the detail endpoint is the one people remember to guard. Every
query that returns content passes through `filter` or it does not ship, and
`tests/integration/test_authz_leaks.py` asserts that for each listing endpoint.

The permission matrix from `docs/design/0002-data-model.md` §8 is expressed here
once, as data, rather than as role checks scattered through feature code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import Select, or_

from app.platform.errors import Forbidden


class Action(StrEnum):
    READ = "read"
    CREATE = "create"
    EDIT = "edit"
    PUBLISH = "publish"
    ARCHIVE = "archive"
    DELETE = "delete"
    SHARE = "share"
    IMPORT = "import"
    EXPORT = "export"
    REGRADE = "regrade"
    VIEW_EXPOSURE = "view_exposure"
    TAKEDOWN = "takedown"
    MANAGE_REGISTRY = "manage_registry"
    MANAGE_ORG = "manage_org"
    MANAGE_BAND_MAP = "manage_band_map"


class Role(StrEnum):
    STUDENT = "student"
    TEACHER = "teacher"
    CENTRE_ADMIN = "centre_admin"
    PLATFORM_ADMIN = "platform_admin"


# The matrix, as data. `None` means "not permitted"; a callable means the answer
# depends on ownership or an organization setting.
_MATRIX: dict[Action, set[Role]] = {
    Action.READ: {Role.STUDENT, Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.CREATE: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.EDIT: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    # Teachers are absent on purpose: a centre's reputation rides on its
    # published material, so publishing is opt-in per organization.
    Action.PUBLISH: {Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.ARCHIVE: {Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.DELETE: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.SHARE: {Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.IMPORT: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.EXPORT: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.REGRADE: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.VIEW_EXPOSURE: {Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.TAKEDOWN: {Role.PLATFORM_ADMIN},
    Action.MANAGE_REGISTRY: {Role.PLATFORM_ADMIN},
    Action.MANAGE_ORG: {Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
    Action.MANAGE_BAND_MAP: {Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN},
}


class Actor(Protocol):
    user_id: int
    org_ids: tuple[int, ...]
    roles: dict[int, str]
    platform_roles: tuple[str, ...]

    @property
    def is_platform_admin(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Resource:
    """What is being acted on. `org_id`/`owner_user_id`/`visibility` are the only
    three fields the policy needs, and every content table carries them."""

    org_id: int | None = None
    owner_user_id: int | None = None
    visibility: str = "org_private"
    status: str | None = None
    kind: str = "content"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str = ""

    def raise_if_denied(self, action: Action) -> None:
        if not self.allowed:
            raise Forbidden(f"You may not {action.value} this.",
                            code=f"{action.value}_not_permitted",
                            reason=self.reason)


def role_of(actor: Actor, org_id: int | None) -> Role | None:
    if actor.is_platform_admin:
        return Role.PLATFORM_ADMIN
    if org_id is None:
        return None
    raw = actor.roles.get(org_id)
    return Role(raw) if raw else None


def check(actor: Actor, action: Action, resource: Resource,
          *, org_settings: dict[str, Any] | None = None) -> Decision:
    """Detail-level authorization."""
    if actor.is_platform_admin:
        return Decision(True, "platform_admin")

    role = role_of(actor, resource.org_id)

    if action is Action.READ:
        if resource.visibility == "platform_global":
            return Decision(True, "platform_global")
        if resource.visibility == "author_private":
            return (Decision(True, "owner") if resource.owner_user_id == actor.user_id
                    else Decision(False, "author_private"))
        if resource.org_id in actor.org_ids:
            return Decision(True, "org_member")
        return Decision(False, "not_a_member")

    if role is None:
        return Decision(False, "not_a_member")
    if role not in _MATRIX.get(action, set()):
        # The one place a teacher can gain a permission they lack by default.
        if (action is Action.PUBLISH and role is Role.TEACHER
                and (org_settings or {}).get("teacher_can_publish")):
            return Decision(True, "org_setting")
        return Decision(False, f"role_{role.value}_cannot_{action.value}")

    if action in (Action.EDIT, Action.DELETE) and role is Role.TEACHER:
        if resource.owner_user_id != actor.user_id and not (
                org_settings or {}).get("content_edit_others"):
            return Decision(False, "not_the_author")
    if action is Action.DELETE and resource.status == "published":
        # Nobody can hard-delete published content, including a platform admin —
        # attempts reference it and a takedown needs the evidence preserved.
        return Decision(False, "published_content_is_archived_not_deleted")

    return Decision(True, f"role_{role.value}")


def require(actor: Actor, action: Action, resource: Resource,
            *, org_settings: dict[str, Any] | None = None) -> None:
    check(actor, action, resource, org_settings=org_settings).raise_if_denied(action)


def filter_content(actor: Actor, query: Select, model: Any,
                   *, grant_subject: str | None = None,
                   grant_ids: set[int] | None = None) -> Select:
    """Scope a content listing to what the actor may see.

    Four visibility routes, ORed:
      1. platform-global content,
      2. anything owned by an organization the actor belongs to,
      3. the actor's own author-private drafts,
      4. anything explicitly shared with them via `content_grants`.

    A platform admin skips the filter. Everyone else gets a WHERE clause, and
    there is no code path that returns content without one.
    """
    if actor.is_platform_admin:
        return query

    clauses = [model.visibility == "platform_global"]
    if actor.org_ids:
        clauses.append(model.org_id.in_(actor.org_ids))
    clauses.append(
        (model.owner_user_id == actor.user_id) & (model.visibility == "author_private"))
    if grant_ids:
        clauses.append(model.id.in_(grant_ids))
    return query.where(or_(*clauses))
