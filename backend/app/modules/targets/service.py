from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.authentication.models import AccountCredential
from app.modules.identity.models import TenantMembership, User
from app.modules.relationships.models import MemberRelationship
from app.modules.subscriptions.models import MemberSubscription
from app.modules.targets.models import (
    TargetAchievementAdjustment,
    TargetAssignment,
    TargetType,
)
from app.modules.targets.schemas import (
    AllocateAgentTargetRequest,
    CreateAdminTargetRequest,
    TargetAdjustmentRequest,
    TargetProgress,
    TargetResponse,
    TargetSummaryResponse,
)
from app.modules.tenants.models import AdminProfile


class TargetService:
    @staticmethod
    def calculate_progress(session: Session, target: TargetAssignment) -> TargetProgress:
        """Calculate dynamic achievement, remaining, progress %, and operational status."""
        start_dt = datetime.combine(target.start_date, time.min).replace(tzinfo=UTC)
        end_dt = datetime.combine(target.deadline, time.max).replace(tzinfo=UTC)

        auto_count = 0
        target_code = target.target_type.code if target.target_type else ""

        if target.target_type and target.target_type.calculation_type == "AUTOMATIC":
            if target_code == "AGENTS_RECRUITED":
                # Authoritative count: approved agent appointments initiated by this admin
                q = select(func.count(AgentAppointment.id)).where(
                    AgentAppointment.requested_by_user_id == target.assigned_to_user_id,
                    AgentAppointment.status == "APPROVED",
                    AgentAppointment.created_at >= start_dt,
                    AgentAppointment.created_at <= end_dt,
                )
                auto_count = session.execute(q).scalar() or 0

            elif target_code == "USERS_REGISTERED":
                if target.assigned_role == "AGENT":
                    # Count subscribers registered by this agent
                    q = select(func.count(SubscriberProfile.id)).where(
                        SubscriberProfile.registered_by_agent_membership_id == target.assigned_to_membership_id,
                        SubscriberProfile.created_at >= start_dt,
                        SubscriberProfile.created_at <= end_dt,
                    )
                    auto_count = session.execute(q).scalar() or 0
                else:
                    # For ADMIN: aggregate subscribers registered by all managed agents in this tenant
                    agent_memberships_q = (
                        select(MemberRelationship.child_member_id)
                        .where(
                            MemberRelationship.parent_member_id == target.assigned_to_membership_id,
                            MemberRelationship.status == "ACTIVE",
                        )
                    )
                    appt_memberships_q = (
                        select(AgentAppointment.membership_id)
                        .where(
                            AgentAppointment.requested_by_user_id == target.assigned_to_user_id,
                            AgentAppointment.status == "APPROVED",
                        )
                    )
                    q = select(func.count(SubscriberProfile.id)).where(
                        SubscriberProfile.tenant_id == target.tenant_id,
                        or_(
                            SubscriberProfile.registered_by_agent_membership_id.in_(agent_memberships_q),
                            SubscriberProfile.registered_by_agent_membership_id.in_(appt_memberships_q),
                        ),
                        SubscriberProfile.created_at >= start_dt,
                        SubscriberProfile.created_at <= end_dt,
                    )
                    auto_count = session.execute(q).scalar() or 0

            elif target_code == "SUBSCRIPTIONS_GENERATED":
                if target.assigned_role == "AGENT":
                    q = select(func.count(MemberSubscription.id)).where(
                        MemberSubscription.created_by_user_id == target.assigned_to_user_id,
                        or_(
                            MemberSubscription.status == "ACTIVE",
                            MemberSubscription.payment_status == "SETTLED",
                        ),
                        MemberSubscription.starts_at >= start_dt,
                        MemberSubscription.starts_at <= end_dt,
                    )
                    auto_count = session.execute(q).scalar() or 0
                else:
                    # For ADMIN: count active subscriptions across the community tenant
                    q = select(func.count(MemberSubscription.id)).where(
                        MemberSubscription.tenant_id == target.tenant_id,
                        or_(
                            MemberSubscription.status == "ACTIVE",
                            MemberSubscription.payment_status == "SETTLED",
                        ),
                        MemberSubscription.starts_at >= start_dt,
                        MemberSubscription.starts_at <= end_dt,
                    )
                    auto_count = session.execute(q).scalar() or 0

            elif target_code == "CONTENT_UPLOADED":
                # Documented gap: No PostgreSQL content catalog table exists.
                # Auto count is 0; achievements must be recorded via manual adjustments.
                auto_count = 0

        # Calculate manual adjustments
        adj_q = select(func.coalesce(func.sum(TargetAchievementAdjustment.adjustment_quantity), 0)).where(
            TargetAchievementAdjustment.target_id == target.id
        )
        adjustments_total = session.execute(adj_q).scalar() or 0

        achieved = max(0, int(auto_count) + int(adjustments_total))
        remaining = max(target.target_quantity - achieved, 0)
        progress_pct = (
            round((Decimal(achieved) / Decimal(target.target_quantity)) * Decimal(100), 2)
            if target.target_quantity > 0
            else Decimal("0.0")
        )

        # Dynamic status resolution
        today = date.today()
        is_completed = achieved >= target.target_quantity
        is_overdue = today > target.deadline and not is_completed

        if target.status == "CANCELLED":
            op_status = "CANCELLED"
        elif is_completed:
            op_status = "COMPLETED"
        elif is_overdue:
            op_status = "OVERDUE"
        else:
            op_status = "ACTIVE"

        return TargetProgress(
            target_quantity=target.target_quantity,
            achieved_quantity=achieved,
            remaining_quantity=remaining,
            progress_percentage=float(progress_pct),
            status=op_status,
            is_overdue=is_overdue,
            is_completed=is_completed,
        )

    @classmethod
    def resolve_assignee_name(cls, session: Session, user_id: UUID) -> str:
        """Resolve a display name for the assignee from profiles or credentials."""
        # Check AdminProfile
        admin = session.execute(
            select(AdminProfile).where(AdminProfile.user_id == user_id)
        ).scalar_one_or_none()
        if admin and (admin.first_name or admin.last_name):
            return f"{admin.first_name} {admin.last_name}".strip()

        # Check AgentProfile
        agent = session.execute(
            select(AgentProfile).where(AgentProfile.user_id == user_id)
        ).scalar_one_or_none()
        if agent and (agent.first_name or agent.last_name):
            return f"{agent.first_name} {agent.last_name}".strip()

        # Check AccountCredential
        cred = session.execute(
            select(AccountCredential).where(AccountCredential.user_id == user_id)
        ).scalar_one_or_none()
        if cred and cred.username:
            return cred.username

        return str(user_id)

    @classmethod
    def build_target_response(cls, session: Session, target: TargetAssignment) -> TargetResponse:
        """Assemble a complete TargetResponse with calculated progress and child allocation metrics."""
        progress = cls.calculate_progress(session, target)
        assignee_name = cls.resolve_assignee_name(session, target.assigned_to_user_id)

        # Child allocations (for admin parent targets)
        allocated_to_children = 0
        if target.assigned_role == "ADMIN":
            child_alloc_q = select(
                func.coalesce(func.sum(TargetAssignment.target_quantity), 0)
            ).where(
                TargetAssignment.parent_target_id == target.id,
                TargetAssignment.status != "CANCELLED",
            )
            allocated_to_children = session.execute(child_alloc_q).scalar() or 0

        remaining_to_allocate = max(target.target_quantity - allocated_to_children, 0)

        # Count adjustments
        adj_count_q = select(func.count(TargetAchievementAdjustment.id)).where(
            TargetAchievementAdjustment.target_id == target.id
        )
        adjustments_count = session.execute(adj_count_q).scalar() or 0

        return TargetResponse(
            id=target.id,
            tenant_id=target.tenant_id,
            parent_target_id=target.parent_target_id,
            target_type_id=target.target_type_id,
            target_type_code=target.target_type.code if target.target_type else "",
            target_type_name=target.target_type.name if target.target_type else "",
            target_type_unit=target.target_type.unit if target.target_type else "",
            calculation_type=target.target_type.calculation_type if target.target_type else "MANUAL",
            assigned_to_user_id=target.assigned_to_user_id,
            assigned_to_name=assignee_name,
            assigned_to_role=target.assigned_role,
            assigned_by_user_id=target.assigned_by_user_id,
            target_quantity=target.target_quantity,
            achieved_quantity=progress.achieved_quantity,
            remaining_quantity=progress.remaining_quantity,
            progress_percentage=progress.progress_percentage,
            start_date=target.start_date,
            deadline=target.deadline,
            period=target.period,
            status=progress.status,
            notes=target.notes,
            created_at=target.created_at,
            updated_at=target.updated_at,
            allocated_to_children=allocated_to_children,
            remaining_to_allocate=remaining_to_allocate,
            adjustments_count=adjustments_count,
        )

    @classmethod
    def create_admin_target(
        cls,
        session: Session,
        creator_user_id: UUID,
        req: CreateAdminTargetRequest,
    ) -> TargetAssignment:
        """Super Admin assigns an authorized target to an Administrator."""
        if req.start_date > req.deadline:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_DATES", "message": "Target start_date cannot be after deadline."},
            )

        # Fetch TargetType
        target_type = session.execute(
            select(TargetType).where(
                TargetType.code == req.target_type_code,
                TargetType.is_active == True,
            )
        ).scalar_one_or_none()
        if not target_type:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "TARGET_TYPE_NOT_FOUND", "message": f"Target type '{req.target_type_code}' not found or inactive."},
            )

        if target_type.applicable_role not in ("ADMIN", "BOTH"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "ROLE_MISMATCH", "message": f"Target type '{target_type.code}' cannot be assigned to an administrator."},
            )

        # Resolve admin's tenant and membership
        admin_membership = session.execute(
            select(TenantMembership).where(
                TenantMembership.user_id == req.admin_user_id,
                TenantMembership.status == "active",
            )
        ).scalars().first()

        tenant_id = admin_membership.tenant_id if admin_membership else None
        membership_id = admin_membership.id if admin_membership else None

        now = datetime.now(UTC)
        assignment = TargetAssignment(
            id=uuid4(),
            tenant_id=tenant_id,
            parent_target_id=None,
            target_type_id=target_type.id,
            assigned_to_user_id=req.admin_user_id,
            assigned_to_membership_id=membership_id,
            assigned_by_user_id=creator_user_id,
            assigned_role="ADMIN",
            target_quantity=req.target_quantity,
            start_date=req.start_date,
            deadline=req.deadline,
            period=req.period,
            status="ACTIVE",
            notes=req.notes,
            created_at=now,
            updated_at=now,
        )
        session.add(assignment)
        session.flush()
        return assignment

    @classmethod
    def allocate_agent_target(
        cls,
        session: Session,
        admin_user_id: UUID,
        admin_tenant_id: UUID | None,
        req: AllocateAgentTargetRequest,
        is_super_admin: bool = False,
    ) -> TargetAssignment:
        """Admin cascades a child target to an Agent from an authorized parent target allocation."""
        # 1. Fetch parent target
        parent_target = session.get(TargetAssignment, req.parent_target_id)
        if not parent_target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PARENT_TARGET_NOT_FOUND", "message": "Parent target assignment not found."},
            )

        if parent_target.assigned_role != "ADMIN":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_PARENT_TARGET", "message": "Can only allocate agent targets from an administrator target."},
            )

        # RBAC tenant / ownership validation
        if not is_super_admin:
            if parent_target.assigned_to_user_id != admin_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "TARGET_ACCESS_DENIED", "message": "You can only allocate from targets assigned to you."},
                )
            if admin_tenant_id and parent_target.tenant_id and parent_target.tenant_id != admin_tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "TENANT_MISMATCH", "message": "Parent target does not belong to your community."},
                )

        if parent_target.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "TARGET_INACTIVE", "message": "Cannot allocate from a cancelled parent target."},
            )

        target_type = parent_target.target_type
        if target_type.applicable_role not in ("AGENT", "BOTH"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "KPI_NOT_APPLICABLE_TO_AGENT",
                    "message": f"Target type '{target_type.code}' cannot be cascaded to field agents.",
                },
            )

        # 2. Date validations
        child_start = req.start_date or parent_target.start_date
        child_deadline = req.deadline or parent_target.deadline

        if child_start > child_deadline:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_DATES", "message": "Start date cannot be after deadline."},
            )

        if child_start < parent_target.start_date or child_deadline > parent_target.deadline:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "DATES_OUT_OF_BOUNDS",
                    "message": (
                        f"Child target date range ({child_start} to {child_deadline}) "
                        f"must fall within parent target range ({parent_target.start_date} to {parent_target.deadline})."
                    ),
                },
            )

        # 3. Check allocation limit (Parent-Child Quota Enforcement)
        allocated_so_far_q = select(
            func.coalesce(func.sum(TargetAssignment.target_quantity), 0)
        ).where(
            TargetAssignment.parent_target_id == parent_target.id,
            TargetAssignment.status != "CANCELLED",
        )
        allocated_so_far = session.execute(allocated_so_far_q).scalar() or 0

        if allocated_so_far + req.target_quantity > parent_target.target_quantity:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "TARGET_ALLOCATION_EXCEEDED",
                    "message": (
                        f"Allocation would exceed parent quota. Cannot allocate quantity {req.target_quantity}. "
                        f"Parent quota is {parent_target.target_quantity}, "
                        f"and {allocated_so_far} has already been cascaded to team members. "
                        f"Remaining available: {max(parent_target.target_quantity - allocated_so_far, 0)}."
                    ),
                },
            )

        # 4. Resolve agent's membership
        agent_membership = session.execute(
            select(TenantMembership).where(
                TenantMembership.user_id == req.agent_user_id,
                TenantMembership.status == "active",
            )
        ).scalars().first()

        tenant_id = parent_target.tenant_id or (agent_membership.tenant_id if agent_membership else None)
        membership_id = agent_membership.id if agent_membership else None

        now = datetime.now(UTC)
        child_target = TargetAssignment(
            id=uuid4(),
            tenant_id=tenant_id,
            parent_target_id=parent_target.id,
            target_type_id=parent_target.target_type_id,
            assigned_to_user_id=req.agent_user_id,
            assigned_to_membership_id=membership_id,
            assigned_by_user_id=admin_user_id,
            assigned_role="AGENT",
            target_quantity=req.target_quantity,
            start_date=child_start,
            deadline=child_deadline,
            period=req.period or parent_target.period,
            status="ACTIVE",
            notes=req.notes,
            created_at=now,
            updated_at=now,
        )
        session.add(child_target)
        session.flush()
        return child_target

    @classmethod
    def record_adjustment(
        cls,
        session: Session,
        target_id: UUID,
        caller_user_id: UUID,
        caller_role: str,
        caller_tenant_id: UUID | None,
        req: TargetAdjustmentRequest,
    ) -> TargetAchievementAdjustment:
        """Record an auditable manual achievement adjustment."""
        target = session.get(TargetAssignment, target_id)
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "TARGET_NOT_FOUND", "message": "Target assignment not found."},
            )

        if target.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "TARGET_CANCELLED", "message": "Cannot adjust achievement on a cancelled target."},
            )

        # RBAC verification
        if caller_role != "SUPER_ADMIN":
            if caller_tenant_id and target.tenant_id and target.tenant_id != caller_tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "TENANT_MISMATCH", "message": "You can only adjust targets within your community."},
                )

        adjustment = TargetAchievementAdjustment(
            id=uuid4(),
            target_id=target.id,
            tenant_id=target.tenant_id,
            adjusted_by_user_id=caller_user_id,
            adjustment_quantity=req.adjustment_quantity,
            reason=req.reason.strip(),
            created_at=datetime.now(UTC),
        )
        session.add(adjustment)
        session.flush()
        return adjustment

    @classmethod
    def calculate_summary(cls, session: Session, targets: list[TargetAssignment]) -> TargetSummaryResponse:
        """Calculate aggregate performance summary over a list of TargetAssignment models."""
        total_targets = len(targets)
        active_count = 0
        completed_count = 0
        overdue_count = 0
        total_target_qty = 0
        total_achieved_qty = 0

        for t in targets:
            progress = cls.calculate_progress(session, t)
            total_target_qty += t.target_quantity
            total_achieved_qty += progress.achieved_quantity

            if progress.status == "COMPLETED":
                completed_count += 1
            elif progress.status == "OVERDUE":
                overdue_count += 1
            elif progress.status == "ACTIVE":
                active_count += 1

        overall_pct = (
            round((Decimal(total_achieved_qty) / Decimal(total_target_qty)) * Decimal(100), 2)
            if total_target_qty > 0
            else Decimal("0.0")
        )

        return TargetSummaryResponse(
            total_targets=total_targets,
            active_targets=active_count,
            completed_targets=completed_count,
            overdue_targets=overdue_count,
            total_target_quantity=total_target_qty,
            total_achieved_quantity=total_achieved_qty,
            overall_progress_percentage=float(overall_pct),
        )
