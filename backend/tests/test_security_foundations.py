from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditEventDraft, AuditInputRejected, AuditService
from app.modules.authentication.models import AuthSession, OtpChallenge, OtpRateBucket
from app.modules.authorization.models import (
    Permission,
    Role,
    RolePermission,
    UserRoleAssignment,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.outbox.models import (
    AuditOutboxAttempt,
    AuditOutboxEvent,
    AuditOutboxWorkerHeartbeat,
)
from app.modules.outbox.repository import SQLAlchemyOutboxRepository
from app.modules.outbox.service import AuditOutboxWorker
from app.modules.outbox.sink import (
    AuditAcknowledgement,
    AuditSinkNotConfigured,
    RetryableAuditSinkError,
    TerminalAuditSinkError,
    UnavailableAuditSink,
)
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformIdentityNotConfigured,
    PlatformOperatorNotAuthorized,
)
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformPermission,
    PlatformRole,
    PlatformRolePermission,
)
from app.modules.platform_identity.provider import (
    ExternalOperatorIdentity,
    OIDCPlatformIdentityProvider,
)
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.modules.platform_identity.service import (
    PlatformAuthorizationService,
    PlatformOperatorAuthenticationService,
    PlatformPrincipal,
)
from app.modules.onboarding.repository import TenantOnboardingRepository
from app.modules.tenants.models import Tenant


@pytest.fixture
def isolated_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'security-foundations.sqlite'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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
        {"kid": "operator-test-key", "use": "sig", "alg": "RS256", "key_ops": ["sign"]},
    )
    other = RSAKey.generate_key(
        2048,
        {"kid": "operator-test-key", "use": "sig", "alg": "RS256", "key_ops": ["sign"]},
    )
    public = signing.as_dict(private=False)
    public["key_ops"] = ["verify"]
    return signing, other, public


def oidc_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "test",
        "platform_oidc_issuer": "https://identity.example.test/issuer",
        "platform_oidc_client_id": "community-ott-platform",
        "platform_oidc_required_amr_values": "mfa",
        "platform_oidc_allowed_algorithms": "RS256",
    }
    values.update(overrides)
    return Settings(**values)


def make_id_token(
    signing_key: RSAKey,
    *,
    now: datetime,
    issuer: str = "https://identity.example.test/issuer",
    audience: str | list[str] = "community-ott-platform",
    nonce: str = "nonce-for-operator-login-that-is-long-enough",
    amr: list[str] | None = None,
    exp: int | None = None,
    auth_time: int | None = None,
    algorithm: str = "RS256",
) -> str:
    timestamp = int(now.timestamp())
    claims = {
        "iss": issuer,
        "sub": "operator-subject-42",
        "aud": audience,
        "iat": timestamp - 5,
        "exp": exp if exp is not None else timestamp + 120,
        "auth_time": auth_time if auth_time is not None else timestamp - 5,
        "nonce": nonce,
        "amr": amr if amr is not None else ["pwd", "mfa"],
    }
    return jwt.encode(
        {"alg": algorithm, "kid": "operator-test-key", "typ": "JWT"},
        claims,
        signing_key,
    )


def oidc_mock_client(jwks: dict[str, object], *, metadata_issuer: str | None = None):
    issuer = "https://identity.example.test/issuer"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/issuer/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": metadata_issuer or issuer,
                    "jwks_uri": "https://identity.example.test/keys",
                    "authorization_endpoint": "https://identity.example.test/authorize",
                    "token_endpoint": "https://identity.example.test/token",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.anyio
