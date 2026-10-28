"""Tests for development-only OTP skip notification dispatch setting.

Verifies:
1. Setting is disabled by default.
2. Setting is allowed and functional in development environment only.
3. Setting is strictly rejected in production, staging, and other non-development environments.
4. When enabled in development:
   - Outbound HTTP requests to the Notification Microservice are skipped entirely.
   - Fresh random OTP is generated and challenge is persisted in PostgreSQL/DB with expiry.
   - Terminal stdout prints the generated OTP notice with skipped dispatch status.
   - Clearly logs 'DEVELOPMENT ONLY: notification dispatch skipped' without leaking secrets.
   - Challenge metadata reflects 'SKIPPED_LOCAL' without claiming delivery.
5. When disabled, normal notification dispatch occurs.
6. Rate limiting, replay protection, and verification remain strictly enforced.
7. Full FastAPI login flow end-to-end completes successfully without calling notification client.
"""
from __future__ import annotations

import logging
import re
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.otp_service import (
    OtpRateLimitExceeded,
    OtpService,
    OtpVerificationFailed,
)
from app.modules.messaging.notification_service import NotificationServiceClient
from app.modules.tenants.models import Tenant


@pytest.fixture
def sqlite_factory(tmp_path):
    db_file = tmp_path / "test_otp_skip.sqlite"
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
    dispatch_res.message_id = "msg_test_dispatch_001"
    dispatch_res.status = "QUEUED"
    dispatch_res.channel = "WHATSAPP"
    client.send_otp = AsyncMock(return_value=dispatch_res)
    client.send_otp_sms = AsyncMock(return_value=dispatch_res)
    return client


# ---------------------------------------------------------------------------
# Requirement 1, 2: Setting Defaults and Environment Constraints
# ---------------------------------------------------------------------------

def test_otp_skip_notification_dispatch_disabled_by_default():
    settings = Settings(_env_file=None, environment="development")
    assert settings.otp_skip_notification_dispatch is False
    assert settings.can_skip_notification_dispatch is False


def test_otp_skip_notification_dispatch_allowed_in_development_when_explicitly_enabled():
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_skip_notification_dispatch=True,
    )
    assert settings.otp_skip_notification_dispatch is True
    assert settings.can_skip_notification_dispatch is True


def test_otp_skip_notification_dispatch_rejected_in_production():
    with pytest.raises(ValidationError, match="OTP_SKIP_NOTIFICATION_DISPATCH is not permitted in production or staging"):
        Settings(
            _env_file=None,
            environment="production",
            otp_skip_notification_dispatch=True,
            database_url="postgresql+psycopg://app:sec123@localhost:5432/community_ott_prod",
            frontend_origins="https://portal.example.com",
        )


def test_otp_skip_notification_dispatch_rejected_in_staging():
    with pytest.raises(ValidationError, match="OTP_SKIP_NOTIFICATION_DISPATCH is not permitted in production or staging"):
        Settings(
            _env_file=None,
            environment="staging",
            otp_skip_notification_dispatch=True,
            database_url="postgresql+psycopg://app:sec123@localhost:5432/community_ott_staging",
            frontend_origins="https://portal.example.com",
        )


def test_otp_skip_notification_dispatch_rejected_in_test_environment():
    with pytest.raises(ValidationError, match="OTP_SKIP_NOTIFICATION_DISPATCH is allowed only in development"):
        Settings(
            _env_file=None,
            environment="test",
            otp_skip_notification_dispatch=True,
        )


def test_can_skip_notification_dispatch_defense_in_depth():
    settings = Settings(_env_file=None, environment="development")
    assert settings.can_skip_notification_dispatch is False

    object.__setattr__(settings, "environment", "production")
    object.__setattr__(settings, "otp_skip_notification_dispatch", True)
    assert settings.can_skip_notification_dispatch is False


# ---------------------------------------------------------------------------
# Requirement 3, 4, 5: Dispatch Bypass, Logging, Persistence, and Integrity
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_skip_dispatch_enabled_skips_http_request_and_logs(sqlite_factory, mock_notif_client, capsys, caplog):
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_skip_notification_dispatch=True,
    )
    otp_svc = OtpService(
        settings=settings,
        notification_client=mock_notif_client,
        session_factory=sqlite_factory,
    )

    mobile = "+919100209205"
    with caplog.at_level(logging.INFO):
        challenge_id, cooldown = await otp_svc.request_otp(
            mobile_number=mobile,
            purpose="login",
        )

    # 1. Assert outbound HTTP request was NOT made
    assert not mock_notif_client.send_otp.called
    assert not mock_notif_client.send_otp_sms.called

    # 2. Assert log contains the explicit development notice without secrets
    assert any("DEVELOPMENT ONLY: notification dispatch skipped" in record.message for record in caplog.records)

    # 3. Assert terminal output displays the OTP notice and skipped status
    captured = capsys.readouterr().out
    assert "[DEVELOPMENT ONLY] Community OTT Login OTP" in captured
    assert f"Challenge ID : {challenge_id}" in captured
    assert "Dispatch     : SKIPPED (development mode)" in captured
    match = re.search(r"Login OTP\s*:\s*(\d{6})", captured)
    assert match is not None, f"Expected 6-digit OTP in output:\n{captured}"
    logged_otp = match.group(1)

    # 4. Assert challenge is persisted in the database with SKIPPED_LOCAL status
    with sqlite_factory() as session:
        challenge = session.execute(
            select(OtpChallenge).where(OtpChallenge.id == challenge_id)
        ).scalar_one_or_none()
        assert challenge is not None
        assert challenge.mobile_e164 == mobile
        assert challenge.purpose == "login"
        assert challenge.metadata_json.get("notification_status") == "SKIPPED_LOCAL"

    # 5. Assert the logged OTP verifies correctly
    verified, v_token = otp_svc.verify_otp(
        challenge_id=challenge_id,
        mobile_number=mobile,
        otp=logged_otp,
        purpose="login",
    )
    assert verified is True


