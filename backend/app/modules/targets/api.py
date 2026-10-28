from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership
from app.modules.targets.models import (
    TargetAchievementAdjustment,
    TargetAssignment,
    TargetType,
)
from app.modules.targets.schemas import (
    AllocateAgentTargetRequest,
    CreateAdminTargetRequest,
    TargetAdjustmentRequest,
    TargetAdjustmentResponse,
    TargetResponse,
    TargetSummaryResponse,
    TargetTypeResponse,
)
from app.modules.targets.service import TargetService
from app.modules.tenants.models import AdminProfile, Tenant

router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("/types", response_model=list[TargetTypeResponse], summary="List available KPI target definitions")
def list_target_types(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> list[TargetTypeResponse]:
    types = session.scalars(
        select(TargetType).where(TargetType.is_active == True).order_by(TargetType.name)
    ).all()
    return [TargetTypeResponse.model_validate(t) for t in types]


@router.get("/administrators", summary="List administrators available for target assignment (Super Admin only)")
def list_administrators_for_targets(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    caller, _ = session_data
    if caller.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Super Admin privileges required to view all administrators."},
        )

    # Query all active admin users via UserRoleAssignment and TenantMembership
    q = (
        select(
            TenantMembership.user_id,
            TenantMembership.tenant_id,
            Tenant.slug.label("community_slug"),
            AdminProfile.first_name,
            AdminProfile.last_name,
            AdminProfile.email,
        )
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(UserRoleAssignment, UserRoleAssignment.membership_id == TenantMembership.id)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .outerjoin(AdminProfile, AdminProfile.user_id == TenantMembership.user_id)
        .where(
            Role.code == "admin",
            UserRoleAssignment.status == "active",
            TenantMembership.status == "active",
        )
        .distinct()
    )
    rows = session.execute(q).all()

    admins = []
    seen = set()
    for row in rows:
        if row.user_id in seen:
            continue
        seen.add(row.user_id)
        name = (
            f"{row.first_name} {row.last_name}".strip()
            if (row.first_name or row.last_name)
            else f"Admin ({row.community_slug})"
        )
        admins.append(
            {
                "user_id": str(row.user_id),
                "tenant_id": str(row.tenant_id),
                "community_slug": row.community_slug,
                "name": name,
                "email": row.email or "",
            }
        )
    return admins


@router.post("/admin", response_model=TargetResponse, status_code=status.HTTP_201_CREATED, summary="Super Admin assigns a target to an Administrator")
def create_admin_target(
    body: CreateAdminTargetRequest,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> TargetResponse:
    caller, _ = session_data
    if caller.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Only Super Admin can assign targets to administrators."},
        )

    target = TargetService.create_admin_target(
        session=session,
        creator_user_id=caller.user_id,
        req=body,
    )
    session.commit()
    session.refresh(target)
    return TargetService.build_target_response(session, target)


@router.get("/admin", response_model=list[TargetResponse], summary="List administrator targets (Super Admin or Admin)")
def list_admin_targets(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_READ))],
    session: Annotated[Session, Depends(get_db)],
    admin_user_id: UUID | None = Query(None, description="Filter by administrator user ID"),
    target_type_code: str | None = Query(None, description="Filter by KPI code"),
    status_filter: str | None = Query(None, alias="status", description="Filter by operational status"),
) -> list[TargetResponse]:
    q = select(TargetAssignment).where(TargetAssignment.assigned_role == "ADMIN")

    if caller.role == "SUPER_ADMIN":
        if admin_user_id:
            q = q.where(TargetAssignment.assigned_to_user_id == admin_user_id)
    elif caller.role == "ADMIN":
        # Admin can only see their own assigned targets
        q = q.where(TargetAssignment.assigned_to_user_id == caller.user_id)
    else:
        # Agents cannot list admin targets
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "PERMISSION_DENIED", "message": "Agents cannot view administrator targets."},
        )

    if target_type_code:
        q = q.join(TargetType).where(TargetType.code == target_type_code)

    q = q.order_by(TargetAssignment.deadline.asc(), TargetAssignment.created_at.desc())
    targets = session.scalars(q).all()

    responses = [TargetService.build_target_response(session, t) for t in targets]
    if status_filter:
        responses = [r for r in responses if r.status.upper() == status_filter.upper()]

    return responses


@router.post("/allocate", response_model=TargetResponse, status_code=status.HTTP_201_CREATED, summary="Allocate an agent target from an administrator parent target")
def allocate_agent_target(
    body: AllocateAgentTargetRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_ALLOCATE))],
    session: Annotated[Session, Depends(get_db)],
) -> TargetResponse:
    is_super = caller.role == "SUPER_ADMIN"
    target = TargetService.allocate_agent_target(
        session=session,
        admin_user_id=caller.user_id,
        admin_tenant_id=caller.tenant_id,
        req=body,
        is_super_admin=is_super,
    )
    session.commit()
    session.refresh(target)
    return TargetService.build_target_response(session, target)