async def test_oidc_provider_validates_signed_id_token_and_mfa(oidc_keys) -> None:
    signing, _, public_jwk = oidc_keys
    now = datetime.now(UTC)
    client = oidc_mock_client({"keys": [public_jwk]})
    provider = OIDCPlatformIdentityProvider(
        oidc_settings(), client=client, clock=lambda: now.timestamp()
    )
    try:
        identity = await provider.validate_identity_token(
            make_id_token(signing, now=now),
            expected_nonce="nonce-for-operator-login-that-is-long-enough",
        )
    finally:
        await client.aclose()

    assert identity.issuer == "https://identity.example.test/issuer"
    assert identity.subject == "operator-subject-42"
    assert identity.audience == "community-ott-platform"
    assert identity.amr == ("pwd", "mfa")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token_options",
    [
        {"issuer": "https://attacker.example"},
        {"audience": "another-client"},
        {"amr": ["pwd"]},
        {"exp": 1},
        {"auth_time": 1},
    ],
)
async def test_oidc_rejects_wrong_claims_and_expired_or_stale_auth(oidc_keys, token_options) -> None:
    signing, _, public_jwk = oidc_keys
    now = datetime.now(UTC)
    client = oidc_mock_client({"keys": [public_jwk]})
    provider = OIDCPlatformIdentityProvider(
        oidc_settings(), client=client, clock=lambda: now.timestamp()
    )
    try:
        with pytest.raises(InvalidPlatformIdentity):
            await provider.validate_identity_token(
                make_id_token(signing, now=now, **token_options),
                expected_nonce="nonce-for-operator-login-that-is-long-enough",
            )
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_oidc_rejects_bad_signature_nonce_and_discovery_issuer(oidc_keys) -> None:
    signing, other, public_jwk = oidc_keys
    now = datetime.now(UTC)
    client = oidc_mock_client({"keys": [public_jwk]})
    provider = OIDCPlatformIdentityProvider(
        oidc_settings(), client=client, clock=lambda: now.timestamp()
    )
    try:
        with pytest.raises(InvalidPlatformIdentity):
            await provider.validate_identity_token(
                make_id_token(other, now=now),
                expected_nonce="nonce-for-operator-login-that-is-long-enough",
            )
        with pytest.raises(InvalidPlatformIdentity):
            await provider.validate_identity_token(
                make_id_token(signing, now=now), expected_nonce="different-long-enough-nonce-value"
            )
    finally:
        await client.aclose()

    bad_metadata_client = oidc_mock_client({"keys": [public_jwk]}, metadata_issuer="https://wrong.example")
    bad_metadata_provider = OIDCPlatformIdentityProvider(
        oidc_settings(), client=bad_metadata_client
    )
    try:
        assert await bad_metadata_provider.check_provider() is False
    finally:
        await bad_metadata_client.aclose()


@pytest.mark.anyio
async def test_oidc_uses_only_strong_public_keys_from_jwks(oidc_keys) -> None:
    _, _, public_jwk = oidc_keys
    weak_jwk = {
        "kid": "weak-key",
        "use": "sig",
        "alg": "RS256",
        "key_ops": ["verify"],
        "kty": "RSA",
        "n": "AQAB",
        "e": "AQAB",
    }
    client = oidc_mock_client({"keys": [public_jwk, weak_jwk]})
    provider = OIDCPlatformIdentityProvider(oidc_settings(), client=client)
    try:
        accepted_jwks = await provider._load_jwks()
    finally:
        await client.aclose()

    assert [key["kid"] for key in accepted_jwks["keys"]] == ["operator-test-key"]


def test_platform_oidc_missing_or_invalid_configuration_fails_closed() -> None:
    settings = Settings(_env_file=None)
    assert settings.platform_oidc_configured is False
    provider = OIDCPlatformIdentityProvider(settings)

    with pytest.raises(PlatformIdentityNotConfigured):
        asyncio.run(
            provider.validate_identity_token(
                "unused", expected_nonce="nonce-for-operator-login-that-is-long-enough"
            )
        )
    asyncio.run(provider.aclose())

    with pytest.raises(ValidationError):
        Settings(_env_file=None, platform_oidc_issuer="https://identity.example.test")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            platform_oidc_issuer="https://identity.example.test",
            platform_oidc_client_id="client",
            platform_oidc_required_amr_values="mfa",
            platform_oidc_allowed_algorithms="none,HS256",
        )


