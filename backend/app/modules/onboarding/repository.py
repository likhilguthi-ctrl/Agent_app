from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditEvent
from app.modules.onboarding.models import (
    DomainVerificationChallenge,
    TenantActivationAttempt,
    TenantOnboardingRequest,
    TenantOnboardingReview,
)
from app.modules.outbox.models import AuditOutboxEvent, AuditOutboxWorkerHeartbeat
from app.modules.tenants.models import Tenant, TenantDomain


class TenantOnboardingRepository:
    """Queries for onboarding rows; transaction ownership stays with the service."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_request(self, request_id: UUID, *, lock: bool = False) -> TenantOnboardingRequest | None:
        statement = select(TenantOnboardingRequest).where(
            TenantOnboardingRequest.id == request_id
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def get_tenant(self, tenant_id: UUID, *, lock: bool = False) -> Tenant | None:
        if not lock:
            return self._session.get(Tenant, tenant_id)
        return self._session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )

    def get_domain(self, domain_id: UUID | None, *, lock: bool = False) -> TenantDomain | None:
        if domain_id is None:
            return None
        if not lock:
            return self._session.get(TenantDomain, domain_id)
        return self._session.scalar(
            select(TenantDomain).where(TenantDomain.id == domain_id).with_for_update()
        )

    def get_review(self, request_id: UUID, *, lock: bool = False) -> TenantOnboardingReview | None:
        statement = select(TenantOnboardingReview).where(
            TenantOnboardingReview.request_id == request_id
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def get_review_by_idempotency(
        self, request_id: UUID, idempotency_key: str
    ) -> TenantOnboardingReview | None:
        return self._session.scalar(
            select(TenantOnboardingReview).where(
                TenantOnboardingReview.request_id == request_id,
                TenantOnboardingReview.idempotency_key == idempotency_key,
            )
        )

    def get_activation_attempt_by_idempotency(
        self, request_id: UUID, idempotency_key: str
    ) -> TenantActivationAttempt | None:
        return self._session.scalar(
            select(TenantActivationAttempt).where(
                TenantActivationAttempt.request_id == request_id,
                TenantActivationAttempt.idempotency_key == idempotency_key,
            )
        )

    def get_activation_attempt(
        self, request_id: UUID, attempt_id: UUID, *, lock: bool = False
    ) -> TenantActivationAttempt | None:
        statement = select(TenantActivationAttempt).where(
            TenantActivationAttempt.request_id == request_id,
            TenantActivationAttempt.id == attempt_id,
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def pending_activation_attempt(
        self, request_id: UUID, *, lock: bool = False
    ) -> TenantActivationAttempt | None:
        statement = select(TenantActivationAttempt).where(
            TenantActivationAttempt.request_id == request_id,
            TenantActivationAttempt.status == "PENDING_AUDIT_ACK",
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def cancel_pending_activation_attempts(self, request_id: UUID) -> int:
        attempts = list(
            self._session.scalars(
                select(TenantActivationAttempt)
                .where(
                    TenantActivationAttempt.request_id == request_id,
                    TenantActivationAttempt.status == "PENDING_AUDIT_ACK",
                )
                .with_for_update()
            )
        )
        for attempt in attempts:
            attempt.status = "CANCELLED"
        return len(attempts)

    def audit_acknowledged(
        self,
        event_id: UUID,
        *,
        tenant_id: UUID,
        request_id: UUID,
        event_type: str,
        action: str,
        outcome: str,
        acknowledged_no_later_than: datetime,
    ) -> bool:
        return self._session.scalar(
            select(AuditOutboxEvent.event_id)
            .join(AuditEvent, AuditEvent.id == AuditOutboxEvent.event_id)
            .where(
                AuditOutboxEvent.event_id == event_id,
                AuditOutboxEvent.status == "delivered",
                AuditOutboxEvent.acknowledgement_ref.is_not(None),
                AuditOutboxEvent.acknowledged_at.is_not(None),
                AuditOutboxEvent.acknowledged_at <= acknowledged_no_later_than,
                AuditEvent.event_type == event_type,
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.target_entity_type == "platform.tenant_onboarding_request",
                AuditEvent.target_entity_ref == str(request_id),
                AuditEvent.action == action,
                AuditEvent.outcome == outcome,
            )
            .limit(1)
        ) is not None

    def has_recent_verified_worker(
        self, *, adapter_name: str, cutoff: datetime, current_time: datetime
    ) -> bool:
        return self._session.scalar(
            select(AuditOutboxWorkerHeartbeat.worker_id)
            .where(
                AuditOutboxWorkerHeartbeat.adapter_name == adapter_name,
                AuditOutboxWorkerHeartbeat.last_seen_at >= cutoff,
                AuditOutboxWorkerHeartbeat.last_seen_at <= current_time,
                AuditOutboxWorkerHeartbeat.sink_verified_at >= cutoff,
                AuditOutboxWorkerHeartbeat.sink_verified_at <= current_time,
            )
            .limit(1)
        ) is not None

    def find_idempotent_request(
        self, operator_id: UUID, idempotency_key: str
    ) -> TenantOnboardingRequest | None:
        return self._session.scalar(
            select(TenantOnboardingRequest).where(
                TenantOnboardingRequest.created_by_operator_id == operator_id,
                TenantOnboardingRequest.idempotency_key == idempotency_key,
            )
        )

    def find_domain(self, normalized_domain: str) -> TenantDomain | None:
        return self._session.scalar(
            select(TenantDomain).where(TenantDomain.hostname == normalized_domain)
        )

    def find_pending_domain_claim(
        self, normalized_domain: str, *, lock: bool = False
    ) -> TenantOnboardingRequest | None:
        statement = select(TenantOnboardingRequest).where(
                TenantOnboardingRequest.normalized_domain == normalized_domain,
                TenantOnboardingRequest.state.in_(
                    ("PENDING_DOMAIN_PROOF", "PENDING_ACTIVATION_REVIEW")
                ),
            )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def find_challenge_idempotency(
        self, request_id: UUID, idempotency_key: str
    ) -> DomainVerificationChallenge | None:
        return self._session.scalar(
            select(DomainVerificationChallenge).where(
                DomainVerificationChallenge.request_id == request_id,
                DomainVerificationChallenge.idempotency_key == idempotency_key,
            )
        )

    def current_challenge(
        self, request_id: UUID, *, lock: bool = False
    ) -> DomainVerificationChallenge | None:
        statement = (
            select(DomainVerificationChallenge)
            .where(
                DomainVerificationChallenge.request_id == request_id,
                DomainVerificationChallenge.status.in_(("active", "checking")),
            )
            .order_by(DomainVerificationChallenge.issued_at.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def get_challenge(
        self, challenge_id: UUID, *, lock: bool = False
    ) -> DomainVerificationChallenge | None:
        statement = select(DomainVerificationChallenge).where(
            DomainVerificationChallenge.id == challenge_id
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def latest_challenge(
        self, request_id: UUID, *, lock: bool = False
    ) -> DomainVerificationChallenge | None:
        statement = (
            select(DomainVerificationChallenge)
            .where(DomainVerificationChallenge.request_id == request_id)
            .order_by(DomainVerificationChallenge.issued_at.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def count_challenges(self, request_id: UUID) -> int:
        from sqlalchemy import func

        return int(
            self._session.scalar(
                select(func.count(DomainVerificationChallenge.id)).where(
                    DomainVerificationChallenge.request_id == request_id
                )
            )
            or 0
        )

    def count_recent_operator_events(
        self,
        *,
        operator_id: UUID,
        event_type: str,
        since: datetime,
    ) -> int:
        from sqlalchemy import func

        return int(
            self._session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.actor_category == "platform_operator",
                    AuditEvent.actor_ref == str(operator_id),
                    AuditEvent.event_type == event_type,
                    AuditEvent.occurred_at >= since,
                )
            )
            or 0
        )

    def list_requests(
        self, *, state: str | None, limit: int, offset: int
    ) -> list[tuple[TenantOnboardingRequest, Tenant]]:
        statement = (
            select(TenantOnboardingRequest, Tenant)
            .join(Tenant, Tenant.id == TenantOnboardingRequest.tenant_id)
            .order_by(TenantOnboardingRequest.created_at.desc(), TenantOnboardingRequest.id)
            .limit(limit)
            .offset(offset)
        )
        if state is not None:
            statement = statement.where(TenantOnboardingRequest.state == state)
        statement = statement.with_for_update(of=TenantOnboardingRequest)
        return list(self._session.execute(statement).all())

    def request_with_tenant(
        self, request_id: UUID
    ) -> tuple[TenantOnboardingRequest, Tenant] | None:
        row = self._session.execute(
            select(TenantOnboardingRequest, Tenant)
            .join(Tenant, Tenant.id == TenantOnboardingRequest.tenant_id)
            .where(TenantOnboardingRequest.id == request_id)
        ).one_or_none()
        return (row[0], row[1]) if row is not None else None

    def audit_history(self, request_id: UUID, *, limit: int = 100) -> list[AuditEvent]:
        return list(
            self._session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.target_entity_type == "platform.tenant_onboarding_request",
                    AuditEvent.target_entity_ref == str(request_id),
                )
                .order_by(AuditEvent.recorded_at, AuditEvent.id)
                .limit(limit)
            )
        )

    def due_requests(self, *, now: datetime, limit: int) -> list[TenantOnboardingRequest]:
        return list(
            self._session.scalars(
                select(TenantOnboardingRequest)
                .where(
                    TenantOnboardingRequest.state.in_(
                        ("PENDING_DOMAIN_PROOF", "PENDING_ACTIVATION_REVIEW")
                    ),
                    TenantOnboardingRequest.expires_at <= now,
                )
                .order_by(TenantOnboardingRequest.expires_at, TenantOnboardingRequest.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )

    def due_challenges(self, *, now: datetime, limit: int) -> list[DomainVerificationChallenge]:
        return list(
            self._session.scalars(
                select(DomainVerificationChallenge)
                .where(
                    DomainVerificationChallenge.status.in_(('active', 'checking')),
                    DomainVerificationChallenge.expires_at <= now,
                )
                .order_by(DomainVerificationChallenge.expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )

