from __future__ import annotations

import os

import httpx
import pytest
import app.db.session as db_session
from app.core.config import Settings
from app.main import create_app


def test_settings_read_database_url_and_frontend_origins_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://sahayak_app:replace_me@localhost:5432/sahayak_partners",
    )
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:3000, https://portal.example")

    settings = Settings(_env_file=None)

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://")
    assert settings.frontend_origin_list == ["http://localhost:3000", "https://portal.example"]


def test_sqlalchemy_engine_uses_postgresql_dialect_without_connecting(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://sahayak_app:replace_me@localhost:5432/sahayak_partners",
    )
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    db_session.get_engine.cache_clear()

    try:
        engine = db_session.get_engine()
        assert engine.dialect.name == "postgresql"
        engine.dispose()
    finally:
        db_session.get_engine.cache_clear()


def test_configured_postgresql_database_connectivity(monkeypatch) -> None:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("Set TEST_DATABASE_URL to run the PostgreSQL connectivity integration check.")

    settings = Settings(_env_file=None, database_url=test_database_url)
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    db_session.get_engine.cache_clear()

    try:
        db_session.check_database_connectivity()
    finally:
        db_session.dispose_database_engine()


@pytest.mark.anyio
async def test_authentication_fails_closed_with_unconfigured_database(monkeypatch) -> None:
    settings = Settings(_env_file=None, environment="development", database_url=None)
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    db_session.get_engine.cache_clear()
    db_session.get_session_factory.cache_clear()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://community.example.test",
        ) as client:
            response = await client.get("/api/v1/auth/me")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
