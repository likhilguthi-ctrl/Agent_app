from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.agent_appointments.api import require_super_admin
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.identity.models import TenantMembership, User
from app.modules.payouts.models import PayoutRequest
from app.modules.payouts.schemas import (
    AgentEligibleBalanceResponse,
    PayoutCancelRequest,
    PayoutCreateRequest,
    PayoutDisburseRequest,
    PayoutFailRequest,
    PayoutListResponse,
    PayoutReviewRequest,
    PayoutSummary,
)
from app.modules.payouts.service import PayoutService, get_payout_service

logger = logging.getLogger(__name__)

# Community Payout Router (Agent & Community Admin operations within tenant)
community_payouts_router = APIRouter(prefix="/payouts", tags=["community-payouts"])

# Platform Payout Router (Platform Super Admin disbursement execution)
platform_payouts_router = APIRouter(prefix="/platform/payouts", tags=["platform-payouts"])


def _to_payout_summary(p: PayoutRequest) -> PayoutSummary:
    return PayoutSummary(
        id=p.id,
        tenant_id=p.tenant_id,
        agent_membership_id=p.agent_membership_id,
        agent_profile_id=p.agent_profile_id,
        account_id=p.account_id,
        amount=p.amount,
        currency=p.currency,
        status=p.status,
        idempotency_key=p.idempotency_key,
        request_note=p.request_note,
        rejection_reason=p.rejection_reason,
        failure_reason=p.failure_reason,
        reviewed_by_membership_id=p.reviewed_by_membership_id,
        reviewed_at=p.reviewed_at,
        review_notes=p.review_notes,
        disbursed_by_user_id=p.disbursed_by_user_id,
        disbursed_at=p.disbursed_at,
        disbursement_reference=p.disbursement_reference,
        disbursement_method=p.disbursement_method,
        bank_account_snapshot=p.bank_account_snapshot,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


# =============================================================================
# Community Payout Endpoints (Agent & Community Admin)
# =============================================================================

@community_payouts_router.get(
    "/eligible-balance",
    response_model=AgentEligibleBalanceResponse,
    summary="Get authoritative eligible balance and withdrawable amount for an agent",
)
def get_eligible_balance(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.PAYOUT_READ))],
    agent_membership_id: UUID | None = None,
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> AgentEligibleBalanceResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "TENANT_REQUIRED", "message": "Community context required."},
        )

    # Determine target agent: Agents can only view their own; Admins can view any in tenant
    target_membership_id = caller.membership_id
    if agent_membership_id is not None and caller.membership_id != agent_membership_id:
        # Check if caller has review/admin privileges
        if "admin" in caller.role.lower():
            target_membership_id = agent_membership_id
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "ACCESS_DENIED", "message": "Agents can only inspect their own balance."},
            )

    if target_membership_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MEMBERSHIP_REQUIRED", "message": "Target membership ID is required."},
        )

    return service.get_agent_eligible_balance(
        session=db,
        tenant_id=caller.tenant_id,
        agent_membership_id=target_membership_id,
    )


@community_payouts_router.post(
    "/requests",
    response_model=PayoutSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Agent submits a payout request subject to balance check and escrow hold",
)
def submit_payout_request(
    request: PayoutCreateRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.PAYOUT_REQUEST))],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "MEMBERSHIP_REQUIRED", "message": "Active community membership required."},
        )

    caller_mem = db.get(TenantMembership, caller.membership_id)
    caller_user = db.get(User, caller.user_id)
    if caller_mem is None or caller_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CALLER_NOT_FOUND", "message": "Caller identity records not found."},
        )

    payout = service.request_payout(
        session=db,
        tenant_id=caller.tenant_id,
        caller_membership=caller_mem,
        caller_user=caller_user,
        request=request,
    )
    db.commit()
    db.refresh(payout)
    return _to_payout_summary(payout)


