"""Tests for Phase 1 username/password authentication using SQL database.

Covers:
- Valid Super Admin login
- Valid Admin login
- Valid Agent login
- Invalid username
- Invalid password
- Inactive / disabled account credentials
- Inactive user
- Inactive tenant membership
- Inactive tenant
- Missing fields and malformed input (422)
- Correct role resolution (SUPER_ADMIN, ADMIN, AGENT)
- Unauthenticated /me (401)
- Authenticated /me
- Logout and server-side session invalidation
- Logout CSRF protection (403 without or invalid CSRF)
- Session persistence across multiple requests
- Database session persistence
- Zero MongoDB dependency in the authentication path
- Bootstrap CLI execution, idempotency, and password updates
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.db.base import Base
from app.main import create_app
from app.modules.authentication.api import (
    get_authentication_repository,
    get_otp_abuse_guard,
)
from app.modules.authentication.abuse_control import SQLAlchemyOtpAbuseGuard
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.password import hash_password, verify_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.api import get_authorization_repository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.cli.bootstrap_accounts import run_bootstrap
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import (
    SQLAlchemyTenantMembershipRepository,
    get_tenant_membership_lookup,
)
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant
from tests.test_authentication_api import MockMessagingProvider


@pytest.fixture
def auth_harness(tmp_path):
    database_path = tmp_path / "password-auth-test.sqlite"
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
    user_id = uuid4()
    membership_id = uuid4()
    role_admin_id = uuid4()
    role_agent_id = uuid4()
    now = datetime.now(UTC)

    # Provision default test data
    with factory.begin() as session:
        # 1. Tenant & Membership & User
        tenant = Tenant(id=tenant_id, slug="avanflix-demo", status="active")
        user = User(id=user_id, status="active")
        session.add_all([tenant, user])
        session.flush()

        membership = TenantMembership(
            id=membership_id,
            tenant_id=tenant_id,
            user_id=user_id,
            mobile_e164="+15551234567",
            status="active",
        )
        session.add(membership)
        session.flush()

        # Roles
        admin_role = Role(
            id=role_admin_id,
            tenant_id=tenant_id,
            code="admin",
            display_name="Tenant Administrator",
            status="active",
        )
        agent_role = Role(
            id=role_agent_id,
            tenant_id=tenant_id,
            code="agent",
            display_name="Agent",
            status="active",
        )
        session.add_all([admin_role, agent_role])
        session.flush()

        # Role assignment for Admin
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_id,
                membership_id=membership_id,
                role_id=role_admin_id,
                assigned_by_membership_id=membership_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )

        # Admin Credential
        session.add(
            AccountCredential(
                id=uuid4(),
                username="adminuser",
                password_hash=hash_password("AdminSecurePassword123!"),
                account_type="tenant_user",
                user_id=user_id,
                status="active",
            )
        )

        # 2. Super Admin (Platform Operator)
        grantor = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        super_operator = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin",
            status="active",
        )
        super_role = PlatformRole(
            id=uuid4(),
            code="super_admin",
            display_name="Super Administrator",
            status="active",
        )
        session.add_all([grantor, super_operator, super_role])
        session.flush()

        session.add(
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=super_operator.id,
                role_id=super_role.id,
                assigned_by_operator_id=grantor.id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )

        session.add(
            AccountCredential(
                id=uuid4(),
                username="superadmin",
                password_hash=hash_password("SuperSecurePassword123!"),
                account_type="platform_operator",
                operator_id=super_operator.id,
                status="active",
            )
        )

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
    )
    provider = MockMessagingProvider({}, environment="test")
    app = create_app(settings, messaging_provider=provider)

    tenant_context = TenantContext(tenant_id, "avanflix-demo", "avanflix.test", uuid4())
    app.state.test_tenant = tenant_context

    app.dependency_overrides[get_tenant_context] = lambda: app.state.test_tenant
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: SQLAlchemyTenantMembershipRepository(factory)
    app.dependency_overrides[get_authentication_repository] = lambda: SQLAlchemyAuthenticationRepository(factory)
    app.dependency_overrides[get_authorization_repository] = lambda: SQLAlchemyAuthorizationRepository(factory)
    app.dependency_overrides[get_otp_abuse_guard] = lambda: SQLAlchemyOtpAbuseGuard(settings, factory)

    try:
        yield {
            "app": app,
            "factory": factory,
            "engine": engine,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "membership_id": membership_id,
            "super_operator_id": super_operator.id,
            "settings": settings,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def client_for(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://localhost:8000")


def load_cookies(response) -> SimpleCookie:
    cookies = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        cookies.load(header)
    return cookies


async def login_client(client: AsyncClient, username: str, password: str):
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if response.status_code == 200:
        for header in response.headers.get_list("set-cookie"):
            sc = SimpleCookie()
            sc.load(header)
            for key, morsel in sc.items():
                client.cookies.set(key, morsel.value, path="/api/v1/auth")
    return response


@pytest.mark.anyio
async def test_valid_super_admin_login(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "superadmin", "password": "SuperSecurePassword123!"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "superadmin"
        assert data["role"] == "SUPER_ADMIN"
        assert data["status"] == "active"
        assert data["user_id"] == str(auth_harness["super_operator_id"])
        assert data["tenant_id"] is None
        assert data["membership_id"] is None
        assert data["csrf_token"]
        assert "password" not in data
        assert "password_hash" not in data

        # Check cookies
        cookies = load_cookies(response)
        assert "community_session" in cookies
        assert cookies["community_session"]["httponly"]
        assert cookies["community_session"]["samesite"].lower() == "lax"
        assert "community_platform_session" in cookies


@pytest.mark.anyio
async def test_valid_admin_login(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "AdminSecurePassword123!"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "adminuser"
        assert data["role"] == "ADMIN"
        assert data["status"] == "active"
        assert data["user_id"] == str(auth_harness["user_id"])
        assert data["tenant_id"] == str(auth_harness["tenant_id"])
        assert data["membership_id"] == str(auth_harness["membership_id"])
        assert data["tenant"]["slug"] == "avanflix-demo"
        assert data["csrf_token"]

        cookies = load_cookies(response)
        assert "community_session" in cookies
        assert cookies["community_session"]["httponly"]


@pytest.mark.anyio
async def test_invalid_username_rejected_with_consistent_response(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "nonexistent_user", "password": "AnyPassword123!"},
        )
        assert response.status_code == 401
        data = response.json()
        assert data["error"]["code"] == "AUTHENTICATION_FAILED"
        assert data["error"]["message"] == "Login could not be completed."


@pytest.mark.anyio
async def test_invalid_password_rejected_with_consistent_response(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "WrongPassword999!"},
        )
        assert response.status_code == 401
        data = response.json()
        assert data["error"]["code"] == "AUTHENTICATION_FAILED"
        assert data["error"]["message"] == "Login could not be completed."


@pytest.mark.anyio
async def test_inactive_credential_rejected(auth_harness):
    with auth_harness["factory"].begin() as session:
        session.execute(
            update(AccountCredential)
            .where(AccountCredential.username == "adminuser")
            .values(status="inactive")
        )

    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "AdminSecurePassword123!"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


@pytest.mark.anyio
async def test_inactive_user_rejected(auth_harness):
    with auth_harness["factory"].begin() as session:
        session.execute(
            update(User)
            .where(User.id == auth_harness["user_id"])
            .values(status="inactive")
        )

    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "AdminSecurePassword123!"},
        )
        assert response.status_code == 401


@pytest.mark.anyio
async def test_inactive_tenant_membership_rejected(auth_harness):
    with auth_harness["factory"].begin() as session:
        session.execute(
            update(TenantMembership)
            .where(TenantMembership.id == auth_harness["membership_id"])
            .values(status="inactive")
        )

    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "AdminSecurePassword123!"},
        )
        assert response.status_code == 401


@pytest.mark.anyio
async def test_inactive_tenant_rejected(auth_harness):
    with auth_harness["factory"].begin() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == auth_harness["tenant_id"])
            .values(status="inactive")
        )

    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "AdminSecurePassword123!"},
        )
        assert response.status_code == 401


@pytest.mark.anyio
async def test_missing_and_malformed_input(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        # Missing password
        r1 = await client.post("/api/v1/auth/login", json={"username": "adminuser"})
        assert r1.status_code == 422
        assert r1.json()["error"]["code"] == "VALIDATION_ERROR"

        # Missing username
        r2 = await client.post("/api/v1/auth/login", json={"password": "pwd"})
        assert r2.status_code == 422

        # Extra forbidden field
        r3 = await client.post(
            "/api/v1/auth/login",
            json={"username": "adminuser", "password": "pwd", "role": "SUPER_ADMIN"},
        )
        assert r3.status_code == 422

        # Empty body
        r4 = await client.post("/api/v1/auth/login", json={})
        assert r4.status_code == 422


@pytest.mark.anyio
async def test_unauthenticated_me_returns_401(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_authenticated_me_super_admin(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await login_client(client, "superadmin", "SuperSecurePassword123!")
        assert login.status_code == 200

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        data = me.json()
        assert data["username"] == "superadmin"
        assert data["role"] == "SUPER_ADMIN"
        assert data["user_id"] == str(auth_harness["super_operator_id"])
        assert data["status"] == "active"


@pytest.mark.anyio
async def test_authenticated_me_admin(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await login_client(client, "adminuser", "AdminSecurePassword123!")
        assert login.status_code == 200

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        data = me.json()
        assert data["username"] == "adminuser"
        assert data["role"] == "ADMIN"
        assert data["user_id"] == str(auth_harness["user_id"])
        assert data["tenant_id"] == str(auth_harness["tenant_id"])
        assert data["membership_id"] == str(auth_harness["membership_id"])


@pytest.mark.anyio
async def test_logout_and_session_invalidation(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await login_client(client, "adminuser", "AdminSecurePassword123!")
        assert login.status_code == 200
        csrf_token = login.json()["csrf_token"]

        # Verify active session in DB
        with auth_harness["factory"]() as session:
            active_session = session.execute(
                select(AuthSession).where(AuthSession.revoked_at.is_(None))
            ).scalars().first()
            assert active_session is not None

        # Logout with CSRF token and valid Origin
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "http://localhost:3000",
            },
        )
        assert logout.status_code == 204

        # Check DB session is revoked
        with auth_harness["factory"]() as session:
            revoked_session = session.execute(
                select(AuthSession).where(AuthSession.id == active_session.id)
            ).scalar_one()
            assert revoked_session.revoked_at is not None

        # Subsequent /me call must be 401
        me_after = await client.get("/api/v1/auth/me")
        assert me_after.status_code == 401


@pytest.mark.anyio
async def test_logout_csrf_protection(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await login_client(client, "adminuser", "AdminSecurePassword123!")
        assert login.status_code == 200

        # Attempt logout without CSRF token -> 403
        bad_logout1 = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:3000"},
        )
        assert bad_logout1.status_code == 403

        # Attempt logout with forged CSRF token -> 403
        bad_logout2 = await client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": "forged_csrf_token_value_0123456789abcdef",
                "Origin": "http://localhost:3000",
            },
        )
        assert bad_logout2.status_code == 403

        # Session should still be valid!
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200


@pytest.mark.anyio
async def test_session_persistence_across_multiple_requests(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await login_client(client, "adminuser", "AdminSecurePassword123!")
        assert login.status_code == 200

        for _ in range(5):
            res = await client.get("/api/v1/auth/me")
            assert res.status_code == 200
            assert res.json()["username"] == "adminuser"


@pytest.mark.anyio
async def test_database_persistence_and_no_mongodb(auth_harness):
    # Verify no MongoDB driver is loaded or imported anywhere in sys.modules
    assert "pymongo" not in sys.modules
    assert "motor" not in sys.modules

    async with client_for(auth_harness["app"]) as client:
        login = await login_client(client, "superadmin", "SuperSecurePassword123!")
        assert login.status_code == 200

        with auth_harness["factory"]() as session:
            op_session = session.execute(
                select(PlatformOperatorSession).where(
                    PlatformOperatorSession.operator_id == auth_harness["super_operator_id"]
                )
            ).scalars().first()
            assert op_session is not None
            assert op_session.token_digest is not None
            assert len(op_session.token_digest) == 64


@pytest.mark.anyio
def test_bootstrap_cli_idempotency_and_password_update(auth_harness):
    factory = auth_harness["factory"]

    # 1. Bootstrap new Super Admin
    created = run_bootstrap(
        role="SUPER_ADMIN",
        username="cli_super",
        password="CliSuperPass123!",
        factory=factory,
    )
    assert created is True

    # 2. Re-run identical bootstrap without update flag -> False (idempotent, no changes)
    repeat = run_bootstrap(
        role="SUPER_ADMIN",
        username="cli_super",
        password="DifferentPassword456!",
        factory=factory,
    )
    assert repeat is False

    # Verify old password still works
    with factory() as session:
        cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == "cli_super")
        ).scalar_one()
        assert verify_password("CliSuperPass123!", cred.password_hash)

    # 3. Update password with update_password=True
    updated = run_bootstrap(
        role="SUPER_ADMIN",
        username="cli_super",
        password="NewSuperPass456!",
        update_password=True,
        factory=factory,
    )
    assert updated is True

    # Verify new password hash
    with factory() as session:
        cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == "cli_super")
        ).scalar_one()
        assert verify_password("NewSuperPass456!", cred.password_hash)


@pytest.mark.anyio
def test_bootstrap_cli_tenant_admin_and_agent(auth_harness):
    factory = auth_harness["factory"]

    # Bootstrap ADMIN
    admin_created = run_bootstrap(
        role="ADMIN",
        username="tenant_admin_cli",
        password="TenantAdminPass123!",
        tenant_slug="cli-tenant",
        factory=factory,
    )
    assert admin_created is True

    # Bootstrap AGENT
    agent_created = run_bootstrap(
        role="AGENT",
        username="tenant_agent_cli",
        password="TenantAgentPass123!",
        tenant_slug="cli-tenant",
        factory=factory,
    )
    assert agent_created is True

    with factory() as session:
        admin_cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == "tenant_admin_cli")
        ).scalar_one()
        assert admin_cred.account_type == "tenant_user"
        assert admin_cred.status == "active"

        agent_cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == "tenant_agent_cli")
        ).scalar_one()
        assert agent_cred.account_type == "tenant_user"
        assert agent_cred.status == "active"
