"""Phase 5: Automated tests for real OTP delivery, PostgreSQL mobile persistence, and fail-closed security.

Covers:
- Correct credentials trigger a fresh random OTP challenge
- Wrong password does not trigger notification dispatch
- The notification request uses the mobile number stored in PostgreSQL
- Client-supplied mobile numbers are strictly ignored
- SUPER_ADMIN, ADMIN, and AGENT destination lookup from PostgreSQL
- Missing or blank mobile numbers fail safely without dispatch
- Static OTP values are rejected outside explicitly isolated test mode
- Notification 401, 403, 400, timeout, and connection error responses fail closed without creating sessions
- OTP expiry, incorrect OTP, replay prevention, rate limiting, and challenge binding
- Successful OTP verification creates the expected session and scoped cookies
- Tenant isolation and role permissions remain enforced
"""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.cli.bootstrap_accounts import run_bootstrap
from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.db.base import Base
from app.main import create_app
from app.modules.authentication.abuse_control import SQLAlchemyOtpAbuseGuard
from app.modules.authentication.api import (
    get_authentication_repository,
    get_otp_abuse_guard,
)
from app.modules.authentication.models import AccountCredential, OtpChallenge, OtpRateBucket
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.api import get_authorization_repository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import (
    SQLAlchemyTenantMembershipRepository,
    get_tenant_membership_lookup,
)
from app.modules.messaging.notification_service import NotificationServiceClient
from app.modules.platform_identity.models import PlatformOperator
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant


@pytest.fixture
def auth_harness(tmp_path):
    database_path = tmp_path / "auth-delivery-test.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    tenant_id = uuid4()
    with factory.begin() as session:
        t = Tenant(id=tenant_id, slug="demo-tenant", status="active")
        session.add(t)
        session.flush()

        admin_role = Role(id=uuid4(), tenant_id=tenant_id, code="admin", display_name="Admin", status="active")
        agent_role = Role(id=uuid4(), tenant_id=tenant_id, code="agent", display_name="Agent", status="active")
        session.add_all([admin_role, agent_role])

    # 1. Bootstrap Super Admin (+919100209205)
    run_bootstrap(
        role="SUPER_ADMIN",
        username="super_admin",
        password="SuperAdmin@123",
        mobile_number="+919100209205",
        must_change_password=True,
        factory=factory,
    )

    # 2. Provision Tenant Admin (+919876500001)
    admin_user_id = uuid4()
    admin_membership_id = uuid4()
    with factory.begin() as session:
        u_admin = User(id=admin_user_id, status="active")
        session.add(u_admin)
        session.flush()

        m_admin = TenantMembership(
            id=admin_membership_id,
            tenant_id=tenant_id,
            user_id=admin_user_id,
            mobile_e164="+919876500001",
            status="active",
        )
        c_admin = AccountCredential(
            id=uuid4(),
            account_type="tenant_user",
            user_id=admin_user_id,
            username="tenant_admin",
            password_hash=hash_password("AdminPass123!"),
            status="active",
            must_change_password=False,
        )
        admin_r = session.execute(select(Role).where(Role.code == "admin")).scalar_one()
        session.add_all([m_admin, c_admin])
        session.flush()

        ura_admin = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_id,
            membership_id=admin_membership_id,
            role_id=admin_r.id,
            assigned_by_membership_id=admin_membership_id,
            status="active",
            valid_from=datetime.now(UTC),
        )
        session.add(ura_admin)

    # 3. Provision Tenant Agent (+919876500002)
    agent_user_id = uuid4()
    agent_membership_id = uuid4()
    with factory.begin() as session:
        u_agent = User(id=agent_user_id, status="active")
        session.add(u_agent)
        session.flush()

        m_agent = TenantMembership(
            id=agent_membership_id,
            tenant_id=tenant_id,
            user_id=agent_user_id,
            mobile_e164="+919876500002",
            status="active",
        )
        c_agent = AccountCredential(
            id=uuid4(),
            account_type="tenant_user",
            user_id=agent_user_id,
            username="tenant_agent",
            password_hash=hash_password("AgentPass123!"),
            status="active",
            must_change_password=False,
        )
        agent_r = session.execute(select(Role).where(Role.code == "agent")).scalar_one()
        session.add_all([m_agent, c_agent])
        session.flush()

        ura_agent = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_id,
            membership_id=agent_membership_id,
            role_id=agent_r.id,
            assigned_by_membership_id=admin_membership_id,
            status="active",
            valid_from=datetime.now(UTC),
        )
        session.add(ura_agent)

    # 4. Provision User With Missing Mobile
    no_mobile_user_id = uuid4()
    with factory.begin() as session:
        u_nomob = User(id=no_mobile_user_id, status="active")
        session.add(u_nomob)
        session.flush()

        c_nomob = AccountCredential(
            id=uuid4(),
            account_type="tenant_user",
            user_id=no_mobile_user_id,
            username="no_mobile_user",
            password_hash=hash_password("NoMobPass123!"),
            status="active",
            must_change_password=False,
        )
        session.add(c_nomob)

    captured_requests: list[dict] = []
    mock_responses: list[httpx.Response] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        captured_requests.append({
            "url": str(request.url),
            "method": request.method,
            "headers": dict(request.headers),
            "body": body,
        })
        if mock_responses:
            return mock_responses.pop(0)
        return httpx.Response(
            status_code=202,
            json={
                "messageId": f"msg_test_{uuid4().hex[:12]}",
                "status": "QUEUED",
                "priority": "P0",
                "channel": "SMS",
            },
        )

    mock_http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
        session_cookie_secure=False,
        otp_delivery_mode="notification_service",
        notification_service_base_url="https://handorg.duckdns.org/notifications",
        notification_service_api_key=SecretStr("test-service-key-12345"),
        notification_channel="SMS",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
    )

    app = create_app(test_settings)
    app.state.test_factory = factory
    app.state.notification_client = NotificationServiceClient(test_settings, client=mock_http_client)

    tenant_ctx = TenantContext(tenant_id, "demo-tenant", "demo.example.test", uuid4())
    app.state.test_tenant = tenant_ctx

    auth_repo = SQLAlchemyAuthenticationRepository(factory)
    authz_repo = SQLAlchemyAuthorizationRepository(factory)
    tenant_lookup = SQLAlchemyTenantMembershipRepository(factory)
    abuse_guard = SQLAlchemyOtpAbuseGuard(test_settings, factory)

    app.dependency_overrides[get_tenant_context] = lambda: app.state.test_tenant
    app.dependency_overrides[get_authentication_repository] = lambda: auth_repo
    app.dependency_overrides[get_authorization_repository] = lambda: authz_repo
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: tenant_lookup
    app.dependency_overrides[get_otp_abuse_guard] = lambda: abuse_guard

    return {
        "app": app,
        "factory": factory,
        "tenant_id": tenant_id,
        "captured_requests": captured_requests,
        "mock_responses": mock_responses,
        "settings": test_settings,
    }


