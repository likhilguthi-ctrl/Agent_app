from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from joserfc.jwk import RSAKey
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.oidc_callback_logging import OIDCCallbackQueryScrubbingMiddleware
from app.db.base import Base
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.authentication.models import AuthSession, OtpChallenge, OtpRateBucket
from app.modules.authorization.models import Permission, Role, RolePermission, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.outbox.models import AuditOutboxAttempt, AuditOutboxEvent, AuditOutboxWorkerHeartbeat
from app.modules.platform_identity.api import (
    get_platform_oidc_login_service,
    get_platform_oidc_session_store,
)
from app.modules.platform_identity.dependencies import get_platform_operator_principal
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformIdentityNotConfigured,
    PlatformOperatorNotAuthorized,
)
from app.modules.platform_identity.models import (
    PlatformOIDCLoginTransaction,
    PlatformOperator,
    PlatformOperatorSession,
    PlatformOperatorRoleAssignment,
    PlatformPermission,
    PlatformRole,
    PlatformRolePermission,
)
from app.modules.platform_identity.oidc_login import (
    LoginCompletion,
    LoginStart,
    PlatformOIDCLoginService,
    SQLAlchemyPlatformOIDCSessionStore,
    _digest,
)
from app.modules.platform_identity.provider import ExternalOperatorIdentity, OIDCPlatformIdentityProvider
from app.modules.platform_identity.service import PlatformPrincipal
from app.modules.tenants.models import Tenant, TenantDomain

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ISSUER = "https://identity.example.test/issuer"
SUBJECT = "operator-subject-test"
CALLBACK = "http://frontend.example.test/backend-api/api/v1/platform/auth/oidc/callback"


@pytest.fixture
def login_session_factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'platform-login.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(scope="module")
def oidc_keys():
    signing = RSAKey.generate_key(
        2048,
        {"kid": "login-test-key", "use": "sig", "alg": "RS256", "key_ops": ["sign"]},
    )
    public = signing.as_dict(private=False)
    public["key_ops"] = ["verify"]
    return signing, public


def login_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "test",
        "frontend_origins": "http://frontend.example.test",
        "platform_oidc_issuer": ISSUER,
        "platform_oidc_client_id": "operator-client",
        "platform_oidc_client_secret": "test-client-secret-only",
        "platform_oidc_redirect_uri": CALLBACK,
        "platform_oidc_transaction_encryption_key": Fernet.generate_key().decode("ascii"),
        "platform_oidc_required_amr_values": "mfa",
        "platform_oidc_allowed_algorithms": "RS256",
    }
    values.update(overrides)
    return Settings(**values)


