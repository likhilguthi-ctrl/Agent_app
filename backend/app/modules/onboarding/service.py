from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.onboarding.dns_verification import DNSLookupFailure, SystemDNSResolver, TXTRecordResolver
from app.modules.onboarding.errors import (
    ChallengeExpired,
    ChallengeIssueRateLimited,
    ChallengeReplay,
    DNSLookupUnavailable,
    DomainRegistryConflict,
    DomainClaimUnavailable,
    DomainProofMismatch,
    IdempotencyConflict,
    IdempotencyKeyInvalid,
    OnboardingConflict,
    OnboardingInputInvalid,
    OnboardingNotFound,
    RequestCreatorCannotReject,
    RequestExpired,
    VerificationInProgress,
    VerificationRateLimited,
    TenantSlugUnavailable,
)
from app.modules.onboarding.models import DomainVerificationChallenge, TenantOnboardingRequest
from app.modules.onboarding.repository import TenantOnboardingRepository
from app.modules.onboarding.schemas import (
    DomainChallengeIssued,
    OnboardingHistoryEntry,
    OnboardingRequestCreate,
    OnboardingRequestDetail,
    OnboardingRequestList,
    OnboardingRequestSummary,
    OnboardingState,
    RejectionReason,
    validate_idempotency_key,
)
from app.modules.platform_identity.errors import PlatformOperatorNotAuthorized
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.modules.platform_identity.service import PlatformAuthorizationService, PlatformPrincipal
from app.modules.tenants.models import Tenant, TenantDomain


_DNS_TXT_OWNER_PREFIX = "_community-ott-verification."
_DNS_TXT_VALUE_PREFIX = "community-ott-domain-verification="
_CHALLENGE_PURPOSE = "tenant_domain_ownership_v1"
_CHALLENGE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
_SYSTEM_ACTOR = "system:onboarding-expiry"
_ACTIVE_REQUEST_STATES = (
    OnboardingState.PENDING_DOMAIN_PROOF.value,
    OnboardingState.PENDING_ACTIVATION_REVIEW.value,
)


@dataclass(frozen=True, slots=True)
class _VerificationJob:
    request_id: UUID
    tenant_id: UUID
    tenant_slug: str
    normalized_domain: str
    challenge_id: UUID
    challenge_hash: str
    lease_id: UUID
    attempt_count: int


