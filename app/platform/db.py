"""Database access.

One `MetaData` shared by every module's models, even though the modules are
otherwise strictly separated. That is deliberate: cross-module foreign keys are
declared as strings (`ForeignKey("users.id")`) and SQLAlchemy resolves them at
mapper-configuration time, which requires one registry. Pretending otherwise
would mean either duplicating table definitions or giving up referential
integrity, and we are one database.

The boundary that matters is enforced elsewhere and differently: `import-linter`
forbids importing another module's `models` or `repo`. A shared MetaData does not
let `exam` read `content.models`; it only lets Postgres enforce the FK.

Migrations remain the canonical schema — these models describe the subset the
application actually reads and write, and `tests/integration` asserts they agree.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings

NAMING = {
    "ix": "%(table_name)s_%(column_0_name)s_idx",
    "uq": "%(table_name)s_%(column_0_name)s_uq",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


_engine = None
_session_factory = None


def engine():
    global _engine
    if _engine is None:
        # pool_pre_ping because a VPS Postgres restart should surface as a
        # reconnect, not as a wall of stale-connection errors.
        _engine = create_engine(settings().database_url, pool_pre_ping=True,
                                pool_size=5, max_overflow=5, future=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def unit_of_work() -> Iterator[Session]:
    """One transaction per request or per job.

    The outbox is written inside this same transaction, which is the whole reason
    a regrade enqueued alongside a key change cannot be lost (ADR-0001 §4.5).
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Test hook: drop cached engine/session factory after DATABASE_URL changes."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