def test_audit_and_outbox_are_written_in_same_transaction(isolated_session_factory) -> None:
    audit = AuditService()
    timestamp = datetime.now(UTC)
    with isolated_session_factory() as session, session.begin():
        recorded = audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="platform.operator.role_change",
                actor_category="platform_operator",
                actor_ref=str(uuid4()),
                action="operator.role.assign",
                outcome="success",
                target_entity_type="platform.role",
                target_entity_ref=str(uuid4()),
                request_id="test-request-01",
                decision_metadata={"reason_code": "approved_change", "policy_version": "v1"},
                occurred_at=timestamp,
            ),
        )
        event_row = session.get(AuditEvent, recorded.event_id)
        outbox_row = session.get(AuditOutboxEvent, recorded.outbox_event_id)
        assert event_row is not None
        assert outbox_row is not None
        assert outbox_row.status == "queued"
        assert outbox_row.idempotency_key == str(event_row.id)
        canonical = json.dumps(
            outbox_row.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        assert hashlib.sha256(canonical).hexdigest() == outbox_row.payload_sha256

    with isolated_session_factory() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.id == recorded.event_id))
        assert session.scalar(
            select(AuditOutboxEvent).where(AuditOutboxEvent.event_id == recorded.event_id)
        )


def test_audit_and_outbox_rollback_together(isolated_session_factory) -> None:
    audit = AuditService()
    event_id = None
    with pytest.raises(RuntimeError):
        with isolated_session_factory() as session, session.begin():
            recorded = audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type="platform.tenant.review",
                    actor_category="system",
                    actor_ref="system:review-worker",
                    action="tenant.review.record",
                    outcome="pending",
                ),
            )
            event_id = recorded.event_id
            raise RuntimeError("force transaction rollback")

    with isolated_session_factory() as session:
        assert session.get(AuditEvent, event_id) is None
        assert session.get(AuditOutboxEvent, event_id) is None


def test_audit_storage_failure_is_returned_to_calling_transaction(isolated_session_factory) -> None:
    with pytest.raises(IntegrityError):
        with isolated_session_factory() as session, session.begin():
            AuditService().record_mandatory(
                session,
                AuditEventDraft(
                    event_type="platform.tenant.review",
                    actor_category="system",
                    actor_ref="system:review-worker",
                    action="tenant.review.record",
                    outcome="pending",
                    tenant_id=uuid4(),
                ),
            )


@pytest.mark.parametrize(
    "metadata",
    [
        {"token": "bearer-secret"},
        {"reason_code": "user@example.test"},
        {"auth_strength": "person@example.test"},
    ],
)
def test_audit_rejects_sensitive_or_unregistered_metadata(isolated_session_factory, metadata) -> None:
    with isolated_session_factory() as session:
        with pytest.raises(AuditInputRejected):
            AuditService().record_mandatory(
                session,
                AuditEventDraft(
                    event_type="platform.operator.authentication",
                    actor_category="unauthenticated",
                    actor_ref=None,
                    action="operator.authenticate",
                    outcome="denied",
                    decision_metadata=metadata,
                ),
            )


class FakeAuditSink:
    configured = True
    supported = True

    def __init__(self, *, healthy: bool = True, result: str = "success") -> None:
        self.healthy = healthy
        self.result = result
        self.keys: list[str] = []
        self.accepted: set[str] = set()
        self.calls = 0
        self.now = datetime.now(UTC)

    async def check_health(self) -> bool:
        return self.healthy

    async def deliver(self, payload: dict[str, object], *, idempotency_key: str):
        self.calls += 1
        self.keys.append(idempotency_key)
        if self.result == "retry_once" and idempotency_key not in self.accepted:
            self.accepted.add(idempotency_key)
            raise TimeoutError
        if self.result == "retryable":
            raise RetryableAuditSinkError("sink_temporarily_unavailable")
        if self.result == "terminal":
            raise TerminalAuditSinkError("sink_rejected_event")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        return AuditAcknowledgement(
            event_id=UUID(payload["event_id"]),
            payload_sha256=digest,
            reference=f"receipt:{idempotency_key}",
            acknowledged_at=self.now,
            verified=self.result != "invalid_ack",
        )

    async def aclose(self) -> None:
        return None