@pytest.mark.anyio
async def test_normal_dispatch_when_switch_disabled(sqlite_factory, mock_notif_client, capsys):
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_skip_notification_dispatch=False,
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

    # Assert outbound notification client was invoked normally
    assert mock_notif_client.send_otp.called
    dispatch_call = mock_notif_client.send_otp.call_args
    assert dispatch_call.kwargs["challenge_id"] == str(challenge_id)
    assert dispatch_call.kwargs["recipient_e164"] == mobile


@pytest.mark.anyio
async def test_otp_rate_limiting_and_replay_protection_enforced(sqlite_factory, mock_notif_client, capsys):
    tenant_id = uuid4()
    with sqlite_factory.begin() as session:
        session.add(Tenant(id=tenant_id, slug="test-rate", status="active"))

    settings = Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
        otp_skip_notification_dispatch=True,
    )
    otp_svc = OtpService(
        settings=settings,
        notification_client=mock_notif_client,
        session_factory=sqlite_factory,
    )

    mobile = "+919100209205"
    challenge_id, _ = await otp_svc.request_otp(
        mobile_number=mobile,
        purpose="login",
        tenant_id=tenant_id,
    )
    captured = capsys.readouterr().out
    logged_otp = re.search(r"Login OTP\s*:\s*(\d{6})", captured).group(1)

    # Cooldown rate limit must still be enforced
    with pytest.raises(OtpRateLimitExceeded):
        await otp_svc.request_otp(
            mobile_number=mobile,
            purpose="login",
            tenant_id=tenant_id,
        )

    # Replay protection: first verify succeeds
    verified, _ = otp_svc.verify_otp(
        challenge_id=challenge_id,
        mobile_number=mobile,
        otp=logged_otp,
        purpose="login",
    )
    assert verified is True

    # Second verify with same OTP raises OtpVerificationFailed (challenge is already consumed)
    with pytest.raises(OtpVerificationFailed, match="already been used"):
        otp_svc.verify_otp(
            challenge_id=challenge_id,
            mobile_number=mobile,
            otp=logged_otp,
            purpose="login",
        )


@pytest.mark.anyio
async def test_fastapi_login_endpoint_with_skip_notification_dispatch(tmp_path, capsys):
    """End-to-end FastAPI login with OTP_SKIP_NOTIFICATION_DISPATCH=true:

    1. POST /api/v1/auth/login triggers challenge generation.
    2. HTTP response returns challenge_id without leaking plain OTP.
    3. No outbound request is made to the Notification Microservice.
    4. Terminal stdout receives the formatted notice with the generated OTP.
    5. The OTP completes second-factor login and establishes session.
    """
    from httpx import ASGITransport, AsyncClient

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

    db_path = tmp_path / "login_skip_test.sqlite"
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
        t = Tenant(id=tenant_id, slug="demo-skip-tenant", status="active")
        session.add(t)

    # Bootstrap Super Admin
    run_bootstrap(
        role="SUPER_ADMIN",
        username="super_admin_skip",
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
        otp_skip_notification_dispatch=True,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
    )

    app = create_app(settings)
    app.state.test_factory = factory

    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock()
    app.state.notification_client = mock_client

    tenant_ctx = TenantContext(tenant_id, "demo-skip-tenant", "demo.example.test", uuid4())
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
                "username": "super_admin_skip",
                "password": "SuperAdmin@123",
                "two_factor": True,
            },
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["status"] == "otp_required"
        challenge_id = data1["challenge_id"]

        # Ensure outbound Notification Microservice client was NEVER invoked
        assert not mock_client.send_otp.called
        assert not mock_client.send_otp_sms.called

        # Ensure response JSON never leaks the plain OTP
        assert "otp" not in data1
        assert data1["masked_mobile"] == "+91 91002 ****05"

        # Check terminal stdout for notice and OTP
        captured = capsys.readouterr().out
        assert "[DEVELOPMENT ONLY] Community OTT Login OTP" in captured
        assert f"Challenge ID : {challenge_id}" in captured
        assert "Purpose      : login" in captured
        assert "Dispatch     : SKIPPED (development mode)" in captured

        match = re.search(r"Login OTP\s*:\s*(\d{6})", captured)
        assert match is not None
        extracted_otp = match.group(1)

        # Step 2: Submit extracted OTP to finalize login
        resp2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin_skip",
                "password": "SuperAdmin@123",
                "otp": extracted_otp,
                "challenge_id": challenge_id,
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["role"] == "SUPER_ADMIN"
        assert "community_session" in resp2.cookies
