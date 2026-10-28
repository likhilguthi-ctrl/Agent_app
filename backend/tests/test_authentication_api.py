from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.db.base import Base
from app.main import create_app
from app.modules.authentication.abuse_control import OtpRateLimited, SQLAlchemyOtpAbuseGuard
from app.modules.authentication.api import (
    get_authorization_context_service,
    get_authentication_repository,
    get_otp_abuse_guard,
    get_tenant_context,
)
from app.modules.authentication.models import AuthSession, OtpChallenge
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.catalog import PERMISSION_DESCRIPTIONS, PermissionCode
from app.modules.authorization.errors import AuthorizationDataUnavailable
from app.modules.authorization.models import (
    Permission,
    Role,
    RolePermission,
    UserRoleAssignment,
)
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import (
    SQLAlchemyTenantMembershipRepository,
    get_tenant_membership_lookup,
)
from app.modules.messaging.testing import MockMessagingProvider
from app.modules.tenants.models import Tenant


MOBILE = "+14155550123"
TEST_OTP = "phase4-test-code"


@pytest.fixture
def auth_harness(tmp_path):
    database_path = tmp_path / "phase4-auth.sqlite"
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
    tenant_id, tenant_b_id = uuid4(), uuid4()
    user_id, membership_id = uuid4(), uuid4()
    with factory.begin() as session:
        session.add_all(
            [
                Tenant(id=tenant_id, slug="auth-test-a", status="active"),
                Tenant(id=tenant_b_id, slug="auth-test-b", status="active"),
                User(id=user_id, status="active"),
            ]
        )
        session.flush()
        session.add(
            TenantMembership(
                id=membership_id,
                tenant_id=tenant_id,
                user_id=user_id,
                mobile_e164=MOBILE,
                status="active",
            )
        )

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
        otp_send_window_seconds=600,
        otp_send_limit=3,
        otp_verify_window_seconds=900,
        otp_verify_limit=5,
        auth_session_cookie_secure=False,
        otp_delivery_mode="mock",
    )
    provider = MockMessagingProvider({MOBILE: TEST_OTP}, environment="test")
    app = create_app(settings, messaging_provider=provider)
    tenant = TenantContext(tenant_id, "auth-test-a", "a.example.test", uuid4())
    app.state.test_tenant = tenant
    app.state.test_factory = factory
    app.state.test_provider = provider

    def tenant_override():
        return app.state.test_tenant

    app.dependency_overrides[get_tenant_context] = tenant_override
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: SQLAlchemyTenantMembershipRepository(
        factory
    )
    app.dependency_overrides[get_authentication_repository] = lambda: SQLAlchemyAuthenticationRepository(
        factory
    )
    app.dependency_overrides[get_authorization_repository] = lambda: SQLAlchemyAuthorizationRepository(
        factory
    )
    app.dependency_overrides[get_otp_abuse_guard] = lambda: SQLAlchemyOtpAbuseGuard(settings, factory)
    try:
        yield {
            "app": app,
            "tenant_id": tenant_id,
            "tenant_b_id": tenant_b_id,
            "user_id": user_id,
            "membership_id": membership_id,
            "factory": factory,
            "engine": engine,
            "provider": provider,
            "settings": settings,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@asynccontextmanager
async def client_for(app, base_url="http://community.test"):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client


async def obtain_challenge(
    client: httpx.AsyncClient, *, mobile: str = MOBILE, headers: dict[str, str] | None = None
) -> UUID:
    response = await client.post(
        "/api/v1/auth/otp/request",
        json={"mobile_number": mobile},
        headers=headers,
    )
    assert response.status_code == 202
    return UUID(response.json()["challenge_id"])


async def authenticate(client: httpx.AsyncClient) -> httpx.Response:
    challenge_id = await obtain_challenge(client)
    response = await client.post(
        "/api/v1/auth/otp/verify",
        json={"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(challenge_id)},
    )
    if response.status_code == 200:
        session_cookie = SimpleCookie()
        session_cookie.load(response.headers["set-cookie"])
        client.cookies.set(
            "community_session",
            session_cookie["community_session"].value,
            path="/api/v1/auth",
        )
    return response


def enable_development_static_otp(auth_harness) -> dict[str, str]:
    settings = auth_harness["settings"]
    settings.environment = "development"
    settings.development_static_otp_enabled = True
    settings.development_static_otp_code = SecretStr("123456")
    return {"Origin": settings.frontend_origin_list[0]}


@pytest.mark.anyio
async def test_otp_endpoints_issue_opaque_tenant_bound_session_and_revoke_on_logout(auth_harness):
    auth_harness["settings"].auth_session_cookie_secure = True
    async with client_for(auth_harness["app"], base_url="https://community.test") as client:
        login = await authenticate(client)
        assert login.status_code == 200
        assert login.json()["user_id"] == str(auth_harness["user_id"])
        assert login.json()["tenant_id"] == str(auth_harness["tenant_id"])
        assert login.json()["membership_id"] == str(auth_harness["membership_id"])
        assert login.json()["csrf_token"]
        issued_cookies = SimpleCookie()
        issued_cookies.load(login.headers["set-cookie"])
        session_cookie = issued_cookies["community_session"]
        assert session_cookie["httponly"]
        assert session_cookie["secure"]
        assert session_cookie["samesite"].lower() == "lax"
        assert session_cookie["path"] == auth_harness["settings"].auth_session_cookie_path
        assert session_cookie["path"] == "/backend-api/api/v1"
        assert session_cookie["max-age"] == str(auth_harness["settings"].auth_session_ttl_seconds)
        assert not session_cookie["domain"]
        assert login.json().get("session_token") is None

        # The browser-visible Next rewrite keeps /backend-api in its URL and must
        # receive the session cookie on all proxied API paths under /backend-api/api/v1.
        proxied_request = client.build_request(
            "GET", "https://community.test/backend-api/api/v1/auth/context"
        )
        assert "community_session" in proxied_request.headers.get("cookie", "")

        proxied_agents_request = client.build_request(
            "GET", "https://community.test/backend-api/api/v1/agents/regions"
        )
        assert "community_session" in proxied_agents_request.headers.get("cookie", "")

        proxied_create_agent_request = client.build_request(
            "POST", "https://community.test/backend-api/api/v1/agents"
        )
        assert "community_session" in proxied_create_agent_request.headers.get("cookie", "")

        with auth_harness["factory"]() as session:
            stored = session.execute(select(AuthSession)).scalar_one()
            assert stored.token_digest != session_cookie.value
            assert len(stored.token_digest) == 64

        current = await client.get("/api/v1/auth/me")
        assert current.status_code == 200
        assert current.json()["user_id"] == login.json()["user_id"]
        assert current.json()["csrf_token"] == login.json()["csrf_token"]

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": current.json()["csrf_token"],
                "Origin": auth_harness["settings"].frontend_origin_list[0],
            },
        )
        assert logout.status_code == 204
        deleted_cookies = SimpleCookie()
        deleted_cookies.load(logout.headers["set-cookie"])
        deleted_cookie = deleted_cookies["community_session"]
        assert deleted_cookie["path"] == session_cookie["path"]
        assert deleted_cookie["httponly"]
        assert deleted_cookie["secure"]
        assert deleted_cookie["samesite"].lower() == session_cookie["samesite"].lower()
        assert deleted_cookie["max-age"] == "0"
        assert not deleted_cookie["domain"]
        assert (await client.get("/api/v1/auth/me")).status_code == 401
        assert (await client.get("/api/v1/auth/context")).status_code == 401


