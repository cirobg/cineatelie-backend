"""The async session factory. Not used directly by use cases — see `uow.py`, which is the
only place a transaction is opened (ADR-003: "a transaction contains SQL and nothing else").
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
