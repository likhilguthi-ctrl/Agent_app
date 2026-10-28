from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.identity.models import TenantMembership
from app.modules.tenants.models import Tenant
from app.modules.subscriptions.models import (
    MemberSubscription,
    PlanStatus,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.subscriptions.schemas import (
    PlanCreateRequest,
    PlanUpdateRequest,
    SubscriptionCreateRequest,
)


class SubscriptionService:
    """Core domain service for subscription plans and subscriber subscriptions.

    Enforces manual subscription amount entry, immutable historical terms snapshots,
    strict multi-tenant isolation, and explicit lifecycle state transitions.
    """

    def __init__(self, audit_service: AuditService | None = None) -> None:
        self._audit = audit_service or AuditService()

    # -------------------------------------------------------------------------
    # 1. Platform Super Admin: Subscription Plans Management
    # -------------------------------------------------------------------------
    def create_plan(
        self,
        session: Session,
        request: PlanCreateRequest,
        creator_user_id: UUID | None,
        expected_community_id: UUID | None = None,
    ) -> SubscriptionPlan:
        # Determine and validate community_id
        target_community_id = request.community_id or expected_community_id
        if not target_community_id:
            first_tenant = session.execute(
                select(Tenant.id).where(Tenant.status == "active").order_by(Tenant.created_at.asc()).limit(1)
            ).scalar_one_or_none()
            if not first_tenant:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "COMMUNITY_REQUIRED", "message": "Community ID is required when creating a subscription plan."},
                )
            target_community_id = first_tenant

        if expected_community_id and target_community_id != expected_community_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_COMMUNITY_PLAN_FORBIDDEN", "message": "Cannot create plans for other communities."},
            )

        tenant = session.execute(
            select(Tenant).where(Tenant.id == target_community_id)
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
            )
        if tenant.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "COMMUNITY_INACTIVE", "message": "Cannot create plan for inactive community."},
            )

        # Check code uniqueness within this specific community
        existing = session.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.community_id == target_community_id,
                SubscriptionPlan.code == request.code,
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "PLAN_CODE_EXISTS", "message": f"Plan code '{request.code}' already exists in this community."},
            )

        nominal = (
            request.nominal_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if request.nominal_price is not None
            else None
        )

        plan = SubscriptionPlan(
            community_id=target_community_id,
            code=request.code,
            name=request.name,
            description=request.description,
            benefits=request.benefits,
            duration_days=request.duration_days,
            status=request.status.value,
            nominal_price=nominal,
            currency=request.currency,
            created_by_user_id=creator_user_id,
            updated_by_user_id=creator_user_id,
        )
        session.add(plan)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.plan.created",
                actor_category="platform_operator" if creator_user_id else "system",
                actor_ref=str(creator_user_id) if creator_user_id else "system:subscription_service",
                action="subscription.plan.created",
                outcome="success",
                tenant_id=target_community_id,
                target_entity_type="subscription_plan",
                target_entity_ref=str(plan.id),
            ),
        )
        return plan

    def update_plan(
        self,
        session: Session,
        plan_id: UUID,
        request: PlanUpdateRequest,
        updater_user_id: UUID | None,
        expected_community_id: UUID | None = None,
    ) -> SubscriptionPlan:
        plan = session.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id).with_for_update()
        ).scalar_one_or_none()
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PLAN_NOT_FOUND", "message": "Subscription plan not found."},
            )

        if expected_community_id and plan.community_id != expected_community_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_COMMUNITY_PLAN_FORBIDDEN", "message": "Cannot modify plans belonging to other communities."},
            )

        if request.name is not None:
            plan.name = request.name
        if request.description is not None:
            plan.description = request.description
        if request.benefits is not None:
            plan.benefits = request.benefits
        if request.duration_days is not None:
            plan.duration_days = request.duration_days
        if request.status is not None:
            plan.status = request.status.value
        if request.nominal_price is not None:
            plan.nominal_price = request.nominal_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if request.currency is not None:
            plan.currency = request.currency

        plan.updated_by_user_id = updater_user_id
        plan.updated_at = datetime.now(UTC)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.plan.updated",
                actor_category="platform_operator" if updater_user_id else "system",
                actor_ref=str(updater_user_id) if updater_user_id else "system:subscription_service",
                action="subscription.plan.updated",
                outcome="success",
                tenant_id=plan.community_id,
                target_entity_type="subscription_plan",
                target_entity_ref=str(plan.id),
            ),
        )
        return plan

    def delete_plan(
        self,
        session: Session,
        plan_id: UUID,
        operator_user_id: UUID | None,
        expected_community_id: UUID | None = None,
    ) -> None:
        plan = session.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id).with_for_update()
        ).scalar_one_or_none()
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PLAN_NOT_FOUND", "message": "Subscription plan not found."},
            )

        if expected_community_id and plan.community_id != expected_community_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_COMMUNITY_PLAN_FORBIDDEN", "message": "Cannot delete plans belonging to other communities."},
            )

        # Check for referencing subscriptions
        count = session.execute(
            select(MemberSubscription.id).where(MemberSubscription.plan_id == plan_id).limit(1)
        ).scalar_one_or_none()
        if count is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "PLAN_IN_USE",
                    "message": "Cannot delete a plan referenced by existing subscriptions. Deactivate it instead.",
                },
            )

        session.delete(plan)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.plan.deleted",
                actor_category="platform_operator" if operator_user_id else "system",
                actor_ref=str(operator_user_id) if operator_user_id else "system:subscription_service",
                action="subscription.plan.deleted",
                outcome="success",
                tenant_id=plan.community_id,
                target_entity_type="subscription_plan",
                target_entity_ref=str(plan_id),
            ),
        )

    def list_plans(
        self,
        session: Session,
        status_filter: str | None = None,
        community_id: UUID | None = None,
    ) -> list[SubscriptionPlan]:
        query = select(SubscriptionPlan).order_by(SubscriptionPlan.created_at.desc())
        if community_id is not None:
            query = query.where(SubscriptionPlan.community_id == community_id)
        if status_filter:
            query = query.where(SubscriptionPlan.status == status_filter)
        return list(session.execute(query).scalars().all())

    def get_plan_by_id(
        self,
        session: Session,
        plan_id: UUID,
        community_id: UUID | None = None,
    ) -> SubscriptionPlan:
        query = select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id)
        if community_id is not None:
            query = query.where(SubscriptionPlan.community_id == community_id)
        plan = session.execute(query).scalar_one_or_none()
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PLAN_NOT_FOUND", "message": "Subscription plan not found."},
            )
        return plan

    # -------------------------------------------------------------------------
    # 2. Tenant Context: Subscriber Subscriptions
    # -------------------------------------------------------------------------
    def create_subscription(
        self,
        session: Session,
        tenant_id: UUID,
        request: SubscriptionCreateRequest,
        creator_user_id: UUID,
    ) -> MemberSubscription:
        # 1. Verify subscriber membership belongs to tenant
        membership = session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.id == request.subscriber_membership_id,
            )
        ).scalar_one_or_none()
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "SUBSCRIBER_NOT_FOUND",
                    "message": "Subscriber membership not found in this community.",
                },
            )

        # 2. Verify plan exists, belongs to this community, and is active
        plan = session.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == request.plan_id)
        ).scalar_one_or_none()
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PLAN_NOT_FOUND", "message": "Subscription plan not found."},
            )
        if plan.community_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "CROSS_COMMUNITY_PLAN_FORBIDDEN",
                    "message": "Cannot subscribe to a plan that belongs to a different community.",
                },
            )
        if plan.status != PlanStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "PLAN_INACTIVE",
                    "message": "Cannot create subscription for an inactive plan.",
                },
            )

        # 3. Monetary precision & manual agreed amount
        amount = request.agreed_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount < Decimal("0.00"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "INVALID_AMOUNT", "message": "Subscription amount cannot be negative."},
            )

        # 4. Dates & term calculation
        now = datetime.now(UTC)
        starts_at = request.starts_at or now
        expires_at = starts_at + timedelta(days=plan.duration_days)

        # 5. Persist MemberSubscription with immutable plan snapshots
        sub = MemberSubscription(
            tenant_id=tenant_id,
            subscriber_membership_id=request.subscriber_membership_id,
            plan_id=request.plan_id,
            agreed_amount=amount,
            currency=request.currency,
            starts_at=starts_at,
            expires_at=expires_at,
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=plan.duration_days,
            notes=request.notes,
            created_by_user_id=creator_user_id,
        )
        session.add(sub)
        session.flush()

        # 6. Audit Trail
        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.created",
                actor_category="tenant_member",
                actor_ref=str(creator_user_id),
                action="subscription.created",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="member_subscription",
                target_entity_ref=str(sub.id),
            ),
        )
        return sub

    def list_subscriptions(
        self,
        session: Session,
        tenant_id: UUID,
        subscriber_membership_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MemberSubscription], int]:
        base_query = select(MemberSubscription).where(MemberSubscription.tenant_id == tenant_id)
        if subscriber_membership_id:
            base_query = base_query.where(
                MemberSubscription.subscriber_membership_id == subscriber_membership_id
            )
        if status_filter:
            base_query = base_query.where(MemberSubscription.status == status_filter)

        total = len(list(session.execute(base_query).scalars().all()))
        items = list(
            session.execute(
                base_query.order_by(MemberSubscription.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return items, total

    def get_subscription_by_id(
        self,
        session: Session,
        tenant_id: UUID,
        subscription_id: UUID,
    ) -> MemberSubscription:
        sub = session.execute(
            select(MemberSubscription).where(
                MemberSubscription.tenant_id == tenant_id,
                MemberSubscription.id == subscription_id,
            )
        ).scalar_one_or_none()
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": "Subscription not found."},
            )
        return sub

    def cancel_subscription(
        self,
        session: Session,
        tenant_id: UUID,
        subscription_id: UUID,
        reason: str,
        operator_user_id: UUID,
    ) -> MemberSubscription:
        sub = session.execute(
            select(MemberSubscription)
            .where(
                MemberSubscription.tenant_id == tenant_id,
                MemberSubscription.id == subscription_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": "Subscription not found."},
            )

        if sub.status in (SubscriptionStatus.CANCELLED.value, SubscriptionStatus.EXPIRED.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "CANNOT_CANCEL_TERMINATED_SUBSCRIPTION",
                    "message": f"Cannot cancel subscription in '{sub.status}' state.",
                },
            )

        sub.status = SubscriptionStatus.CANCELLED.value
        if isinstance(sub.notes, dict):
            updated_notes = dict(sub.notes)
            updated_notes["cancellation_reason"] = reason
            sub.notes = updated_notes
        elif isinstance(sub.notes, str):
            sub.notes = f"{sub.notes}\nCancelled: {reason}".strip()
        else:
            sub.notes = {"cancellation_reason": reason}
        sub.updated_at = datetime.now(UTC)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.cancelled",
                actor_category="tenant_member",
                actor_ref=str(operator_user_id),
                action="subscription.cancelled",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="member_subscription",
                target_entity_ref=str(sub.id),
            ),
        )
        return sub

    def activate_subscription(
        self,
        session: Session,
        tenant_id: UUID,
        subscription_id: UUID,
        operator_user_id: UUID,
        verified_settlement: bool = False,
    ) -> MemberSubscription:
        sub = session.execute(
            select(MemberSubscription)
            .where(
                MemberSubscription.tenant_id == tenant_id,
                MemberSubscription.id == subscription_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": "Subscription not found."},
            )

        if sub.status == SubscriptionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "ALREADY_ACTIVE", "message": "Subscription is already active."},
            )
        if sub.status in (SubscriptionStatus.CANCELLED.value, SubscriptionStatus.EXPIRED.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "CANNOT_ACTIVATE_TERMINATED_SUBSCRIPTION",
                    "message": f"Cannot activate subscription in '{sub.status}' state.",
                },
            )

        sub.status = SubscriptionStatus.ACTIVE.value
        if verified_settlement:
            sub.payment_status = SubscriptionPaymentStatus.SETTLED.value
        sub.updated_at = datetime.now(UTC)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.activated",
                actor_category="tenant_member",
                actor_ref=str(operator_user_id),
                action="subscription.activated",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="member_subscription",
                target_entity_ref=str(sub.id),
            ),
        )
        return sub


def get_subscription_service() -> SubscriptionService:
    return SubscriptionService()

