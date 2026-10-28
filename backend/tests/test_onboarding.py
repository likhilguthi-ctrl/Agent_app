from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.tenant_context import TenantResolutionError
from app.db.base import Base
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditService
from app.modules.authentication.models import AuthSession, OtpChallenge, OtpRateBucket
from app.modules.authorization.models import (
    Permission,
    Role,
    RolePermission,
    UserRoleAssignment,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.onboarding.dns_verification import DNSLookupFailure, SystemDNSResolver
from app.modules.onboarding.activation import TenantActivationService
from app.modules.onboarding.errors import (
    ActivationBlocked,
    ActivationReviewConflict,
    ChallengeExpired,
    ChallengeIssueRateLimited,
    ChallengeReplay,
    DomainClaimUnavailable,
    DomainProofMismatch,
    DNSLookupUnavailable,
    OnboardingConflict,
    IdempotencyConflict,
    RequestCreatorCannotReject,
    VerificationInProgress,
    VerificationRateLimited,
)
from app.modules.onboarding.models import (
    DomainVerificationChallenge,
    TenantActivationAttempt,
    TenantOnboardingRequest,
    TenantOnboardingReview,
)
from app.modules.onboarding.provisioning import (
    CommunityAdminProvisioningUnavailable,
    UnavailableCommunityAdminProvisioner,
)
from app.modules.onboarding.api import (
    get_tenant_activation_service,
    get_tenant_onboarding_service,
)
from app.modules.onboarding.schemas import OnboardingRequestCreate, OnboardingReviewSubmission
from app.modules.onboarding.service import TenantOnboardingService
from app.modules.outbox.models import AuditOutboxAttempt, AuditOutboxEvent, AuditOutboxWorkerHeartbeat
from app.modules.platform_identity.dependencies import get_platform_operator_principal
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
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.modules.platform_identity.service import (
    PlatformAuthorizationService,
    PlatformPrincipal,
)
from app.modules.tenants.models import Tenant, TenantDomain
from app.modules.tenants.repository import SQLAlchemyTenantDomainRepository
from app.modules.tenants.service import resolve_tenant_context


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
PERMISSIONS = (
    "platform.onboarding.create",
    "platform.onboarding.read",
    "platform.onboarding.cancel",
    "platform.domain.challenge",
    "platform.domain.verify",
    "platform.tenant.reject",
    "platform.tenant.approve",
    "platform.tenant.activate",
)


class StaticTXTResolver:
    def __init__(self, values: tuple[str, ...] = (), error: Exception | None = None) -> None:
        self.values = values
        self.error = error
        self.calls = 0

    async def lookup_txt(self, owner_name: str, **_limits: object) -> tuple[str, ...]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.values


@dataclass
class World:
    factory: sessionmaker[Session]
    settings: Settings
    creator: PlatformPrincipal
    reviewer: PlatformPrincipal
    unprivileged: PlatformPrincipal

    def service(
        self,
        resolver: StaticTXTResolver | None = None,
        audit_service=None,
        clock=None,
        settings: Settings | None = None,
    ):
        return TenantOnboardingService(
            settings or self.settings,
            resolver=resolver or StaticTXTResolver(),
            session_factory=self.factory,
            authorization=PlatformAuthorizationService(
                SQLAlchemyPlatformIdentityRepository(self.factory),
                session_factory=self.factory,
                audit_service=audit_service or AuditService(),
            ),
            audit_service=audit_service or AuditService(),
            clock=clock or (lambda: NOW),
        )


@pytest.fixture
def world(tmp_path) -> World:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'onboarding.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    operator_ids = {name: uuid4() for name in ("creator", "reviewer", "grantor", "unprivileged")}
    role_id = uuid4()
    with factory.begin() as session:
        for name, operator_id in operator_ids.items():
            session.add(
                PlatformOperator(
                    id=operator_id,
                    issuer="https://issuer.example.test",
                    subject=f"operator:{name}",
                    status="active",
                )
            )
        session.add(
            PlatformRole(
                id=role_id,
                code="onboarding-review",
                display_name="Onboarding operator",
                status="active",
            )
        )
        for code in PERMISSIONS:
            session.add(PlatformPermission(code=code, description=f"Test permission {code}"))
            session.add(
                PlatformRolePermission(
                    role_id=role_id, permission_code=code, status="active"
                )
            )
        session.flush()
        for name in ("creator", "reviewer"):
            session.add(
                PlatformOperatorRoleAssignment(
                    id=uuid4(),
                    operator_id=operator_ids[name],
                    role_id=role_id,
                    assigned_by_operator_id=operator_ids["grantor"],
                    status="active",
                    valid_from=NOW - timedelta(days=1),
                )
            )

    def principal(name: str) -> PlatformPrincipal:
        return PlatformPrincipal(
            operator_id=operator_ids[name],
            issuer="https://issuer.example.test",
            subject=f"operator:{name}",
            authenticated_at=NOW,
            acr="urn:test:mfa",
            amr=("pwd", "mfa"),
            session_id=uuid4(),
            csrf_token="test-platform-csrf",
        )

    value = World(
        factory=factory,
        settings=Settings(
            _env_file=None,
            environment="test",
            frontend_origins="http://test",
            onboarding_request_ttl_seconds=3600,
            domain_challenge_ttl_seconds=300,
            domain_challenge_issue_limit=3,
            domain_challenge_verify_limit=3,
            domain_dns_query_timeout_seconds=0.5,
            domain_dns_max_records=10,
            domain_dns_max_response_bytes=1024,
            onboarding_expiry_batch_size=100,
        ),
        creator=principal("creator"),
        reviewer=principal("reviewer"),
        unprivileged=principal("unprivileged"),
    )
    try:
        yield value
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_request(world: World, *, slug: str = "north-coast", domain: str = "community.example"):
    body = OnboardingRequestCreate(tenant_slug=slug, domain=domain)
    return world.service().create_request(
        world.creator,
        tenant_slug=body.tenant_slug,
        normalized_domain=body.domain,
        idempotency_key=f"request-{slug}-001",
        request_id="test-onboarding-create",
    )[0]


def create_reviewable_request(
    world: World, *, slug: str = "review-community", domain: str = "review.example"
):
    request = create_request(world, slug=slug, domain=domain)
    onboarding = world.service()
    challenge = onboarding.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key=f"challenge-{slug}-001",
        request_id="activation-test-challenge",
    )
    return asyncio.run(
        world.service(StaticTXTResolver((challenge.dns_txt_value,))).verify_domain(
            world.creator, request.request_id, request_id="activation-test-domain-verify"
        )
    )


