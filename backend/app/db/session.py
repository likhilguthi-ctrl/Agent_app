from __future__ import annotations

from functools import lru_cache
from typing import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


class DatabaseUnavailable(Exception):
    """Database-backed operations cannot run with the current configuration."""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL is required for database-backed operations.")
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that closes the session and rolls back failures."""
    try:
        session_factory = get_session_factory()
    except RuntimeError as exc:
        raise DatabaseUnavailable from exc
    with session_factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def check_database_connectivity() -> None:
    """Raise when the configured PostgreSQL service cannot execute a simple query."""
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))


def dispose_database_engine() -> None:
    """Release a lazily initialized engine during application shutdown."""
    get_session_factory.cache_clear()
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
