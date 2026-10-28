from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.onboarding.errors import (
    ActivationBlocked,
    ActivationReviewConflict,
    IdempotencyConflict,
    IdempotencyKeyInvalid,
    OnboardingNotFound,
    RequestCreatorCannotReview,
)
from app.modules.onboarding.models import (
    TenantActivationAttempt,
    TenantOnboardingRequest,
    TenantOnboardingReview,
)
from app.modules.onboarding.repository import TenantOnboardingRepository
from app.modules.onboarding.schemas import (
    ActivationEligibility,
    ActivationIntentResult,
    OnboardingReviewResult,
    ReviewDecision,
    ReviewReason,
    TenantActivationResult,
    validate_idempotency_key,
)
from app.modules.outbox.sink import AuditSink
from app.modules.platform_identity.dependencies import PlatformPrincipalResolver
from app.modules.platform_identity.errors import PlatformOperatorNotAuthorized
from app.modules.platform_identity.provider import PlatformIdentityProvider
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.modules.platform_identity.service import (
    PlatformAuthorizationService,
    PlatformPrincipal,
)
from app.modules.tenants.models import Tenant, TenantDomain


_REVIEW_POLICY_VERSION = "tenant-activation-review-v1"
_SYSTEM_EXPIRY_ACTOR = "system:onboarding-expiry"
_REVIEW_REASONS = {
    ReviewDecision.APPROVE.value: {
        ReviewReason.ORGANIZATION_VERIFIED.value,
        ReviewReason.POLICY_REVIEW_PASSED.value,
    },
    ReviewDecision.REJECT.value: {
        ReviewReason.EVIDENCE_INCOMPLETE.value,
        ReviewReason.REQUEST_INELIGIBLE.value,
        ReviewReason.POLICY_REVIEW_FAILED.value,
    },
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ActivationFacts:
    request: TenantOnboardingRequest
    review: TenantOnboardingReview | None
    tenant: Tenant | None
    domain: TenantDomain | None
    attempt: TenantActivationAttempt | None


class TenantActivationPolicy:
    """Central fail-closed gates shared by eligibility and activation operations."""

    def __init__(
        self,
        settings: Settings,
        *,
        identity_provider: PlatformIdentityProvider | None,
        principal_resolver: PlatformPrincipalResolver | None,
        audit_sink: AuditSink | None,
        clock,
    ) -> None:
        self._settings = settings
        self._identity_provider = identity_provider
        self._principal_resolver = principal_resolver
        self._audit_sink = audit_sink
        self._clock = clock

    async def external_blockers(self, principal: PlatformPrincipal) -> list[str]:
        blockers: list[str] = []
        now = self._clock()
        settings = self._settings
        if (
            not settings.platform_oidc_configured
            or self._identity_provider is None
            or self._principal_resolver is None
            or principal.issuer != settings.platform_oidc_issuer
            or not principal.subject
            or principal.authenticated_at.tzinfo is None
            or principal.authenticated_at > now
            or (now - principal.authenticated_at).total_seconds()
            > settings.platform_oidc_max_auth_age_seconds
            or not set(settings.platform_oidc_required_amr_list).issubset(set(principal.amr))
            or (
                settings.platform_oidc_required_acr_list
                and principal.acr not in settings.platform_oidc_required_acr_list
            )
        ):
            blockers.append("OPERATOR_IDENTITY_UNAVAILABLE")
        elif not await self._provider_verified():
            blockers.append("OPERATOR_IDENTITY_UNAVAILABLE")

        sink = self._audit_sink
        if (
            not settings.audit_sink_configuration_complete
            or sink is None
            or not sink.configured
            or not sink.supported
            or not await self._sink_verified(sink)
        ):
            blockers.append("AUDIT_SINK_UNAVAILABLE")
        return blockers

    async def _provider_verified(self) -> bool:
        try:
            assert self._identity_provider is not None
            return bool(await self._identity_provider.check_provider())
        except Exception:
            return False

    @staticmethod
    async def _sink_verified(sink: AuditSink) -> bool:
        try:
            return bool(await sink.check_health())
        except Exception:
            return False

    def local_blockers(
        self,
        session: Session,
        facts: ActivationFacts,
        *,
        principal: PlatformPrincipal,
        now: datetime,
        require_intent_ack: bool,
    ) -> list[str]:
        repository = TenantOnboardingRepository(session)
        blockers: list[str] = []
        if session.get_bind().dialect.name != "postgresql":
            blockers.append("POSTGRESQL_REQUIRED")
        if not repository.has_recent_verified_worker(
            adapter_name=self._settings.audit_sink_adapter or "unconfigured",
            cutoff=now - timedelta(seconds=self._settings.outbox_worker_heartbeat_ttl_seconds),
            current_time=now,
        ):
            blockers.append("OUTBOX_WORKER_UNAVAILABLE")

        onboarding = facts.request
        if onboarding.state != "PENDING_ACTIVATION_REVIEW":
            blockers.append("REQUEST_NOT_REVIEWABLE")
        if _as_utc(onboarding.expires_at) <= now:
            blockers.append("REQUEST_EXPIRED")

        review = facts.review
        if review is None or review.decision != ReviewDecision.APPROVE.value:
            blockers.append("REVIEW_NOT_APPROVED")
        else:
            if review.reviewer_operator_id == principal.operator_id:
                blockers.append("REVIEWER_CANNOT_ACTIVATE")
            if not repository.audit_acknowledged(
                review.audit_event_id,
                tenant_id=onboarding.tenant_id,
                request_id=onboarding.id,
                event_type="platform.onboarding.review.decision",
                action="onboarding.review.submit",
                outcome="success",
                acknowledged_no_later_than=now,
            ):
                blockers.append("APPROVAL_AUDIT_NOT_ACKNOWLEDGED")

        tenant = facts.tenant
        domain = facts.domain
        if (
            onboarding.tenant_domain_id is None
            or tenant is None
            or domain is None
            or tenant.id != onboarding.tenant_id
            or domain.id != onboarding.tenant_domain_id
            or domain.tenant_id != onboarding.tenant_id
            or domain.hostname != onboarding.normalized_domain
        ):
            blockers.append("TENANT_DOMAIN_MISMATCH")
        elif domain.verification_status != "verified" or domain.verified_at is None:
            blockers.append("DOMAIN_NOT_VERIFIED")

        if tenant is not None and tenant.status != "inactive":
            blockers.append("TENANT_ALREADY_ACTIVE")
        if domain is not None and domain.is_active:
            blockers.append("DOMAIN_ALREADY_ACTIVE")

        if require_intent_ack:
            attempt = facts.attempt
            if attempt is None:
                blockers.append("ACTIVATION_ATTEMPT_NOT_FOUND")
            elif attempt.status == "CANCELLED":
                blockers.append("ACTIVATION_ATTEMPT_CANCELLED")
            elif attempt.status != "PENDING_AUDIT_ACK":
                blockers.append("ACTIVATION_ATTEMPT_NOT_PENDING")
            elif not repository.audit_acknowledged(
                attempt.intent_audit_event_id,
                tenant_id=onboarding.tenant_id,
                request_id=onboarding.id,
                event_type="platform.tenant.activation.intent",
                action="tenant.activation.intent",
                outcome="pending",
                acknowledged_no_later_than=now,
            ):
                blockers.append("ACTIVATION_INTENT_NOT_ACKNOWLEDGED")
        return list(dict.fromkeys(blockers))


class TenantActivationService:
    """Independent review and acknowledgement-gated tenant activation boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        authorization: PlatformAuthorizationService | None = None,
        audit_service: AuditService | None = None,
        identity_provider: PlatformIdentityProvider | None = None,
        principal_resolver: PlatformPrincipalResolver | None = None,
        audit_sink: AuditSink | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory or get_session_factory()
        self._audit = audit_service or AuditService()
        self._authorization = authorization or PlatformAuthorizationService(
            SQLAlchemyPlatformIdentityRepository(self._session_factory),
            session_factory=self._session_factory,
            audit_service=self._audit,
        )
        self._clock = clock
        self._policy = TenantActivationPolicy(
            settings,
            identity_provider=identity_provider,
            principal_resolver=principal_resolver,
            audit_sink=audit_sink,
            clock=clock,
        )

    def submit_review(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        decision: str,
        reason_code: str,
        idempotency_key: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> OnboardingReviewResult:
        current_time = now or self._clock()
        try:
            idempotency_key = validate_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise IdempotencyKeyInvalid from exc
        if decision not in _REVIEW_REASONS or reason_code not in _REVIEW_REASONS[decision]:
            raise ActivationReviewConflict
        permission = (
            "platform.tenant.approve"
            if decision == ReviewDecision.APPROVE.value
            else "platform.tenant.reject"
        )

        result: OnboardingReviewResult | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._authorization.authorize_in_transaction(
                session, principal, permission, request_id=request_id, now=current_time
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    failure = OnboardingNotFound()
                else:
                    existing_by_key = repository.get_review_by_idempotency(
                        onboarding.id, idempotency_key
                    )
                    if existing_by_key is not None:
                        if (
                            existing_by_key.reviewer_operator_id == principal.operator_id
                            and existing_by_key.decision == decision
                            and existing_by_key.reason_code == reason_code
                        ):
                            result = self._review_result(onboarding, existing_by_key, replayed=True)
                        else:
                            failure = IdempotencyConflict()
                    elif repository.get_review(onboarding.id, lock=True) is not None:
                        failure = ActivationReviewConflict()
                    elif _as_utc(onboarding.expires_at) <= current_time:
                        self._expire_request(session, onboarding, current_time, request_id)
                        failure = ActivationReviewConflict()
                    elif decision == ReviewDecision.APPROVE.value and (
                        onboarding.state != "PENDING_ACTIVATION_REVIEW"
                    ):
                        failure = ActivationReviewConflict()
                    elif onboarding.state not in {
                        "PENDING_DOMAIN_PROOF",
                        "PENDING_ACTIVATION_REVIEW",
                    }:
                        failure = ActivationReviewConflict()
                    elif onboarding.created_by_operator_id == principal.operator_id:
                        self._record_event(
                            session,
                            principal,
                            request=onboarding,
                            action="onboarding.review.submit",
                            outcome="denied",
                            reason_code="requester_cannot_review",
                            request_id=request_id,
                            occurred_at=current_time,
                        )
                        failure = RequestCreatorCannotReview()
                    else:
                        if decision == ReviewDecision.APPROVE.value:
                            domain = repository.get_domain(onboarding.tenant_domain_id)
                            tenant = repository.get_tenant(onboarding.tenant_id)
                            if (
                                onboarding.state != "PENDING_ACTIVATION_REVIEW"
                                or domain is None
                                or tenant is None
                                or domain.tenant_id != onboarding.tenant_id
                                or domain.hostname != onboarding.normalized_domain
                                or domain.verification_status != "verified"
                                or domain.verified_at is None
                                or domain.is_active
                                or tenant.status != "inactive"
                            ):
                                failure = ActivationReviewConflict()

                        if failure is None:
                            event = self._record_event(
                                session,
                                principal,
                                request=onboarding,
                                action="onboarding.review.submit",
                                outcome="success",
                                reason_code=reason_code,
                                request_id=request_id,
                                occurred_at=current_time,
                            )
                            review = TenantOnboardingReview(
                                id=uuid4(),
                                request_id=onboarding.id,
                                requester_operator_id=onboarding.created_by_operator_id,
                                reviewer_operator_id=principal.operator_id,
                                audit_event_id=event.event_id,
                                decision=decision,
                                reason_code=reason_code,
                                policy_version=_REVIEW_POLICY_VERSION,
                                idempotency_key=idempotency_key,
                                decision_at=current_time,
                            )
                            session.add(review)
                            if decision == ReviewDecision.REJECT.value:
                                onboarding.state = "REJECTED"
                                onboarding.version += 1
                                onboarding.updated_at = current_time
                                repository.cancel_pending_activation_attempts(onboarding.id)
                            session.flush()
                            result = self._review_result(onboarding, review, replayed=False)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def activation_eligibility(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime | None = None,
    ) -> ActivationEligibility:
        current_time = now or self._clock()
        external = await self._policy.external_blockers(principal)
        result: ActivationEligibility | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._authorization.authorize_in_transaction(
                session,
                principal,
                "platform.onboarding.read",
                request_id=request_id,
                now=current_time,
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid)
                if onboarding is None:
                    failure = OnboardingNotFound()
                else:
                    facts = self._facts(session, onboarding, attempt=None)
                    blockers = external + self._policy.local_blockers(
                        session,
                        facts,
                        principal=principal,
                        now=current_time,
                        require_intent_ack=False,
                    )
                    self._record_event(
                        session,
                        principal,
                        request=onboarding,
                        action="tenant.activation.eligibility.read",
                        outcome="success" if not blockers else "denied",
                        reason_code=None if not blockers else "activation_gate_closed",
                        request_id=request_id,
                        occurred_at=current_time,
                    )
                    result = ActivationEligibility(
                        request_id=onboarding.id,
                        eligible=not blockers,
                        blockers=sorted(set(blockers)),
                    )
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def create_activation_intent(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        idempotency_key: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> ActivationIntentResult:
        return await self._run_activation_step(
            principal,
            request_uuid,
            idempotency_key=idempotency_key,
            request_id=request_id,
            now=now,
            complete=False,
        )

    async def activate(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        attempt_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime | None = None,
    ) -> TenantActivationResult:
        return await self._run_activation_step(
            principal,
            request_uuid,
            attempt_uuid=attempt_uuid,
            request_id=request_id,
            now=now,
            complete=True,
        )

    async def _run_activation_step(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime | None,
        complete: bool,
        idempotency_key: str | None = None,
        attempt_uuid: UUID | None = None,
    ):
        current_time = now or self._clock()
        if not complete:
            try:
                idempotency_key = validate_idempotency_key(idempotency_key or "")
            except ValueError as exc:
                raise IdempotencyKeyInvalid from exc
        external_blockers = await self._policy.external_blockers(principal)
        result: ActivationIntentResult | TenantActivationResult | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._authorization.authorize_in_transaction(
                session,
                principal,
                "platform.tenant.activate",
                request_id=request_id,
                now=current_time,
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    failure = OnboardingNotFound()
                elif not complete:
                    existing = repository.get_activation_attempt_by_idempotency(
                        onboarding.id, idempotency_key or ""
                    )
                    if existing is not None:
                        if existing.initiated_by_operator_id != principal.operator_id:
                            failure = IdempotencyConflict()
                        else:
                            result = ActivationIntentResult(
                                request_id=onboarding.id,
                                activation_attempt_id=existing.id,
                                state=existing.status,
                                created_at=_as_utc(existing.created_at),
                                replayed=True,
                            )
                    elif repository.pending_activation_attempt(onboarding.id) is not None:
                        failure = ActivationBlocked(("ACTIVATION_ATTEMPT_IN_PROGRESS",))
                    else:
                        facts = self._facts(session, onboarding, attempt=None, lock=True)
                        blockers = external_blockers + self._policy.local_blockers(
                            session,
                            facts,
                            principal=principal,
                            now=current_time,
                            require_intent_ack=False,
                        )
                        if blockers:
                            self._record_event(
                                session,
                                principal,
                                request=onboarding,
                                action="tenant.activation.intent",
                                outcome="denied",
                                reason_code="activation_gate_closed",
                                request_id=request_id,
                                occurred_at=current_time,
                            )
                            failure = ActivationBlocked(tuple(sorted(set(blockers))))
                        else:
                            event = self._record_event(
                                session,
                                principal,
                                request=onboarding,
                                action="tenant.activation.intent",
                                outcome="pending",
                                reason_code="activation_intent_pending_audit_ack",
                                request_id=request_id,
                                occurred_at=current_time,
                                event_type="platform.tenant.activation.intent",
                            )
                            attempt = TenantActivationAttempt(
                                id=uuid4(),
                                request_id=onboarding.id,
                                initiated_by_operator_id=principal.operator_id,
                                intent_audit_event_id=event.event_id,
                                idempotency_key=idempotency_key or "",
                                status="PENDING_AUDIT_ACK",
                                created_at=current_time,
                            )
                            session.add(attempt)
                            session.flush()
                            result = ActivationIntentResult(
                                request_id=onboarding.id,
                                activation_attempt_id=attempt.id,
                                state=attempt.status,
                                created_at=current_time,
                                replayed=False,
                            )
                else:
                    assert attempt_uuid is not None
                    attempt = repository.get_activation_attempt(
                        onboarding.id, attempt_uuid, lock=True
                    )
                    if attempt is None:
                        failure = ActivationBlocked(("ACTIVATION_ATTEMPT_NOT_FOUND",))
                    elif attempt.status == "ACTIVATED" and onboarding.state == "ACTIVE":
                        tenant = repository.get_tenant(onboarding.tenant_id)
                        domain = repository.get_domain(onboarding.tenant_domain_id)
                        if tenant is None or domain is None:
                            failure = ActivationBlocked(("TENANT_DOMAIN_MISMATCH",))
                        else:
                            result = TenantActivationResult(
                                request_id=onboarding.id,
                                activation_attempt_id=attempt.id,
                                request_state="ACTIVE",
                                tenant_active=tenant.status == "active",
                                domain_active=domain.is_active,
                                tenant_resolution_enabled=bool(
                                    attempt.completion_audit_event_id
                                    and repository.audit_acknowledged(
                                        attempt.completion_audit_event_id,
                                        tenant_id=onboarding.tenant_id,
                                        request_id=onboarding.id,
                                        event_type="platform.tenant.activation.completed",
                                        action="tenant.activate",
                                        outcome="success",
                                        acknowledged_no_later_than=current_time,
                                    )
                                ),
                                activated_at=(
                                    _as_utc(attempt.completed_at)
                                    if attempt.completed_at is not None
                                    else current_time
                                ),
                                replayed=True,
                            )
                    else:
                        facts = self._facts(session, onboarding, attempt=attempt, lock=True)
                        blockers = external_blockers + self._policy.local_blockers(
                            session,
                            facts,
                            principal=principal,
                            now=current_time,
                            require_intent_ack=True,
                        )
                        if blockers:
                            completion_event = self._record_event(
                                session,
                                principal,
                                request=onboarding,
                                action="tenant.activate",
                                outcome="denied",
                                reason_code="activation_gate_closed",
                                request_id=request_id,
                                occurred_at=current_time,
                            )
                            failure = ActivationBlocked(tuple(sorted(set(blockers))))
                        else:
                            assert facts.tenant is not None and facts.domain is not None
                            facts.tenant.status = "active"
                            facts.tenant.updated_at = current_time
                            facts.domain.is_active = True
                            facts.domain.updated_at = current_time
                            onboarding.state = "ACTIVE"
                            onboarding.version += 1
                            onboarding.updated_at = current_time
                            attempt.status = "ACTIVATED"
                            attempt.completed_at = current_time
                            completion_event = self._record_event(
                                session,
                                principal,
                                request=onboarding,
                                action="tenant.activate",
                                outcome="success",
                                reason_code="activation_intent_acknowledged",
                                request_id=request_id,
                                occurred_at=current_time,
                                event_type="platform.tenant.activation.completed",
                            )
                            attempt.completion_audit_event_id = completion_event.event_id
                            session.flush()
                            result = TenantActivationResult(
                                request_id=onboarding.id,
                                activation_attempt_id=attempt.id,
                                request_state="ACTIVE",
                                tenant_active=True,
                                domain_active=True,
                                tenant_resolution_enabled=False,
                                activated_at=current_time,
                                replayed=False,
                            )
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def cancel_pending_attempts(
        self, session: Session, request_uuid: UUID
    ) -> int:
        return TenantOnboardingRepository(session).cancel_pending_activation_attempts(request_uuid)

    def _facts(
        self,
        session: Session,
        onboarding: TenantOnboardingRequest,
        *,
        attempt: TenantActivationAttempt | None,
        lock: bool = False,
    ) -> ActivationFacts:
        repository = TenantOnboardingRepository(session)
        return ActivationFacts(
            request=onboarding,
            review=repository.get_review(onboarding.id, lock=lock),
            tenant=repository.get_tenant(onboarding.tenant_id, lock=lock),
            domain=repository.get_domain(onboarding.tenant_domain_id, lock=lock),
            attempt=attempt,
        )

    def _expire_request(
        self,
        session: Session,
        onboarding: TenantOnboardingRequest,
        now: datetime,
        request_id: str | None,
    ) -> None:
        repository = TenantOnboardingRepository(session)
        challenge = repository.current_challenge(onboarding.id, lock=True)
        if challenge is not None:
            challenge.status = "expired"
            challenge.verification_lease_id = None
            challenge.lease_expires_at = None
        repository.cancel_pending_activation_attempts(onboarding.id)
        onboarding.state = "EXPIRED"
        onboarding.version += 1
        onboarding.updated_at = now
        self._record_event(
            session,
            None,
            request=onboarding,
            action="onboarding.request.expire",
            outcome="failure",
            reason_code="request_expired",
            request_id=request_id,
            occurred_at=now,
            event_type="platform.onboarding.request.expired",
        )

    def _record_event(
        self,
        session: Session,
        principal: PlatformPrincipal | None,
        *,
        request: TenantOnboardingRequest,
        action: str,
        outcome: str,
        reason_code: str | None,
        request_id: str | None,
        occurred_at: datetime,
        event_type: str | None = None,
    ):
        return self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type=event_type or "platform.onboarding.review.decision",
                actor_category="platform_operator" if principal is not None else "system",
                actor_ref=str(principal.operator_id) if principal is not None else _SYSTEM_EXPIRY_ACTOR,
                tenant_id=request.tenant_id,
                target_entity_type="platform.tenant_onboarding_request",
                target_entity_ref=str(request.id),
                action=action,
                outcome=outcome,
                request_id=request_id,
                decision_metadata={"reason_code": reason_code} if reason_code is not None else {},
                occurred_at=occurred_at,
            ),
        )

    @staticmethod
    def _review_result(
        onboarding: TenantOnboardingRequest,
        review: TenantOnboardingReview,
        *,
        replayed: bool,
    ) -> OnboardingReviewResult:
        return OnboardingReviewResult(
            request_id=onboarding.id,
            decision=review.decision,
            request_state=onboarding.state,
            decision_at=review.decision_at,
            replayed=replayed,
        )