def worker_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "test",
        "outbox_worker_enabled": True,
        "outbox_retry_base_seconds": 1,
        "outbox_retry_max_seconds": 5,
        "outbox_max_attempts": 3,
        "audit_sink_adapter": "test-contract",
        "audit_sink_endpoint": "https://audit.example.test/events",
        "audit_sink_auth_token": SecretStr("test-token-only"),
        "audit_sink_ack_verification_key": SecretStr("test-key-only"),
    }
    values.update(overrides)
    return Settings(**values)


def queue_audit_event(session_factory, now: datetime | None = None):
    occurred_at = now or datetime.now(UTC)
    with session_factory() as session, session.begin():
        return AuditService().record_mandatory(
            session,
            AuditEventDraft(
                event_type="platform.operator.authentication",
                actor_category="system",
                actor_ref="system:test-worker",
                action="operator.authenticate",
                outcome="success",
                occurred_at=occurred_at,
            ),
        )


def get_outbox_row(session_factory, event_id):
    with session_factory() as session:
        return session.get(AuditOutboxEvent, event_id)


def test_outbox_idempotent_delivery_and_acknowledgement(isolated_session_factory) -> None:
    now = datetime.now(UTC)
    recorded = queue_audit_event(isolated_session_factory, now)
    sink = FakeAuditSink()
    sink.now = now
    worker = AuditOutboxWorker(
        worker_settings(),
        sink,
        repository=SQLAlchemyOutboxRepository(isolated_session_factory),
        worker_id="worker-a",
        clock=lambda: now,
    )

    result = asyncio.run(worker.run_once(now=now))
    duplicate = asyncio.run(worker.run_once(now=now))
    row = get_outbox_row(isolated_session_factory, recorded.event_id)
    with isolated_session_factory() as session:
        attempts = list(session.scalars(select(AuditOutboxAttempt).where(
            AuditOutboxAttempt.event_id == recorded.event_id
        )))

    assert result.delivered == 1
    assert duplicate.claimed == 0
    assert sink.calls == 1
    assert row.status == "delivered"
    assert row.acknowledgement_ref == f"receipt:{recorded.event_id}"
    assert row.acknowledged_at.replace(tzinfo=UTC) == now
    assert len(attempts) == 1
    assert attempts[0].status == "delivered"


def test_outbox_retry_uses_stable_idempotency_key_after_timeout(isolated_session_factory) -> None:
    now = datetime.now(UTC)
    later = now + timedelta(seconds=10)
    recorded = queue_audit_event(isolated_session_factory, now)
    sink = FakeAuditSink(result="retry_once")
    sink.now = now
    clock = [now]
    worker = AuditOutboxWorker(
        worker_settings(),
        sink,
        repository=SQLAlchemyOutboxRepository(isolated_session_factory),
        worker_id="worker-retry",
        clock=lambda: clock[0],
    )

    failed = asyncio.run(worker.run_once(now=now))
    with isolated_session_factory() as session, session.begin():
        session.execute(
            update(AuditOutboxEvent)
            .where(AuditOutboxEvent.event_id == recorded.event_id)
            .values(next_attempt_at=later)
        )
    clock[0] = later
    sink.now = later
    recovered = asyncio.run(worker.run_once(now=later))

    row = get_outbox_row(isolated_session_factory, recorded.event_id)
    assert failed.retryable_failures == 1
    assert recovered.delivered == 1
    assert sink.calls == 2
    assert sink.keys == [str(recorded.event_id), str(recorded.event_id)]
    assert row.status == "delivered"


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [("retryable", "retryable_failure"), ("terminal", "terminal_failure"), ("invalid_ack", "terminal_failure")],
)
def test_outbox_classifies_sink_failures(isolated_session_factory, result, expected_status) -> None:
    now = datetime.now(UTC)
    recorded = queue_audit_event(isolated_session_factory, now)
    sink = FakeAuditSink(result=result)
    sink.now = now
    worker = AuditOutboxWorker(
        worker_settings(),
        sink,
        repository=SQLAlchemyOutboxRepository(isolated_session_factory),
        worker_id="worker-failure",
        clock=lambda: now,
    )

    asyncio.run(worker.run_once(now=now))
    row = get_outbox_row(isolated_session_factory, recorded.event_id)
    with isolated_session_factory() as session:
        attempt = session.scalar(
            select(AuditOutboxAttempt).where(AuditOutboxAttempt.event_id == recorded.event_id)
        )

    assert row.status == expected_status
    assert attempt.status == expected_status
    assert attempt.completed_at is not None


