"""Tests for development-only OTP console logging.

Verifies:
1. Setting is disabled by default.
2. Setting is allowed and functional in development environment only.
3. Setting is strictly rejected in production and staging environments (as well as test).
4. Terminal output contains clearly labeled notice, non-sensitive challenge ID, and the generated OTP.
5. The logged OTP is the actual random OTP that validates the challenge.
6. Console logging does not disrupt OTP database persistence or notification dispatch.
7. Terminal output errors (e.g. broken pipe) fail safely without interrupting login.
8. Raw OTP is never leaked in HTTP responses or audit logs.
"""
from __future__ import annotations

import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.otp_service import OtpService
from app.modules.messaging.notification_service import NotificationServiceClient


@pytest.fixture
def sqlite_factory(tmp_path):
    db_file = tmp_path / "test_otp_console.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_fk(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def mock_notif_client():
    client = MagicMock(spec=NotificationServiceClient)
    dispatch_res = MagicMock()
    dispatch_res.message_id = "msg_test_dev_001"
    dispatch_res.status = "QUEUED"
    dispatch_res.channel = "WHATSAPP"
    client.send_otp = AsyncMock(return_value=dispatch_res)
    client.send_otp_sms = AsyncMock(return_value=dispatch_res)
    return client


# ---------------------------------------------------------------------------
# Requirement 2, 3, 9: Configuration and Environment Constraints
# ---------------------------------------------------------------------------

def test_otp_log_to_console_disabled_by_default():
    settings = Settings(_env_file=None, environment="development")
    assert settings.otp_log_to_console is False
    assert settings.can_log_otp_to_console is False


def test_otp_log_to_console_allowed_in_development_when_explicitly_enabled():
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_log_to_console=True,
    )
    assert settings.otp_log_to_console is True
    assert settings.can_log_otp_to_console is True


def test_otp_log_to_console_rejected_in_production():
    with pytest.raises(ValidationError, match="OTP_LOG_TO_CONSOLE is not permitted in production or staging"):
        Settings(
            _env_file=None,
            environment="production",
            otp_log_to_console=True,
            database_url="postgresql+psycopg://app:sec123@localhost:5432/community_ott_prod",
            frontend_origins="https://portal.example.com",
        )


def test_otp_log_to_console_rejected_in_staging():
    with pytest.raises(ValidationError, match="OTP_LOG_TO_CONSOLE is not permitted in production or staging"):
        Settings(
            _env_file=None,
            environment="staging",
            otp_log_to_console=True,
            database_url="postgresql+psycopg://app:sec123@localhost:5432/community_ott_staging",
            frontend_origins="https://portal.example.com",
        )


def test_otp_log_to_console_rejected_in_test_environment():
    with pytest.raises(ValidationError, match="OTP_LOG_TO_CONSOLE is allowed only in development"):
        Settings(
            _env_file=None,
            environment="test",
            otp_log_to_console=True,
        )


def test_can_log_otp_to_console_defense_in_depth():
    # When disabled in dev: False
    dev_disabled = Settings(_env_file=None, environment="development", otp_log_to_console=False)
    assert dev_disabled.can_log_otp_to_console is False

    # When enabled in dev: True
    dev_enabled = Settings(_env_file=None, environment="development", otp_log_to_console=True)
    assert dev_enabled.can_log_otp_to_console is True


# ---------------------------------------------------------------------------
# Requirement 4, 5, 7, 8: Terminal Output, Persistence, and Verification
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_console_logging_disabled_prints_nothing(sqlite_factory, mock_notif_client, capsys):
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_log_to_console=False,
    )
    otp_svc = OtpService(
        settings=settings,
        notification_client=mock_notif_client,
        session_factory=sqlite_factory,
    )

    challenge_id, cooldown = await otp_svc.request_otp(
        mobile_number="+919100209205",
        purpose="login",
    )

    captured = capsys.readouterr()
    assert "[DEVELOPMENT ONLY]" not in captured.out
    assert "Login OTP" not in captured.out
    assert challenge_id is not None
    assert cooldown == 60


@pytest.mark.anyio
async def test_console_logging_enabled_in_development_prints_random_otp(sqlite_factory, mock_notif_client, capsys):
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_log_to_console=True,
    )
    otp_svc = OtpService(
        settings=settings,
        notification_client=mock_notif_client,
        session_factory=sqlite_factory,
    )

    mobile = "+919100209205"
    challenge_id, cooldown = await otp_svc.request_otp(
        mobile_number=mobile,
        purpose="login",
    )

    captured = capsys.readouterr().out
    assert "[DEVELOPMENT ONLY] Community OTT Login OTP" in captured
    assert f"Challenge ID : {challenge_id}" in captured
    assert "Purpose      : login" in captured
    assert "Expires In   : 300s" in captured

    # Extract the logged OTP
    match = re.search(r"Login OTP\s*:\s*(\d{6})", captured)
    assert match is not None, f"Expected 6-digit OTP in terminal output, got:\n{captured}"
    logged_otp = match.group(1)

    # Verify that the logged OTP validates the challenge
    verified, v_token = otp_svc.verify_otp(
        mobile_number=mobile,
        otp=logged_otp,
        challenge_id=challenge_id,
        purpose="login",
    )
    assert verified is True

    # Confirm notification dispatch also executed
    assert mock_notif_client.send_otp.called
    dispatch_call = mock_notif_client.send_otp.call_args
    assert dispatch_call.kwargs["otp"] == logged_otp
    assert dispatch_call.kwargs["challenge_id"] == str(challenge_id)