@community_payouts_router.get(
    "/requests",
    response_model=PayoutListResponse,
    summary="List payout requests within caller's community context",
)
def list_community_payout_requests(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.PAYOUT_READ))],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    agent_membership_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutListResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "TENANT_REQUIRED", "message": "Community context required."},
        )

    # Agents only see their own requests; Admins can see all or filter
    effective_agent_id = agent_membership_id
    if "admin" not in caller.role.lower():
        effective_agent_id = caller.membership_id

    items, total = service.list_payouts(
        session=db,
        tenant_id=caller.tenant_id,
        agent_membership_id=effective_agent_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )
    return PayoutListResponse(
        items=[_to_payout_summary(p) for p in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@community_payouts_router.get(
    "/requests/{payout_id}",
    response_model=PayoutSummary,
    summary="Get single payout request details within community context",
)
def get_community_payout_detail(
    payout_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.PAYOUT_READ))],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "TENANT_REQUIRED", "message": "Community context required."},
        )

    payout = service.get_payout_by_id(db, payout_id=payout_id, tenant_id=caller.tenant_id)
    if "admin" not in caller.role.lower() and payout.agent_membership_id != caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "ACCESS_DENIED", "message": "You can only view your own payout requests."},
        )
    return _to_payout_summary(payout)


@community_payouts_router.post(
    "/requests/{payout_id}/review",
    response_model=PayoutSummary,
    summary="Community Admin approves or rejects payout request; self-approval strictly rejected",
)
def review_community_payout(
    payout_id: UUID,
    request: PayoutReviewRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.PAYOUT_REVIEW))],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "MEMBERSHIP_REQUIRED", "message": "Community membership required for review."},
        )

    caller_mem = db.get(TenantMembership, caller.membership_id)
    caller_user = db.get(User, caller.user_id)
    if caller_mem is None or caller_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CALLER_NOT_FOUND", "message": "Reviewer identity records not found."},
        )

    payout = service.review_payout(
        session=db,
        tenant_id=caller.tenant_id,
        payout_id=payout_id,
        reviewer_membership=caller_mem,
        reviewer_user=caller_user,
        request=request,
    )
    db.commit()
    db.refresh(payout)
    return _to_payout_summary(payout)


@community_payouts_router.post(
    "/requests/{payout_id}/cancel",
    response_model=PayoutSummary,
    summary="Agent cancels their own pending payout request",
)
def cancel_community_payout(
    payout_id: UUID,
    request: PayoutCancelRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.PAYOUT_REQUEST))],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "MEMBERSHIP_REQUIRED", "message": "Community membership required."},
        )

    caller_mem = db.get(TenantMembership, caller.membership_id)
    caller_user = db.get(User, caller.user_id)
    if caller_mem is None or caller_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CALLER_NOT_FOUND", "message": "Caller identity records not found."},
        )

    payout = service.cancel_payout(
        session=db,
        tenant_id=caller.tenant_id,
        payout_id=payout_id,
        caller_membership=caller_mem,
        caller_user=caller_user,
        request=request,
    )
    db.commit()
    db.refresh(payout)
    return _to_payout_summary(payout)


# =============================================================================
# Platform Super Admin Disbursement Endpoints
# =============================================================================

@platform_payouts_router.get(
    "/requests",
    response_model=PayoutListResponse,
    summary="Platform Super Admin lists payout requests across all communities",
)
def list_platform_payout_requests(
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    tenant_id: UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutListResponse:
    items, total = service.list_payouts(
        session=db,
        tenant_id=tenant_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )
    return PayoutListResponse(
        items=[_to_payout_summary(p) for p in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@platform_payouts_router.get(
    "/requests/{payout_id}",
    response_model=PayoutSummary,
    summary="Platform Super Admin inspects a specific payout request",
)
def get_platform_payout_detail(
    payout_id: UUID,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    payout = service.get_payout_by_id(db, payout_id=payout_id)
    return _to_payout_summary(payout)


@platform_payouts_router.post(
    "/requests/{payout_id}/disburse",
    response_model=PayoutSummary,
    summary="Platform Super Admin executes or confirms fund disbursement with verifiable evidence",
)
def disburse_platform_payout(
    payout_id: UUID,
    request: PayoutDisburseRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    payout = service.disburse_payout(
        session=db,
        payout_id=payout_id,
        operator=operator,
        request=request,
    )
    db.commit()
    db.refresh(payout)
    return _to_payout_summary(payout)


@platform_payouts_router.post(
    "/requests/{payout_id}/fail",
    response_model=PayoutSummary,
    summary="Platform Super Admin records disbursement failure and returns held funds to agent",
)
def fail_platform_payout_disbursement(
    payout_id: UUID,
    request: PayoutFailRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    db: Session = Depends(get_db),
    service: PayoutService = Depends(get_payout_service),
) -> PayoutSummary:
    payout = service.fail_disbursement(
        session=db,
        payout_id=payout_id,
        operator=operator,
        request=request,
    )
    db.commit()
    db.refresh(payout)
    return _to_payout_summary(payout)