def activation_service(world: World, *, audit_service=None) -> TenantActivationService:
    audit = audit_service or AuditService()
    return TenantActivationService(
        world.settings,
        session_factory=world.factory,
        authorization=PlatformAuthorizationService(
            SQLAlchemyPlatformIdentityRepository(world.factory),
            session_factory=world.factory,
            audit_service=audit,
        ),
        audit_service=audit,
        clock=lambda: NOW,
    )


def read_rows(world: World):
    with world.factory() as session:
        return {
            "tenants": list(session.scalars(select(Tenant))),
            "requests": list(session.scalars(select(TenantOnboardingRequest))),
            "challenges": list(session.scalars(select(DomainVerificationChallenge))),
            "domains": list(session.scalars(select(TenantDomain))),
            "memberships": list(session.scalars(select(TenantMembership))),
            "role_assignments": list(session.scalars(select(UserRoleAssignment))),
            "audit": list(session.scalars(select(AuditEvent))),
            "outbox": list(session.scalars(select(AuditOutboxEvent))),
            "reviews": list(session.scalars(select(TenantOnboardingReview))),
            "activation_attempts": list(session.scalars(select(TenantActivationAttempt))),
        }


def test_authorized_request_creation_is_idempotent_and_creates_no_membership_or_role(world):
    service = world.service()
    body = OnboardingRequestCreate(tenant_slug="North-Coast", domain="COMMUNITY.example.")
    first, replayed = service.create_request(
        world.creator,
        tenant_slug=body.tenant_slug,
        normalized_domain=body.domain,
        idempotency_key="request-north-coast-001",
        request_id="req-create-1",
    )
    second, replayed_again = service.create_request(
        world.creator,
        tenant_slug=body.tenant_slug,
        normalized_domain=body.domain,
        idempotency_key="request-north-coast-001",
        request_id="req-create-2",
    )

    assert first.request_id == second.request_id
    assert replayed is False and replayed_again is True
    rows = read_rows(world)
    assert len(rows["tenants"]) == len(rows["requests"]) == 1
    assert rows["tenants"][0].status == "inactive"
    assert rows["requests"][0].state == "PENDING_DOMAIN_PROOF"
    assert rows["requests"][0].normalized_domain == "community.example"
    assert rows["memberships"] == []
    assert rows["role_assignments"] == []
    assert len(rows["audit"]) == len(rows["outbox"])
    assert {item.event_id for item in rows["outbox"]} == {item.id for item in rows["audit"]}

    with pytest.raises(IdempotencyConflict):
        service.create_request(
            world.creator,
            tenant_slug="different-community",
            normalized_domain="different.example",
            idempotency_key="request-north-coast-001",
            request_id="req-create-conflict",
        )


def test_tenant_role_does_not_grant_platform_onboarding_permission(world):
    tenant_id, user_id, membership_id, tenant_role_id = uuid4(), uuid4(), uuid4(), uuid4()
    with world.factory.begin() as session:
        session.add(Tenant(id=tenant_id, slug="unrelated-community", status="inactive"))
        session.add(User(id=user_id, status="active"))
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
            Permission(
                code="platform.onboarding.create",
                description="This tenant permission is deliberately unrelated.",
            )
        )
        session.add(
            Role(
                id=tenant_role_id,
                tenant_id=tenant_id,
                code="tenant-admin",
                display_name="Tenant administrator",
                status="active",
            )
        )
        session.flush()
        session.add(
            RolePermission(
                tenant_id=tenant_id,
                role_id=tenant_role_id,
                permission_code="platform.onboarding.create",
                status="active",
            )
        )
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_id,
                membership_id=membership_id,
                role_id=tenant_role_id,
                assigned_by_membership_id=membership_id,
                status="active",
                valid_from=NOW - timedelta(days=1),
            )
        )

    with pytest.raises(PlatformOperatorNotAuthorized):
        world.service().create_request(
            world.unprivileged,
            tenant_slug="new-community",
            normalized_domain="new.example",
            idempotency_key="request-tenant-role-001",
            request_id="req-tenant-role",
        )
    assert not read_rows(world)["requests"]