class MockProvider:
    def __init__(self, identity: ExternalOperatorIdentity | None = None):
        self.identity = identity or ExternalOperatorIdentity(
            issuer=ISSUER,
            subject=SUBJECT,
            audience="operator-client",
            auth_time=NOW - timedelta(seconds=5),
            acr="urn:test:mfa",
            amr=("pwd", "mfa"),
        )
        self.authorization: dict[str, str] | None = None
        self.exchange: dict[str, str] | None = None

    async def authorization_url(self, **values) -> str:
        self.authorization = values
        return "https://identity.example.test/authorize?request=opaque"

    async def exchange_authorization_code(self, **values) -> str:
        self.exchange = values
        return "mocked-id-token"

    async def validate_identity_token(self, identity_token: str, *, expected_nonce: str):
        assert identity_token == "mocked-id-token"
        assert self.authorization is not None
        assert expected_nonce == self.authorization["nonce"]
        return self.identity

    async def check_provider(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def test_login_service_uses_hashed_state_and_browser_binding_and_consumes_once(login_session_factory):
    test_now = datetime.now(UTC)
    with login_session_factory.begin() as session:
        operator = PlatformOperator(issuer=ISSUER, subject=SUBJECT, status="active")
        session.add(operator)
        session.flush()
        operator_id = operator.id
    provider = MockProvider(
        ExternalOperatorIdentity(
            issuer=ISSUER,
            subject=SUBJECT,
            audience="operator-client",
            auth_time=test_now - timedelta(seconds=5),
            acr="urn:test:mfa",
            amr=("pwd", "mfa"),
        )
    )
    store = SQLAlchemyPlatformOIDCSessionStore(login_session_factory)
    settings = login_settings()
    service = PlatformOIDCLoginService(
        settings, provider, store=store, now=lambda: test_now
    )

    started = asyncio.run(service.start())
    assert provider.authorization is not None
    assert 43 <= len(provider.authorization["state"]) <= 128
    assert 43 <= len(provider.authorization["nonce"]) <= 128
    assert 43 <= len(provider.authorization["code_challenge"]) <= 128
    with login_session_factory() as session:
        transaction = session.scalar(select(PlatformOIDCLoginTransaction))
        assert transaction is not None
        assert transaction.state_digest == _digest(provider.authorization["state"])
        assert transaction.browser_binding_digest == _digest(started.browser_binding)
        assert provider.authorization["state"] not in transaction.state_digest
        assert provider.authorization["nonce"] not in transaction.encrypted_flow_secrets
        assert started.browser_binding not in transaction.browser_binding_digest

    with pytest.raises(InvalidPlatformIdentity):
        asyncio.run(
            service.complete(
                state=provider.authorization["state"],
                code="authorization-code",
                browser_binding="wrong-browser-binding-value",
                request_id="request-oidc-wrong-binding",
            )
        )

    completed = asyncio.run(
        service.complete(
            state=provider.authorization["state"],
            code="authorization-code",
            browser_binding=started.browser_binding,
            request_id="request-oidc-1",
        )
    )
    assert completed.principal.operator_id == operator_id
    assert provider.exchange == {
        "code": "authorization-code",
        "redirect_uri": CALLBACK,
        "code_verifier": provider.exchange["code_verifier"],
    }
    assert 43 <= len(provider.exchange["code_verifier"]) <= 128
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(provider.exchange["code_verifier"].encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert provider.authorization["code_challenge"] == expected_challenge
    with login_session_factory() as session:
        row = session.scalar(select(PlatformOperatorSession))
        assert row is not None
        assert row.token_digest == _digest(completed.raw_session_token)
        assert row.token_digest != completed.raw_session_token
        assert row.csrf_token == completed.csrf_token
        assert row.expires_at.replace(tzinfo=UTC) == test_now + timedelta(
            seconds=settings.platform_oidc_session_ttl_seconds
        )
        assert session.scalar(select(AuditEvent).where(AuditEvent.action == "operator.authenticate"))

    with pytest.raises(InvalidPlatformIdentity):
        asyncio.run(
            service.complete(
                state=provider.authorization["state"],
                code="authorization-code",
                browser_binding=started.browser_binding,
                request_id="request-oidc-replay",
            )
        )

    resolved = store.resolve_session(completed.raw_session_token, now=test_now)
    assert resolved is not None and resolved.operator_id == operator_id
    assert store.resolve_session(completed.raw_session_token, now=row.expires_at) is None
    store.revoke_session(
        session_id=completed.principal.session_id,
        operator_id=operator_id,
        now=test_now,
        request_id="request-logout",
    )
    assert store.resolve_session(completed.raw_session_token, now=test_now) is None
    with login_session_factory() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.action == "operator.logout"))


def test_oidc_login_configuration_requires_the_exact_proxy_callback_and_key_material():
    base = {
        "_env_file": None,
        "environment": "test",
        "platform_oidc_issuer": ISSUER,
        "platform_oidc_client_id": "operator-client",
        "platform_oidc_required_amr_values": "mfa",
    }
    with pytest.raises(ValidationError):
        Settings(**base, platform_oidc_client_secret="partial-secret")
    with pytest.raises(ValidationError):
        login_settings(platform_oidc_redirect_uri="http://frontend.example.test/callback")


def test_unknown_operator_is_not_provisioned_and_authentication_is_audited(login_session_factory):
    provider = MockProvider()
    store = SQLAlchemyPlatformOIDCSessionStore(login_session_factory)
    service = PlatformOIDCLoginService(login_settings(), provider, store=store, now=lambda: NOW)
    started = asyncio.run(service.start())
    with pytest.raises(PlatformOperatorNotAuthorized):
        asyncio.run(
            service.complete(
                state=provider.authorization["state"],
                code="authorization-code",
                browser_binding=started.browser_binding,
                request_id="request-unknown-operator",
            )
        )
    with login_session_factory() as session:
        assert session.scalar(select(PlatformOperator)) is None
        assert session.scalar(select(PlatformOperatorSession)) is None
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "operator.authenticate"))
        assert audit is not None and audit.outcome == "denied"


def test_expired_oidc_login_transaction_is_rejected_before_provider_exchange(login_session_factory):
    current = [NOW]
    provider = MockProvider()
    store = SQLAlchemyPlatformOIDCSessionStore(login_session_factory)
    service = PlatformOIDCLoginService(
        login_settings(platform_oidc_login_ttl_seconds=60),
        provider,
        store=store,
        now=lambda: current[0],
    )
    started = asyncio.run(service.start())
    current[0] = NOW + timedelta(seconds=61)
    with pytest.raises(InvalidPlatformIdentity):
        asyncio.run(
            service.complete(
                state=provider.authorization["state"],
                code="authorization-code",
                browser_binding=started.browser_binding,
                request_id="request-expired-oidc",
            )
        )
    assert provider.exchange is None
    with login_session_factory() as session:
        assert session.scalar(select(PlatformOperatorSession)) is None