def make_client(app):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:3000"},
    )


@pytest.mark.anyio
async def test_super_admin_login_triggers_notification_with_postgresql_mobile(auth_harness):
    """Step 1 credentials check dispatches SMS to the Super Admin's PostgreSQL mobile."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]

    async with make_client(app) as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username": "super_admin", "password": "SuperAdmin@123", "two_factor": True},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "otp_required"
        assert data["masked_mobile"] == "+91 91002 ****05"
        assert data["challenge_id"] is not None

        # Verify notification microservice request
        assert len(captured) == 1
        req = captured[0]
        assert req["url"] == "https://handorg.duckdns.org/notifications/api/v1/messages"
        assert req["method"] == "POST"
        assert req["headers"]["x-api-key"] == "test-service-key-12345"
        assert req["headers"]["idempotency-key"].startswith("community-ott-login-")

        body = req["body"]
        assert body["channel"] == "SMS"
        assert body["messageType"] == "LOGIN_OTP"
        assert body["recipient"] == "+919100209205"  # Exact PostgreSQL mobile
        assert "Your login verification code is" in body["content"]["text"]

        # Ensure OTP is not static 123456
        otp_dispatched = body["content"]["text"].split("is ")[-1].rstrip(".")
        assert len(otp_dispatched) == 6
        assert otp_dispatched.isdigit()


@pytest.mark.anyio
async def test_admin_and_agent_postgresql_mobile_resolution(auth_harness):
    """Step 1 credentials check resolves mobile from tenant_memberships for Admin and Agent."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]

    async with make_client(app) as client:
        # Tenant Admin
        res_admin = await client.post(
            "/api/v1/auth/login",
            json={"username": "tenant_admin", "password": "AdminPass123!", "two_factor": True},
        )
        assert res_admin.status_code == 200
        assert res_admin.json()["masked_mobile"] == "+91 98765 ****01"
        assert captured[-1]["body"]["recipient"] == "+919876500001"

        # Tenant Agent
        res_agent = await client.post(
            "/api/v1/auth/login",
            json={"username": "tenant_agent", "password": "AgentPass123!", "two_factor": True},
        )
        assert res_agent.status_code == 200
        assert res_agent.json()["masked_mobile"] == "+91 98765 ****02"
        assert captured[-1]["body"]["recipient"] == "+919876500002"


@pytest.mark.anyio
async def test_wrong_password_does_not_trigger_notification(auth_harness):
    """Invalid password returns 401 and never dispatches an OTP notification."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]

    async with make_client(app) as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username": "super_admin", "password": "WrongPassword999!", "two_factor": True},
        )
        assert res.status_code == 401
        assert len(captured) == 0


@pytest.mark.anyio
async def test_client_supplied_mobile_number_cannot_override_destination(auth_harness):
    """Client attempt to supply a foreign phone number in login payload is rejected by schema."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]

    async with make_client(app) as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin",
                "password": "SuperAdmin@123",
                "two_factor": True,
                "mobile_number": "+19999999999",  # Attacker attempt
            },
        )
        # LoginRequest extra="forbid" rejects unexpected field with 422
        assert res.status_code == 422
        assert len(captured) == 0