@pytest.mark.anyio
async def test_development_static_otp_reuses_challenge_session_and_logout_lifecycle(auth_harness):
    origin_headers = enable_development_static_otp(auth_harness)
    forged_headers = {
        **origin_headers,
        "X-Tenant-ID": str(auth_harness["tenant_b_id"]),
        "X-Identity-ID": str(uuid4()),
        "X-User-ID": str(uuid4()),
        "X-Role": "platform_admin",
    }
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client, headers=forged_headers)
        invalid = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": "654321", "challenge_id": str(challenge_id)},
            headers=forged_headers,
        )
        assert invalid.status_code == 401
        assert auth_harness["provider"].sent_otp_requests == []

        login = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": "123456", "challenge_id": str(challenge_id)},
            headers=forged_headers,
        )
        assert login.status_code == 200
        assert login.json()["user_id"] == str(auth_harness["user_id"])
        assert login.json()["tenant_id"] == str(auth_harness["tenant_id"])
        assert login.json()["membership_id"] == str(auth_harness["membership_id"])
        assert login.headers["set-cookie"].startswith("community_session=")
        session_cookie = SimpleCookie()
        session_cookie.load(login.headers["set-cookie"])
        client.cookies.set(
            "community_session",
            session_cookie["community_session"].value,
            path="/api/v1/auth",
        )

        current = await client.get("/api/v1/auth/me")
        assert current.status_code == 200
        assert current.json()["membership_id"] == str(auth_harness["membership_id"])
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={
                **origin_headers,
                "X-CSRF-Token": current.json()["csrf_token"],
            },
        )
        assert logout.status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 401
        replay = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": "123456", "challenge_id": str(challenge_id)},
            headers=forged_headers,
        )
        assert replay.status_code == 401


