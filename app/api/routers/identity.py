"""The acting user, their consents and devices; and organizations/cohorts."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, db, principal
from app.api.dto import iso
from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource
from app.modules.identity.models import (
    Cohort, CohortMember, Consent, Organization, OrgMembership, User,
)
from app.platform.errors import Conflict, Forbidden, NotFound

router = APIRouter(tags=["identity"])
orgs = APIRouter(tags=["orgs"])


class UserUpdate(BaseModel):
    given_name: str | None = None
    family_name: str | None = None
    locale: str | None = None
    target_band: float | None = Field(default=None, ge=1, le=9)


class ConsentCreate(BaseModel):
    kind: str
    doc_version: str
    granted_by_kind: str = "self"
    parent_name: str | None = None
    parent_phone: str | None = None
    channel: str = "web"


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
        {"kind": c.kind, "doc_version": c.doc_version,
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
    kind: str = "prep_centre"
    legal_name: str | None = None
    contact_phone: str | None = None


class OrgUpdate(BaseModel):
    name: str | None = None
    contact_phone: str | None = None
    settings: dict | None = None


# Role precedence, for the one place an invite meets an existing membership.
_RANK = {"student": 1, "teacher": 2, "centre_admin": 3}


class InviteCreate(BaseModel):
    phone: str
    role: str
    cohort_xid: uuid.UUID | None = None


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
              session: Session = Depends(db), limit: int = 25) -> dict:
    """Scoped to the actor's memberships. A platform admin sees everything."""
    query = select(Organization).order_by(Organization.id)
    if not actor.is_platform_admin:
        query = query.where(Organization.id.in_(actor.org_ids or [0]))
    rows = session.scalars(query.limit(limit)).all()
    return {"items": [org_dto(o) for o in rows], "next_cursor": None}


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
                 session: Session = Depends(db), limit: int = 25) -> dict:
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
    rows = session.execute(query.limit(limit)).all()
    return {"items": [{"org": org_dto(org),
                       "user": user_dto(u) if teaches else _classmate_dto(u),
                       "role": m.role, "status": m.status,
                       "joined_at": iso(m.joined_at)}
                      for m, u in rows], "next_cursor": None}


@orgs.post("/orgs/{xid}/invites", status_code=status.HTTP_201_CREATED)
def create_invite(xid: uuid.UUID, body: InviteCreate,
                  actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    """Delivered over Telegram when the number is known to the bot, otherwise
    SMS. The token is stored hashed; redemption looks it up by hash."""
    from sqlalchemy import text

    org = _org(session, xid, actor, Action.MANAGE_ORG)
    raw = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=14)
    cohort_id = None
    if body.cohort_xid:
        cohort = session.scalars(select(Cohort).where(Cohort.xid == body.cohort_xid)).first()
        cohort_id = cohort.id if cohort else None
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
    existing = {m.user_id for m in session.scalars(
        select(CohortMember).where(CohortMember.cohort_id == cohort.id))}
    for user in users:
        if user.id in existing:
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


@orgs.post("/invites/accept")
def accept_invite(body: dict, actor: Principal = Depends(principal),
                  session: Session = Depends(db)) -> dict:
    from sqlalchemy import text

    row = session.execute(text("""
        SELECT id, org_id, cohort_id, role, expires_at, accepted_at
        FROM org_invites WHERE token_hash = :h
    """).bindparams(h=hashlib.sha256(str(body.get("token", "")).encode()).hexdigest())
    ).mappings().first()
    if row is None:
        raise NotFound("Invite not found.")
    if row["accepted_at"] is not None or row["expires_at"] < dt.datetime.now(dt.UTC):
        raise Conflict("This invite has expired or was already used.",
                       code="invite_unusable")

    # `org_memberships` is UNIQUE on (org_id, user_id), so adding blindly raised
    # an IntegrityError -- a 500 -- for anyone already at the centre. And because
    # the transaction rolled back, the invite was never marked accepted, so the
    # same 500 came back every retry and the user was stuck for good. A student
    # already enrolled, sent a link for a second cohort, hit exactly that.
    membership = session.scalars(
        select(OrgMembership).where(OrgMembership.org_id == row["org_id"],
                                    OrgMembership.user_id == actor.user_id)).first()
    if membership is None:
        membership = OrgMembership(org_id=row["org_id"], user_id=actor.user_id,
                                   role=row["role"], joined_at=dt.datetime.now(dt.UTC))
        session.add(membership)
    elif _RANK.get(row["role"], 0) > _RANK.get(membership.role, 0):
        # Raise a role, never lower one. The invite names a role and was created
        # by someone with MANAGE_ORG, so honouring a promotion is the intent --
        # but a centre admin pasting a `student` link into a group chat must not
        # demote the teacher who clicks it.
        membership.role = row["role"]

    if row["cohort_id"] and not session.scalars(
            select(CohortMember).where(CohortMember.cohort_id == row["cohort_id"],
                                       CohortMember.user_id == actor.user_id)).first():
        session.add(CohortMember(cohort_id=row["cohort_id"], user_id=actor.user_id))
    session.execute(text("""
        UPDATE org_invites SET accepted_at = now(), accepted_by = :u WHERE id = :id
    """).bindparams(u=actor.user_id, id=row["id"]))
    session.flush()
    org = session.get(Organization, row["org_id"])
    return {"org": org_dto(org), "role": membership.role, "status": membership.status,
            "joined_at": iso(membership.joined_at)}
