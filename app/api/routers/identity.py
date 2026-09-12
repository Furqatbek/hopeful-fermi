"""The acting user, their consents and devices; and organizations/cohorts."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, principal
from app.api.dto import iso
from app.api.paging import decode_id, page
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.identity.models import (
    PHONE_PATTERN,
    Cohort,
    CohortMember,
    Consent,
    Organization,
    OrgMembership,
    User,
)
from app.platform.errors import Conflict, Forbidden, Gone, NotFound

router = APIRouter(tags=["identity"])
orgs = APIRouter(tags=["orgs"])


class UserUpdate(BaseModel):
    given_name: str | None = None
    family_name: str | None = None
    # The four locales the contract declares and `users.locale` CHECKs.
    locale: str | None = Field(default=None, pattern="^(uz-Latn|uz-Cyrl|ru|en)$")
    target_band: float | None = Field(default=None, ge=1, le=9)


class ConsentCreate(BaseModel):
    # The three enums the contract declares, spelled the way the `consents`
    # CHECKs spell them. Typed `str` they reached the INSERT, and an out-of-enum
    # value came back as a 500 from the constraint rather than a 422 here.
    kind: str = Field(pattern="^(terms|privacy|parental|stranger_matching|marketing)$")
    doc_version: str
    granted_by_kind: str = Field(default="self", pattern="^(self|parent|centre_admin)$")
    parent_name: str | None = None
    parent_phone: str | None = None
    channel: str = Field(default="web", pattern="^(web|telegram|sms|paper)$")


def user_dto(user: User) -> dict:
    """`date_of_birth` is deliberately absent. It is collected for the 18
    boundary and nothing else, and never appears in a response, a leaderboard or
    a B2B export."""
    return {
        "xid": str(user.xid), "phone": user.phone, "given_name": user.given_name,
        "family_name": user.family_name, "locale": user.locale,
        "timezone": user.timezone, "telegram_username": user.telegram_username,
        "target_band": float(user.target_band) if user.target_band else None,
        "is_minor": user.adult_at > dt.datetime.now(dt.UTC).date(),
        "created_at": iso(user.created_at),
    }


@router.get("/me")
def read_me(actor: Principal = Depends(principal),
            session: Session = Depends(db)) -> dict:
    return user_dto(session.get(User, actor.user_id))


@router.patch("/me")
def update_me(body: UserUpdate, actor: Principal = Depends(principal),
              session: Session = Depends(db)) -> dict:
    """`date_of_birth` is not editable here.

    Changing it moves a user across the minor/adult boundary and silently alters
    which speaking pools they can enter, so it needs a support action with an
    audit record — not a self-service PATCH.
    """
    user = session.get(User, actor.user_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    session.flush()
    return user_dto(user)


@router.get("/me/consents")
def list_consents(actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> list[dict]:
    return [
        # `doc_hash` is the part that makes a consent EVIDENCE rather than a
        # boolean — it pins which words were agreed to — and it was stored and
        # never returned, so the one field a regulator would ask about could not
        # be read back through the API at all.
        {"kind": c.kind, "doc_version": c.doc_version, "doc_hash": c.doc_hash,
         "granted_by_kind": c.granted_by_kind, "channel": c.channel,
         "granted_at": iso(c.granted_at), "revoked_at": iso(c.revoked_at)}
        for c in session.scalars(
            select(Consent).where(Consent.user_id == actor.user_id)
            .order_by(Consent.granted_at.desc()))
    ]


@router.post("/me/consents", status_code=status.HTTP_201_CREATED)
def record_consent(body: ConsentCreate, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    """Consent is evidence, not a boolean: the document version and its hash are
    stored with the grant.

    `stranger_matching` for a minor additionally requires a parent, because a
    general terms acceptance does not cover voice calls with strangers and a
    regulator will not read it that way.
    """
    user = session.get(User, actor.user_id)
    is_minor = user.adult_at > dt.datetime.now(dt.UTC).date()
    if body.kind == "stranger_matching" and is_minor:
        if body.granted_by_kind != "parent" or not body.parent_phone:
            raise Forbidden(
                "Stranger matching for a minor requires parental consent with "
                "contact details.", code="parental_consent_required")

    row = Consent(user_id=actor.user_id, kind=body.kind, doc_version=body.doc_version,
                  doc_hash=hashlib.sha256(body.doc_version.encode()).hexdigest(),
                  granted_by_kind=body.granted_by_kind, parent_name=body.parent_name,
                  parent_phone=body.parent_phone, channel=body.channel)
    session.add(row)
    session.flush()
    return {"kind": row.kind, "doc_version": row.doc_version,
            "granted_by_kind": row.granted_by_kind, "granted_at": iso(row.granted_at)}


@router.delete("/me/consents/{kind}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_consent(kind: str, actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> Response:
    """Withdraw a consent.

    **`consents.revoked_at` was read and written by nothing**, and of the
    sixteen columns with that shape this is the one that matters most: the read
    is `speaking.book_slot`, which lets a minor into a public speaking pool only
    while a parent's `stranger_matching` consent is live. Consent could be given
    and never taken back. A parent who changed their mind had no way to say so
    through the product, and the only remedy was SQL against a child-safety
    control.

    **The acting user may withdraw, including a minor withdrawing a consent a
    parent gave.** That looks wrong for a second and then does not: withdrawal
    only ever REMOVES capability. A minor who revokes `stranger_matching` stops
    being matched with strangers, which is the direction this whole subsystem
    exists to push. The dangerous asymmetry would be the other one — a minor
    GRANTING it — and `record_consent` already refuses that.

    Revoked, never deleted, and a new grant is a new row. `granted_at` and
    `revoked_at` are the window a regulator asks about — "was there parental
    consent on the day of this call" is answered by the interval, and a deleted
    row answers it with silence.
    """
    row = session.scalars(
        select(Consent).where(Consent.user_id == actor.user_id, Consent.kind == kind,
                              Consent.revoked_at.is_(None))
        .order_by(Consent.granted_at.desc())).first()
    if row is None:
        raise NotFound("There is no active consent of that kind to withdraw.")
    row.revoked_at = dt.datetime.now(dt.UTC)
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/devices")
def list_devices(actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> list[dict]:
    from app.modules.identity.models import AuthSession

    return [
        {"xid": str(s.xid), "label": s.device_label, "platform": None,
         "last_seen_at": iso(s.last_used_at or s.issued_at), "current": False}
        for s in session.scalars(
            select(AuthSession).where(AuthSession.user_id == actor.user_id,
                                      AuthSession.revoked_at.is_(None)))
    ]


@router.delete("/me/devices/{xid}", status_code=status.HTTP_204_NO_CONTENT)
def forget_device(xid: uuid.UUID, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> Response:
    from app.modules.identity.models import AuthSession

    row = session.scalars(select(AuthSession).where(AuthSession.xid == xid,
                                                    AuthSession.user_id == actor.user_id)
                          ).first()
    if row is None:
        raise NotFound("Device not found.")
    row.revoked_at = dt.datetime.now(dt.UTC)
    row.revoked_reason = "user_forgot_device"
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── organizations ────────────────────────────────────────────────────

class OrgCreate(BaseModel):
    name: str
    slug: str = Field(pattern=r"^[a-z0-9-]{3,40}$")
    # The contract's enum, which is also the `organizations` CHECK.
    kind: str = Field(default="prep_centre",
                      pattern="^(prep_centre|school|university|internal)$")
    legal_name: str | None = None
    contact_phone: str | None = None


class OrgUpdate(BaseModel):
    name: str | None = None
    contact_phone: str | None = None
    settings: dict | None = None


# Role precedence, for the one place an invite meets an existing membership.
_RANK = {"student": 1, "teacher": 2, "centre_admin": 3}


class InviteCreate(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    # `platform_admin` is not an org role and never was: `org_invites.role` has a
    # CHECK over these three, and a bare `str` let the request reach it and
    # answer 500 instead of 422.
    role: str = Field(pattern="^(student|teacher|centre_admin)$")
    cohort_xid: uuid.UUID | None = None


class InviteAccept(BaseModel):
    """Exactly one of `token` — what arrives in the message — or `xid`, an invite
    the caller has just read off `GET /invites/pending`.

    It replaces a `body: dict` whose only reader was `body.get("token", "")`, so
    a request with no token at all reached the database as a hash of the empty
    string.
    """

    token: str | None = None
    xid: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_key(self) -> InviteAccept:
        if (self.token is None) == (self.xid is None):
            raise ValueError("Send exactly one of `token` or `xid`.")
        return self


class CohortCreate(BaseModel):
    name: str
    academic_year: str | None = None


class CohortMembers(BaseModel):
    user_xids: list[uuid.UUID]


def org_dto(org: Organization) -> dict:
    return {"xid": str(org.xid), "name": org.name, "slug": org.slug,
            "kind": org.kind, "status": org.status, "settings": org.settings or {}}


def _org(session: Session, xid: uuid.UUID, actor: Principal,
         action: Action = Action.READ) -> Organization:
    org = session.scalars(select(Organization).where(Organization.xid == xid)).first()
    if org is None:
        raise NotFound("Organization not found.")
    if action is Action.READ:
        if org.id not in actor.org_ids and not actor.is_platform_admin:
            raise NotFound("Organization not found.")
    else:
        policy.require(actor, action, Resource(org_id=org.id))
    return org


@orgs.get("/orgs")
def list_orgs(actor: Principal = Depends(principal),
              session: Session = Depends(db), limit: int = 25,
              cursor: str | None = None) -> dict:
    """Scoped to the actor's memberships. A platform admin sees everything."""
    query = select(Organization).order_by(Organization.id)
    if not actor.is_platform_admin:
        query = query.where(Organization.id.in_(actor.org_ids or [0]))
    if (after := decode_id(cursor)) is not None:
        query = query.where(Organization.id > after)
    rows, next_cursor = page(list(session.scalars(query.limit(limit + 1))), limit)
    return {"items": [org_dto(o) for o in rows], "next_cursor": next_cursor}