@pytest.mark.anyio
async def test_development_static_otp_does_not_authenticate_unknown_or_inactive_membership(auth_harness):
    origin_headers = enable_development_static_otp(auth_harness)
    async with client_for(auth_harness["app"]) as client:
        unknown_mobile = "+14155550999"
        unknown_challenge = await obtain_challenge(
            client, mobile=unknown_mobile, headers=origin_headers
        )
        unknown = await client.post(
            "/api/v1/auth/otp/verify",
            json={
                "mobile_number": unknown_mobile,
                "otp": "123456",
                "challenge_id": str(unknown_challenge),
            },
            headers=origin_headers,
        )
        assert unknown.status_code == 401
        assert "community_session" not in unknown.headers.get("set-cookie", "")

        with auth_harness["factory"].begin() as session:
            session.execute(
                update(TenantMembership)
                .where(TenantMembership.id == auth_harness["membership_id"])
                .values(status="inactive")
            )
        inactive_challenge = await obtain_challenge(client, headers=origin_headers)
        inactive = await client.post(
            "/api/v1/auth/otp/verify",
            json={
                "mobile_number": MOBILE,
                "otp": "123456",
                "challenge_id": str(inactive_challenge),
            },
            headers=origin_headers,
        )
        assert inactive.status_code == 401
        assert "community_session" not in inactive.headers.get("set-cookie", "")


