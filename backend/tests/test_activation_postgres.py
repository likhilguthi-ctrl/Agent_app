from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import Settings
from app.db.base import Base
from app.modules.audit.models import AuditEvent
from app.modules.onboarding.activation import TenantActivationService
from app.modules.onboarding.errors import ActivationReviewConflict
from app.modules.onboarding.models import TenantOnboardingRequest, TenantOnboardingReview
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformPermission,
    PlatformRole,
    PlatformRolePermission,
)
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.modules.platform_identity.service import PlatformAuthorizationService, PlatformPrincipal
from app.modules.tenants.models import Tenant, TenantDomain


@pytest.fixture(scope="module")
def isolated_postgres_factory():
    configured_url = os.environ.get("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("TEST_DATABASE_URL is not configured for isolated PostgreSQL tests")
    url = make_url(configured_url)
    database_name = (url.database or "").lower()
    if (
        url.get_backend_name() != "postgresql"
        or "test" not in database_name
        or "bunny" in database_name
    ):
        pytest.skip("TEST_DATABASE_URL must point to a dedicated non-bunny PostgreSQL test database")

    admin_engine = create_engine(url)
    schema_name = f"activation_test_{uuid4().hex}"
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            actual_database = str(connection.scalar(text("SELECT current_database()"))).lower()
            if actual_database != database_name or "bunny" in actual_database or "test" not in actual_database:
                pytest.skip("Connected database name failed isolated PostgreSQL safeguards")
            connection.execute(CreateSchema(schema_name))
            schema_created = True

        isolated_engine = create_engine(
            url,
            connect_args={"options": f"-csearch_path={schema_name}"},
        )
        Base.metadata.create_all(isolated_engine)
        factory = sessionmaker(bind=isolated_engine, autoflush=False, expire_on_commit=False)
        yield factory
        Base.metadata.drop_all(isolated_engine)
        isolated_engine.dispose()
    finally:
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()


def test_postgresql_row_lock_allows_only_one_concurrent_review(isolated_postgres_factory):
    factory: sessionmaker[Session] = isolated_postgres_factory
    request_id = uuid4()
    tenant_id = uuid4()
    domain_id = uuid4()
    creator_id, first_reviewer_id, second_reviewer_id, grantor_id = [uuid4() for _ in range(4)]
    role_id = uuid4()
    now = datetime.now(UTC)
    domain_name = f"review-{uuid4().hex[:12]}.example"

    with factory.begin() as session:
        for operator_id, subject in (
            (creator_id, "creator"),
            (first_reviewer_id, "reviewer-a"),
            (second_reviewer_id, "reviewer-b"),
            (grantor_id, "grantor"),
        ):
            session.add(
                PlatformOperator(
                    id=operator_id,
                    issuer="https://issuer.example.test",
                    subject=subject,
                    status="active",
                )
            )
        session.add(Tenant(id=tenant_id, slug=f"review-{uuid4().hex[:12]}", status="inactive"))
        session.flush()
        session.add(
            TenantDomain(
                id=domain_id,
                tenant_id=tenant_id,
                hostname=domain_name,
                verification_status="verified",
                is_active=False,
                verified_at=now,
            )
        )
        session.add(
            TenantOnboardingRequest(
                id=request_id,
                tenant_id=tenant_id,
                tenant_domain_id=domain_id,
                created_by_operator_id=creator_id,
                normalized_domain=domain_name,
                state="PENDING_ACTIVATION_REVIEW",
                idempotency_key=f"request-{uuid4().hex[:12]}",
                expires_at=now + timedelta(hours=1),
            )
        )
        session.add(
            PlatformRole(id=role_id, code="activation-review-test", display_name="Activation review", status="active")
        )
        for permission_code in ("platform.tenant.approve", "platform.tenant.reject"):
            session.add(PlatformPermission(code=permission_code, description=permission_code))
            session.add(
                PlatformRolePermission(role_id=role_id, permission_code=permission_code, status="active")
            )
        session.flush()
        for operator_id in (first_reviewer_id, second_reviewer_id):
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

    settings = Settings(_env_file=None, environment="test")
    authorization = PlatformAuthorizationService(
        SQLAlchemyPlatformIdentityRepository(factory), session_factory=factory
    )
    service = TenantActivationService(
        settings,
        session_factory=factory,
        authorization=authorization,
        clock=lambda: now,
    )
    barrier = Barrier(2)

    def decide(operator_id, subject, decision, idempotency_key):
        principal = PlatformPrincipal(
            operator_id=operator_id,
            issuer="https://issuer.example.test",
            subject=subject,
            authenticated_at=now,
            acr="urn:test:mfa",
            amr=("mfa",),
        )
        barrier.wait(timeout=5)
        return service.submit_review(
            principal,
            request_id,
            decision=decision,
            reason_code="policy_review_passed" if decision == "approve" else "request_ineligible",
            idempotency_key=idempotency_key,
            request_id="postgres-review-race",
            now=now,
        )

    def attempt(*args):
        try:
            return decide(*args)
        except ActivationReviewConflict:
            return "conflict"
        except IntegrityError:
            return "constraint-conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            attempt,
            first_reviewer_id,
            "reviewer-a",
            "approve",
            "pg-approve-0001",
        )
        second = executor.submit(
            attempt,
            second_reviewer_id,
            "reviewer-b",
            "reject",
            "pg-reject-0001",
        )
        outcomes = (first.result(timeout=20), second.result(timeout=20))

    assert sum(outcome != "conflict" and outcome != "constraint-conflict" for outcome in outcomes) == 1
    with factory() as session:
        reviews = list(session.scalars(select(TenantOnboardingReview)))
        assert len(reviews) == 1
        assert session.scalar(select(TenantOnboardingRequest.state).where(TenantOnboardingRequest.id == request_id)) in {
            "PENDING_ACTIVATION_REVIEW",
            "REJECTED",
        }
        assert session.scalar(select(Tenant.status).where(Tenant.id == tenant_id)) == "inactive"
        assert session.scalar(select(TenantDomain.is_active).where(TenantDomain.id == domain_id)) is False
        assert session.scalar(select(AuditEvent.id).where(AuditEvent.id == reviews[0].audit_event_id)) is not None