def test_missing_required_mfa_assurance_is_audited_and_creates_no_session(login_session_factory):
    identity = ExternalOperatorIdentity(
        issuer=ISSUER,
        subject=SUBJECT,
        audience="operator-client",
        auth_time=NOW - timedelta(seconds=1),
        acr=None,
        amr=("pwd",),
    )
    provider = MockProvider(identity)
    store = SQLAlchemyPlatformOIDCSessionStore(login_session_factory)
    service = PlatformOIDCLoginService(login_settings(), provider, store=store, now=lambda: NOW)
    started = asyncio.run(service.start())
    with pytest.raises(InvalidPlatformIdentity):
        asyncio.run(
            service.complete(
                state=provider.authorization["state"],
                code="authorization-code",
                browser_binding=started.browser_binding,
                request_id="request-no-mfa",
            )
        )
    with login_session_factory() as session:
        denied = session.scalar(select(AuditEvent).where(AuditEvent.outcome == "denied"))
        assert denied is not None
        assert denied.decision_metadata["reason_code"] == "authentication_assurance_rejected"
        assert session.scalar(select(PlatformOperatorSession)) is None


def test_oidc_login_fails_closed_when_not_configured():
    settings = Settings(_env_file=None)
    with pytest.raises(PlatformIdentityNotConfigured):
        PlatformOIDCLoginService(settings, MockProvider())