def test_missing_or_unverified_sink_leaves_event_queued(isolated_session_factory) -> None:
    recorded = queue_audit_event(isolated_session_factory)
    disabled = Settings(_env_file=None, environment="test")
    worker = AuditOutboxWorker(
        disabled,
        UnavailableAuditSink(),
        repository=SQLAlchemyOutboxRepository(isolated_session_factory),
    )
    with pytest.raises(AuditSinkNotConfigured):
        asyncio.run(worker.run_once())

    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", outbox_worker_enabled=True)
    row = get_outbox_row(isolated_session_factory, recorded.event_id)
    assert row.status == "queued"
    assert row.attempt_count == 0


def test_configured_but_unhealthy_sink_does_not_claim_events(isolated_session_factory) -> None:
    recorded = queue_audit_event(isolated_session_factory)
    sink = FakeAuditSink(healthy=False)
    worker = AuditOutboxWorker(
        worker_settings(),
        sink,
        repository=SQLAlchemyOutboxRepository(isolated_session_factory),
        worker_id="worker-unhealthy-sink",
    )
    with pytest.raises(AuditSinkNotConfigured):
        asyncio.run(worker.run_once())
    row = get_outbox_row(isolated_session_factory, recorded.event_id)
    assert row.status == "queued"
    assert row.attempt_count == 0


