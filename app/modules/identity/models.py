"""PRIVATE to the identity module. Importing this from another module is a CI
failure (`import-linter`); cross-module code goes through `service.py`."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Computed, Date, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin
from app.platform.ids import new_xid

# The one spelling of a phone number this product stores: E.164, Uzbekistan.
#
# Sign-in has always pinned it. An invite did not — `phone: str`, anything
# accepted — which was harmless while nothing read the column back and became a
# silent trap the moment redemption matched on it: `998901234567` and
# `+998 90 123 45 67` are the same handset and neither will ever equal the
# `+998901234567` on the account. One constant, so the two cannot drift apart.
PHONE_PATTERN = r"^\+998[0-9]{9}$"


class User(IdMixin, Base):
    __tablename__ = "users"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    phone: Mapped[str] = mapped_column(Text)
    phone_verified_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    telegram_username: Mapped[str | None] = mapped_column(Text, default=None)
    given_name: Mapped[str] = mapped_column(Text, default="")
    family_name: Mapped[str | None] = mapped_column(Text, default=None)
    date_of_birth: Mapped[dt.date] = mapped_column(Date)
    # Generated column. Computed() keeps it out of INSERT/UPDATE entirely, so age
    # banding cannot drift from the date of birth it is derived from.
    adult_at: Mapped[dt.date] = mapped_column(
        Date, Computed("((date_of_birth + INTERVAL '18 years'))::date", persisted=True))
    locale: Mapped[str] = mapped_column(Text, default="uz-Latn")
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Tashkent")
    status: Mapped[str] = mapped_column(Text, default="active")
    target_band: Mapped[float | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class Organization(IdMixin, Base):
    __tablename__ = "organizations"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text, default=None)
    kind: Mapped[str] = mapped_column(Text, default="prep_centre")
    status: Mapped[str] = mapped_column(Text, default="pending")
    contact_phone: Mapped[str | None] = mapped_column(Text, default=None)
    country: Mapped[str] = mapped_column(Text, default="UZ")
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Tashkent")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class OrgMembership(IdMixin, Base):
    """Role lives HERE, not on the user. That single choice is what lets an
    unaffiliated individual and an org cohort coexist without a discriminator,
    and lets one human be a teacher at one centre and a student at another."""

    __tablename__ = "org_memberships"

    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    invited_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    joined_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    left_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now())


class PlatformRoleGrant(IdMixin, Base):
    __tablename__ = "platform_role_grants"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    granted_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)


class Cohort(IdMixin, Base):
    __tablename__ = "cohorts"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(Text)
    academic_year: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class CohortMember(IdMixin, Base):
    __tablename__ = "cohort_members"

    cohort_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cohorts.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(Text, default="active")
    joined_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    left_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class AuthSession(IdMixin, Base):
    """Opaque, stored, rotating. A safety ban must kill a live session now, which
    a stateless JWT cannot do."""

    __tablename__ = "auth_sessions"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(Text, default=None)
    user_agent_hash: Mapped[str | None] = mapped_column(Text, default=None)
    issued_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    expires_at: Mapped[dt.datetime] = mapped_column()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str | None] = mapped_column(Text, default=None)
    rotated_from_id: Mapped[int | None] = mapped_column(BigInteger, default=None)


class Consent(IdMixin, Base):
    """Evidence, not a boolean: who consented, when, through what channel, and
    against which version of the text."""

    __tablename__ = "consents"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(Text)
    doc_version: Mapped[str] = mapped_column(Text)
    doc_hash: Mapped[str] = mapped_column(Text)
    granted_by_kind: Mapped[str] = mapped_column(Text)
    parent_name: Mapped[str | None] = mapped_column(Text, default=None)
    parent_phone: Mapped[str | None] = mapped_column(Text, default=None)
    channel: Mapped[str] = mapped_column(Text, default="web")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    granted_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)


class UserBlock(IdMixin, Base):
    __tablename__ = "user_blocks"

    blocker_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    blocked_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