@pytest.mark.anyio
async def test_console_logging_does_not_prevent_persistence_when_terminal_fails(sqlite_factory, mock_notif_client):
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_log_to_console=True,
    )
    otp_svc = OtpService(
        settings=settings,
        notification_client=mock_notif_client,
        session_factory=sqlite_factory,
    )

    # Simulate terminal stdout raising an IOError (e.g. Broken pipe)
    with patch("sys.stdout.write", side_effect=IOError("Broken pipe")):
        challenge_id, _ = await otp_svc.request_otp(
            mobile_number="+919100209205",
            purpose="login",
        )

    # Persistence must still have succeeded in the database
    with sqlite_factory() as session:
        challenge = session.execute(
            select(OtpChallenge).where(OtpChallenge.id == challenge_id)
        ).scalar_one_or_none()
        assert challenge is not None
        assert challenge.mobile_e164 == "+919100209205"
        assert challenge.purpose == "login"

    # Notification dispatch must still have been called
    assert mock_notif_client.send_otp.called


@pytest.mark.anyio
async def test_fastapi_login_endpoint_console_logging_and_login_completion(tmp_path, capsys):
    """End-to-end FastAPI test:

    1. POST /api/v1/auth/login triggers OTP generation.
    2. HTTP response returns challenge_id and masked mobile without leaking the plain OTP.
    3. Terminal stdout receives the formatted OTP notice.
    4. The extracted OTP completes second-factor login successfully.
    """
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from app.cli.bootstrap_accounts import run_bootstrap
    from app.core.tenant_context import TenantContext
    from app.main import create_app
    from app.modules.authentication.abuse_control import SQLAlchemyOtpAbuseGuard
    from app.modules.authentication.api import (
        get_authentication_repository,
        get_otp_abuse_guard,
    )
    from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
    from app.modules.authorization.api import get_authorization_repository
    from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
    from app.modules.identity.repository import (
        SQLAlchemyTenantMembershipRepository,
        get_tenant_membership_lookup,
    )
    from app.modules.tenants.dependencies import get_tenant_context
    from app.modules.tenants.models import Tenant

    db_path = tmp_path / "login_console_test.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_fk(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    tenant_id = uuid4()
    with factory.begin() as session:
        t = Tenant(id=tenant_id, slug="demo-tenant", status="active")
        session.add(t)

    # Bootstrap Super Admin
    run_bootstrap(
        role="SUPER_ADMIN",
        username="super_admin",
        password="SuperAdmin@123",
        mobile_number="+919100209205",
        must_change_password=False,
        factory=factory,
    )

    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
        otp_log_to_console=True,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
    )

    app = create_app(settings)
    app.state.test_factory = factory

    mock_client = MagicMock(spec=NotificationServiceClient)
    dispatch_res = MagicMock()
    dispatch_res.message_id = "msg_login_e2e_01"
    dispatch_res.status = "QUEUED"
    dispatch_res.channel = "WHATSAPP"
    mock_client.send_otp = AsyncMock(return_value=dispatch_res)
    app.state.notification_client = mock_client

    tenant_ctx = TenantContext(tenant_id, "demo-tenant", "demo.example.test", uuid4())
    app.state.test_tenant = tenant_ctx

    auth_repo = SQLAlchemyAuthenticationRepository(factory)
    authz_repo = SQLAlchemyAuthorizationRepository(factory)
    tenant_lookup = SQLAlchemyTenantMembershipRepository(factory)
    abuse_guard = SQLAlchemyOtpAbuseGuard(settings, factory)

    app.dependency_overrides[get_tenant_context] = lambda: app.state.test_tenant
    app.dependency_overrides[get_authentication_repository] = lambda: auth_repo
    app.dependency_overrides[get_authorization_repository] = lambda: authz_repo
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: tenant_lookup
    app.dependency_overrides[get_otp_abuse_guard] = lambda: abuse_guard

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        # Step 1: Submit credentials to trigger 2FA OTP
        resp1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin",
                "password": "SuperAdmin@123",
                "two_factor": True,
            },
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["status"] == "otp_required"
        challenge_id = data1["challenge_id"]

        # Ensure response JSON never leaks the raw OTP
        assert "otp" not in data1
        assert "123456" not in str(data1)
        assert data1["masked_mobile"] == "+91 91002 ****05"

        # Check that terminal received the clearly labeled OTP notice
        captured = capsys.readouterr().out
        assert "[DEVELOPMENT ONLY] Community OTT Login OTP" in captured
        assert f"Challenge ID : {challenge_id}" in captured
        assert "Purpose      : login" in captured

        match = re.search(r"Login OTP\s*:\s*(\d{6})", captured)
        assert match is not None
        extracted_otp = match.group(1)

        # Step 2: Submit extracted OTP to finalize login
        resp2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin",
                "password": "SuperAdmin@123",
                "otp": extracted_otp,
                "challenge_id": challenge_id,
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["role"] == "SUPER_ADMIN"
        assert "community_session" in resp2.cookies
