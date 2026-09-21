"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every model."""


class IdMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _now() -> datetime:
    return datetime.now(UTC)


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        # SQLite (used in tests) uses a NullPool that rejects pool sizing.
        kwargs: dict[str, object] = {} if url.startswith("sqlite") else {
            "pool_size": 5,
            "pool_pre_ping": True,
        }
        _engine = create_async_engine(url, **kwargs)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session, commit on success, roll back on error."""
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