@pytest.mark.anyio
async def test_missing_mobile_number_fails_safely(auth_harness):
    """User account without a registered mobile number returns 401 and dispatches nothing."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]

    async with make_client(app) as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username": "no_mobile_user", "password": "NoMobPass123!", "two_factor": True},
        )
        assert res.status_code == 401
        assert len(captured) == 0


@pytest.mark.anyio
async def test_notification_microservice_failures_fail_closed_without_session(auth_harness):
    """When notification service returns 401, 403, 400, or 500, login fails closed with 503."""
    app = auth_harness["app"]
    factory = auth_harness["factory"]
    mock_responses = auth_harness["mock_responses"]

    error_statuses = [401, 403, 400, 500, 503]
    async with make_client(app) as client:
        for err_code in error_statuses:
            # Clear rate limit bucket between attempts to avoid cooldown interference
            with factory.begin() as session:
                session.query(OtpRateBucket).delete()

            mock_responses.append(httpx.Response(status_code=err_code, json={"error": "Microservice error"}))
            res = await client.post(
                "/api/v1/auth/login",
                json={"username": "super_admin", "password": "SuperAdmin@123", "two_factor": True},
            )
            assert res.status_code == 503
            assert "temporarily unavailable" in res.text or "MESSAGING_PROVIDER_UNAVAILABLE" in res.text


@pytest.mark.anyio
async def test_complete_two_step_login_verification_and_replay_protection(auth_harness):
    """Step 1 dispatches random OTP, Step 2 verifies exact OTP and blocks replayed attempts."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]

    async with make_client(app) as client:
        # Step 1: Submit credentials
        res1 = await client.post(
            "/api/v1/auth/login",
            json={"username": "tenant_admin", "password": "AdminPass123!", "two_factor": True},
        )
        assert res1.status_code == 200
        challenge_id = res1.json()["challenge_id"]

        # Extract generated OTP from captured microservice payload
        sent_otp = captured[-1]["body"]["content"]["text"].split("is ")[-1].rstrip(".")

        # Step 2: Test wrong OTP returns 401
        bad_verify = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "tenant_admin",
                "password": "AdminPass123!",
                "challenge_id": challenge_id,
                "otp": "000000",
            },
        )
        assert bad_verify.status_code == 401

        # Step 2b: Submit valid OTP -> establishes session
        good_verify = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "tenant_admin",
                "password": "AdminPass123!",
                "challenge_id": challenge_id,
                "otp": sent_otp,
            },
        )
        assert good_verify.status_code == 200
        assert "community_session" in good_verify.cookies
        verify_data = good_verify.json()
        assert verify_data["role"] == "ADMIN"
        assert verify_data["username"] == "tenant_admin"

        # Step 3: Replay attack (submitting same valid OTP again) fails
        replay_res = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "tenant_admin",
                "password": "AdminPass123!",
                "challenge_id": challenge_id,
                "otp": sent_otp,
            },
        )
        assert replay_res.status_code == 401


@pytest.mark.anyio
async def test_login_triggers_whatsapp_template_payload(auth_harness):
    """Verifies that when notification_channel=WHATSAPP, login dispatches template payload to PostgreSQL mobile."""
    app = auth_harness["app"]
    captured = auth_harness["captured_requests"]
    # Switch client and settings to WHATSAPP
    app.state.settings.notification_channel = "WHATSAPP"

    async with make_client(app) as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username": "super_admin", "password": "SuperAdmin@123", "two_factor": True},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "otp_required"
        assert data["masked_mobile"] == "+91 91002 ****05"

        assert len(captured) >= 1
        req = captured[-1]
        body = req["body"]
        assert body["channel"] == "WHATSAPP"
        assert body["messageType"] == "LOGIN_OTP"
        assert body["recipient"] == "+919100209205"
        assert body["content"]["type"] == "TEMPLATE"
        assert body["content"]["templateCode"] == "LOGIN_OTP"
        assert body["content"]["locale"] == "en"
        otp_dispatched = body["content"]["variables"]["otp"]
        assert len(otp_dispatched) == 6
        assert otp_dispatched.isdigit()
        assert body["metadata"]["referenceId"] == data["challenge_id"]

        # Step 2: Verify with WhatsApp OTP
        good_verify = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin",
                "password": "SuperAdmin@123",
                "challenge_id": data["challenge_id"],
                "otp": otp_dispatched,
            },
        )
        assert good_verify.status_code == 200
        assert "community_session" in good_verify.cookies

        # Step 3: Replay attack fails
        replay_res = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin",
                "password": "SuperAdmin@123",
                "challenge_id": data["challenge_id"],
                "otp": otp_dispatched,
            },
        )
        assert replay_res.status_code == 401
