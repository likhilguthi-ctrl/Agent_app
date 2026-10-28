from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from fastapi import Depends
from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.audit.models import AuditEvent
from app.modules.onboarding.models import TenantActivationAttempt, TenantOnboardingRequest
from app.modules.outbox.models import AuditOutboxEvent
from app.modules.tenants.models import Tenant, TenantDomain


@dataclass(frozen=True, slots=True)
class TenantDomainRecord:
    domain_id: UUID
    tenant_id: UUID
    tenant_slug: str
    hostname: str
    domain_is_active: bool
    verification_status: str
    tenant_status: str
    activation_outcome_acknowledged: bool = False


class TenantDomainLookup(Protocol):
    def find_by_hostname(self, hostname: str) -> TenantDomainRecord | None: ...


class SQLAlchemyTenantDomainRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_by_hostname(self, hostname: str) -> TenantDomainRecord | None:
        resolution_time = datetime.now(UTC)
        active_onboarding_request = (
            select(TenantOnboardingRequest.id)
            .where(
                TenantOnboardingRequest.tenant_id == TenantDomain.tenant_id,
                TenantOnboardingRequest.tenant_domain_id == TenantDomain.id,
                TenantOnboardingRequest.state == "ACTIVE",
            )
            .exists()
        )
        acknowledged_activation_outcome = (
            select(TenantActivationAttempt.id)
            .select_from(TenantOnboardingRequest)
            .join(
                TenantActivationAttempt,
                TenantActivationAttempt.request_id == TenantOnboardingRequest.id,
            )
            .join(
                AuditEvent,
                AuditEvent.id == TenantActivationAttempt.completion_audit_event_id,
            )
            .join(AuditOutboxEvent, AuditOutboxEvent.event_id == AuditEvent.id)
            .where(
                TenantOnboardingRequest.tenant_id == TenantDomain.tenant_id,
                TenantOnboardingRequest.tenant_domain_id == TenantDomain.id,
                TenantOnboardingRequest.state == "ACTIVE",
                TenantActivationAttempt.status == "ACTIVATED",
                TenantActivationAttempt.completion_audit_event_id.is_not(None),
                AuditEvent.event_type == "platform.tenant.activation.completed",
                AuditEvent.tenant_id == TenantDomain.tenant_id,
                AuditEvent.target_entity_type == "platform.tenant_onboarding_request",
                AuditEvent.target_entity_ref == cast(TenantOnboardingRequest.id, String),
                AuditEvent.action == "tenant.activate",
                AuditEvent.outcome == "success",
                AuditOutboxEvent.status == "delivered",
                AuditOutboxEvent.acknowledgement_ref.is_not(None),
                AuditOutboxEvent.acknowledged_at.is_not(None),
                AuditOutboxEvent.acknowledged_at <= resolution_time,
            )
            .exists()
        )
        activation_outcome_acknowledged = or_(
            ~active_onboarding_request,
            acknowledged_activation_outcome,
        ).label("activation_outcome_acknowledged")
        statement = (
            select(
                TenantDomain.id.label("domain_id"),
                TenantDomain.tenant_id.label("tenant_id"),
                Tenant.slug.label("tenant_slug"),
                TenantDomain.hostname.label("hostname"),
                TenantDomain.is_active.label("domain_is_active"),
                TenantDomain.verification_status.label("verification_status"),
                Tenant.status.label("tenant_status"),
                activation_outcome_acknowledged,
            )
            .join(Tenant, Tenant.id == TenantDomain.tenant_id)
            .where(TenantDomain.hostname == hostname)
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None
        return TenantDomainRecord(**row._mapping)


def get_tenant_domain_repository(
    session: Session = Depends(get_db),
) -> TenantDomainLookup:
    return SQLAlchemyTenantDomainRepository(session)
