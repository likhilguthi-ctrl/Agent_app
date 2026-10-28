import pytest
from pydantic import ValidationError

from app.core.config import Settings

def test_production_requires_database_and_explicit_https_frontend_origin(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FRONTEND_ORIGINS", raising=False)

    with pytest.raises(
        ValidationError,
        match="DATABASE_URL is required in production",
    ):
        Settings(_env_file=None, environment="production")

    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://app:replace_me@localhost:5432/community_ott_test",
            frontend_origins="http://portal.example",
        )

    with pytest.raises(ValidationError, match="placeholder password"):
        Settings(
            _env_file=None,
            environment="prod",
            database_url="postgresql+psycopg://app:replace_me@localhost:5432/community_ott_test",
            frontend_origins="https://portal.example",
        )