def test_missing_oidc_configuration_fails_closed():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(_env_file=None, environment="test"),
                platform_principal_resolver=object(),
            )
        )
    )

    async def invoke():
        with pytest.raises(PlatformIdentityNotConfigured):
            await get_platform_operator_principal(request)

    asyncio.run(invoke())


@pytest.mark.parametrize(
    "issuer,amr",
    [
        ("https://wrong-issuer.example", ("pwd", "mfa")),
        ("https://issuer.example.test", ("pwd",)),
    ],
)
def test_operator_principal_requires_configured_issuer_and_mfa(issuer, amr):
    settings = Settings(
        _env_file=None,
        environment="test",
        platform_oidc_issuer="https://issuer.example.test",
        platform_oidc_client_id="community-platform-test",
        platform_oidc_required_amr_values="mfa",
    )

    class TestResolver:
        async def resolve(self, _request):
            return PlatformPrincipal(
                operator_id=uuid4(),
                issuer=issuer,
                subject="operator:invalid-test",
                authenticated_at=datetime.now(UTC),
                acr=None,
                amr=amr,
            )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, platform_principal_resolver=TestResolver())
        )
    )

    async def invoke():
        with pytest.raises(InvalidPlatformIdentity):
            await get_platform_operator_principal(request)

    asyncio.run(invoke())


def test_api_uses_operator_dependency_and_marks_challenge_secret_no_store(world):
    class TestResolver:
        async def resolve(self, _request):
            return PlatformPrincipal(
                operator_id=world.creator.operator_id,
                issuer=world.creator.issuer,
                subject=world.creator.subject,
                authenticated_at=datetime.now(UTC),
                acr=world.creator.acr,
                amr=world.creator.amr,
                session_id=uuid4(),
                csrf_token="test-platform-csrf",
            )

    settings = Settings(
        _env_file=None,
        environment="test",
        frontend_origins="http://platform.test",
        platform_oidc_issuer="https://issuer.example.test",
        platform_oidc_client_id="community-platform-test",
        platform_oidc_required_amr_values="mfa",
    )
    app = create_app(settings, platform_principal_resolver=TestResolver())
    app.dependency_overrides[get_tenant_onboarding_service] = lambda: world.service()

    async def invoke():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
        ) as client:
            created = await client.post(
                "/api/v1/platform/onboarding-requests",
                json={"tenant_slug": "api-community", "domain": "api.example"},
                headers={
                    "Idempotency-Key": "request-api-0001",
                    "Origin": "http://platform.test",
                    "X-CSRF-Token": "test-platform-csrf",
                },
            )
            assert created.status_code == 201, created.text
            request_id = created.json()["request"]["request_id"]
            challenge = await client.post(
                f"/api/v1/platform/onboarding-requests/{request_id}/domains/challenge",
                headers={
                    "Idempotency-Key": "challenge-api-0001",
                    "Origin": "http://platform.test",
                    "X-CSRF-Token": "test-platform-csrf",
                },
            )
            assert challenge.status_code == 200
            assert challenge.headers["cache-control"] == "no-store, max-age=0"
            assert challenge.json()["dns_txt_value"].startswith(
                "community-ott-domain-verification="
            )

    asyncio.run(invoke())
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "slug,domain",
    [
        ("bad slug", "community.example"),
        ("valid", "https://community.example/path"),
        ("valid", "127.0.0.1"),
        ("valid", "singlelabel"),
        ("valid", "bad..example"),
        ("valid", "community.example:443"),
    ],
)
def test_malformed_slug_or_domain_is_rejected(slug, domain):
    with pytest.raises(ValueError):
        OnboardingRequestCreate(tenant_slug=slug, domain=domain)


def test_hostname_normalization_and_duplicate_domain_claim_are_safe(world):
    service = world.service()
    body = OnboardingRequestCreate(tenant_slug="A", domain="BÜCHER.example.")
    created, _ = service.create_request(
        world.creator,
        tenant_slug=body.tenant_slug,
        normalized_domain=body.domain,
        idempotency_key="request-domain-001",
        request_id="req-domain-1",
    )
    assert created.domain == "xn--bcher-kva.example"

    with pytest.raises(DomainClaimUnavailable):
        service.create_request(
            world.reviewer,
            tenant_slug="another",
            normalized_domain="XN--BCHER-KVA.EXAMPLE",
            idempotency_key="request-domain-002",
            request_id="req-domain-2",
        )
    assert len(read_rows(world)["requests"]) == 1