class TenantOnboardingService:
    """Operator-mediated request lifecycle and exact DNS TXT proof service."""

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: TXTRecordResolver | None = None,
        session_factory: sessionmaker[Session] | None = None,
        authorization: PlatformAuthorizationService | None = None,
        audit_service: AuditService | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._resolver = resolver or SystemDNSResolver()
        self._session_factory = session_factory or get_session_factory()
        self._audit = audit_service or AuditService()
        self._authorization = authorization or PlatformAuthorizationService(
            SQLAlchemyPlatformIdentityRepository(self._session_factory),
            session_factory=self._session_factory,
            audit_service=self._audit,
        )
        self._clock = clock

    def create_request(
        self,
        principal: PlatformPrincipal,
        *,
        tenant_slug: str,
        normalized_domain: str,
        idempotency_key: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> tuple[OnboardingRequestSummary, bool]:
        try:
            validated = OnboardingRequestCreate(
                tenant_slug=tenant_slug,
                domain=normalized_domain,
            )
            tenant_slug = validated.tenant_slug
            normalized_domain = validated.domain
            idempotency_key = validate_idempotency_key(idempotency_key)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                raise OnboardingInputInvalid from exc
            raise IdempotencyKeyInvalid from exc
        current_time = now or self._clock()
        result: OnboardingRequestSummary | None = None
        replayed = False
        failure: Exception | None = None

        try:
            with self._session_factory() as session, session.begin():
                repository = TenantOnboardingRepository(session)
                if not self._allow(
                    session, principal, "platform.onboarding.create", request_id, current_time
                ):
                    failure = PlatformOperatorNotAuthorized()
                else:
                    self._expire_due(session, current_time)
                    existing = repository.find_idempotent_request(
                        principal.operator_id, idempotency_key
                    )
                    if existing is not None:
                        tenant = repository.get_tenant(existing.tenant_id)
                        if (
                            tenant is None
                            or tenant.slug != tenant_slug
                            or existing.normalized_domain != normalized_domain
                        ):
                            self._audit_request_attempt(
                                session,
                                principal,
                                action="onboarding.request.create",
                                outcome="denied",
                                reason_code="idempotency_key_conflict",
                                request_id=request_id,
                                target_ref=existing.id,
                                tenant_id=existing.tenant_id,
                                occurred_at=current_time,
                            )
                            failure = IdempotencyConflict()
                        else:
                            self._audit_request_attempt(
                                session,
                                principal,
                                action="onboarding.request.create",
                                outcome="success",
                                reason_code="idempotent_replay",
                                request_id=request_id,
                                target_ref=existing.id,
                                tenant_id=existing.tenant_id,
                                occurred_at=current_time,
                            )
                            result = self._summary(existing, tenant)
                            replayed = True
                    else:
                        pending_claim = repository.find_pending_domain_claim(
                            normalized_domain, lock=True
                        )
                        if (
                            pending_claim is not None
                            and self._as_utc(pending_claim.expires_at) <= current_time
                        ):
                            self._expire_request(session, pending_claim, current_time)
                            pending_claim = None
                    if failure is None and result is None and (
                        repository.find_domain(normalized_domain) is not None
                        or pending_claim is not None
                    ):
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.request.create",
                            outcome="denied",
                            reason_code="domain_claim_unavailable",
                            request_id=request_id,
                            occurred_at=current_time,
                        )
                        failure = DomainClaimUnavailable()
                    elif failure is None and result is None and session.scalar(
                        select(Tenant.id).where(func.lower(Tenant.slug) == tenant_slug).limit(1)
                    ) is not None:
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.request.create",
                            outcome="denied",
                            reason_code="tenant_slug_unavailable",
                            request_id=request_id,
                            occurred_at=current_time,
                        )
                        failure = TenantSlugUnavailable()
                    elif failure is None and result is None:
                        tenant = Tenant(id=uuid4(), slug=tenant_slug, status="inactive")
                        session.add(tenant)
                        session.flush()
                        onboarding = TenantOnboardingRequest(
                            id=uuid4(),
                            tenant_id=tenant.id,
                            tenant_domain_id=None,
                            created_by_operator_id=principal.operator_id,
                            normalized_domain=normalized_domain,
                            state=OnboardingState.PENDING_DOMAIN_PROOF.value,
                            idempotency_key=idempotency_key,
                            expires_at=current_time
                            + timedelta(seconds=self._settings.onboarding_request_ttl_seconds),
                            version=1,
                        )
                        session.add(onboarding)
                        session.flush()
                        self._record_event(
                            session,
                            principal,
                            event_type="platform.onboarding.request.created",
                            action="onboarding.request.create",
                            outcome="success",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=tenant.id,
                            reason_code="operator_request",
                            occurred_at=current_time,
                        )
                        result = self._summary(onboarding, tenant)
        except IntegrityError:
            result, replayed, failure = self._recover_create_integrity(
                principal,
                tenant_slug=tenant_slug,
                normalized_domain=normalized_domain,
                idempotency_key=idempotency_key,
                request_id=request_id,
                now=current_time,
            )

        if failure is not None:
            raise failure
        assert result is not None
        return result, replayed

    def list_requests(
        self,
        principal: PlatformPrincipal,
        *,
        state: str | None,
        limit: int,
        offset: int,
        request_id: str | None,
        now: datetime | None = None,
    ) -> OnboardingRequestList:
        current_time = now or self._clock()
        items: list[OnboardingRequestSummary] = []
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._allow(
                session, principal, "platform.onboarding.read", request_id, current_time
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                self._expire_due(session, current_time)
                rows = TenantOnboardingRepository(session).list_requests(
                    state=state, limit=limit, offset=offset
                )
                repository = TenantOnboardingRepository(session)
                for onboarding, _tenant in rows:
                    self._expire_request_if_due(session, onboarding, current_time)
                items = [self._summary(request, tenant) for request, tenant in rows]
                self._record_event(
                    session,
                    principal,
                    event_type="platform.onboarding.request.listed",
                    action="onboarding.request.list",
                    outcome="success",
                    request_id=request_id,
                    target_type="platform.tenant_onboarding_collection",
                    occurred_at=current_time,
                )
        if failure is not None:
            raise failure
        return OnboardingRequestList(items=items, limit=limit, offset=offset)

    def get_request(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime | None = None,
    ) -> OnboardingRequestDetail:
        current_time = now or self._clock()
        result: OnboardingRequestDetail | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._allow(
                session, principal, "platform.onboarding.read", request_id, current_time
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    self._audit_request_attempt(
                        session,
                        principal,
                        action="onboarding.request.read",
                        outcome="denied",
                        reason_code="request_not_found",
                        request_id=request_id,
                        occurred_at=current_time,
                    )
                    failure = OnboardingNotFound()
                else:
                    self._expire_request_if_due(session, onboarding, current_time)
                    self._expire_challenge_for_request(session, onboarding, current_time)
                    tenant = repository.get_tenant(onboarding.tenant_id)
                    history = repository.audit_history(request_uuid)
                    self._record_event(
                        session,
                        principal,
                        event_type="platform.onboarding.request.read",
                        action="onboarding.request.read",
                        outcome="success",
                        request_id=request_id,
                        target_ref=onboarding.id,
                        tenant_id=onboarding.tenant_id,
                        occurred_at=current_time,
                    )
                    if tenant is not None:
                        result = OnboardingRequestDetail(
                            **self._summary(onboarding, tenant).model_dump(),
                            history=[self._history_entry(item) for item in history],
                        )
                    else:
                        failure = OnboardingNotFound()
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def issue_challenge(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        idempotency_key: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> DomainChallengeIssued:
        try:
            idempotency_key = validate_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise IdempotencyKeyInvalid from exc
        current_time = now or self._clock()
        result: DomainChallengeIssued | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._allow(
                session, principal, "platform.domain.challenge", request_id, current_time
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                self._expire_due(session, current_time)
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    failure = self._not_found(session, principal, request_id, current_time)
                else:
                    self._expire_request_if_due(session, onboarding, current_time)
                    if onboarding.state == OnboardingState.EXPIRED.value:
                        failure = RequestExpired()
                    else:
                        prior = repository.find_challenge_idempotency(
                            request_uuid, idempotency_key
                        )
                        if prior is not None:
                            self._audit_request_attempt(
                                session,
                                principal,
                                action="onboarding.domain.challenge.issue",
                                outcome="success",
                                reason_code="idempotent_replay",
                                request_id=request_id,
                                target_ref=onboarding.id,
                                tenant_id=onboarding.tenant_id,
                                occurred_at=current_time,
                            )
                            result = DomainChallengeIssued(
                                request_id=onboarding.id,
                                dns_txt_name=self._dns_owner(onboarding.normalized_domain),
                                dns_txt_value=None,
                                expires_at=prior.expires_at,
                                replayed=True,
                            )
                        elif onboarding.state != OnboardingState.PENDING_DOMAIN_PROOF.value:
                            self._audit_request_attempt(
                                session,
                                principal,
                                action="onboarding.domain.challenge.issue",
                                outcome="denied",
                                reason_code="request_state_conflict",
                                request_id=request_id,
                                target_ref=onboarding.id,
                                tenant_id=onboarding.tenant_id,
                                occurred_at=current_time,
                            )
                            failure = OnboardingConflict()
                        elif repository.count_recent_operator_events(
                            operator_id=principal.operator_id,
                            event_type="platform.domain.challenge.issued",
                            since=current_time
                            - timedelta(
                                seconds=self._settings.domain_challenge_issue_operator_window_seconds
                            ),
                        ) >= self._settings.domain_challenge_issue_operator_limit:
                            self._audit_request_attempt(
                                session,
                                principal,
                                action="onboarding.domain.challenge.issue",
                                outcome="denied",
                                reason_code="operator_challenge_issue_limit",
                                request_id=request_id,
                                target_ref=onboarding.id,
                                tenant_id=onboarding.tenant_id,
                                occurred_at=current_time,
                            )
                            failure = ChallengeIssueRateLimited(
                                retry_after_seconds=self._settings.domain_challenge_issue_operator_window_seconds
                            )
                        elif repository.count_challenges(request_uuid) >= self._settings.domain_challenge_issue_limit:
                            self._audit_request_attempt(
                                session,
                                principal,
                                action="onboarding.domain.challenge.issue",
                                outcome="denied",
                                reason_code="challenge_issue_limit",
                                request_id=request_id,
                                target_ref=onboarding.id,
                                tenant_id=onboarding.tenant_id,
                                occurred_at=current_time,
                            )
                            failure = ChallengeIssueRateLimited(retry_after_seconds=60)
                        else:
                            current = repository.current_challenge(request_uuid, lock=True)
                            if current is not None and current.status == "checking":
                                if current.lease_expires_at is not None and self._as_utc(
                                    current.lease_expires_at
                                ) > current_time:
                                    failure = VerificationInProgress()
                                else:
                                    self._expire_stale_lease(
                                        session, onboarding, current, current_time
                                    )
                            if failure is None:
                                if current is not None and current.status == "active":
                                    current.status = "superseded"
                                    self._audit_request_attempt(
                                        session,
                                        principal,
                                        action="onboarding.domain.challenge.supersede",
                                        outcome="success",
                                        reason_code="challenge_rotated",
                                        request_id=request_id,
                                        target_ref=onboarding.id,
                                        tenant_id=onboarding.tenant_id,
                                        occurred_at=current_time,
                                    )
                                raw_challenge = secrets.token_urlsafe(32)
                                challenge = DomainVerificationChallenge(
                                    id=uuid4(),
                                    request_id=onboarding.id,
                                    issued_by_operator_id=principal.operator_id,
                                    purpose=_CHALLENGE_PURPOSE,
                                    normalized_domain=onboarding.normalized_domain,
                                    challenge_hash=self._challenge_digest(
                                        onboarding.id, onboarding.normalized_domain, raw_challenge
                                    ),
                                    idempotency_key=idempotency_key,
                                    status="active",
                                    attempt_count=0,
                                    issued_at=current_time,
                                    expires_at=current_time
                                    + timedelta(seconds=self._settings.domain_challenge_ttl_seconds),
                                )
                                session.add(challenge)
                                session.flush()
                                self._record_event(
                                    session,
                                    principal,
                                    event_type="platform.domain.challenge.issued",
                                    action="onboarding.domain.challenge.issue",
                                    outcome="success",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    reason_code="challenge_issued",
                                    occurred_at=current_time,
                                )
                                result = DomainChallengeIssued(
                                    request_id=onboarding.id,
                                    dns_txt_name=self._dns_owner(onboarding.normalized_domain),
                                    dns_txt_value=f"{_DNS_TXT_VALUE_PREFIX}{raw_challenge}",
                                    expires_at=challenge.expires_at,
                                    replayed=False,
                                )
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def verify_domain(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime | None = None,
    ) -> OnboardingRequestSummary:
        job = await run_in_threadpool(
            self._begin_verification,
            principal,
            request_uuid,
            request_id=request_id,
            now=now or self._clock(),
        )
        owner_name = self._dns_owner(job.normalized_domain)
        try:
            txt_values = await self._resolver.lookup_txt(
                owner_name,
                timeout_seconds=self._settings.domain_dns_query_timeout_seconds,
                max_records=self._settings.domain_dns_max_records,
                max_response_bytes=self._settings.domain_dns_max_response_bytes,
            )
            if not isinstance(txt_values, tuple) or len(txt_values) > self._settings.domain_dns_max_records:
                raise DNSLookupFailure
            total_bytes = sum(len(value.encode("utf-8", errors="replace")) for value in txt_values)
            if total_bytes > self._settings.domain_dns_max_response_bytes:
                raise DNSLookupFailure
        except Exception as exc:
            await run_in_threadpool(
                self._finish_verification_failure,
                principal,
                job,
                request_id=request_id,
                reason_code="dns_lookup_failed",
                outcome="failure",
                public_error=DNSLookupUnavailable(),
                terminal=False,
                now=self._clock(),
            )
            raise DNSLookupUnavailable from exc

        if not self._contains_exact_proof(job, txt_values):
            await run_in_threadpool(
                self._finish_verification_failure,
                principal,
                job,
                request_id=request_id,
                reason_code="txt_record_mismatch",
                outcome="denied",
                public_error=DomainProofMismatch(),
                terminal=False,
                now=self._clock(),
            )
            raise DomainProofMismatch

        try:
            return await run_in_threadpool(
                self._finish_verification_success,
                principal,
                job,
                request_id=request_id,
                now=self._clock(),
            )
        except DomainRegistryConflict:
            await run_in_threadpool(
                self._finish_verification_failure,
                principal,
                job,
                request_id=request_id,
                reason_code="domain_claim_unavailable",
                outcome="denied",
                public_error=DomainClaimUnavailable(),
                terminal=True,
                now=self._clock(),
            )
            raise DomainClaimUnavailable

    def cancel_request(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime | None = None,
    ) -> OnboardingRequestSummary:
        return self._finish_request_decision(
            principal,
            request_uuid,
            permission_code="platform.onboarding.cancel",
            target_state=OnboardingState.CANCELLED.value,
            event_type="platform.onboarding.request.cancelled",
            action="onboarding.request.cancel",
            reason_code="operator_cancelled",
            request_id=request_id,
            now=now or self._clock(),
        )

    def reject_request(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        reason_code: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> OnboardingRequestSummary:
        if reason_code not in {item.value for item in RejectionReason}:
            # Route schemas normally enforce the enum; keep direct service use fail-closed.
            raise OnboardingConflict
        current_time = now or self._clock()
        result: OnboardingRequestSummary | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._allow(
                session, principal, "platform.tenant.reject", request_id, current_time
            ):
                failure = PlatformOperatorNotAuthorized()
            else:
                self._expire_due(session, current_time)
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    failure = self._not_found(session, principal, request_id, current_time)
                else:
                    self._expire_request_if_due(session, onboarding, current_time)
                    if onboarding.state == OnboardingState.EXPIRED.value:
                        failure = RequestExpired()
                    elif onboarding.state not in _ACTIVE_REQUEST_STATES:
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.request.reject",
                            outcome="denied",
                            reason_code="request_state_conflict",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=onboarding.tenant_id,
                            occurred_at=current_time,
                        )
                        failure = OnboardingConflict()
                    elif onboarding.created_by_operator_id == principal.operator_id:
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.request.reject",
                            outcome="denied",
                            reason_code="requester_cannot_reject",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=onboarding.tenant_id,
                            occurred_at=current_time,
                        )
                        failure = RequestCreatorCannotReject()
                    else:
                        repository.cancel_pending_activation_attempts(onboarding.id)
                        self._close_current_challenge(
                            session,
                            onboarding,
                            status="superseded",
                            actor=principal,
                            request_id=request_id,
                            now=current_time,
                            reason_code="request_rejected",
                        )
                        self._transition(
                            session,
                            onboarding,
                            state=OnboardingState.REJECTED.value,
                            actor=principal,
                            event_type="platform.onboarding.request.rejected",
                            action="onboarding.request.reject",
                            outcome="success",
                            reason_code=reason_code,
                            request_id=request_id,
                            now=current_time,
                        )
                        tenant = repository.get_tenant(onboarding.tenant_id)
                        if tenant is not None:
                            result = self._summary(onboarding, tenant)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def expire_due_requests(self, *, now: datetime | None = None) -> int:
        """Internal maintenance operation; invoke from a trusted scheduler if desired."""
        current_time = now or self._clock()
        with self._session_factory() as session, session.begin():
            return self._expire_due(session, current_time)

    def _begin_verification(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        request_id: str | None,
        now: datetime,
    ) -> _VerificationJob:
        job: _VerificationJob | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._allow(session, principal, "platform.domain.verify", request_id, now):
                failure = PlatformOperatorNotAuthorized()
            else:
                self._expire_due(session, now)
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    failure = self._not_found(session, principal, request_id, now)
                elif onboarding.state != OnboardingState.PENDING_DOMAIN_PROOF.value:
                    latest = repository.latest_challenge(request_uuid, lock=True)
                    if latest is not None and latest.status == "consumed":
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.domain.verify",
                            outcome="denied",
                            reason_code="challenge_replay",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=onboarding.tenant_id,
                            occurred_at=now,
                        )
                        failure = ChallengeReplay()
                    else:
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.domain.verify",
                            outcome="denied",
                            reason_code="request_state_conflict",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=onboarding.tenant_id,
                            occurred_at=now,
                        )
                        failure = OnboardingConflict()
                else:
                    self._expire_request_if_due(session, onboarding, now)
                    if onboarding.state == OnboardingState.EXPIRED.value:
                        failure = RequestExpired()
                    elif onboarding.state != OnboardingState.PENDING_DOMAIN_PROOF.value:
                        self._audit_request_attempt(
                            session,
                            principal,
                            action="onboarding.domain.verify",
                            outcome="denied",
                            reason_code="request_state_conflict",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=onboarding.tenant_id,
                            occurred_at=now,
                        )
                        failure = OnboardingConflict()
                    else:
                        challenge = repository.current_challenge(request_uuid, lock=True)
                        if challenge is None:
                            latest = repository.latest_challenge(request_uuid, lock=True)
                            if latest is not None and latest.status == "consumed":
                                self._audit_request_attempt(
                                    session,
                                    principal,
                                    action="onboarding.domain.verify",
                                    outcome="denied",
                                    reason_code="challenge_replay",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    occurred_at=now,
                                )
                                failure = ChallengeReplay()
                            elif latest is not None and latest.status == "locked":
                                failure = VerificationRateLimited(retry_after_seconds=60)
                            elif latest is not None and latest.status == "expired":
                                failure = ChallengeExpired()
                            else:
                                self._audit_request_attempt(
                                    session,
                                    principal,
                                    action="onboarding.domain.verify",
                                    outcome="denied",
                                    reason_code="challenge_not_issued",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    occurred_at=now,
                                )
                                failure = OnboardingConflict()
                        else:
                            if (
                                challenge.purpose != _CHALLENGE_PURPOSE
                                or challenge.normalized_domain != onboarding.normalized_domain
                            ):
                                challenge.status = "locked"
                                self._clear_lease(challenge)
                                challenge.last_failure_code = "challenge_binding_invalid"
                                self._audit_request_attempt(
                                    session,
                                    principal,
                                    action="onboarding.domain.verify",
                                    outcome="denied",
                                    reason_code="challenge_binding_invalid",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    occurred_at=now,
                                )
                                failure = OnboardingConflict()
                            elif self._as_utc(challenge.expires_at) <= now:
                                self._expire_challenge(
                                    session,
                                    onboarding,
                                    challenge,
                                    actor_category="system",
                                    actor_ref=_SYSTEM_ACTOR,
                                    request_id=request_id,
                                    now=now,
                                    reason_code="challenge_expired",
                                )
                                failure = ChallengeExpired()
                            elif challenge.status == "checking":
                                if challenge.lease_expires_at is not None and self._as_utc(
                                    challenge.lease_expires_at
                                ) > now:
                                    self._audit_request_attempt(
                                        session,
                                        principal,
                                        action="onboarding.domain.verify",
                                        outcome="denied",
                                        reason_code="verification_in_progress",
                                        request_id=request_id,
                                        target_ref=onboarding.id,
                                        tenant_id=onboarding.tenant_id,
                                        occurred_at=now,
                                    )
                                    failure = VerificationInProgress()
                                else:
                                    self._expire_stale_lease(session, onboarding, challenge, now)
                                    challenge.status = "active"
                            if failure is None and repository.count_recent_operator_events(
                                operator_id=principal.operator_id,
                                event_type="platform.domain.verification.started",
                                since=now
                                - timedelta(
                                    seconds=self._settings.domain_challenge_verify_operator_window_seconds
                                ),
                            ) >= self._settings.domain_challenge_verify_operator_limit:
                                self._audit_request_attempt(
                                    session,
                                    principal,
                                    action="onboarding.domain.verify",
                                    outcome="denied",
                                    reason_code="operator_verification_limit",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    occurred_at=now,
                                )
                                failure = VerificationRateLimited(
                                    retry_after_seconds=self._settings.domain_challenge_verify_operator_window_seconds
                                )
                            if failure is None and challenge.attempt_count >= self._settings.domain_challenge_verify_limit:
                                challenge.status = "locked"
                                self._clear_lease(challenge)
                                self._audit_request_attempt(
                                    session,
                                    principal,
                                    action="onboarding.domain.verify",
                                    outcome="denied",
                                    reason_code="verification_attempt_limit",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    occurred_at=now,
                                )
                                failure = VerificationRateLimited(retry_after_seconds=60)
                            elif failure is None:
                                lease_id = uuid4()
                                challenge.status = "checking"
                                challenge.attempt_count += 1
                                challenge.verification_lease_id = lease_id
                                challenge.lease_expires_at = now + timedelta(
                                    seconds=max(15, int(self._settings.domain_dns_query_timeout_seconds) + 5)
                                )
                                self._record_event(
                                    session,
                                    principal,
                                    event_type="platform.domain.verification.started",
                                    action="onboarding.domain.verify",
                                    outcome="pending",
                                    request_id=request_id,
                                    target_ref=onboarding.id,
                                    tenant_id=onboarding.tenant_id,
                                    reason_code="verification_started",
                                    occurred_at=now,
                                )
                                tenant = repository.get_tenant(onboarding.tenant_id)
                                if tenant is not None:
                                    job = _VerificationJob(
                                        request_id=onboarding.id,
                                        tenant_id=onboarding.tenant_id,
                                        tenant_slug=tenant.slug,
                                        normalized_domain=onboarding.normalized_domain,
                                        challenge_id=challenge.id,
                                        challenge_hash=challenge.challenge_hash,
                                        lease_id=lease_id,
                                        attempt_count=challenge.attempt_count,
                                    )
        if failure is not None:
            raise failure
        assert job is not None
        return job

    def _finish_verification_failure(
        self,
        principal: PlatformPrincipal,
        job: _VerificationJob,
        *,
        request_id: str | None,
        reason_code: str,
        outcome: str,
        public_error: Exception,
        terminal: bool,
        now: datetime,
    ) -> None:
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            authorized = self._allow(
                session, principal, "platform.domain.verify", request_id, now
            )
            repository = TenantOnboardingRepository(session)
            onboarding = repository.get_request(job.request_id, lock=True)
            challenge = repository.get_challenge(job.challenge_id, lock=True)
            if not authorized:
                failure = PlatformOperatorNotAuthorized()
                if challenge is not None and self._owns_lease(challenge, job.lease_id):
                    challenge.status = (
                        "locked"
                        if challenge.attempt_count >= self._settings.domain_challenge_verify_limit
                        else "active"
                    )
                    self._clear_lease(challenge)
                    challenge.last_failure_code = "permission_revoked"
            elif onboarding is None or challenge is None:
                failure = OnboardingNotFound()
            elif self._expire_request_if_due(session, onboarding, now):
                self._expire_challenge_for_request(session, onboarding, now)
                failure = RequestExpired()
            elif self._as_utc(challenge.expires_at) <= now:
                self._expire_challenge(
                    session,
                    onboarding,
                    challenge,
                    actor_category="system",
                    actor_ref=_SYSTEM_ACTOR,
                    request_id=request_id,
                    now=now,
                    reason_code="challenge_expired",
                )
                failure = ChallengeExpired()
            elif not self._owns_lease(challenge, job.lease_id):
                failure = ChallengeReplay()
            else:
                challenge.status = (
                    "locked"
                    if terminal
                    or challenge.attempt_count >= self._settings.domain_challenge_verify_limit
                    else "active"
                )
                self._clear_lease(challenge)
                challenge.last_failure_code = reason_code
                self._record_event(
                    session,
                    principal,
                    event_type=(
                        "platform.domain.verification.failed"
                        if outcome == "failure"
                        else "platform.domain.verification.denied"
                    ),
                    action="onboarding.domain.verify",
                    outcome=outcome,
                    request_id=request_id,
                    target_ref=onboarding.id,
                    tenant_id=onboarding.tenant_id,
                    reason_code=reason_code,
                    occurred_at=now,
                )
                failure = public_error
        if failure is not None:
            raise failure

    def _finish_verification_success(
        self,
        principal: PlatformPrincipal,
        job: _VerificationJob,
        *,
        request_id: str | None,
        now: datetime,
    ) -> OnboardingRequestSummary:
        result: OnboardingRequestSummary | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            authorized = self._allow(
                session, principal, "platform.domain.verify", request_id, now
            )
            repository = TenantOnboardingRepository(session)
            onboarding = repository.get_request(job.request_id, lock=True)
            challenge = repository.get_challenge(job.challenge_id, lock=True)
            if not authorized:
                failure = PlatformOperatorNotAuthorized()
                if challenge is not None and self._owns_lease(challenge, job.lease_id):
                    challenge.status = (
                        "locked"
                        if challenge.attempt_count >= self._settings.domain_challenge_verify_limit
                        else "active"
                    )
                    self._clear_lease(challenge)
                    challenge.last_failure_code = "permission_revoked"
            elif onboarding is None or challenge is None:
                failure = OnboardingNotFound()
            elif self._expire_request_if_due(session, onboarding, now):
                self._expire_challenge_for_request(session, onboarding, now)
                failure = RequestExpired()
            elif self._as_utc(challenge.expires_at) <= now:
                self._expire_challenge(
                    session,
                    onboarding,
                    challenge,
                    actor_category="system",
                    actor_ref=_SYSTEM_ACTOR,
                    request_id=request_id,
                    now=now,
                    reason_code="challenge_expired",
                )
                failure = ChallengeExpired()
            elif (
                challenge.status != "checking"
                or challenge.verification_lease_id != job.lease_id
                or onboarding.state != OnboardingState.PENDING_DOMAIN_PROOF.value
                or challenge.purpose != _CHALLENGE_PURPOSE
                or challenge.normalized_domain != onboarding.normalized_domain
            ):
                self._audit_request_attempt(
                    session,
                    principal,
                    action="onboarding.domain.verify",
                    outcome="denied",
                    reason_code="stale_verification_result",
                    request_id=request_id,
                    target_ref=onboarding.id,
                    tenant_id=onboarding.tenant_id,
                    occurred_at=now,
                )
                failure = ChallengeReplay()
            elif repository.find_domain(onboarding.normalized_domain) is not None:
                challenge.status = "locked"
                self._clear_lease(challenge)
                challenge.last_failure_code = "domain_claim_unavailable"
                self._record_event(
                    session,
                    principal,
                    event_type="platform.domain.verification.denied",
                    action="onboarding.domain.verify",
                    outcome="denied",
                    request_id=request_id,
                    target_ref=onboarding.id,
                    tenant_id=onboarding.tenant_id,
                    reason_code="domain_claim_unavailable",
                    occurred_at=now,
                )
                failure = DomainClaimUnavailable()
            else:
                domain = TenantDomain(
                    id=uuid4(),
                    tenant_id=onboarding.tenant_id,
                    hostname=onboarding.normalized_domain,
                    verification_status="verified",
                    is_active=False,
                    verified_at=now,
                )
                session.add(domain)
                try:
                    session.flush()
                except IntegrityError as exc:
                    raise DomainRegistryConflict from exc
                onboarding.tenant_domain_id = domain.id
                onboarding.state = OnboardingState.PENDING_ACTIVATION_REVIEW.value
                onboarding.version += 1
                onboarding.updated_at = now
                challenge.status = "consumed"
                challenge.consumed_at = now
                self._clear_lease(challenge)
                challenge.last_failure_code = None
                self._record_event(
                    session,
                    principal,
                    event_type="platform.domain.verification.verified",
                    action="onboarding.domain.verify",
                    outcome="success",
                    request_id=request_id,
                    target_ref=onboarding.id,
                    tenant_id=onboarding.tenant_id,
                    reason_code="txt_record_verified",
                    occurred_at=now,
                )
                tenant = repository.get_tenant(onboarding.tenant_id)
                if tenant is not None:
                    result = self._summary(onboarding, tenant, domain=domain)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _finish_request_decision(
        self,
        principal: PlatformPrincipal,
        request_uuid: UUID,
        *,
        permission_code: str,
        target_state: str,
        event_type: str,
        action: str,
        reason_code: str,
        request_id: str | None,
        now: datetime,
    ) -> OnboardingRequestSummary:
        result: OnboardingRequestSummary | None = None
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            if not self._allow(session, principal, permission_code, request_id, now):
                failure = PlatformOperatorNotAuthorized()
            else:
                self._expire_due(session, now)
                repository = TenantOnboardingRepository(session)
                onboarding = repository.get_request(request_uuid, lock=True)
                if onboarding is None:
                    failure = self._not_found(session, principal, request_id, now)
                else:
                    self._expire_request_if_due(session, onboarding, now)
                    if onboarding.state == OnboardingState.EXPIRED.value:
                        failure = RequestExpired()
                    elif onboarding.state not in _ACTIVE_REQUEST_STATES:
                        self._audit_request_attempt(
                            session,
                            principal,
                            action=action,
                            outcome="denied",
                            reason_code="request_state_conflict",
                            request_id=request_id,
                            target_ref=onboarding.id,
                            tenant_id=onboarding.tenant_id,
                            occurred_at=now,
                        )
                        failure = OnboardingConflict()
                    else:
                        repository.cancel_pending_activation_attempts(onboarding.id)
                        self._close_current_challenge(
                            session,
                            onboarding,
                            status="superseded",
                            actor=principal,
                            request_id=request_id,
                            now=now,
                            reason_code="request_cancelled",
                        )
                        self._transition(
                            session,
                            onboarding,
                            state=target_state,
                            actor=principal,
                            event_type=event_type,
                            action=action,
                            outcome="success",
                            reason_code=reason_code,
                            request_id=request_id,
                            now=now,
                        )
                        tenant = repository.get_tenant(onboarding.tenant_id)
                        if tenant is not None:
                            result = self._summary(onboarding, tenant)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _allow(
        self,
        session: Session,
        principal: PlatformPrincipal,
        permission_code: str,
        request_id: str | None,
        now: datetime,
    ) -> bool:
        return self._authorization.authorize_in_transaction(
            session,
            principal,
            permission_code,
            request_id=request_id,
            now=now,
        )

    def _record_event(
        self,
        session: Session,
        principal: PlatformPrincipal | None,
        *,
        event_type: str,
        action: str,
        outcome: str,
        request_id: str | None,
        target_ref: UUID | None = None,
        target_type: str = "platform.tenant_onboarding_request",
        tenant_id: UUID | None = None,
        reason_code: str | None = None,
        occurred_at: datetime,
    ) -> None:
        actor_category = "platform_operator" if principal is not None else "system"
        actor_ref = str(principal.operator_id) if principal is not None else _SYSTEM_ACTOR
        metadata = {"reason_code": reason_code} if reason_code is not None else {}
        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type=event_type,
                actor_category=actor_category,
                actor_ref=actor_ref,
                tenant_id=tenant_id,
                target_entity_type=target_type,
                target_entity_ref=str(target_ref) if target_ref is not None else None,
                action=action,
                outcome=outcome,
                request_id=request_id,
                decision_metadata=metadata,
                occurred_at=occurred_at,
            ),
        )

    def _audit_request_attempt(
        self,
        session: Session,
        principal: PlatformPrincipal,
        *,
        action: str,
        outcome: str,
        reason_code: str,
        request_id: str | None,
        occurred_at: datetime,
        target_ref: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> None:
        self._record_event(
            session,
            principal,
            event_type="platform.onboarding.request.attempt",
            action=action,
            outcome=outcome,
            request_id=request_id,
            target_ref=target_ref,
            tenant_id=tenant_id,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )

    def _not_found(
        self,
        session: Session,
        principal: PlatformPrincipal,
        request_id: str | None,
        now: datetime,
    ) -> OnboardingNotFound:
        self._audit_request_attempt(
            session,
            principal,
            action="onboarding.request.read",
            outcome="denied",
            reason_code="request_not_found",
            request_id=request_id,
            occurred_at=now,
        )
        return OnboardingNotFound()

    def _transition(
        self,
        session: Session,
        onboarding: TenantOnboardingRequest,
        *,
        state: str,
        actor: PlatformPrincipal | None,
        event_type: str,
        action: str,
        outcome: str,
        reason_code: str,
        request_id: str | None,
        now: datetime,
    ) -> None:
        onboarding.state = state
        onboarding.version += 1
        onboarding.updated_at = now
        self._record_event(
            session,
            actor,
            event_type=event_type,
            action=action,
            outcome=outcome,
            request_id=request_id,
            target_ref=onboarding.id,
            tenant_id=onboarding.tenant_id,
            reason_code=reason_code,
            occurred_at=now,
        )

    def _expire_due(self, session: Session, now: datetime) -> int:
        repository = TenantOnboardingRepository(session)
        expired = 0
        for onboarding in repository.due_requests(
            now=now, limit=self._settings.onboarding_expiry_batch_size
        ):
            self._expire_request(session, onboarding, now)
            expired += 1
        for challenge in repository.due_challenges(
            now=now, limit=self._settings.onboarding_expiry_batch_size
        ):
            onboarding = repository.get_request(challenge.request_id, lock=True)
            if onboarding is None:
                continue
            self._expire_challenge(
                session,
                onboarding,
                challenge,
                actor_category="system",
                actor_ref=_SYSTEM_ACTOR,
                request_id=None,
                now=now,
                reason_code="challenge_expired",
            )
        return expired

    def _expire_request_if_due(
        self, session: Session, onboarding: TenantOnboardingRequest, now: datetime
    ) -> bool:
        if onboarding.state not in _ACTIVE_REQUEST_STATES or self._as_utc(
            onboarding.expires_at
        ) > now:
            return False
        self._expire_request(session, onboarding, now)
        return True

    def _expire_request(
        self, session: Session, onboarding: TenantOnboardingRequest, now: datetime
    ) -> None:
        TenantOnboardingRepository(session).cancel_pending_activation_attempts(onboarding.id)
        self._close_current_challenge(
            session,
            onboarding,
            status="expired",
            actor=None,
            request_id=None,
            now=now,
            reason_code="request_expired",
        )
        self._transition(
            session,
            onboarding,
            state=OnboardingState.EXPIRED.value,
            actor=None,
            event_type="platform.onboarding.request.expired",
            action="onboarding.request.expire",
            outcome="failure",
            reason_code="request_expired",
            request_id=None,
            now=now,
        )

    def _expire_challenge_for_request(
        self, session: Session, onboarding: TenantOnboardingRequest, now: datetime
    ) -> None:
        challenge = TenantOnboardingRepository(session).current_challenge(
            onboarding.id, lock=True
        )
        if challenge is not None and self._as_utc(challenge.expires_at) <= now:
            self._expire_challenge(
                session,
                onboarding,
                challenge,
                actor_category="system",
                actor_ref=_SYSTEM_ACTOR,
                request_id=None,
                now=now,
                reason_code="challenge_expired",
            )

    def _expire_challenge(
        self,
        session: Session,
        onboarding: TenantOnboardingRequest,
        challenge: DomainVerificationChallenge,
        *,
        actor_category: str,
        actor_ref: str,
        request_id: str | None,
        now: datetime,
        reason_code: str,
    ) -> None:
        challenge.status = "expired"
        self._clear_lease(challenge)
        challenge.last_failure_code = reason_code
        self._record_event(
            session,
            None,
            event_type="platform.domain.challenge.expired",
            action="onboarding.domain.challenge.expire",
            outcome="failure",
            request_id=request_id,
            target_ref=onboarding.id,
            tenant_id=onboarding.tenant_id,
            reason_code=reason_code,
            occurred_at=now,
        )

    def _close_current_challenge(
        self,
        session: Session,
        onboarding: TenantOnboardingRequest,
        *,
        status: str,
        actor: PlatformPrincipal | None,
        request_id: str | None,
        now: datetime,
        reason_code: str,
    ) -> None:
        challenge = TenantOnboardingRepository(session).current_challenge(
            onboarding.id, lock=True
        )
        if challenge is None:
            return
        challenge.status = status
        self._clear_lease(challenge)
        challenge.last_failure_code = reason_code if status == "expired" else None
        self._record_event(
            session,
            actor,
            event_type=(
                "platform.domain.challenge.expired"
                if status == "expired"
                else "platform.domain.challenge.superseded"
            ),
            action=(
                "onboarding.domain.challenge.expire"
                if status == "expired"
                else "onboarding.domain.challenge.supersede"
            ),
            outcome="failure" if status == "expired" else "success",
            request_id=request_id,
            target_ref=onboarding.id,
            tenant_id=onboarding.tenant_id,
            reason_code=reason_code,
            occurred_at=now,
        )

    def _expire_stale_lease(
        self,
        session: Session,
        onboarding: TenantOnboardingRequest,
        challenge: DomainVerificationChallenge,
        now: datetime,
    ) -> None:
        challenge.status = "superseded"
        self._clear_lease(challenge)
        challenge.last_failure_code = "verification_lease_expired"
        self._record_event(
            session,
            None,
            event_type="platform.domain.verification.failed",
            action="onboarding.domain.verify",
            outcome="failure",
            request_id=None,
            target_ref=onboarding.id,
            tenant_id=onboarding.tenant_id,
            reason_code="verification_lease_expired",
            occurred_at=now,
        )

    @staticmethod
    def _clear_lease(challenge: DomainVerificationChallenge) -> None:
        challenge.verification_lease_id = None
        challenge.lease_expires_at = None

    @staticmethod
    def _owns_lease(challenge: DomainVerificationChallenge, lease_id: UUID) -> bool:
        return challenge.status == "checking" and challenge.verification_lease_id == lease_id

    @staticmethod
    def _challenge_digest(request_id: UUID, domain: str, raw_value: str) -> str:
        material = b"\x00".join(
            (
                _CHALLENGE_PURPOSE.encode("ascii"),
                request_id.bytes,
                domain.encode("ascii"),
                raw_value.encode("ascii"),
            )
        )
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _dns_owner(domain: str) -> str:
        return f"{_DNS_TXT_OWNER_PREFIX}{domain}."

    def _contains_exact_proof(self, job: _VerificationJob, txt_values: tuple[str, ...]) -> bool:
        for value in txt_values:
            if not value.startswith(_DNS_TXT_VALUE_PREFIX):
                continue
            raw_value = value[len(_DNS_TXT_VALUE_PREFIX) :]
            if len(raw_value) != 43 or any(char not in _CHALLENGE_ALPHABET for char in raw_value):
                continue
            candidate = self._challenge_digest(job.request_id, job.normalized_domain, raw_value)
            if hmac.compare_digest(candidate, job.challenge_hash):
                return True
        return False

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize SQL timestamps returned without tzinfo by SQLite test stores."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _summary(
        onboarding: TenantOnboardingRequest,
        tenant: Tenant,
        *,
        domain: TenantDomain | None = None,
    ) -> OnboardingRequestSummary:
        return OnboardingRequestSummary(
            request_id=onboarding.id,
            tenant_slug=tenant.slug,
            domain=onboarding.normalized_domain,
            state=onboarding.state,
            created_at=TenantOnboardingService._as_utc(onboarding.created_at),
            expires_at=TenantOnboardingService._as_utc(onboarding.expires_at),
            domain_verified=bool(domain is not None or onboarding.tenant_domain_id is not None),
        )

    @staticmethod
    def _history_entry(event: AuditEvent) -> OnboardingHistoryEntry:
        return OnboardingHistoryEntry(
            event_type=event.event_type,
            action=event.action,
            outcome=event.outcome,
            occurred_at=TenantOnboardingService._as_utc(event.occurred_at),
            actor_category=event.actor_category,
            reason_code=event.decision_metadata.get("reason_code"),
        )

    def _recover_create_integrity(
        self,
        principal: PlatformPrincipal,
        *,
        tenant_slug: str,
        normalized_domain: str,
        idempotency_key: str,
        request_id: str | None,
        now: datetime,
    ) -> tuple[OnboardingRequestSummary | None, bool, Exception | None]:
        result: OnboardingRequestSummary | None = None
        replayed = False
        failure: Exception | None = None
        with self._session_factory() as session, session.begin():
            authorized = self._allow(
                session, principal, "platform.onboarding.create", request_id, now
            )
            repository = TenantOnboardingRepository(session)
            existing = (
                repository.find_idempotent_request(principal.operator_id, idempotency_key)
                if authorized
                else None
            )
            if not authorized:
                failure = PlatformOperatorNotAuthorized()
            elif existing is not None:
                tenant = repository.get_tenant(existing.tenant_id)
                if (
                    tenant is None
                    or tenant.slug != tenant_slug
                    or existing.normalized_domain != normalized_domain
                ):
                    failure = IdempotencyConflict()
                    reason = "idempotency_key_conflict"
                else:
                    self._expire_request_if_due(session, existing, now)
                    result = self._summary(existing, tenant)
                    replayed = True
                    reason = "idempotent_replay"
                self._audit_request_attempt(
                    session,
                    principal,
                    action="onboarding.request.create",
                    outcome="denied" if failure else "success",
                    reason_code=reason,
                    request_id=request_id,
                    target_ref=existing.id,
                    tenant_id=existing.tenant_id,
                    occurred_at=now,
                )
            elif session.scalar(
                select(Tenant.id).where(func.lower(Tenant.slug) == tenant_slug).limit(1)
            ) is not None:
                failure = TenantSlugUnavailable()
                self._audit_request_attempt(
                    session,
                    principal,
                    action="onboarding.request.create",
                    outcome="denied",
                    reason_code="tenant_slug_unavailable",
                    request_id=request_id,
                    occurred_at=now,
                )
            else:
                failure = DomainClaimUnavailable()
                self._audit_request_attempt(
                    session,
                    principal,
                    action="onboarding.request.create",
                    outcome="denied",
                    reason_code="domain_claim_unavailable",
                    request_id=request_id,
                    occurred_at=now,
                )
        return result, replayed, failure



