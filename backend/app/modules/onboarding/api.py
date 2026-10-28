from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.core.config import Settings
from app.core.request_id import request_id_context
from app.modules.onboarding.activation import TenantActivationService
from app.modules.onboarding.schemas import (
    ActivationEligibility,
    ActivationIntentResult,
    OnboardingRequestCreate,
    OnboardingRequestCreated,
    OnboardingRequestDetail,
    OnboardingRequestList,
    OnboardingReviewResult,
    OnboardingReviewSubmission,
    OnboardingRequestSummary,
    OnboardingState,
    RejectOnboardingRequest,
    DomainChallengeIssued,
    TenantActivationResult,
)
from app.modules.onboarding.service import TenantOnboardingService
from app.modules.platform_identity.dependencies import (
    get_platform_operator_principal,
    require_platform_csrf,
)
from app.modules.platform_identity.service import PlatformPrincipal


router = APIRouter(prefix="/platform/onboarding-requests", tags=["platform onboarding"])


def get_tenant_onboarding_service(request: Request) -> TenantOnboardingService:
    settings: Settings = request.app.state.settings
    return TenantOnboardingService(settings)


def get_tenant_activation_service(request: Request) -> TenantActivationService:
    return TenantActivationService(
        request.app.state.settings,
        identity_provider=getattr(request.app.state, "platform_identity_provider", None),
        principal_resolver=getattr(request.app.state, "platform_principal_resolver", None),
        audit_sink=getattr(request.app.state, "audit_sink", None),
    )


@router.post(
    "",
    response_model=OnboardingRequestCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pending tenant onboarding request",
    dependencies=[Depends(require_platform_csrf)],
)
def create_request(
    body: OnboardingRequestCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantOnboardingService, Depends(get_tenant_onboarding_service)],
) -> OnboardingRequestCreated:
    result, replayed = service.create_request(
        principal,
        tenant_slug=body.tenant_slug,
        normalized_domain=body.domain,
        idempotency_key=idempotency_key,
        request_id=request_id_context.get(),
    )
    return OnboardingRequestCreated(request=result, replayed=replayed)


@router.get(
    "",
    response_model=OnboardingRequestList,
    summary="List onboarding requests",
)
def list_requests(
    request: Request,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantOnboardingService, Depends(get_tenant_onboarding_service)],
    state: OnboardingState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> OnboardingRequestList:
    return service.list_requests(
        principal,
        state=state.value if state is not None else None,
        limit=limit,
        offset=offset,
        request_id=request_id_context.get(),
    )


@router.get(
    "/{request_id}",
    response_model=OnboardingRequestDetail,
    summary="Retrieve safe onboarding status and audit history",
)
def get_request(
    request_id: UUID,
    request: Request,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantOnboardingService, Depends(get_tenant_onboarding_service)],
) -> OnboardingRequestDetail:
    return service.get_request(
        principal, request_id, request_id=request_id_context.get()
    )


@router.post(
    "/{request_id}/domains/challenge",
    response_model=DomainChallengeIssued,
    summary="Issue a one-time DNS TXT challenge",
    dependencies=[Depends(require_platform_csrf)],
)
def issue_domain_challenge(
    request_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantOnboardingService, Depends(get_tenant_onboarding_service)],
) -> DomainChallengeIssued:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return service.issue_challenge(
        principal,
        request_id,
        idempotency_key=idempotency_key,
        request_id=request_id_context.get(),
    )


@router.post(
    "/{request_id}/domains/verify",
    response_model=OnboardingRequestSummary,
    summary="Verify the exact DNS TXT challenge",
    dependencies=[Depends(require_platform_csrf)],
)
async def verify_domain(
    request_id: UUID,
    request: Request,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantOnboardingService, Depends(get_tenant_onboarding_service)],
) -> OnboardingRequestSummary:
    return await service.verify_domain(
        principal, request_id, request_id=request_id_context.get()
    )


@router.post(
    "/{request_id}/cancel",
    response_model=OnboardingRequestSummary,
    summary="Cancel an eligible onboarding request",
    dependencies=[Depends(require_platform_csrf)],
)
def cancel_request(
    request_id: UUID,
    request: Request,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantOnboardingService, Depends(get_tenant_onboarding_service)],
) -> OnboardingRequestSummary:
    return service.cancel_request(principal, request_id, request_id=request_id_context.get())


@router.post(
    "/{request_id}/reject",
    response_model=OnboardingReviewResult,
    summary="Reject an eligible onboarding request",
    dependencies=[Depends(require_platform_csrf)],
)
def reject_request(
    request_id: UUID,
    body: RejectOnboardingRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantActivationService, Depends(get_tenant_activation_service)],
) -> OnboardingReviewResult:
    return service.submit_review(
        principal,
        request_id,
        decision="reject",
        reason_code=body.reason_code.value,
        idempotency_key=idempotency_key,
        request_id=request_id_context.get(),
    )


@router.post(
    "/{request_id}/review",
    response_model=OnboardingReviewResult,
    summary="Record an independent onboarding review decision",
    dependencies=[Depends(require_platform_csrf)],
)
def submit_review(
    request_id: UUID,
    body: OnboardingReviewSubmission,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantActivationService, Depends(get_tenant_activation_service)],
) -> OnboardingReviewResult:
    return service.submit_review(
        principal,
        request_id,
        decision=body.decision.value,
        reason_code=body.reason_code.value,
        idempotency_key=idempotency_key,
        request_id=request_id_context.get(),
    )


@router.get(
    "/{request_id}/activation-eligibility",
    response_model=ActivationEligibility,
    summary="Evaluate safe tenant activation prerequisites",
)
async def activation_eligibility(
    request_id: UUID,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantActivationService, Depends(get_tenant_activation_service)],
) -> ActivationEligibility:
    return await service.activation_eligibility(
        principal, request_id, request_id=request_id_context.get()
    )


@router.post(
    "/{request_id}/activation-intents",
    response_model=ActivationIntentResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record an activation intent awaiting external audit acknowledgement",
    dependencies=[Depends(require_platform_csrf)],
)
async def create_activation_intent(
    request_id: UUID,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantActivationService, Depends(get_tenant_activation_service)],
) -> ActivationIntentResult:
    return await service.create_activation_intent(
        principal,
        request_id,
        idempotency_key=idempotency_key,
        request_id=request_id_context.get(),
    )


@router.post(
    "/{request_id}/activation-attempts/{attempt_id}/activate",
    response_model=TenantActivationResult,
    summary="Activate only after the matching acknowledged activation intent",
    dependencies=[Depends(require_platform_csrf)],
)
async def activate_tenant(
    request_id: UUID,
    attempt_id: UUID,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    service: Annotated[TenantActivationService, Depends(get_tenant_activation_service)],
) -> TenantActivationResult:
    return await service.activate(
        principal,
        request_id,
        attempt_id,
        request_id=request_id_context.get(),
    )