def test_pending_domain_partial_unique_index_rejects_a_competing_claim(world):
    create_request(world)
    with world.factory() as session:
        source = session.scalar(select(TenantOnboardingRequest))
        tenant_id = uuid4()
        session.add(Tenant(id=tenant_id, slug="competing", status="inactive"))
        session.flush()
        session.add(
            TenantOnboardingRequest(
                id=uuid4(),
                tenant_id=tenant_id,
                created_by_operator_id=world.reviewer.operator_id,
                normalized_domain=source.normalized_domain,
                state="PENDING_DOMAIN_PROOF",
                idempotency_key="direct-competing-claim",
                expires_at=NOW + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_challenge_is_high_entropy_hash_only_and_idempotency_never_replays_secret(world):
    request = create_request(world)
    service = world.service()
    issued = service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-issue-001",
        request_id="req-challenge-issue",
    )
    raw_value = issued.dns_txt_value.removeprefix("community-ott-domain-verification=")
    replay = service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-issue-001",
        request_id="req-challenge-replay",
    )
    assert len(raw_value) == 43
    assert issued.dns_txt_name == "_community-ott-verification.community.example."
    assert issued.dns_txt_value is not None and replay.dns_txt_value is None
    rows = read_rows(world)
    assert len(rows["challenges"]) == 1
    assert len(rows["challenges"][0].challenge_hash) == 64
    assert raw_value not in rows["challenges"][0].challenge_hash
    assert all(raw_value not in str(item.payload) for item in rows["outbox"])
    assert all(raw_value not in str(item.decision_metadata) for item in rows["audit"])


def test_challenge_issuance_limit_is_enforced_and_audited(world):
    request = create_request(world)
    service = world.service()
    for index in range(world.settings.domain_challenge_issue_limit):
        service.issue_challenge(
            world.creator,
            request.request_id,
            idempotency_key=f"challenge-limit-{index:03d}",
            request_id=f"req-challenge-limit-{index}",
        )
    with pytest.raises(ChallengeIssueRateLimited):
        service.issue_challenge(
            world.creator,
            request.request_id,
            idempotency_key="challenge-limit-overflow",
            request_id="req-challenge-limit-overflow",
        )
    assert len(read_rows(world)["challenges"]) == world.settings.domain_challenge_issue_limit


def test_operator_issue_window_cannot_be_bypassed_with_multiple_requests(world):
    first = create_request(world, slug="issue-first", domain="issue-first.example")
    second = world.service().create_request(
        world.creator,
        tenant_slug="issue-second",
        normalized_domain="issue-second.example",
        idempotency_key="request-issue-second-001",
        request_id="req-issue-second",
    )[0]
    limited_settings = world.settings.model_copy(
        update={"domain_challenge_issue_operator_limit": 1}
    )
    service = world.service(settings=limited_settings)
    service.issue_challenge(
        world.creator,
        first.request_id,
        idempotency_key="challenge-operator-issue-1",
        request_id="req-operator-issue-1",
    )
    with pytest.raises(ChallengeIssueRateLimited):
        service.issue_challenge(
            world.creator,
            second.request_id,
            idempotency_key="challenge-operator-issue-2",
            request_id="req-operator-issue-2",
        )


def test_exact_txt_proof_moves_only_to_review_and_tenant_resolver_still_denies(world):
    request = create_request(world)
    service = world.service()
    challenge = service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-correct-001",
        request_id="req-challenge-correct",
    )
    resolver = StaticTXTResolver((challenge.dns_txt_value,))
    result = asyncio.run(
        world.service(resolver).verify_domain(
            world.creator, request.request_id, request_id="req-domain-verify"
        )
    )

    assert result.state.value == "PENDING_ACTIVATION_REVIEW"
    assert result.domain_verified is True
    rows = read_rows(world)
    assert rows["tenants"][0].status == "inactive"
    assert rows["domains"][0].verification_status == "verified"
    assert rows["domains"][0].is_active is False
    assert rows["challenges"][0].status == "consumed"
    assert rows["memberships"] == [] and rows["role_assignments"] == []
    with world.factory() as session:
        repository = SQLAlchemyTenantDomainRepository(session)
        with pytest.raises(TenantResolutionError):
            resolve_tenant_context("community.example", repository)
    assert len(rows["audit"]) == len(rows["outbox"])


@pytest.mark.parametrize(
    "records",
    [
        (),
        ("community-ott-domain-verification=unrelated",),
    ],
)
def test_incorrect_or_partial_txt_values_do_not_verify(world, records):
    request = create_request(world)
    service = world.service()
    challenge = service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-partial-001",
        request_id="req-challenge-partial",
    )
    if records == ():
        # Even the correct token embedded in a longer TXT value is not a match.
        records = (f"{challenge.dns_txt_value}-suffix",)
    with pytest.raises(DomainProofMismatch):
        asyncio.run(
            world.service(StaticTXTResolver(records)).verify_domain(
                world.creator, request.request_id, request_id="req-partial-proof"
            )
        )
    rows = read_rows(world)
    assert rows["requests"][0].state == "PENDING_DOMAIN_PROOF"
    assert rows["domains"] == []
    assert rows["challenges"][0].status == "active"
    assert any(item.outcome == "denied" for item in rows["audit"])


