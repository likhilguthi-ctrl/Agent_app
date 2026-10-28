from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.agent_appointments.api import require_super_admin
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.subscriptions.models import MemberSubscription, SubscriptionPlan
from app.modules.subscriptions.schemas import (
    PlanCreateRequest,
    PlanListResponse,
    PlanSummary,
    PlanUpdateRequest,
    SubscriptionCancelRequest,
    SubscriptionCreateRequest,
    SubscriptionListResponse,
    SubscriptionSummary,
)
from app.modules.subscriptions.service import SubscriptionService, get_subscription_service

# Routers
platform_plans_router = APIRouter(
    prefix="/platform/subscription-plans",
    tags=["platform-subscription-plans"],
)

tenant_plans_router = APIRouter(
    prefix="/subscription-plans",
    tags=["tenant-subscription-plans"],
)

tenant_subscriptions_router = APIRouter(
    prefix="/subscriptions",
    tags=["tenant-subscriptions"],
)


def _to_plan_summary(plan: SubscriptionPlan) -> PlanSummary:
    comm_name = None
    if hasattr(plan, "community") and plan.community:
        comm_name = getattr(plan.community, "slug", None)
    return PlanSummary(
        id=plan.id,
        community_id=plan.community_id,
        community_name=comm_name,
        code=plan.code,
        name=plan.name,
        description=plan.description,
        benefits=plan.benefits,
        duration_days=plan.duration_days,
        status=plan.status,
        nominal_price=plan.nominal_price,
        currency=plan.currency,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def _to_subscription_summary(sub: MemberSubscription) -> SubscriptionSummary:
    return SubscriptionSummary(
        id=sub.id,
        tenant_id=sub.tenant_id,
        subscriber_membership_id=sub.subscriber_membership_id,
        plan_id=sub.plan_id,
        agreed_amount=sub.agreed_amount,
        currency=sub.currency,
        starts_at=sub.starts_at,
        expires_at=sub.expires_at,
        status=sub.status,
        payment_status=sub.payment_status,
        snapshot_plan_name=sub.snapshot_plan_name,
        snapshot_duration_days=sub.snapshot_duration_days,
        notes=sub.notes,
        created_by_user_id=sub.created_by_user_id,
        created_at=sub.created_at,
        updated_at=sub.updated_at,
    )


# -----------------------------------------------------------------------------
# 1. Platform Super Admin: Plan Management Endpoints
# -----------------------------------------------------------------------------
@platform_plans_router.post(
    "",
    response_model=PlanSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Platform Super Admin: Create a new subscription plan",
)
def create_subscription_plan(
    body: PlanCreateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> PlanSummary:
    if not body.community_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "COMMUNITY_REQUIRED", "message": "Community ID is required when creating a subscription plan."},
        )
    plan = service.create_plan(
        session,
        request=body,
        creator_user_id=operator.user_id,
    )
    session.commit()
    return _to_plan_summary(plan)


@platform_plans_router.get(
    "",
    response_model=PlanListResponse,
    summary="Platform Super Admin: List all subscription plans across all statuses",
)
def list_platform_subscription_plans(
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
    status: Annotated[str | None, Query(pattern=r"^(active|inactive)$")] = None,
    community_id: Annotated[UUID | None, Query()] = None,
) -> PlanListResponse:
    plans = service.list_plans(session, status_filter=status, community_id=community_id)
    items = [_to_plan_summary(p) for p in plans]
    return PlanListResponse(items=items, total=len(items))