@pytest.mark.anyio
async def test_development_static_otp_requires_loopback_peer_and_configured_origin(auth_harness):
    origin_headers = enable_development_static_otp(auth_harness)
    async with client_for(auth_harness["app"]) as client:
        bad_origin = await client.post(
            "/api/v1/auth/otp/request",
            json={"mobile_number": MOBILE},
            headers={"Origin": "https://attacker.example"},
        )
        assert bad_origin.status_code == 403

    transport = httpx.ASGITransport(
        app=auth_harness["app"],
        client=("198.51.100.25", 12345),
        raise_app_exceptions=False,
    )
    async with auth_harness["app"].router.lifespan_context(auth_harness["app"]):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://community.test"
        ) as remote_client:
            response = await remote_client.post(
                "/api/v1/auth/otp/request",
                json={"mobile_number": MOBILE},
                headers=origin_headers,
            )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_development_static_otp_rejects_expired_challenge(auth_harness):
    origin_headers = enable_development_static_otp(auth_harness)
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client, headers=origin_headers)
        with auth_harness["factory"].begin() as session:
            session.execute(
                update(OtpChallenge)
                .where(OtpChallenge.id == challenge_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        response = await client.post(
            "/api/v1/auth/otp/verify",
            json={
                "mobile_number": MOBILE,
                "otp": "123456",
                "challenge_id": str(challenge_id),
            },
            headers=origin_headers,
        )
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        with auth_harness["factory"]() as session:
            assert session.execute(select(AuthSession)).scalars().all() == []


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_development_static_otp_configuration_is_rejected_outside_development(environment):
    with pytest.raises(ValueError, match="allowed only in development"):
        Settings(
            _env_file=None,
            environment=environment,
            frontend_origins="http://localhost:3000",
            otp_challenge_ttl_seconds=300,
            development_static_otp_enabled=True,
            development_static_otp_code=SecretStr("123456"),
        )


def test_development_static_otp_requires_explicit_code_ttl_and_loopback_origin():
    with pytest.raises(ValueError, match="CODE is required"):
        Settings(
            _env_file=None,
            environment="development",
            frontend_origins="http://localhost:3000",
            otp_challenge_ttl_seconds=300,
            development_static_otp_enabled=True,
        )
    with pytest.raises(ValueError, match="CHALLENGE_TTL_SECONDS is required"):
        Settings(
            _env_file=None,
            environment="development",
            frontend_origins="http://localhost:3000",
            development_static_otp_enabled=True,
            development_static_otp_code=SecretStr("123456"),
        )
    with pytest.raises(ValueError, match="loopback FRONTEND_ORIGINS"):
        Settings(
            _env_file=None,
            environment="development",
            frontend_origins="https://frontend.example",
            otp_challenge_ttl_seconds=300,
            development_static_otp_enabled=True,
            development_static_otp_code=SecretStr("123456"),
        )
    assert Settings(_env_file=None, environment="development").development_static_otp_enabled is False
    local_tenant_origin = Settings(
        _env_file=None,
        environment="development",
        frontend_origins="http://auth-test.localhost:3000",
        otp_challenge_ttl_seconds=300,
        development_static_otp_enabled=True,
        development_static_otp_code=SecretStr("123456"),
    )
    assert local_tenant_origin.frontend_origin_list == [
        "http://auth-test.localhost:3000"
    ]


@pytest.mark.anyio
async def test_authorization_context_returns_only_resolved_tenant_member_context(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        assert (await authenticate(client)).status_code == 200

        response = await client.get("/api/v1/auth/context")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["authentication"]["status"] == "authenticated"
        assert body["principal"] == {
            "type": "tenant_member",
            "id": str(auth_harness["user_id"]),
            "membership_id": str(auth_harness["membership_id"]),
        }
        assert body["authorization"] == {
            "status": "resolved",
            "effective_permissions": [],
            "community_scope": {
                "status": "resolved",
                "resolution": "current_trusted_tenant",
                "communities": [
                    {
                        "id": str(auth_harness["tenant_id"]),
                        "slug": "auth-test-a",
                    }
                ],
            },
        }
        assert body["employee_authority"] == {
            "status": "unavailable",
            "reason_code": "EMPLOYEE_LINK_NOT_ESTABLISHED",
        }
        assert body["product_role"] == {
            "status": "unavailable",
            "reason_code": "PRODUCT_ROLE_MAPPING_NOT_APPROVED",
        }
        assert "csrf_token" not in response.text
        assert "platform_permissions" not in body


@pytest.mark.anyio
async def test_authorization_context_ignores_client_identity_and_scope_headers(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        assert (await authenticate(client)).status_code == 200

        response = await client.get(
            f"/api/v1/auth/context?tenant_id={auth_harness['tenant_b_id']}&role=platform_admin",
            headers={
                "X-Tenant-ID": str(auth_harness["tenant_b_id"]),
                "X-Identity-ID": str(uuid4()),
                "X-User-ID": str(uuid4()),
                "X-Role": "platform_admin",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["principal"] == {
        "type": "tenant_member",
        "id": str(auth_harness["user_id"]),
        "membership_id": str(auth_harness["membership_id"]),
    }
    assert body["authorization"]["community_scope"]["communities"] == [
        {
            "id": str(auth_harness["tenant_id"]),
            "slug": "auth-test-a",
        }
    ]
    assert body["authorization"]["effective_permissions"] == []
    assert body["product_role"]["status"] == "unavailable"


@pytest.mark.anyio
async def test_authorization_context_lists_only_current_active_tenant_grants(auth_harness):
    permission = PermissionCode.AGENT_READ
    role_id = uuid4()
    with auth_harness["factory"].begin() as session:
        session.add(
            Permission(
                code=permission.value,
                description=PERMISSION_DESCRIPTIONS[permission],
            )
        )
        session.flush()
        session.add(
            Role(
                id=role_id,
                tenant_id=auth_harness["tenant_id"],
                code="context_test_role",
                display_name="Context Test Role",
                status="active",
            )
        )
        session.flush()
        session.add_all(
            [
                RolePermission(
                    tenant_id=auth_harness["tenant_id"],
                    role_id=role_id,
                    permission_code=permission.value,
                    status="active",
                    is_delegable=False,
                ),
                UserRoleAssignment(
                    id=uuid4(),
                    tenant_id=auth_harness["tenant_id"],
                    membership_id=auth_harness["membership_id"],
                    role_id=role_id,
                    assigned_by_membership_id=auth_harness["membership_id"],
                    status="active",
                    valid_from=datetime.now(UTC) - timedelta(minutes=1),
                ),
            ]
        )

    async with client_for(auth_harness["app"]) as client:
        assert (await authenticate(client)).status_code == 200
        response = await client.get("/api/v1/auth/context")

    assert response.status_code == 200
    assert response.json()["authorization"]["effective_permissions"] == [permission.value]
    assert response.json()["principal"]["type"] == "tenant_member"


@pytest.mark.anyio
async def test_authorization_context_rejects_missing_and_cross_tenant_sessions(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        client.cookies.set("ep_session", '{"type":"admin","role":"superadmin"}')
        client.cookies.set("unrelated_cookie", "must-not-authenticate")
        assert (await client.get("/api/v1/auth/context")).status_code == 401
        client.cookies.set("community_platform_session", "untrusted-platform-cookie")
        assert (await client.get("/api/v1/auth/context")).status_code == 401
        assert (await authenticate(client)).status_code == 200
        assert (await client.get("/api/v1/auth/context")).status_code == 200
        auth_harness["app"].state.test_tenant = TenantContext(
            auth_harness["tenant_b_id"], "auth-test-b", "b.example.test", uuid4()
        )
        response = await client.get("/api/v1/auth/context")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_authorization_context_fails_closed_when_grant_data_is_unavailable(auth_harness):
    class UnavailableAuthorizationService:
        def resolve_effective_permissions(self, *, tenant, authenticated):
            raise AuthorizationDataUnavailable

    async with client_for(auth_harness["app"]) as client:
        assert (await authenticate(client)).status_code == 200
        auth_harness["app"].dependency_overrides[
            get_authorization_context_service
        ] = lambda: UnavailableAuthorizationService()
        response = await client.get("/api/v1/auth/context")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTHORIZATION_UNAVAILABLE"


@pytest.mark.anyio
async def test_logout_requires_session_bound_csrf_header(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        assert (await authenticate(client)).status_code == 200
        csrf_token = (await client.get("/api/v1/auth/me")).json()["csrf_token"]
        allowed_origin = auth_harness["settings"].frontend_origin_list[0]
        missing = await client.post(
            "/api/v1/auth/logout", headers={"Origin": allowed_origin}
        )
        wrong = await client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": "wrong", "Origin": allowed_origin},
        )
        assert missing.status_code == wrong.status_code == 403
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        assert csrf_token


@pytest.mark.anyio
async def test_logout_rejects_missing_or_untrusted_origin_even_with_valid_csrf(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        assert (await authenticate(client)).status_code == 200
        csrf_token = (await client.get("/api/v1/auth/me")).json()["csrf_token"]
        missing_origin = await client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        untrusted_origin = await client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "https://attacker.example",
            },
        )

        assert missing_origin.status_code == untrusted_origin.status_code == 403
        assert (await client.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.anyio
async def test_otp_login_fails_closed_when_challenge_policy_is_unconfigured(auth_harness):
    auth_harness["settings"].otp_challenge_ttl_seconds = None
    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/otp/request", json={"mobile_number": MOBILE}
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTHENTICATION_UNAVAILABLE"
    assert "set-cookie" not in response.headers
    assert MOBILE not in response.text


@pytest.mark.anyio
async def test_failed_otp_does_not_create_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client)
        response = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": "wrong", "challenge_id": str(challenge_id)},
        )
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        with auth_harness["factory"]() as session:
            assert session.execute(select(AuthSession)).scalars().all() == []


@pytest.mark.anyio
async def test_otp_challenge_replay_cannot_issue_a_second_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client)
        payload = {"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(challenge_id)}
        assert (await client.post("/api/v1/auth/otp/verify", json=payload)).status_code == 200
        replay = await client.post("/api/v1/auth/otp/verify", json=payload)
        assert replay.status_code == 401
        with auth_harness["factory"]() as session:
            assert len(session.execute(select(AuthSession)).scalars().all()) == 1


@pytest.mark.anyio
async def test_session_is_bound_to_host_resolved_tenant_and_client_tenant_id_is_rejected(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client)
        login = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(challenge_id)},
        )
        assert login.status_code == 200
        auth_harness["app"].state.test_tenant = TenantContext(
            auth_harness["tenant_b_id"], "auth-test-b", "b.example.test", uuid4()
        )
        assert (await client.get("/api/v1/auth/me")).status_code == 401

        spoof = await client.post(
            "/api/v1/auth/otp/request",
            json={"mobile_number": MOBILE, "tenant_id": str(auth_harness["tenant_id"])},
        )
        assert spoof.status_code == 422
        assert "tenant_id" not in spoof.text


def test_session_composite_foreign_key_prevents_cross_tenant_membership_binding(auth_harness):
    with auth_harness["factory"]() as session:
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=auth_harness["tenant_b_id"],
                membership_id=auth_harness["membership_id"],
                token_digest="f" * 64,
                csrf_token="c" * 32,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.anyio
async def test_expired_or_disabled_membership_invalidates_existing_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await authenticate(client)
        assert login.status_code == 200
        with auth_harness["factory"].begin() as session:
            session.execute(
                update(TenantMembership)
                .where(TenantMembership.id == auth_harness["membership_id"])
                .values(status="inactive")
            )
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "TENANT_MEMBERSHIP_REQUIRED"


@pytest.mark.anyio
async def test_inactive_user_invalidates_existing_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await authenticate(client)
        assert login.status_code == 200
        with auth_harness["factory"].begin() as session:
            session.execute(
                update(User).where(User.id == auth_harness["user_id"]).values(status="inactive")
            )
        assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_inactive_tenant_invalidates_existing_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await authenticate(client)
        assert login.status_code == 200
        with auth_harness["factory"].begin() as session:
            session.execute(
                update(Tenant)
                .where(Tenant.id == auth_harness["tenant_id"])
                .values(status="inactive")
            )
        assert (await client.get("/api/v1/auth/me")).status_code == 403


@pytest.mark.anyio
async def test_expired_otp_challenge_never_reaches_provider_or_creates_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client)
        with auth_harness["factory"].begin() as session:
            session.execute(
                update(OtpChallenge)
                .where(OtpChallenge.id == challenge_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        sent_count = len(auth_harness["provider"].sent_otp_requests)
        response = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(challenge_id)},
        )
        assert response.status_code == 401
        assert len(auth_harness["provider"].sent_otp_requests) == sent_count
        with auth_harness["factory"]() as session:
            assert session.execute(select(AuthSession)).scalars().all() == []


@pytest.mark.anyio
async def test_expired_session_is_rejected(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        login = await authenticate(client)
        assert login.status_code == 200
        with auth_harness["factory"].begin() as session:
            session.execute(
                update(AuthSession)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert (await client.get("/api/v1/auth/me")).status_code == 401
        assert (await client.get("/api/v1/auth/context")).status_code == 401


@pytest.mark.anyio
async def test_unknown_mobile_gets_generic_accepted_response_without_delivery(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        response = await client.post(
            "/api/v1/auth/otp/request", json={"mobile_number": "+442079460958"}
        )
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        assert UUID(response.json()["challenge_id"])
        assert auth_harness["provider"].sent_otp_requests == []


@pytest.mark.anyio
async def test_verification_attempt_limit_is_enforced_before_provider_verification(auth_harness):
    limited_settings = auth_harness["settings"].model_copy(update={"otp_verify_limit": 1})
    auth_harness["app"].dependency_overrides[get_otp_abuse_guard] = lambda: SQLAlchemyOtpAbuseGuard(
        limited_settings, auth_harness["factory"]
    )
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client)
        body = {"mobile_number": MOBILE, "otp": "wrong", "challenge_id": str(challenge_id)}
        first = await client.post("/api/v1/auth/otp/verify", json=body)
        second = await client.post("/api/v1/auth/otp/verify", json=body)
        assert first.status_code == 401
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) > 0
        assert second.json()["error"]["code"] == "OTP_RATE_LIMITED"


@pytest.mark.anyio
async def test_resend_limit_keeps_generic_response_and_suppresses_extra_delivery(auth_harness):
    limited_settings = auth_harness["settings"].model_copy(
        update={"otp_send_cooldown_seconds": 120, "otp_send_limit": 1}
    )
    auth_harness["app"].dependency_overrides[get_otp_abuse_guard] = lambda: SQLAlchemyOtpAbuseGuard(
        limited_settings, auth_harness["factory"]
    )
    async with client_for(auth_harness["app"]) as client:
        first = await client.post("/api/v1/auth/otp/request", json={"mobile_number": MOBILE})
        second = await client.post("/api/v1/auth/otp/request", json={"mobile_number": MOBILE})
        assert first.status_code == second.status_code == 202
        assert first.json()["status"] == second.json()["status"] == "accepted"
        assert auth_harness["provider"].sent_otp_requests == [MOBILE]


@pytest.mark.anyio
async def test_newly_sent_challenge_replaces_previous_challenge_for_same_tenant_mobile(auth_harness):
    resend_settings = auth_harness["settings"].model_copy(
        update={"otp_send_cooldown_seconds": 0, "otp_send_limit": 3}
    )
    auth_harness["app"].dependency_overrides[get_otp_abuse_guard] = lambda: SQLAlchemyOtpAbuseGuard(
        resend_settings, auth_harness["factory"]
    )
    async with client_for(auth_harness["app"]) as client:
        first = await obtain_challenge(client)
        second = await obtain_challenge(client)
        assert first != second
        with auth_harness["factory"]() as session:
            challenges = session.execute(select(OtpChallenge)).scalars().all()
            assert len(challenges) == 1
            assert challenges[0].id == second
        stale = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(first)},
        )
        current = await client.post(
            "/api/v1/auth/otp/verify",
            json={"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(second)},
        )
        assert stale.status_code == 401
        assert current.status_code == 200


@pytest.mark.anyio
async def test_shared_send_cooldown_is_persistent_across_guard_instances(auth_harness):
    settings = auth_harness["settings"].model_copy(update={"otp_send_cooldown_seconds": 120})
    first = SQLAlchemyOtpAbuseGuard(settings, auth_harness["factory"])
    second = SQLAlchemyOtpAbuseGuard(settings, auth_harness["factory"])
    await first.authorize_send(auth_harness["tenant_id"], MOBILE)
    with pytest.raises(OtpRateLimited) as error:
        await second.authorize_send(auth_harness["tenant_id"], MOBILE)
    assert 1 <= error.value.retry_after_seconds <= 120


@pytest.mark.anyio
async def test_concurrent_send_requests_cannot_bypass_shared_atomic_limit(auth_harness):
    settings = auth_harness["settings"].model_copy(
        update={"otp_send_cooldown_seconds": 0, "otp_send_limit": 1}
    )
    guards = [SQLAlchemyOtpAbuseGuard(settings, auth_harness["factory"]) for _ in range(8)]

    async def attempt(guard):
        try:
            await guard.authorize_send(auth_harness["tenant_id"], MOBILE)
            return "allowed"
        except OtpRateLimited:
            return "limited"

    results = await asyncio.gather(*(attempt(guard) for guard in guards))
    assert results.count("allowed") == 1
    assert results.count("limited") == 7


@pytest.mark.anyio
async def test_concurrent_verification_of_one_challenge_issues_at_most_one_session(auth_harness):
    async with client_for(auth_harness["app"]) as client:
        challenge_id = await obtain_challenge(client)
        body = {"mobile_number": MOBILE, "otp": TEST_OTP, "challenge_id": str(challenge_id)}
        responses = await asyncio.gather(
            client.post("/api/v1/auth/otp/verify", json=body),
            client.post("/api/v1/auth/otp/verify", json=body),
        )
        assert sorted(response.status_code for response in responses) == [200, 401]
        with auth_harness["factory"]() as session:
            assert len(session.execute(select(AuthSession)).scalars().all()) == 1