def test_dns_timeout_is_retryable_and_never_counts_as_proof(world):
    request = create_request(world)
    service = world.service()
    service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-timeout-001",
        request_id="req-challenge-timeout",
    )
    with pytest.raises(DNSLookupUnavailable):
        asyncio.run(
            world.service(StaticTXTResolver(error=TimeoutError())).verify_domain(
                world.creator, request.request_id, request_id="req-dns-timeout"
            )
        )
    rows = read_rows(world)
    assert rows["requests"][0].state == "PENDING_DOMAIN_PROOF"
    assert rows["domains"] == []
    assert rows["challenges"][0].status == "active"


def test_verification_attempt_limit_locks_the_challenge(world):
    request = create_request(world)
    service = world.service()
    service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-attempt-limit-001",
        request_id="req-attempt-limit-issue",
    )
    for index in range(world.settings.domain_challenge_verify_limit):
        with pytest.raises(DomainProofMismatch):
            asyncio.run(
                world.service(StaticTXTResolver(("unrelated=wrong",))).verify_domain(
                    world.creator,
                    request.request_id,
                    request_id=f"req-attempt-limit-{index}",
                )
            )
    with pytest.raises(VerificationRateLimited):
        asyncio.run(
            service.verify_domain(
                world.creator,
                request.request_id,
                request_id="req-attempt-limit-overflow",
            )
        )
    assert read_rows(world)["challenges"][0].status == "locked"


def test_operator_verification_window_caps_dns_queries_across_requests(world):
    first = create_request(world, slug="verify-first", domain="verify-first.example")
    second = world.service().create_request(
        world.creator,
        tenant_slug="verify-second",
        normalized_domain="verify-second.example",
        idempotency_key="request-verify-second-001",
        request_id="req-verify-second",
    )[0]
    regular_service = world.service()
    for index, request in enumerate((first, second)):
        regular_service.issue_challenge(
            world.creator,
            request.request_id,
            idempotency_key=f"challenge-operator-verify-{index}",
            request_id=f"req-operator-verify-issue-{index}",
        )
    limited_settings = world.settings.model_copy(
        update={"domain_challenge_verify_operator_limit": 1}
    )
    limited_service = world.service(settings=limited_settings)
    with pytest.raises(DomainProofMismatch):
        asyncio.run(
            limited_service.verify_domain(
                world.creator,
                first.request_id,
                request_id="req-operator-verify-1",
            )
        )
    blocked_resolver = StaticTXTResolver(("must-not-query.example",))
    with pytest.raises(VerificationRateLimited):
        asyncio.run(
            world.service(blocked_resolver, settings=limited_settings).verify_domain(
                world.creator,
                second.request_id,
                request_id="req-operator-verify-2",
            )
        )
    assert blocked_resolver.calls == 0


def test_challenge_expiry_and_successful_challenge_replay_are_audited(world):
    request = create_request(world)
    service = world.service()
    issued = service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-expiry-001",
        request_id="req-challenge-expiry",
        now=NOW,
    )
    later = NOW + timedelta(minutes=6)
    later_service = world.service(clock=lambda: later)
    with pytest.raises(ChallengeExpired):
        asyncio.run(
            later_service.verify_domain(
                world.creator,
                request.request_id,
                request_id="req-expired-proof",
                now=later,
            )
        )
    rows = read_rows(world)
    assert rows["challenges"][0].status == "expired"
    assert rows["requests"][0].state == "PENDING_DOMAIN_PROOF"

    # A new challenge may be issued within the configured per-request issue limit.
    next_challenge = later_service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-replay-002",
        request_id="req-challenge-replay-2",
        now=later,
    )
    resolver = StaticTXTResolver((next_challenge.dns_txt_value,))
    asyncio.run(
        world.service(resolver, clock=lambda: later).verify_domain(
            world.creator,
            request.request_id,
            request_id="req-proof-success",
            now=later,
        )
    )
    with pytest.raises(ChallengeReplay):
        asyncio.run(
            later_service.verify_domain(
                world.creator,
                request.request_id,
                request_id="req-proof-replay",
                now=later,
            )
        )
    assert issued.dns_txt_value is not None
    assert any(
        event.decision_metadata.get("reason_code") == "challenge_replay"
        for event in read_rows(world)["audit"]
    )