@pytest.mark.anyio
async def test_readiness_reports_controls_without_claiming_liveness_is_provisioning_ready(
    monkeypatch,
) -> None:
    settings = oidc_settings(
        audit_sink_adapter="unselected-provider",
        audit_sink_endpoint="https://audit.example.test/events",
        audit_sink_auth_token=SecretStr("test-token-only"),
        audit_sink_ack_verification_key=SecretStr("test-key-only"),
    )
    monkeypatch.setattr("app.api.v1.health.check_database_connectivity", lambda: None)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health")
        ready = await client.get("/health/ready")
        versioned_ready = await client.get("/api/v1/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.json()["checks"]["database_verified"] is True
    assert ready.json()["checks"]["operator_identity_configured"] is True
    assert ready.json()["checks"]["operator_identity_verified"] is False
    assert ready.json()["checks"]["external_audit_delivery_configured"] is True
    assert ready.json()["checks"]["external_audit_delivery_verified"] is False
    assert ready.json()["checks"]["privileged_provisioning_eligible"] is False
    assert versioned_ready.status_code == 503
    assert "test-token-only" not in ready.text
    assert "audit.example.test" not in ready.text


def test_expired_worker_lease_is_recovered_after_restart(isolated_session_factory) -> None:
    now = datetime.now(UTC)
    recovered_at = now + timedelta(seconds=61)
    recorded = queue_audit_event(isolated_session_factory, now)
    repository = SQLAlchemyOutboxRepository(isolated_session_factory)
    assert len(
        repository.claim_batch(
            now=now, worker_id="crashed-worker", limit=1, lease_seconds=60
        )
    ) == 1
    sink = FakeAuditSink()
    sink.now = recovered_at
    worker = AuditOutboxWorker(
        worker_settings(),
        sink,
        repository=repository,
        worker_id="recovery-worker",
        clock=lambda: recovered_at,
    )

    result = asyncio.run(worker.run_once(now=recovered_at))
    with isolated_session_factory() as session:
        attempts = list(session.scalars(
            select(AuditOutboxAttempt)
            .where(AuditOutboxAttempt.event_id == recorded.event_id)
            .order_by(AuditOutboxAttempt.attempt_number)
        ))

    assert result.delivered == 1
    assert [attempt.status for attempt in attempts] == ["retryable_failure", "delivered"]
    assert attempts[0].error_code == "worker_lease_expired"


def test_future_worker_heartbeat_does_not_satisfy_readiness_or_activation(
    isolated_session_factory,
) -> None:
    now = datetime.now(UTC)
    future = now + timedelta(minutes=5)
    with isolated_session_factory.begin() as session:
        session.add(
            AuditOutboxWorkerHeartbeat(
                worker_id="worker-clock-skew-test",
                adapter_name="test-contract",
                last_seen_at=future,
                sink_verified_at=future,
            )
        )

    outbox_repository = SQLAlchemyOutboxRepository(isolated_session_factory)
    assert not outbox_repository.has_recent_verified_worker(
        now=now,
        ttl_seconds=60,
        adapter_name="test-contract",
    )
    with isolated_session_factory() as session:
        onboarding_repository = TenantOnboardingRepository(session)
        assert not onboarding_repository.has_recent_verified_worker(
            adapter_name="test-contract",
            cutoff=now - timedelta(seconds=60),
            current_time=now,
        )


def test_platform_operator_authentication_is_oidc_mapped_and_audited(isolated_session_factory) -> None:
    operator_id = uuid4()
    with isolated_session_factory() as session, session.begin():
        session.add(
            PlatformOperator(
                id=operator_id,
                issuer="https://identity.example.test/issuer",
                subject="operator-subject-42",
                status="active",
            )
        )

    class Provider:
        async def validate_identity_token(self, token, *, expected_nonce):
            assert token == "already-validated-oidc-token"
            assert expected_nonce == "nonce-for-operator-login-that-is-long-enough"
            return ExternalOperatorIdentity(
                issuer="https://identity.example.test/issuer",
                subject="operator-subject-42",
                audience="platform-client",
                auth_time=datetime.now(UTC),
                acr=None,
                amr=("mfa",),
            )

        async def check_provider(self):
            return True

        async def aclose(self):
            return None

    service = PlatformOperatorAuthenticationService(
        Provider(), session_factory=isolated_session_factory
    )
    principal = asyncio.run(
        service.authenticate(
            "already-validated-oidc-token",
            expected_nonce="nonce-for-operator-login-that-is-long-enough",
            request_id="request-operator-auth-1",
        )
    )

    assert principal.operator_id == operator_id
    with isolated_session_factory() as session:
        record = session.scalar(select(AuditEvent))
        assert record.event_type == "platform.operator.authentication"
        assert record.actor_ref == str(operator_id)
        assert record.outcome == "success"


def test_tenant_role_does_not_authorize_platform_permission(isolated_session_factory) -> None:
    now = datetime.now(UTC)
    tenant_id, user_id, membership_id, role_id = uuid4(), uuid4(), uuid4(), uuid4()
    operator_id = uuid4()
    with isolated_session_factory() as session, session.begin():
        session.add_all(
            [
                Tenant(id=tenant_id, slug="community-a", status="active"),
                User(id=user_id, status="active"),
                PlatformOperator(
                    id=operator_id,
                    issuer="https://identity.example.test/issuer",
                    subject="operator-subject-42",
                    status="active",
                ),
            ]
        )
        session.flush()
        session.add(
            TenantMembership(
                id=membership_id,
                tenant_id=tenant_id,
                user_id=user_id,
                mobile_e164="+14155550123",
                status="active",
            )
        )
        session.flush()
        session.add(
            Role(
                id=role_id,
                tenant_id=tenant_id,
                code="community_admin",
                display_name="Community Admin",
                status="active",
            )
        )
        session.add(Permission(code="audit.read", description="Tenant audit read"))
        session.flush()
        session.add_all(
            [
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=role_id,
                    permission_code="audit.read",
                    status="active",
                ),
                UserRoleAssignment(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    membership_id=membership_id,
                    role_id=role_id,
                    assigned_by_membership_id=membership_id,
                    status="active",
                    valid_from=now - timedelta(minutes=1),
                ),
            ]
        )

    principal = PlatformPrincipal(
        operator_id=operator_id,
        issuer="https://identity.example.test/issuer",
        subject="operator-subject-42",
        authenticated_at=now,
        acr=None,
        amr=("mfa",),
    )
    authorization = PlatformAuthorizationService(
        SQLAlchemyPlatformIdentityRepository(isolated_session_factory),
        session_factory=isolated_session_factory,
    )
    with pytest.raises(PlatformOperatorNotAuthorized):
        authorization.require(principal, "platform.audit.read", now=now)