@platform_plans_router.get(
    "/{plan_id}",
    response_model=PlanSummary,
    summary="Platform Super Admin: Get subscription plan details",
)
def get_platform_subscription_plan(
    plan_id: UUID,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> PlanSummary:
    plan = service.get_plan_by_id(session, plan_id=plan_id)
    return _to_plan_summary(plan)


@platform_plans_router.patch(
    "/{plan_id}",
    response_model=PlanSummary,
    summary="Platform Super Admin: Update subscription plan terms, benefits, or status",
)
def update_subscription_plan(
    plan_id: UUID,
    body: PlanUpdateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> PlanSummary:
    plan = service.update_plan(
        session,
        plan_id=plan_id,
        request=body,
        updater_user_id=operator.user_id,
    )
    session.commit()
    return _to_plan_summary(plan)


@platform_plans_router.delete(
    "/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Platform Super Admin: Delete subscription plan (only if unreferenced)",
)
def delete_subscription_plan(
    plan_id: UUID,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> None:
    service.delete_plan(
        session,
        plan_id=plan_id,
        operator_user_id=operator.user_id,
    )
    session.commit()


# -----------------------------------------------------------------------------
# 2. Tenant Context: Available Plans Catalog
# -----------------------------------------------------------------------------
@tenant_plans_router.get(
    "",
    response_model=PlanListResponse,
    summary="Tenant: List active subscription plans available for subscribers",
)
def list_available_subscription_plans(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> PlanListResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )
    plans = service.list_plans(session, status_filter="active", community_id=caller.tenant_id)
    items = [_to_plan_summary(p) for p in plans]
    return PlanListResponse(items=items, total=len(items))


@tenant_plans_router.get(
    "/{plan_id}",
    response_model=PlanSummary,
    summary="Tenant: Get plan details",
)
def get_available_subscription_plan(
    plan_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> PlanSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )
    plan = service.get_plan_by_id(session, plan_id=plan_id, community_id=caller.tenant_id)
    return _to_plan_summary(plan)


# -----------------------------------------------------------------------------
# 3. Tenant Context: Subscriber Subscriptions
# -----------------------------------------------------------------------------
@tenant_subscriptions_router.post(
    "",
    response_model=SubscriptionSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Tenant: Create and record a subscription with manually entered agreed amount",
)
def create_subscriber_subscription(
    body: SubscriptionCreateRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    sub = service.create_subscription(
        session,
        tenant_id=caller.tenant_id,
        request=body,
        creator_user_id=caller.user_id,
    )
    session.commit()
    return _to_subscription_summary(sub)


@tenant_subscriptions_router.get(
    "",
    response_model=SubscriptionListResponse,
    summary="Tenant: List subscriptions within community",
)
def list_community_subscriptions(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
    subscriber_membership_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[str | None, Query(pattern=r"^(PENDING_PAYMENT|ACTIVE|EXPIRED|CANCELLED)$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> SubscriptionListResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    subs, total = service.list_subscriptions(
        session,
        tenant_id=caller.tenant_id,
        subscriber_membership_id=subscriber_membership_id,
        status_filter=status,
        limit=limit,
        offset=offset,
    )
    items = [_to_subscription_summary(s) for s in subs]
    return SubscriptionListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@tenant_subscriptions_router.get(
    "/{subscription_id}",
    response_model=SubscriptionSummary,
    summary="Tenant: Get subscription details",
)
def get_community_subscription(
    subscription_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    sub = service.get_subscription_by_id(
        session,
        tenant_id=caller.tenant_id,
        subscription_id=subscription_id,
    )
    return _to_subscription_summary(sub)


@tenant_subscriptions_router.post(
    "/{subscription_id}/cancel",
    response_model=SubscriptionSummary,
    summary="Tenant: Cancel a subscription with justification",
)
def cancel_community_subscription(
    subscription_id: UUID,
    body: SubscriptionCancelRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    sub = service.cancel_subscription(
        session,
        tenant_id=caller.tenant_id,
        subscription_id=subscription_id,
        reason=body.reason,
        operator_user_id=caller.user_id,
    )
    session.commit()
    return _to_subscription_summary(sub)


@tenant_subscriptions_router.post(
    "/{subscription_id}/activate",
    response_model=SubscriptionSummary,
    summary="Tenant: Explicitly activate a subscription",
)
def activate_community_subscription(
    subscription_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.SUBSCRIPTION_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    sub = service.activate_subscription(
        session,
        tenant_id=caller.tenant_id,
        subscription_id=subscription_id,
        operator_user_id=caller.user_id,
    )
    session.commit()
    return _to_subscription_summary(sub)