def test_concurrent_challenge_consumption_allows_only_one_dns_lookup(world):
    request = create_request(world)
    issuance = world.service().issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-race-001",
        request_id="req-race-issue",
    )

    class BlockingResolver(StaticTXTResolver):
        def __init__(self):
            super().__init__((issuance.dns_txt_value,))
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def lookup_txt(self, owner_name: str, **limits: object) -> tuple[str, ...]:
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return self.values

    resolver = BlockingResolver()
    service = world.service(resolver)

    async def race():
        first = asyncio.create_task(
            service.verify_domain(world.creator, request.request_id, request_id="req-race-a")
        )
        await resolver.entered.wait()
        with pytest.raises(VerificationInProgress):
            await service.verify_domain(
                world.creator, request.request_id, request_id="req-race-b"
            )
        resolver.release.set()
        return await first

    result = asyncio.run(race())
    assert result.state.value == "PENDING_ACTIVATION_REVIEW"
    assert resolver.calls == 1


def test_cancellation_rejection_and_expiry_are_service_transitions(world):
    service = world.service()
    request = create_request(world, slug="cancel-me", domain="cancel.example")
    cancelled = service.cancel_request(
        world.creator, request.request_id, request_id="req-cancel"
    )
    assert cancelled.state.value == "CANCELLED"

    rejected_request = service.create_request(
        world.creator,
        tenant_slug="reject-me",
        normalized_domain="reject.example",
        idempotency_key="request-reject-me-001",
        request_id="req-reject-create",
    )[0]
    with pytest.raises(RequestCreatorCannotReject):
        service.reject_request(
            world.creator,
            rejected_request.request_id,
            reason_code="evidence_incomplete",
            request_id="req-self-reject",
        )
    rejected = service.reject_request(
        world.reviewer,
        rejected_request.request_id,
        reason_code="evidence_incomplete",
        request_id="req-review-reject",
    )
    assert rejected.state.value == "REJECTED"

    expiring = service.create_request(
        world.creator,
        tenant_slug="expire-me",
        normalized_domain="expire.example",
        idempotency_key="request-expire-me-001",
        request_id="req-expire-create",
        now=NOW,
    )[0]
    assert service.expire_due_requests(now=NOW + timedelta(hours=2)) >= 1
    rows = read_rows(world)
    state = next(row.state for row in rows["requests"] if row.id == expiring.request_id)
    assert state == "EXPIRED"
    assert all(tenant.status == "inactive" for tenant in rows["tenants"])


def test_audit_failure_rolls_back_request_and_tenant(world):
    class FailingAudit:
        def record_mandatory(self, *_args, **_kwargs):
            raise RuntimeError("local audit unavailable")

    with pytest.raises(RuntimeError, match="local audit unavailable"):
        world.service(audit_service=FailingAudit()).create_request(
            world.creator,
            tenant_slug="rollback-me",
            normalized_domain="rollback.example",
            idempotency_key="request-rollback-001",
            request_id="req-audit-rollback",
        )
    rows = read_rows(world)
    assert rows["tenants"] == []
    assert rows["requests"] == []
    assert rows["audit"] == []
    assert rows["outbox"] == []


def test_verified_domain_and_request_audit_outbox_are_same_transaction(world):
    request = create_request(world)
    issued = world.service().issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-consistency-001",
        request_id="req-consistency-issue",
    )
    result = asyncio.run(
        world.service(StaticTXTResolver((issued.dns_txt_value,))).verify_domain(
            world.creator, request.request_id, request_id="req-consistency-verify"
        )
    )
    assert result.state.value == "PENDING_ACTIVATION_REVIEW"
    rows = read_rows(world)
    assert len(rows["audit"]) == len(rows["outbox"])
    outbox_by_event = {item.event_id: item for item in rows["outbox"]}
    for event in rows["audit"]:
        record = outbox_by_event[event.id]
        assert record.payload["event_id"] == str(event.id)
    assert all(item.status == "queued" for item in rows["outbox"])
    assert all(item.acknowledgement_ref is None for item in rows["outbox"])


def test_successful_proof_rolls_back_when_mandatory_local_audit_fails(world):
    request = create_request(world)
    issue_service = world.service()
    issued = issue_service.issue_challenge(
        world.creator,
        request.request_id,
        idempotency_key="challenge-audit-gate-001",
        request_id="req-audit-gate-issue",
    )
    delegate = AuditService()

    class FailOnVerificationSuccess:
        def record_mandatory(self, session, draft):
            if draft.event_type == "platform.domain.verification.verified":
                raise RuntimeError("mandatory audit event unavailable")
            return delegate.record_mandatory(session, draft)

    with pytest.raises(RuntimeError, match="mandatory audit event unavailable"):
        asyncio.run(
            world.service(
                StaticTXTResolver((issued.dns_txt_value,)),
                audit_service=FailOnVerificationSuccess(),
            ).verify_domain(
                world.creator,
                request.request_id,
                request_id="req-audit-gate-proof",
            )
        )

    rows = read_rows(world)
    assert rows["requests"][0].state == "PENDING_DOMAIN_PROOF"
    assert rows["domains"] == []
    assert rows["challenges"][0].status == "checking"
    assert all(
        event.event_type != "platform.domain.verification.verified"
        for event in rows["audit"]
    )


