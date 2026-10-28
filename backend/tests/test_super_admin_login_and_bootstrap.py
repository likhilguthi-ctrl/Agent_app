"""Stage 1: Verification tests for Super Admin bootstrap, multi-step 2FA login, and password change.

Covers:
- Idempotent bootstrap creating Super Admin account in PostgreSQL
- Password hashed with OWASP-recommended memory-hard scrypt (never plaintext)
- Bootstrap idempotency: subsequent executions do not overwrite existing credentials/roles/mobile
- Step 1: Valid credentials trigger OTP dispatch via notification service with masked mobile
- Anti-enumeration: Invalid credentials return generic error without user leakage
- Step 2: Valid OTP verification establishes authenticated session and platform cookies
- Replay protection: Used OTP cannot be re-verified
- Expiration: Expired OTP is rejected
- First-login enforcement: Must change password flag blocks access to protected endpoints (403)
- First-login allowed paths: /me, /password/change, /logout
- Password change updates password hash, clears must_change_password flag
- Post-password-change access to protected endpoints succeeds
- Inactive/disabled account rejection
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.cli.bootstrap_accounts import (
    DEFAULT_SUPER_ADMIN_MOBILE,
    DEFAULT_SUPER_ADMIN_PASSWORD,
    DEFAULT_SUPER_ADMIN_USERNAME,
    bootstrap_super_admin,
    reset_rate_limit,
    run_bootstrap,
)
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
from app.modules.authentication.password import hash_password, verify_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.api import get_authorization_repository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import (
    SQLAlchemyTenantMembershipRepository,
    get_tenant_membership_lookup,
)
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformRole,
)
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant
from tests.test_authentication_api import MockMessagingProvider


@pytest.fixture
def stage1_harness(tmp_path):
    database_path = tmp_path / "stage1-test.sqlite"
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

    # Initial tenant setup for general isolation testing
    tenant_id = uuid4()
    with factory.begin() as session:
        t = Tenant(id=tenant_id, slug="avanflix-demo", status="active")
        session.add(t)

    # Bootstrap Super Admin via run_bootstrap
    created = run_bootstrap(
        role="SUPER_ADMIN",
        username=DEFAULT_SUPER_ADMIN_USERNAME,
        password=DEFAULT_SUPER_ADMIN_PASSWORD,
        mobile_number=DEFAULT_SUPER_ADMIN_MOBILE,
        must_change_password=True,
        factory=factory,
    )
    assert created is True

    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
        session_cookie_secure=False,
        otp_delivery_mode="development_dummy",
        development_dummy_otp="123456",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
    )

    mock_provider = MockMessagingProvider({}, environment="test")
    app = create_app(test_settings, messaging_provider=mock_provider)

    tenant_context = TenantContext(tenant_id, "avanflix-demo", "avanflix.test", uuid4())
    app.state.test_tenant = tenant_context

    auth_repo = SQLAlchemyAuthenticationRepository(factory)
    authz_repo = SQLAlchemyAuthorizationRepository(factory)
    tenant_lookup = SQLAlchemyTenantMembershipRepository(factory)
    abuse_guard = SQLAlchemyOtpAbuseGuard(test_settings, factory)

    app.dependency_overrides[get_tenant_context] = lambda: app.state.test_tenant
    app.dependency_overrides[get_authentication_repository] = lambda: auth_repo
    app.dependency_overrides[get_authorization_repository] = lambda: authz_repo
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: tenant_lookup
    app.dependency_overrides[get_otp_abuse_guard] = lambda: abuse_guard
    app.state.test_factory = factory

    return {
        "app": app,
        "factory": factory,
        "provider": mock_provider,
        "tenant_id": tenant_id,
        "settings": test_settings,
    }


def client_for(app):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:3000"},
    )


@pytest.mark.anyio
async def test_bootstrap_idempotency_and_password_hashing(stage1_harness):
    factory = stage1_harness["factory"]

    # 1. Verify Super Admin exists in DB and is hashed with scrypt
    with factory() as session:
        cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == DEFAULT_SUPER_ADMIN_USERNAME)
        ).scalar_one()
        assert cred.status == "active"
        assert cred.account_type == "platform_operator"
        assert cred.must_change_password is True
        # Verify scrypt format and OWASP-grade parameters
        assert cred.password_hash.startswith("scrypt$16384$8$1$")
        assert verify_password(DEFAULT_SUPER_ADMIN_PASSWORD, cred.password_hash) is True
        # Plaintext password is never in DB
        assert DEFAULT_SUPER_ADMIN_PASSWORD not in cred.password_hash

        op = session.execute(
            select(PlatformOperator).where(PlatformOperator.id == cred.operator_id)
        ).scalar_one()
        assert op.status == "active"
        assert op.mobile_e164 == "+919100209205"

    # 2. Re-run bootstrap -> must return False and make zero changes
    with factory() as session:
        original_hash = cred.password_hash
        original_updated_at = cred.updated_at

    second_run = run_bootstrap(
        role="SUPER_ADMIN",
        username=DEFAULT_SUPER_ADMIN_USERNAME,
        password="SomeDifferentPassword999!",
        mobile_number="9876543210",
        factory=factory,
    )
    assert second_run is False

    with factory() as session:
        cred_after = session.execute(
            select(AccountCredential).where(AccountCredential.username == DEFAULT_SUPER_ADMIN_USERNAME)
        ).scalar_one()
        assert cred_after.password_hash == original_hash
        assert cred_after.updated_at == original_updated_at
        assert verify_password(DEFAULT_SUPER_ADMIN_PASSWORD, cred_after.password_hash) is True

        op_after = session.execute(
            select(PlatformOperator).where(PlatformOperator.id == cred_after.operator_id)
        ).scalar_one()
        assert op_after.mobile_e164 == "+919100209205"


@pytest.mark.anyio
async def test_step1_valid_credentials_triggers_otp_and_masks_mobile(stage1_harness):
    app = stage1_harness["app"]
    async with client_for(app) as client:
        # Step 1: Submit credentials with two_factor=True
        res = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "otp_required"
        assert "challenge_id" in data
        assert data["masked_mobile"] == "+91 91002 ****05"
        assert data["cooldown_seconds"] == 60


@pytest.mark.anyio
async def test_step1_anti_enumeration_invalid_username_and_password(stage1_harness):
    app = stage1_harness["app"]
    async with client_for(app) as client:
        # Non-existent username
        r1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "non_existent_user_999",
                "password": "AnyPassword123!",
                "two_factor": True,
            },
        )
        assert r1.status_code == 401
        assert r1.json()["error"]["code"] == "AUTHENTICATION_FAILED"

        # Existing user, wrong password
        r2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": "WrongPassword123!",
                "two_factor": True,
            },
        )
        assert r2.status_code == 401
        assert r2.json()["error"]["code"] == "AUTHENTICATION_FAILED"


@pytest.mark.anyio
async def test_step2_correct_otp_creates_session_with_must_change_password(stage1_harness):
    app = stage1_harness["app"]
    async with client_for(app) as client:
        # Step 1
        s1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert s1.status_code == 200
        challenge_id = s1.json()["challenge_id"]

        # Step 2: Submit correct OTP
        s2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "challenge_id": challenge_id,
                "otp": "123456",
            },
        )
        assert s2.status_code == 200
        data = s2.json()
        assert data["username"] == DEFAULT_SUPER_ADMIN_USERNAME
        assert data["role"] == "SUPER_ADMIN"
        assert data["must_change_password"] is True
        assert "community_session" in s2.headers.get("set-cookie", "")
        assert "community_platform_session" in s2.headers.get("set-cookie", "")


@pytest.mark.anyio
async def test_step2_incorrect_and_expired_otp_rejected(stage1_harness):
    app = stage1_harness["app"]
    factory = stage1_harness["factory"]

    async with client_for(app) as client:
        # Step 1
        s1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        challenge_id = s1.json()["challenge_id"]

        # Wrong OTP
        bad = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "challenge_id": challenge_id,
                "otp": "999999",
            },
        )
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] in ("HTTP_ERROR", "AUTHENTICATION_FAILED")

        # Expired OTP
        with factory.begin() as session:
            challenge = session.execute(
                select(OtpChallenge).where(OtpChallenge.id == UUID(challenge_id))
            ).scalar_one()
            challenge.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        expired = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "challenge_id": challenge_id,
                "otp": "123456",
            },
        )
        assert expired.status_code == 401


@pytest.mark.anyio
async def test_step2_replay_protection_used_otp_rejected(stage1_harness):
    app = stage1_harness["app"]
    async with client_for(app) as client:
        # Step 1
        s1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        challenge_id = s1.json()["challenge_id"]

        # Step 2: Verify first time
        v1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "challenge_id": challenge_id,
                "otp": "123456",
            },
        )
        assert v1.status_code == 200

        # Attempt replay with the same challenge_id and OTP
        v2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "challenge_id": challenge_id,
                "otp": "123456",
            },
        )
        assert v2.status_code == 401


@pytest.mark.anyio
async def test_first_login_password_change_enforcement_and_flow(stage1_harness):
    app = stage1_harness["app"]
    factory = stage1_harness["factory"]

    async with client_for(app) as client:
        # 1. Login to obtain session
        s1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        challenge_id = s1.json()["challenge_id"]

        s2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "challenge_id": challenge_id,
                "otp": "123456",
            },
        )
        assert s2.status_code == 200
        assert s2.json()["must_change_password"] is True

        # Extract session cookies into client
        for header in s2.headers.get_list("set-cookie"):
            sc = SimpleCookie()
            sc.load(header)
            for k, morsel in sc.items():
                client.cookies.set(k, morsel.value)

        # 2. Check /me: Allowed! Returns must_change_password=True
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["must_change_password"] is True

        # 3. Accessing protected resource before changing password: Blocked with 403!
        protected = await client.get("/api/v1/agent/policy")
        assert protected.status_code == 403
        assert protected.json()["error"]["code"] == "HTTP_ERROR"

        # 4. Attempt invalid password changes
        # a. Same password rejected
        bad1 = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "new_password": DEFAULT_SUPER_ADMIN_PASSWORD,
            },
        )
        assert bad1.status_code == 400
        assert bad1.json()["error"]["code"] == "HTTP_ERROR"

        # b. Too short rejected
        bad2 = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "new_password": "short",
            },
        )
        assert bad2.status_code == 422
        assert bad2.json()["error"]["code"] == "VALIDATION_ERROR"

        # c. Wrong current password rejected
        bad3 = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": "WrongCurrentPassword123!",
                "new_password": "NewSuperAdminPass@2026",
            },
        )
        assert bad3.status_code == 400
        assert bad3.json()["error"]["code"] == "HTTP_ERROR"

        # 5. Successful password change
        good = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "new_password": "NewSuperAdminPass@2026",
            },
        )
        assert good.status_code == 200
        assert good.json()["status"] == "success"

        # 6. Check /me: must_change_password is now False
        me_after = await client.get("/api/v1/auth/me")
        assert me_after.status_code == 200
        assert me_after.json()["must_change_password"] is False

        # 7. Check database: must_change_password is False, password is updated
        with factory() as session:
            cred = session.execute(
                select(AccountCredential).where(AccountCredential.username == DEFAULT_SUPER_ADMIN_USERNAME)
            ).scalar_one()
            assert cred.must_change_password is False
            assert verify_password("NewSuperAdminPass@2026", cred.password_hash) is True

        # 8. Login with old password fails
        old_login = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert old_login.status_code == 401

        # Clear rate-limit bucket so subsequent OTP dispatch is not blocked by 60s cooldown
        with factory.begin() as session:
            session.query(OtpRateBucket).delete()

        # 9. Login with new password succeeds
        new_login = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": "NewSuperAdminPass@2026",
                "two_factor": True,
            },
        )
        assert new_login.status_code == 200
        assert new_login.json()["status"] == "otp_required"


@pytest.mark.anyio
async def test_disabled_account_cannot_log_in(stage1_harness):
    app = stage1_harness["app"]
    factory = stage1_harness["factory"]

    # Disable Super Admin account
    with factory.begin() as session:
        cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == DEFAULT_SUPER_ADMIN_USERNAME)
        ).scalar_one()
        cred.status = "inactive"

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "AUTHENTICATION_FAILED"
        assert res.json()["error"]["message"] == "Login could not be completed."


@pytest.mark.anyio
async def test_rate_limiting_and_administrative_reset(stage1_harness):
    app = stage1_harness["app"]
    factory = stage1_harness["factory"]

    async with client_for(app) as client:
        # First attempt triggers OTP dispatch
        r1 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "otp_required"

        # Immediate second attempt within cooldown window returns 429
        r2 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert r2.status_code == 429
        assert r2.json()["error"]["code"] in ("RATE_LIMIT_EXCEEDED", "HTTP_ERROR")

        # Administrative reset clears rate limit bucket for this account
        with factory.begin() as session:
            cleared = reset_rate_limit(session, username=DEFAULT_SUPER_ADMIN_USERNAME)
            assert cleared >= 1

        # Third attempt immediately succeeds after administrative reset
        r3 = await client.post(
            "/api/v1/auth/login",
            json={
                "username": DEFAULT_SUPER_ADMIN_USERNAME,
                "password": DEFAULT_SUPER_ADMIN_PASSWORD,
                "two_factor": True,
            },
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "otp_required"