@orgs.post("/orgs", status_code=status.HTTP_201_CREATED)
def create_org(body: OrgCreate, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    if not actor.is_platform_admin:
        raise Forbidden("Only a platform admin may create an organization.",
                        code="create_not_permitted")
    org = Organization(**body.model_dump(), status="active", created_by=actor.user_id)
    session.add(org)
    session.flush()
    return org_dto(org)


@orgs.get("/orgs/{xid}")
def read_org(xid: uuid.UUID, actor: Principal = Depends(principal),
             session: Session = Depends(db)) -> dict:
    return org_dto(_org(session, xid, actor))


@orgs.patch("/orgs/{xid}")
def update_org(xid: uuid.UUID, body: OrgUpdate, actor: Principal = Depends(principal),
               session: Session = Depends(db)) -> dict:
    """`teacher_can_publish` and `content_edit_others` both default to false.
    Both widen who can affect published material, so both are an explicit
    opt-in by the centre rather than a platform default."""
    org = _org(session, xid, actor, Action.MANAGE_ORG)
    data = body.model_dump(exclude_none=True)
    if "settings" in data:
        org.settings = {**(org.settings or {}), **data.pop("settings")}
    for field, value in data.items():
        setattr(org, field, value)
    session.flush()
    return org_dto(org)


@orgs.get("/orgs/{xid}/members")
def list_members(xid: uuid.UUID, role: str | None = None,
                 actor: Principal = Depends(principal),
                 session: Session = Depends(db), limit: int = 25,
                 cursor: str | None = None) -> dict:
    """Full contact details for a TEACHING role, names only for everyone else.

    It used to return `user_dto` to any member — so a student could read their
    whole centre's roster, complete with every phone number, Telegram username
    and `is_minor` flag. That is the same defect already fixed one endpoint down
    in `list_cohort_members`, and it is worse here: the whole organization rather
    than one class, and `is_minor` on a roster of phone numbers is a targeting
    list, not a directory.
    """
    org = _org(session, xid, actor)
    teaches = actor.roles.get(org.id) in ("teacher", "centre_admin") or \
        actor.is_platform_admin
    query = select(OrgMembership, User).join(User, User.id == OrgMembership.user_id).where(
        OrgMembership.org_id == org.id, OrgMembership.status == "active")
    if role:
        query = query.where(OrgMembership.role == role)
    # **The roster is where the missing cursor cost the most.** A centre with
    # four hundred students returned twenty-five of them, `next_cursor: null`,
    # and nothing anywhere said the other three hundred and seventy-five
    # existed. Ordered by membership id, which is monotonic and never reused.
    if (after := decode_id(cursor)) is not None:
        query = query.where(OrgMembership.id > after)
    rows = session.execute(query.order_by(OrgMembership.id).limit(limit + 1)).all()
    kept, next_cursor = page([m for m, _ in rows], limit)
    return {"items": [{"org": org_dto(org),
                       "user": user_dto(u) if teaches else _classmate_dto(u),
                       "role": m.role, "status": m.status,
                       "joined_at": iso(m.joined_at)}
                      for m, u in rows[:len(kept)]], "next_cursor": next_cursor}


@orgs.post("/orgs/{xid}/invites", status_code=status.HTTP_201_CREATED)
def create_invite(xid: uuid.UUID, body: InviteCreate,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    """Addressed to a phone number, and from now on redeemable only by it.

    The token is stored hashed; redemption looks it up by hash and then checks
    that the number on the invite is the caller's own verified number. Before
    that check the token was a bearer credential for a ROLE — forward the
    `centre_admin` link your admin sent you and whoever opens it first is a
    centre admin. Nothing in this product delivers the token, so it is handed
    around by copy-paste; that is the normal path, not the abusive one.
    """
    org = _org(session, xid, actor, Action.MANAGE_ORG)
    raw = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=14)
    cohort_id = None
    if body.cohort_xid:
        # Scoped to the org just authorised, on top of MANAGE_ORG on it. The
        # lookup was by xid alone, so a centre admin who knew a cohort xid of
        # another centre — any member of that centre can read them off
        # `list_cohorts` — could write the invitee into that centre's class:
        # `redeem_invite` grants the membership in the INVITING org and the
        # cohort row wherever the id points. "Nor may a competitor touch its
        # roster" is the rule `revoke_invite` already states one door along,
        # and `add_cohort_members` refuses the same write made directly.
        #
        # Refused rather than silently nulled, which is what an unknown xid
        # used to become: the admin believed the student would land in the
        # class, and they did not.
        cohort = session.scalars(select(Cohort).where(
            Cohort.xid == body.cohort_xid, Cohort.org_id == org.id,
            Cohort.status == "active")).first()
        if cohort is None:
            raise NotFound("Cohort not found.")
        cohort_id = cohort.id
    row = session.execute(text("""
        INSERT INTO org_invites (org_id, cohort_id, role, phone, token_hash,
                                 created_by, expires_at)
        VALUES (:org, :cohort, :role, :phone, :token_hash, :by, :expires)
        RETURNING xid
    """).bindparams(org=org.id, cohort=cohort_id, role=body.role, phone=body.phone,
                    token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    by=actor.user_id, expires=expires)).scalar()
    return {"xid": str(row), "role": body.role, "expires_at": iso(expires),
            "delivered_via": "telegram", "token": raw}


@orgs.get("/orgs/{xid}/cohorts")
def list_cohorts(xid: uuid.UUID, actor: Principal = Depends(principal),
                 session: Session = Depends(db)) -> list[dict]:
    org = _org(session, xid, actor)
    return [
        {"xid": str(c.xid), "name": c.name, "academic_year": c.academic_year,
         "status": c.status,
         "member_count": session.scalar(
             select(func.count()).select_from(CohortMember)
             .where(CohortMember.cohort_id == c.id,
                    CohortMember.status == "active")) or 0}
        for c in session.scalars(
            select(Cohort).where(Cohort.org_id == org.id, Cohort.status == "active"))
    ]


@orgs.post("/orgs/{xid}/cohorts", status_code=status.HTTP_201_CREATED)
def create_cohort(xid: uuid.UUID, body: CohortCreate,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    org = _org(session, xid, actor, Action.MANAGE_ORG)
    cohort = Cohort(org_id=org.id, name=body.name, academic_year=body.academic_year,
                    created_by=actor.user_id)
    session.add(cohort)
    session.flush()
    return {"xid": str(cohort.xid), "name": cohort.name,
            "academic_year": cohort.academic_year, "status": cohort.status,
            "member_count": 0}


def _cohort(session: Session, xid: uuid.UUID, actor: Principal) -> Cohort:
    cohort = session.scalars(select(Cohort).where(Cohort.xid == xid)).first()
    if cohort is None or (cohort.org_id not in actor.org_ids
                          and not actor.is_platform_admin):
        raise NotFound("Cohort not found.")
    return cohort


@orgs.get("/cohorts/{xid}/members")
def list_cohort_members(xid: uuid.UUID, actor: Principal = Depends(principal),
                        session: Session = Depends(db)) -> list[dict]:
    """A class roster to a teacher; names only to a classmate.

    `user_dto` carries a phone number, and half this cohort may be fifteen years
    old. A student seeing who else is in their class is fine; a student
    harvesting their classmates' phone numbers is a safeguarding incident.
    """
    cohort = _cohort(session, xid, actor)
    teaches = (actor.roles.get(cohort.org_id) in ("teacher", "centre_admin")
               or actor.is_platform_admin)
    rows = session.execute(
        select(CohortMember, User).join(User, User.id == CohortMember.user_id)
        .where(CohortMember.cohort_id == cohort.id, CohortMember.status == "active")).all()
    return [{"user": user_dto(u) if teaches else _classmate_dto(u),
             "joined_at": iso(m.joined_at), "status": m.status}
            for m, u in rows]


def _classmate_dto(user: User) -> dict:
    return {"xid": str(user.xid), "given_name": user.given_name,
            "family_name": user.family_name, "phone": None, "locale": user.locale,
            "telegram_username": None, "timezone": user.timezone,
            "target_band": None, "is_minor": None, "created_at": None}


@orgs.post("/cohorts/{xid}/members")
def add_cohort_members(xid: uuid.UUID, body: CohortMembers,
                       actor: Principal = Depends(principal),
                       session: Session = Depends(db)) -> list[dict]:
    cohort = _cohort(session, xid, actor)
    policy.require(actor, Action.MANAGE_ORG, Resource(org_id=cohort.org_id))
    users = session.scalars(select(User).where(User.xid.in_(body.user_xids))).all()
    # Keyed by user, keeping the ROW rather than just the id: a membership that
    # has been left must be revived, not skipped. This read every row and skipped
    # every match, so once removal began writing `status = 'left'`, putting a
    # student back matched their old row, did nothing, and answered 200 with a
    # roster they were still absent from — a success and an unchanged list.
    #
    # Revived rather than inserted, because `(cohort_id, user_id)` is unique and
    # a second row would count the student twice in `member_count` and target
    # them twice in an assignment. Terms change and people come back.
    existing = {m.user_id: m for m in session.scalars(
        select(CohortMember).where(CohortMember.cohort_id == cohort.id))}
    for user in users:
        if (row := existing.get(user.id)) is not None:
            if row.status != "active":
                row.status = "active"
                row.left_at = None
                # `joined_at` is when this membership began, and it began again.
                # The attendance report reads it, and a date from a previous term
                # would credit them with weeks they were not in the room.
                row.joined_at = dt.datetime.now(dt.UTC)
            continue
        # A student must belong to the organization before joining one of its
        # cohorts, or a centre could add anyone's account to its reporting.
        if not session.scalars(
            select(OrgMembership).where(OrgMembership.org_id == cohort.org_id,
                                        OrgMembership.user_id == user.id,
                                        OrgMembership.status == "active")).first():
            raise Conflict(f"{user.given_name} is not a member of this organization.",
                           code="not_an_org_member")
        session.add(CohortMember(cohort_id=cohort.id, user_id=user.id))
    session.flush()
    return list_cohort_members(xid, actor, session)


@orgs.delete("/cohorts/{xid}/members/{user_xid}",
             status_code=status.HTTP_204_NO_CONTENT)
def remove_cohort_member(xid: uuid.UUID, user_xid: uuid.UUID,
                         actor: Principal = Depends(principal),
                         session: Session = Depends(db)) -> Response:
    """Take a student out of a class.

    **`cohort_members.left_at` is read in four places and was written by
    nothing.** A student's own assignment list, the member count, and the
    expansion of a cohort into `assignment_targets` all say
    `left_at IS NULL` — so leaving a class was designed throughout the query
    layer and reachable from nowhere. A student who changed groups, or left the
    centre, kept receiving that class's mocks for ever, and the only remedy was
    SQL.

    Soft, not a delete, and NOT because past assignments depend on it —
    `assignment_targets` holds `user_id` directly, so a hard delete would leave
    every sat mock resolving perfectly well. It is soft because the row is the
    only record that this student was ever in this class: `joined_at` and
    `left_at` are what `/cohorts/{xid}/attendance` reports against, and deleting
    the row answers "was Aziza in the evening group last term?" with silence.

    **Both columns, because two queries disagree about which one means
    "still here".** `list_cohort_members` filters on `status = 'active'` and the
    assignment expansion filters on `left_at IS NULL`; setting one would take a
    student out of the roster while still assigning them work, or the reverse.
    """
    cohort = _cohort(session, xid, actor)
    policy.require(actor, Action.MANAGE_ORG, Resource(org_id=cohort.org_id))
    user = session.scalars(select(User).where(User.xid == user_xid)).first()
    membership = session.scalars(
        select(CohortMember).where(CohortMember.cohort_id == cohort.id,
                                   CohortMember.user_id == (user.id if user else 0),
                                   CohortMember.status == "active")).first() if user else None
    if membership is None:
        raise NotFound("This student is not in this class.")
    membership.status = "left"
    membership.left_at = dt.datetime.now(dt.UTC)
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@orgs.delete("/orgs/{xid}/members/{user_xid}",
             status_code=status.HTTP_204_NO_CONTENT)
def remove_org_member(xid: uuid.UUID, user_xid: uuid.UUID,
                      actor: Principal = Depends(principal),
                      session: Session = Depends(db)) -> Response:
    """Take somebody off the centre's roster.

    **`org_memberships.left_at` was read and written by nothing**, and the same
    was true of `status` here — a centre could enrol a student and had no way to
    un-enrol them. A student could be taken out of a class (`remove_cohort_member`
    one endpoint up) and stayed a member of the organization for ever: still
    counted on the roster, still a valid `target_kind="users"` assignment target,
    still holding a seat.

    **Ends their cohort memberships in the same transaction, and that is the part
    worth reading twice.** Leaving the centre and leaving its classes are two
    tables, and `expand_targets` reads `cohort_members` alone. Removing only the
    org row would take a departed student off the roster while their class kept
    delivering mocks to them — the exact half-state the cohort fix was written to
    avoid, one level up.

    Soft, for the same reason as the cohort row: `joined_at`/`left_at` are what
    `/cohorts/{xid}/attendance` and a billing dispute are settled against.

    A seat is NOT released here. Seats are a paid resource with their own
    endpoint and their own audit trail, and quietly handing one back as a side
    effect of a roster edit is how a centre discovers it has been billed for
    something it did not do. `release_seat` is one call and it is deliberate.
    """
    org = _org(session, xid, actor, Action.MANAGE_ORG)
    user = session.scalars(select(User).where(User.xid == user_xid)).first()
    membership = session.scalars(
        select(OrgMembership).where(OrgMembership.org_id == org.id,
                                    OrgMembership.user_id == (user.id if user else 0),
                                    OrgMembership.status == "active")).first() if user else None
    if membership is None:
        raise NotFound("This person is not a member of this organization.")
    if membership.role == "centre_admin" and session.scalar(
            select(func.count()).select_from(OrgMembership)
            .where(OrgMembership.org_id == org.id,
                   OrgMembership.role == "centre_admin",
                   OrgMembership.status == "active")) <= 1:
        # Otherwise a centre admin removes themselves and the organization has
        # nobody who can add one back — recoverable only by platform admin.
        raise Conflict("This is the organization's last centre admin. Promote "
                       "somebody else first.", code="last_centre_admin")
    now = dt.datetime.now(dt.UTC)
    membership.status = "left"
    membership.left_at = now
    for row in session.scalars(
        select(CohortMember).join(Cohort, Cohort.id == CohortMember.cohort_id)
        .where(Cohort.org_id == org.id, CohortMember.user_id == membership.user_id,
               CohortMember.status == "active")):
        row.status = "left"
        row.left_at = now
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@orgs.delete("/orgs/{xid}/invites/{invite_xid}",
             status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(xid: uuid.UUID, invite_xid: uuid.UUID,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> Response:
    """Withdraw an invite that has not been taken up.

    `org_invites.revoked_at` has existed since migration 0003 and nothing wrote
    it and nothing read it — so the only way to un-send an invite was to wait
    fourteen days for it to expire. The comment on the role-promotion rule below
    already names the scenario ("a centre admin pasting a `student` link into a
    group chat"); this is the remedy it assumed existed.

    Scoped by `org_id` as well as by `xid`, on top of MANAGE_ORG on that org: one
    centre must not be able to touch another's invites even holding the xid.
    """
    org = _org(session, xid, actor, Action.MANAGE_ORG)
    revoked = session.execute(text("""
        UPDATE org_invites SET revoked_at = now()
        WHERE xid = CAST(:i AS uuid) AND org_id = :o
          AND accepted_at IS NULL AND revoked_at IS NULL
        RETURNING id
    """).bindparams(i=str(invite_xid), o=org.id)).first()
    if revoked is None:
        raise NotFound("Invite not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _verified_phone(session: Session, actor: Principal) -> User:
    """The caller's number, and only once a code sent to that handset proved it.

    `users.phone` on its own is a self-declared string: registration takes it
    from Telegram's `requestContact`, which is client-side, and the router says
    so in as many words. Matching an invite against an unverified number would be
    a lock whose key is "type the number you want" — worse than no lock, because
    it reads like one.
    """
    user = session.get(User, actor.user_id)
    if user.phone_verified_at is None:
        raise Forbidden(
            "Confirm your phone number by SMS code before joining an organization.",
            code="phone_not_verified")
    return user


@orgs.get("/invites/pending")
def list_pending_invites(actor: Principal = Depends(principal),
                         session: Session = Depends(db)) -> list[dict]:
    """Invitations addressed to my number, so the phone IS the delivery channel.

    Nothing in this product sends an invite: `create_invite` returns the token
    to the admin who made it and hardcodes `delivered_via: "telegram"`. A centre
    onboarding forty students has forty numbers and no way to reach any of them,
    which is why the token got pasted into group chats in the first place.

    So the student's own number becomes the channel: sign in, confirm the
    number, and the invitations sent to it are here to accept. Migration 0003
    built `org_invites (phone) WHERE accepted_at IS NULL` for exactly this query
    and no code had ever issued it.

    Revoked, spent and expired invites are excluded rather than listed as dead
    entries — this is a to-do list, not a history.
    """
    user = _verified_phone(session, actor)
    rows = session.execute(text("""
        SELECT xid, org_id, role, expires_at FROM org_invites
        WHERE phone = :p AND accepted_at IS NULL AND revoked_at IS NULL
              AND expires_at > now()
        ORDER BY expires_at
    """).bindparams(p=user.phone)).mappings().all()
    by_id = {o.id: o for o in session.scalars(
        select(Organization).where(Organization.id.in_([r["org_id"] for r in rows])))}
    return [{"xid": str(r["xid"]), "org": org_dto(by_id[r["org_id"]]),
             "role": r["role"], "expires_at": iso(r["expires_at"])} for r in rows]


def _invite_row(session: Session, body: InviteAccept, phone: str):
    """Two lookups, because the two keys carry different authority.

    A token is a secret and holding it is itself evidence, so an unknown one is a
    plain 404 and a good one addressed to somebody else is told exactly that. An
    `xid` is not a secret — it is handed to the centre admin who created the
    invite — so that lookup is scoped to the caller's own number and an invite
    for anyone else's simply does not exist.
    """
    if body.token is not None:
        return session.execute(text("""
            SELECT id, org_id, cohort_id, role, phone, expires_at, accepted_at,
                   revoked_at
            FROM org_invites WHERE token_hash = :h
        """).bindparams(h=hashlib.sha256(body.token.encode()).hexdigest())
        ).mappings().first()
    return session.execute(text("""
        SELECT id, org_id, cohort_id, role, phone, expires_at, accepted_at,
               revoked_at
        FROM org_invites WHERE xid = CAST(:x AS uuid) AND phone = :p
    """).bindparams(x=str(body.xid), p=phone)).mappings().first()


@orgs.post("/invites/accept")
def accept_invite(body: InviteAccept, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    """Redeem an invitation addressed to my verified phone number.

    **The invite is bound to the number it was sent to.** It was not: the row
    carried a `phone`, `create_invite` wrote it, and redemption selected six
    columns that did not include it. Whoever held the token took the role, and a
    `centre_admin` invite is worth forwarding.

    The suite proved it and read as if it passed — every invite test in
    `test_identity.py` addressed `+998909900001` and then redeemed it as a user
    with an entirely different number, twelve times over.
    """
    user = _verified_phone(session, actor)
    row = _invite_row(session, body, user.phone)
    check_invite(row, user.phone)
    return redeem_invite(session, row, user.id)


def check_invite(row, phone: str) -> None:
    """The four ways an invite is not usable, in one place.

    Shared with `auth.invite_redeem`, which runs the same checks before it
    creates an account. Two copies would be two places for "revoked" or "already
    used" to be forgotten, and the copy that forgets is the one that hands a
    stranger a role.
    """
    if row is None:
        raise NotFound("Invite not found.")
    if row["phone"] != phone:
        # Deliberately does not say which number, which would turn a leaked token
        # into a lookup of the invited student's phone.
        raise Forbidden("This invitation was sent to a different phone number.",
                        code="invite_not_yours")
    if row["revoked_at"] is not None:
        raise Gone("This invitation was withdrawn.", code="invite_revoked")
    if row["accepted_at"] is not None or row["expires_at"] < dt.datetime.now(dt.UTC):
        # 410, not the 409 this sent for a year while the contract said 410 —
        # there is no state in which retrying a spent invite works.
        raise Gone("This invite has expired or was already used.",
                   code="invite_unusable")


def redeem_invite(session: Session, row, user_id: int) -> dict:
    """Grant the membership the invite names, and spend it.

    Split out from `accept_invite` so the registration path in `auth.py` reaches
    the SAME code rather than a second version of it — the role-raising rule
    below is subtle enough that a reimplementation would get it wrong quietly.
    """
    # `org_memberships` is UNIQUE on (org_id, user_id), so adding blindly raised
    # an IntegrityError -- a 500 -- for anyone already at the centre. And because
    # the transaction rolled back, the invite was never marked accepted, so the
    # same 500 came back every retry and the user was stuck for good. A student
    # already enrolled, sent a link for a second cohort, hit exactly that.
    membership = session.scalars(
        select(OrgMembership).where(OrgMembership.org_id == row["org_id"],
                                    OrgMembership.user_id == user_id)).first()
    if membership is None:
        membership = OrgMembership(org_id=row["org_id"], user_id=user_id,
                                   role=row["role"], joined_at=dt.datetime.now(dt.UTC))
        session.add(membership)
    elif _RANK.get(row["role"], 0) > _RANK.get(membership.role, 0):
        # Raise a role, never lower one. The invite names a role and was created
        # by someone with MANAGE_ORG, so honouring a promotion is the intent --
        # but a centre admin pasting a `student` link into a group chat must not
        # demote the teacher who clicks it.
        membership.role = row["role"]

    if row["cohort_id"]:
        _join_invited_cohort(session, row, user_id)
    session.execute(text("""
        UPDATE org_invites SET accepted_at = now(), accepted_by = :u WHERE id = :id
    """).bindparams(u=user_id, id=row["id"]))
    session.flush()
    org = session.get(Organization, row["org_id"])
    return {"org": org_dto(org), "role": membership.role, "status": membership.status,
            "joined_at": iso(membership.joined_at)}


def _join_invited_cohort(session: Session, row, user_id: int) -> None:
    """The class an invite names, joined only if it belongs to the inviting org.

    `create_invite` now refuses a cohort from another centre, so no NEW row can
    disagree with itself. This is the check for rows that already exist — or
    that arrive by hand — because the membership above was granted in
    `row["org_id"]` and a cohort row anywhere else is the cross-tenant write
    the create-side scope closes. Skipped with a log line rather than refused:
    the invitee did nothing wrong, the org membership is the substance of the
    invitation, and a 4xx here would burn a token they cannot get back.
    """
    import structlog

    cohort = session.get(Cohort, row["cohort_id"])
    if cohort is None or cohort.org_id != row["org_id"]:
        structlog.get_logger().warning(
            "invite_cohort_outside_inviting_org", invite_id=row["id"],
            cohort_id=row["cohort_id"], org_id=row["org_id"])
        return
    if not session.scalars(
            select(CohortMember).where(CohortMember.cohort_id == cohort.id,
                                       CohortMember.user_id == user_id)).first():
        session.add(CohortMember(cohort_id=cohort.id, user_id=user_id))