def test_system_dns_adapter_bounds_txt_processing_and_maps_failures():
    class Record:
        def __init__(self, value: bytes):
            self.strings = (value,)

    class Answer:
        def __init__(self, records):
            self.rrset = records

    class Resolver:
        def __init__(self, result):
            self.result = result

        async def resolve(self, *_args, **_kwargs):
            if isinstance(self.result, Exception):
                raise self.result
            return Answer(self.result)

    owner = "_community-ott-verification.community.example."
    values = asyncio.run(
        SystemDNSResolver(Resolver([Record(b"proof=value")])).lookup_txt(
            owner,
            timeout_seconds=1.0,
            max_records=2,
            max_response_bytes=64,
        )
    )
    assert values == ("proof=value",)

    with pytest.raises(DNSLookupFailure):
        asyncio.run(
            SystemDNSResolver(Resolver([Record(b"a"), Record(b"b")])).lookup_txt(
                owner,
                timeout_seconds=1.0,
                max_records=1,
                max_response_bytes=64,
            )
        )
    with pytest.raises(DNSLookupFailure):
        asyncio.run(
            SystemDNSResolver(Resolver(TimeoutError())).lookup_txt(
                owner,
                timeout_seconds=1.0,
                max_records=1,
                max_response_bytes=64,
            )
        )


def test_request_creator_cannot_approve_and_tenant_roles_do_not_grant_platform_review(world):
    pending = create_reviewable_request(world)
    service = activation_service(world)

    from app.modules.onboarding.errors import RequestCreatorCannotReview

    with pytest.raises(RequestCreatorCannotReview):
        service.submit_review(
            world.creator,
            pending.request_id,
            decision="approve",
            reason_code="policy_review_passed",
            idempotency_key="review-self-approve-001",
            request_id="req-review-self",
        )

    with pytest.raises(PlatformOperatorNotAuthorized):
        service.submit_review(
            world.unprivileged,
            pending.request_id,
            decision="approve",
            reason_code="policy_review_passed",
            idempotency_key="review-no-grant-0001",
            request_id="req-review-unprivileged",
        )
    with pytest.raises(PlatformOperatorNotAuthorized):
        asyncio.run(
            activation_service(world).create_activation_intent(
                world.unprivileged,
                pending.request_id,
                idempotency_key="unprivileged-activate-001",
                request_id="req-activate-unprivileged",
            )
        )

    rows = read_rows(world)
    assert rows["reviews"] == []
    assert rows["requests"][0].state == "PENDING_ACTIVATION_REVIEW"
    assert rows["tenants"][0].status == "inactive"
    assert rows["domains"][0].is_active is False


def test_independent_review_is_append_only_idempotent_and_does_not_activate(world):
    pending = create_reviewable_request(world)
    service = activation_service(world)

    first = service.submit_review(
        world.reviewer,
        pending.request_id,
        decision="approve",
        reason_code="policy_review_passed",
        idempotency_key="review-approve-0001",
        request_id="req-review-approve",
    )
    replay = service.submit_review(
        world.reviewer,
        pending.request_id,
        decision="approve",
        reason_code="policy_review_passed",
        idempotency_key="review-approve-0001",
        request_id="req-review-approve-retry",
    )

    assert first.decision.value == "approve"
    assert first.replayed is False and replay.replayed is True
    assert first.request_state.value == "PENDING_ACTIVATION_REVIEW"
    with pytest.raises(ActivationReviewConflict):
        service.submit_review(
            world.reviewer,
            pending.request_id,
            decision="reject",
            reason_code="request_ineligible",
            idempotency_key="review-conflict-0001",
            request_id="req-review-conflict",
        )

    rows = read_rows(world)
    assert len(rows["reviews"]) == 1
    assert rows["reviews"][0].reviewer_operator_id == world.reviewer.operator_id
    assert rows["reviews"][0].requester_operator_id == world.creator.operator_id
    assert rows["requests"][0].state == "PENDING_ACTIVATION_REVIEW"
    assert rows["tenants"][0].status == "inactive"
    assert rows["domains"][0].is_active is False
    assert rows["memberships"] == []
    assert rows["role_assignments"] == []
    assert len(rows["audit"]) == len(rows["outbox"])
    assert rows["reviews"][0].audit_event_id in {event.event_id for event in rows["outbox"]}


def test_independent_rejection_records_decision_and_terminal_state(world):
    pending = create_reviewable_request(world)
    result = activation_service(world).submit_review(
        world.reviewer,
        pending.request_id,
        decision="reject",
        reason_code="request_ineligible",
        idempotency_key="review-reject-0001",
        request_id="req-review-reject",
    )
    rows = read_rows(world)

    assert result.decision.value == "reject"
    assert result.request_state.value == "REJECTED"
    assert rows["requests"][0].state == "REJECTED"
    assert rows["reviews"][0].decision == "reject"
    assert rows["tenants"][0].status == "inactive"
    assert rows["domains"][0].is_active is False


