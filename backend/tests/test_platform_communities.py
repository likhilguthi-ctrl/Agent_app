from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession, OtpChallenge, EmailOtpChallenge
from app.modules.authentication.password import hash_password, verify_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import PlatformOperator, PlatformOperatorSession, PlatformRole, PlatformOperatorRoleAssignment
from app.modules.tenants.models import Tenant, TenantDomain


@pytest.fixture
def community_harness(tmp_path):
    database_path = tmp_path / "platform-community-test.sqlite"
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

    now = datetime.now(UTC)
    operator_id = uuid4()
    operator_token = "test-superadmin-token-12345"
    token_digest = __import__("hashlib").sha256(operator_token.encode("utf-8")).hexdigest()
    csrf_token = __import__("hashlib").sha256(b"superadmin-csrf-token").hexdigest()

    tenant_a_id = uuid4()
    admin_user_id = uuid4()
    admin_membership_id = uuid4()
    admin_token = "test-admin-token-12345"
    admin_token_digest = __import__("hashlib").sha256(admin_token.encode("utf-8")).hexdigest()
    admin_csrf = __import__("hashlib").sha256(b"admin-csrf-token").hexdigest()

    with factory.begin() as session:
        assigner_op = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        op = PlatformOperator(
            id=operator_id,
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin",
            status="active",
        )
        p_role = PlatformRole(
            id=uuid4(),
            code="super_admin",
            display_name="Super Administrator",
            status="active",
        )
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="existing-community",
            status="active",
        )
        user = User(
            id=admin_user_id,
            status="active",
        )
        session.add_all([op, assigner_op, p_role, tenant_a, user])
        session.flush()

        assign = PlatformOperatorRoleAssignment(
            id=uuid4(),
            operator_id=op.id,
            role_id=p_role.id,
            assigned_by_operator_id=assigner_op.id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        op_session = PlatformOperatorSession(
            id=uuid4(),
            operator_id=op.id,
            token_digest=token_digest,
            csrf_token=csrf_token,
            authenticated_at=now,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        op_cred = AccountCredential(
            id=uuid4(),
            username="superadmin",
            password_hash=hash_password("SuperSecure123!"),
            account_type="platform_operator",
            operator_id=op.id,
            status="active",
        )
        membership = TenantMembership(
            id=admin_membership_id,
            tenant_id=tenant_a_id,
            user_id=admin_user_id,
            mobile_e164="+15551112222",
            status="active",
        )
        from app.modules.authorization.seeding import seed_tenant_role_permissions
        admin_role, _ = seed_tenant_role_permissions(session, tenant_a_id)
        session.add_all([assign, op_session, op_cred, membership])
        session.flush()

        role_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=admin_membership_id,
            role_id=admin_role.id,
            assigned_by_membership_id=admin_membership_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        admin_session = AuthSession(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=admin_membership_id,
            token_digest=admin_token_digest,
            csrf_token=admin_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        admin_cred = AccountCredential(
            id=uuid4(),
            username="adminuser",
            password_hash=hash_password("AdminSecure123!"),
            account_type="tenant_user",
            user_id=admin_user_id,
            status="active",
        )
        session.add_all([role_assign, admin_session, admin_cred])
        session.flush()

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
    )
    app = create_app(settings)

    def db_override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_authentication_repository] = lambda: SQLAlchemyAuthenticationRepository(factory)
    app.dependency_overrides[get_authorization_repository] = lambda: SQLAlchemyAuthorizationRepository(factory)

    try:
        yield {
            "app": app,
            "factory": factory,
            "operator_id": operator_id,
            "operator_token": operator_token,
            "csrf_token": csrf_token,
            "tenant_a_id": tenant_a_id,
            "admin_token": admin_token,
            "admin_csrf": admin_csrf,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@asynccontextmanager
async def client_for(app, base_url="http://localhost:3000"):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client


@pytest.mark.anyio
async def test_super_admin_can_list_and_create_communities(community_harness):
    h = community_harness
    app = h["app"]
    cookies = {"community_session": h["operator_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": h["csrf_token"],
    }

    async with client_for(app) as client:
        # 1. List initial communities
        res = await client.get("/api/v1/platform/communities", cookies=cookies)
        assert res.status_code == 200
        data = res.json()
        assert data["total"] >= 1
        assert any(item["slug"] == "existing-community" for item in data["items"])

        # 2. Create a new community
        create_res = await client.post(
            "/api/v1/platform/communities",
            cookies=cookies,
            headers=headers,
            json={
                "slug": "green-valley",
                "domain_hostname": "green.valley.test",
                "status": "active",
            },
        )
        assert create_res.status_code == 201
        created = create_res.json()
        assert created["slug"] == "green-valley"
        assert created["status"] == "active"
        assert len(created["domains"]) == 1
        assert created["domains"][0]["hostname"] == "green.valley.test"
        community_id = created["id"]

        # 3. Retrieve community details
        detail_res = await client.get(
            f"/api/v1/platform/communities/{community_id}",
            cookies=cookies,
        )
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["id"] == community_id
        assert detail["slug"] == "green-valley"
        assert "registration_policy" in detail


@pytest.mark.anyio
async def test_duplicate_slug_and_domain_rejected(community_harness):
    h = community_harness
    app = h["app"]
    cookies = {"community_session": h["operator_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": h["csrf_token"],
    }

    async with client_for(app) as client:
        # Duplicate slug
        res = await client.post(
            "/api/v1/platform/communities",
            cookies=cookies,
            headers=headers,
            json={"slug": "existing-community"},
        )
        assert res.status_code == 409

        # Invalid slug characters
        invalid_res = await client.post(
            "/api/v1/platform/communities",
            cookies=cookies,
            headers=headers,
            json={"slug": "UPPER_CASE_INVALID!"},
        )
        assert invalid_res.status_code == 422


@pytest.mark.anyio
async def test_community_activation_deactivation_lifecycle(community_harness):
    h = community_harness
    app = h["app"]
    cookies = {"community_session": h["operator_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": h["csrf_token"],
    }
    tenant_id = str(h["tenant_a_id"])

    async with client_for(app) as client:
        # Deactivate community
        patch_res = await client.patch(
            f"/api/v1/platform/communities/{tenant_id}",
            cookies=cookies,
            headers=headers,
            json={"status": "inactive"},
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["status"] == "inactive"

        # Verify active session for that tenant was revoked
        with h["factory"]() as s:
            revoked_count = s.execute(
                select(AuthSession).where(
                    AuthSession.tenant_id == h["tenant_a_id"],
                    AuthSession.revoked_at.is_not(None),
                )
            ).scalars().all()
            assert len(revoked_count) >= 1

        # Reactivate community
        reactivate_res = await client.patch(
            f"/api/v1/platform/communities/{tenant_id}",
            cookies=cookies,
            headers=headers,
            json={"status": "active"},
        )
        assert reactivate_res.status_code == 200
        assert reactivate_res.json()["status"] == "active"


@pytest.mark.anyio
async def test_admin_cannot_access_platform_management(community_harness):
    h = community_harness
    app = h["app"]
    # Community Admin cookies
    admin_cookies = {"community_session": h["admin_token"]}

    async with client_for(app) as client:
        # GET communities list forbidden for ADMIN
        res = await client.get("/api/v1/platform/communities", cookies=admin_cookies)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "HTTP_ERROR"

        # Unauthenticated request
        unauth_res = await client.get("/api/v1/platform/communities")
        assert unauth_res.status_code == 401


@pytest.mark.anyio
async def test_administrator_creation_and_isolation(community_harness):
    h = community_harness
    app = h["app"]
    cookies = {"community_session": h["operator_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": h["csrf_token"],
    }
    tenant_id = str(h["tenant_a_id"])

    # Seed mobile and email OTP challenges for registration
    now = datetime.now(UTC)
    mobile_token = "valid-admin-mobile-token"
    email_token = "valid-admin-email-token"
    with h["factory"]() as s:
        s.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=UUID(tenant_id),
                mobile_e164="+919876543210",
                purpose="admin_registration",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=mobile_token,
            )
        )
        s.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=UUID(tenant_id),
                email="lead@community.org",
                purpose="email_verification",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=email_token,
            )
        )
        s.commit()

    async with client_for(app) as client:
        # 1. Create a new administrator
        create_res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            cookies=cookies,
            headers=headers,
            json={
                "username": "new_community_lead",
                "password": "SecurePassword123!",
                "mobile_e164": "+919876543210",
                "email": "lead@community.org",
                "state": "Telangana",
                "verification_token": mobile_token,
                "email_verification_token": email_token,
                "status": "active",
            },
        )
        assert create_res.status_code == 201
        admin = create_res.json()
        assert admin["username"] == "new_community_lead"
        assert admin["role"] == "ADMIN"
        assert admin["mobile_e164"] == "+919876543210"
        assert admin["email"] == "lead@community.org"
        assert admin["state"] == "Telangana"
        admin_user_id = admin["user_id"]

        # Verify password is not plaintext
        with h["factory"]() as s:
            cred = s.execute(
                select(AccountCredential).where(AccountCredential.username == "new_community_lead")
            ).scalar_one()
            assert cred.password_hash != "SecurePassword123!"
            assert verify_password("SecurePassword123!", cred.password_hash)

        # 2. List administrators for this community
        list_res = await client.get(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            cookies=cookies,
        )
        assert list_res.status_code == 200
        admin_list = list_res.json()
        assert admin_list["total"] >= 2
        assert any(a["username"] == "new_community_lead" for a in admin_list["items"])

        # 3. Duplicate username conflict
        dup_username_res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            cookies=cookies,
            headers=headers,
            json={
                "username": "new_community_lead",
                "password": "OtherPassword123!",
                "mobile_e164": "+919876500000",
                "email": "lead2@community.org",
                "state": "Telangana",
                "verification_token": "some-token",
                "email_verification_token": "some-token",
            },
        )
        assert dup_username_res.status_code == 409

        # 4. Duplicate mobile conflict within tenant
        dup_mobile_res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            cookies=cookies,
            headers=headers,
            json={
                "username": "another_admin",
                "password": "OtherPassword123!",
                "mobile_e164": "+919876543210",
                "email": "lead3@community.org",
                "state": "Telangana",
                "verification_token": "some-token",
                "email_verification_token": "some-token",
            },
        )
        assert dup_mobile_res.status_code == 409

        # 5. Invalid phone number format
        invalid_phone_res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            cookies=cookies,
            headers=headers,
            json={
                "username": "valid_user",
                "password": "OtherPassword123!",
                "mobile_e164": "not-a-phone",
                "email": "lead4@community.org",
                "state": "Telangana",
            },
        )
        assert invalid_phone_res.status_code == 422

        # 6. Deactivate administrator
        patch_admin_res = await client.patch(
            f"/api/v1/platform/communities/{tenant_id}/administrators/{admin_user_id}",
            cookies=cookies,
            headers=headers,
            json={"status": "inactive"},
        )
        assert patch_admin_res.status_code == 200
        assert patch_admin_res.json()["status"] == "inactive"