@pytest.mark.anyio
async def test_oidc_callback_query_is_available_to_handler_then_scrubbed_from_access_log_scope():
    observed: list[bytes] = []

    async def downstream(scope, _receive, send):
        observed.append(scope["query_string"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "path": "/api/v1/platform/auth/oidc/callback",
        "query_string": b"code=one-time-secret&state=one-time-state",
    }
    sent: list[dict[str, object]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await OIDCCallbackQueryScrubbingMiddleware(downstream)(scope, receive, send)
    assert observed == [b"code=one-time-secret&state=one-time-state"]
    assert scope["query_string"] == b""
    assert len(sent) == 2


@pytest.mark.anyio
async def test_oidc_authorization_url_pkce_and_code_exchange_are_bound_to_exact_callback(oidc_keys):
    _, public_jwk = oidc_keys
    settings = login_settings()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/issuer/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "jwks_uri": "https://identity.example.test/keys",
                    "authorization_endpoint": "https://identity.example.test/authorize",
                    "token_endpoint": "https://identity.example.test/token",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path == "/token":
            assert request.headers.get("authorization", "").startswith("Basic ")
            assert b"client_secret" not in request.content
            assert b"code_verifier=" + (b"v" * 64) in request.content
            return httpx.Response(200, json={"id_token": "signed-id-token"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = OIDCPlatformIdentityProvider(settings, client=client)
    try:
        url = await provider.authorization_url(
            redirect_uri=CALLBACK,
            state="state-" + "s" * 48,
            nonce="nonce-" + "n" * 48,
            code_challenge="c" * 43,
        )
        query = parse_qs(urlsplit(url).query)
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [CALLBACK]
        assert query["state"] == ["state-" + "s" * 48]
        assert query["nonce"] == ["nonce-" + "n" * 48]
        assert await provider.exchange_authorization_code(
            code="authorization-code",
            redirect_uri=CALLBACK,
            code_verifier="v" * 64,
        ) == "signed-id-token"
        with pytest.raises(InvalidPlatformIdentity):
            await provider.authorization_url(
                redirect_uri="https://attacker.example/callback",
                state="state",
                nonce="nonce",
                code_challenge="challenge",
            )
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_unconfigured_login_route_returns_safe_unavailable():
    app = create_app(Settings(_env_file=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/platform/auth/oidc/start")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PLATFORM_AUTHENTICATION_UNAVAILABLE"
    assert "issuer" not in response.text.lower()


def test_injected_mock_principal_resolver_is_not_accepted_outside_test_environment():
    settings = Settings(
        _env_file=None,
        environment="development",
        platform_oidc_issuer=ISSUER,
        platform_oidc_client_id="operator-client",
        platform_oidc_required_amr_values="mfa",
    )

    class MockResolver:
        async def resolve(self, _request):
            return PlatformPrincipal(
                operator_id=uuid4(),
                issuer=ISSUER,
                subject=SUBJECT,
                authenticated_at=NOW,
                acr=None,
                amr=("mfa",),
            )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, platform_principal_resolver=MockResolver())
        )
    )
    with pytest.raises(PlatformIdentityNotConfigured):
        asyncio.run(get_platform_operator_principal(request))

    with pytest.raises(ValueError, match="test-only"):
        create_app(settings, platform_principal_resolver=MockResolver())


@pytest.mark.anyio
async def test_oidc_provider_timeout_is_unavailable_and_does_not_follow_redirects():
    settings = login_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(302, headers={"Location": "https://attacker.example/metadata"})
        raise AssertionError("redirects must not be followed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = OIDCPlatformIdentityProvider(settings, client=client)
    try:
        from app.modules.platform_identity.errors import PlatformIdentityUnavailable

        with pytest.raises(PlatformIdentityUnavailable):
            await provider.authorization_url(
                redirect_uri=CALLBACK,
                state="state-" + "s" * 48,
                nonce="nonce-" + "n" * 48,
                code_challenge="c" * 43,
            )
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_login_callback_sets_http_only_host_scoped_session_cookie():
    settings = login_settings(auth_session_cookie_secure=True)
    app = create_app(settings)

    class StubLoginService:
        async def complete(self, **_values):
            return LoginCompletion(
                principal=PlatformPrincipal(
                    operator_id=uuid4(),
                    issuer=ISSUER,
                    subject=SUBJECT,
                    authenticated_at=NOW,
                    acr="urn:test:mfa",
                    amr=("mfa",),
                ),
                raw_session_token="opaque-session-credential",
                csrf_token="c" * 64,
            )

    app.dependency_overrides[get_platform_oidc_login_service] = lambda: StubLoginService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://frontend.example.test"
    ) as client:
        response = await client.get(
            "/api/v1/platform/auth/oidc/callback?state=state&code=code",
            headers={"Cookie": "community_platform_oidc_binding=binding"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers.get_list("set-cookie")
    session_header = next(value for value in cookie if "community_platform_session=" in value)
    assert "HttpOnly" in session_header
    assert "Secure" in session_header
    assert "SameSite=lax" in session_header
    assert "Path=/backend-api/api/v1/platform" in session_header
    assert "Domain=" not in session_header
    assert "opaque-session-credential" not in response.text
    assert not any("community_platform_session=" in value and "Max-Age=0" in value for value in cookie)


@pytest.mark.anyio
async def test_login_start_sets_http_only_binding_cookie_for_proxy_callback_path():
    settings = login_settings(auth_session_cookie_secure=True)
    app = create_app(settings)

    class StubLoginService:
        async def start(self):
            return LoginStart(
                authorization_url="https://identity.example.test/authorize?state=opaque",
                browser_binding="b" * 43,
                expires_at=NOW + timedelta(minutes=5),
            )

    app.dependency_overrides[get_platform_oidc_login_service] = lambda: StubLoginService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://frontend.example.test"
    ) as client:
        response = await client.get("/api/v1/platform/auth/oidc/start")
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://identity.example.test/authorize?")
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers.get_list("set-cookie")[0]
    assert "community_platform_oidc_binding=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Path=/backend-api/api/v1/platform/auth/oidc" in cookie
    assert "Domain=" not in cookie


@pytest.mark.anyio
async def test_platform_logout_rejects_csrf_and_requires_configured_origin():
    settings = login_settings()
    app = create_app(settings)
    principal = PlatformPrincipal(
        operator_id=uuid4(),
        issuer=ISSUER,
        subject=SUBJECT,
        authenticated_at=NOW,
        acr="urn:test:mfa",
        amr=("mfa",),
        session_id=uuid4(),
        csrf_token="x" * 64,
        expires_at=NOW + timedelta(hours=1),
    )
    revoked: list[UUID] = []

    class StubStore:
        def revoke_session(self, **values):
            revoked.append(values["session_id"])

    app.dependency_overrides[get_platform_operator_principal] = lambda: principal
    app.dependency_overrides[get_platform_oidc_session_store] = lambda: StubStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://frontend.example.test"
    ) as client:
        missing = await client.post("/api/v1/platform/auth/oidc/logout")
        rejected = await client.post(
            "/api/v1/platform/auth/oidc/logout",
            headers={"X-CSRF-Token": "x" * 64, "Origin": "https://attacker.example"},
        )
        accepted = await client.post(
            "/api/v1/platform/auth/oidc/logout",
            headers={"X-CSRF-Token": "x" * 64, "Origin": "http://frontend.example.test"},
        )
    assert missing.status_code == rejected.status_code == 403
    assert accepted.status_code == 204
    assert revoked == [principal.session_id]