def test_activation_gates_fail_closed_without_oidc_sink_ack_postgres_or_worker(world):
    pending = create_reviewable_request(world)
    service = activation_service(world)
    service.submit_review(
        world.reviewer,
        pending.request_id,
        decision="approve",
        reason_code="policy_review_passed",
        idempotency_key="review-activation-0001",
        request_id="req-review-before-activation",
    )

    eligibility = asyncio.run(
        service.activation_eligibility(
            world.reviewer, pending.request_id, request_id="req-eligibility"
        )
    )
    assert eligibility.eligible is False
    assert {
        "OPERATOR_IDENTITY_UNAVAILABLE",
        "AUDIT_SINK_UNAVAILABLE",
        "POSTGRESQL_REQUIRED",
        "OUTBOX_WORKER_UNAVAILABLE",
        "APPROVAL_AUDIT_NOT_ACKNOWLEDGED",
    } <= set(eligibility.blockers)

    with pytest.raises(ActivationBlocked) as blocked:
        asyncio.run(
            service.create_activation_intent(
                world.reviewer,
                pending.request_id,
                idempotency_key="activation-intent-0001",
                request_id="req-create-activation-intent",
            )
        )
    assert "AUDIT_SINK_UNAVAILABLE" in blocked.value.blockers
    assert "APPROVAL_AUDIT_NOT_ACKNOWLEDGED" in blocked.value.blockers

    rows = read_rows(world)
    assert rows["activation_attempts"] == []
    assert rows["requests"][0].state == "PENDING_ACTIVATION_REVIEW"
    assert rows["tenants"][0].status == "inactive"
    assert rows["domains"][0].is_active is False
    assert rows["memberships"] == []
    assert rows["role_assignments"] == []


def test_review_audit_failure_rolls_back_review_decision(world):
    pending = create_reviewable_request(world)

    class FailingAudit:
        def record_mandatory(self, *_args, **_kwargs):
            raise RuntimeError("local audit unavailable")

    with pytest.raises(RuntimeError, match="local audit unavailable"):
        activation_service(world, audit_service=FailingAudit()).submit_review(
            world.reviewer,
            pending.request_id,
            decision="approve",
            reason_code="organization_verified",
            idempotency_key="review-audit-fails-01",
            request_id="req-review-audit-fails",
        )

    rows = read_rows(world)
    assert rows["reviews"] == []
    assert rows["requests"][0].state == "PENDING_ACTIVATION_REVIEW"
    assert rows["tenants"][0].status == "inactive"
    assert rows["domains"][0].is_active is False


def test_privileged_review_api_fails_closed_without_operator_resolver(world):
    app = create_app(settings=world.settings)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/api/v1/platform/onboarding-requests/{uuid4()}/review",
                headers={"Idempotency-Key": "review-api-no-auth-001"},
                json={"decision": "approve", "reason_code": "policy_review_passed"},
            )

    response = asyncio.run(request())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PLATFORM_AUTHENTICATION_UNAVAILABLE"


def test_community_admin_provisioning_boundary_fails_closed():
    provisioner = UnavailableCommunityAdminProvisioner()

    with pytest.raises(CommunityAdminProvisioningUnavailable):
        provisioner.provision_after_invitation_acceptance(
            request_id=uuid4(),
            tenant_id=uuid4(),
            nominee_identity_id=uuid4(),
            invitation_id=uuid4(),
        )


@pytest.mark.parametrize(
    "extra_field",
    ["reviewer_operator_id", "tenant_id", "permission", "domain_verified"],
)
def test_review_schema_rejects_client_supplied_authority(extra_field):
    body = {
        "decision": "approve",
        "reason_code": "organization_verified",
        extra_field: str(uuid4()),
    }
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OnboardingReviewSubmission.model_validate(body)


def test_reject_api_returns_review_contract(world):
    pending = create_reviewable_request(world)
    app = create_app(settings=world.settings)
    app.dependency_overrides[get_platform_operator_principal] = lambda: world.reviewer
    app.dependency_overrides[get_tenant_activation_service] = lambda: activation_service(world)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/api/v1/platform/onboarding-requests/{pending.request_id}/reject",
                headers={
                    "Idempotency-Key": "reject-api-review-001",
                    "Origin": "http://test",
                    "X-CSRF-Token": "test-platform-csrf",
                },
                json={"reason_code": "request_ineligible"},
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["decision"] == "reject"
    assert response.json()["request_state"] == "REJECTED"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://test"},
        {"Origin": "http://test", "X-CSRF-Token": "wrong-token"},
        {"Origin": "http://attacker.test", "X-CSRF-Token": "test-platform-csrf"},
    ],
)
def test_onboarding_mutation_rejects_invalid_csrf_or_origin(world, headers):
    pending = create_reviewable_request(world)
    app = create_app(settings=world.settings)
    app.dependency_overrides[get_platform_operator_principal] = lambda: world.reviewer
    app.dependency_overrides[get_tenant_activation_service] = lambda: activation_service(world)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/api/v1/platform/onboarding-requests/{pending.request_id}/reject",
                headers={
                    **headers,
                    "Idempotency-Key": "reject-api-csrf-check-001",
                },
                json={"reason_code": "request_ineligible"},
            )

    response = asyncio.run(request())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"
    assert response.headers["cache-control"] == "no-store"
    assert read_rows(world)["reviews"] == []
