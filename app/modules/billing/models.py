"""PRIVATE to the billing module."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin
from app.platform.ids import new_xid


class EntitlementRow(IdMixin, Base):
    """The one table the whole product asks "is this allowed" against."""

    __tablename__ = "entitlements"

    xid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=new_xid)
    subject_kind: Mapped[str] = mapped_column(Text, default="user")
    subject_id: Mapped[int] = mapped_column(BigInteger, default=0)
    feature: Mapped[str] = mapped_column(Text, default="")
    source_kind: Mapped[str] = mapped_column(Text, default="order")
    source_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # NULL quantity = unlimited; otherwise a consumable balance.
    quantity: Mapped[int | None] = mapped_column(default=None)
    consumed: Mapped[int] = mapped_column(default=0)
    starts_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str | None] = mapped_column(Text, default=None)
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class SeatAssignment(IdMixin, Base):
    """A seat licence only covers users who actually hold a seat — otherwise ten
    seats would entitle a four-hundred-student centre."""

    __tablename__ = "seat_assignments"

    entitlement_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entitlements.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    assigned_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    assigned_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    released_at: Mapped[dt.datetime | None] = mapped_column(default=None)