@router.get("/agent", response_model=list[TargetResponse], summary="List agent targets (Super Admin, Admin, or Agent)")
def list_agent_targets(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_READ))],
    session: Annotated[Session, Depends(get_db)],
    parent_target_id: UUID | None = Query(None, description="Filter by parent admin target ID"),
    agent_user_id: UUID | None = Query(None, description="Filter by agent user ID"),
    target_type_code: str | None = Query(None, description="Filter by KPI code"),
    status_filter: str | None = Query(None, alias="status", description="Filter by operational status"),
) -> list[TargetResponse]:
    q = select(TargetAssignment).where(TargetAssignment.assigned_role == "AGENT")

    if caller.role == "SUPER_ADMIN":
        if agent_user_id:
            q = q.where(TargetAssignment.assigned_to_user_id == agent_user_id)
        if parent_target_id:
            q = q.where(TargetAssignment.parent_target_id == parent_target_id)
    elif caller.role == "ADMIN":
        # Admin can see agent targets within their tenant or allocated by them
        if caller.tenant_id:
            q = q.where(
                or_(
                    TargetAssignment.tenant_id == caller.tenant_id,
                    TargetAssignment.assigned_by_user_id == caller.user_id,
                )
            )
        else:
            q = q.where(TargetAssignment.assigned_by_user_id == caller.user_id)

        if agent_user_id:
            q = q.where(TargetAssignment.assigned_to_user_id == agent_user_id)
        if parent_target_id:
            q = q.where(TargetAssignment.parent_target_id == parent_target_id)
    elif caller.role == "AGENT":
        # Agent can only view their own personal targets
        q = q.where(TargetAssignment.assigned_to_user_id == caller.user_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "PERMISSION_DENIED", "message": "Unauthorized target access."},
        )

    if target_type_code:
        q = q.join(TargetType).where(TargetType.code == target_type_code)

    q = q.order_by(TargetAssignment.deadline.asc(), TargetAssignment.created_at.desc())
    targets = session.scalars(q).all()

    responses = [TargetService.build_target_response(session, t) for t in targets]
    if status_filter:
        responses = [r for r in responses if r.status.upper() == status_filter.upper()]

    return responses


@router.get("/summary", response_model=TargetSummaryResponse, summary="Get target summary statistics for caller's dashboard")
def get_targets_summary(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> TargetSummaryResponse:
    q = select(TargetAssignment)

    if caller.role == "SUPER_ADMIN":
        # Super Admin summary covers all admin targets across the platform
        q = q.where(TargetAssignment.assigned_role == "ADMIN")
    elif caller.role == "ADMIN":
        # Admin summary covers their assigned targets
        q = q.where(
            TargetAssignment.assigned_role == "ADMIN",
            TargetAssignment.assigned_to_user_id == caller.user_id,
        )
    elif caller.role == "AGENT":
        # Agent summary covers their personal agent targets
        q = q.where(
            TargetAssignment.assigned_role == "AGENT",
            TargetAssignment.assigned_to_user_id == caller.user_id,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "PERMISSION_DENIED", "message": "Unauthorized target access."},
        )

    targets = session.scalars(q).all()
    return TargetService.calculate_summary(session, list(targets))


@router.get("/{target_id}", response_model=TargetResponse, summary="Get target details by ID")
def get_target_detail(
    target_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> TargetResponse:
    target = session.get(TargetAssignment, target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "TARGET_NOT_FOUND", "message": "Target assignment not found."},
        )

    # Tenant and role isolation checks
    if caller.role == "SUPER_ADMIN":
        pass
    elif caller.role == "ADMIN":
        if (
            target.assigned_to_user_id != caller.user_id
            and target.assigned_by_user_id != caller.user_id
            and (not caller.tenant_id or target.tenant_id != caller.tenant_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "TARGET_ACCESS_DENIED", "message": "Target not accessible."},
            )
    elif caller.role == "AGENT":
        if target.assigned_to_user_id != caller.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "TARGET_ACCESS_DENIED", "message": "You can only view your own targets."},
            )

    return TargetService.build_target_response(session, target)


@router.post("/{target_id}/adjust", response_model=TargetAdjustmentResponse, status_code=status.HTTP_201_CREATED, summary="Record a manual achievement adjustment")
def adjust_target_achievement(
    target_id: UUID,
    body: TargetAdjustmentRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
) -> TargetAdjustmentResponse:
    adj = TargetService.record_adjustment(
        session=session,
        target_id=target_id,
        caller_user_id=caller.user_id,
        caller_role=caller.role,
        caller_tenant_id=caller.tenant_id,
        req=body,
    )
    session.commit()
    session.refresh(adj)
    return TargetAdjustmentResponse.model_validate(adj)


@router.get("/{target_id}/adjustments", response_model=list[TargetAdjustmentResponse], summary="List manual adjustments for a target")
def list_target_adjustments(
    target_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.TARGET_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> list[TargetAdjustmentResponse]:
    target = session.get(TargetAssignment, target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "TARGET_NOT_FOUND", "message": "Target not found."},
        )

    if caller.role != "SUPER_ADMIN":
        if caller.tenant_id and target.tenant_id and target.tenant_id != caller.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "TENANT_MISMATCH", "message": "Target does not belong to your community."},
            )

    adjustments = session.scalars(
        select(TargetAchievementAdjustment)
        .where(TargetAchievementAdjustment.target_id == target_id)
        .order_by(TargetAchievementAdjustment.created_at.desc())
    ).all()
    return [TargetAdjustmentResponse.model_validate(a) for a in adjustments]