def test_platform_permissions_resolve_from_platform_tables_only(isolated_session_factory) -> None:
    now = datetime.now(UTC)
    operator_id, grantor_id, role_id = uuid4(), uuid4(), uuid4()
    with isolated_session_factory() as session, session.begin():
        session.add_all(
            [
                PlatformOperator(
                    id=operator_id,
                    issuer="https://identity.example.test/issuer",
                    subject="operator-subject-1",
                    status="active",
                ),
                PlatformOperator(
                    id=grantor_id,
                    issuer="https://identity.example.test/issuer",
                    subject="operator-subject-2",
                    status="active",
                ),
                PlatformPermission(code="platform.audit.read", description="Platform audit read"),
                PlatformRole(
                    id=role_id,
                    code="platform_auditor",
                    display_name="Platform Auditor",
                    status="active",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                PlatformRolePermission(
                    role_id=role_id,
                    permission_code="platform.audit.read",
                    status="active",
                ),
                PlatformOperatorRoleAssignment(
                    id=uuid4(),
                    operator_id=operator_id,
                    role_id=role_id,
                    assigned_by_operator_id=grantor_id,
                    status="active",
                    valid_from=now - timedelta(minutes=1),
                ),
            ]
        )

    principal = PlatformPrincipal(
        operator_id=operator_id,
        issuer="https://identity.example.test/issuer",
        subject="operator-subject-1",
        authenticated_at=now,
        acr=None,
        amr=("mfa",),
    )
    authorization = PlatformAuthorizationService(
        SQLAlchemyPlatformIdentityRepository(isolated_session_factory),
        session_factory=isolated_session_factory,
    )
    authorization.require(principal, "platform.audit.read", now=now)


def test_readiness_requires_review_activation_grants_and_distinct_operators(
    isolated_session_factory,
) -> None:
    repository = SQLAlchemyPlatformIdentityRepository(isolated_session_factory)
    now = datetime.now(UTC)
    permission_codes = (
        "platform.onboarding.create",
        "platform.onboarding.read",
        "platform.onboarding.cancel",
        "platform.domain.challenge",
        "platform.domain.verify",
        "platform.tenant.reject",
    )
    creator_id, reviewer_id, activation_id, grantor_id = [uuid4() for _ in range(4)]
    role_id = uuid4()
    with isolated_session_factory() as session, session.begin():
        session.add_all(
            PlatformOperator(
                id=operator_id,
                issuer="https://issuer.example.test",
                subject=str(operator_id),
                status="active",
            )
            for operator_id in (creator_id, reviewer_id, activation_id, grantor_id)
        )
        session.add(
            PlatformRole(
                id=role_id,
                code="readiness-operator",
                display_name="Readiness operator",
                status="active",
            )
        )
        for permission_code in permission_codes:
            session.add(PlatformPermission(code=permission_code, description=permission_code))
            session.add(
                PlatformRolePermission(
                    role_id=role_id,
                    permission_code=permission_code,
                    status="active",
                )
            )
        session.flush()
        for operator_id in (creator_id, reviewer_id, activation_id):
            session.add(
                PlatformOperatorRoleAssignment(
                    id=uuid4(),
                    operator_id=operator_id,
                    role_id=role_id,
                    assigned_by_operator_id=grantor_id,
                    status="active",
                    valid_from=now - timedelta(minutes=1),
                )
            )

    with isolated_session_factory() as session:
        assert repository.has_onboarding_authority(session, now=now) is False

    with isolated_session_factory() as session, session.begin():
        for permission_code in ("platform.tenant.approve", "platform.tenant.activate"):
            session.add(PlatformPermission(code=permission_code, description=permission_code))
            session.add(
                PlatformRolePermission(
                    role_id=role_id,
                    permission_code=permission_code,
                    status="active",
                )
            )

    with isolated_session_factory() as session:
        assert repository.has_onboarding_authority(session, now=now) is True

