"""Async engine / sessionmaker factories built from settings."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ledgercore.config import Settings, get_settings


def create_engine(settings: Settings | None = None, **kwargs: object) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True, **kwargs)


def create_sessionmaker(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: results stay usable after commit without implicit lazy IO.
    return async_sessionmaker(engine or create_engine(), expire_on_commit=False)
