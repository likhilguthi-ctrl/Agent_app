from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agent_appointments.schemas import (
    AppointmentDecisionRequest,
    AppointmentListResponse,
    AppointmentSubmitRequest,
    AppointmentSummary,
)
from app.modules.agent_appointments.service import AgentAppointmentService
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.tenants.models import Tenant

router = APIRouter(tags=["agent appointments"])
appointment_service = AgentAppointmentService()


def require_super_admin(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> ResolvedSession:
    resolved, _ = session_data
    if resolved.role != "SUPER_ADMIN" or resolved.account_type != "platform_operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Super Admin privileges required."},
        )
    return resolved


def _require_tenant_id(caller: ResolvedSession) -> UUID:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "TENANT_REQUIRED", "message": "Active community context is required."},
        )
    return caller.tenant_id


# -----------------------------------------------------------------------------
# 1. Appointment Submission (POST /agent-appointments and /agents/appointments)
# -----------------------------------------------------------------------------
@router.post(
    "/agent-appointments",
    response_model=AppointmentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Submit an agent appointment for review",
)
@router.post(
    "/agents/appointments",
    response_model=AppointmentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Submit an agent appointment for review (alias)",
)
def submit_agent_appointment(
    body: AppointmentSubmitRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_CREATE))],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> AppointmentSummary:
    tenant_id = _require_tenant_id(caller)
    settings = getattr(request.app.state, "settings", None)
    return appointment_service.submit_appointment(
        session=session,
        tenant_id=tenant_id,
        requested_by_user_id=caller.user_id,
        requested_by_membership_id=caller.membership_id,
        data=body,
        settings=settings,
    )


# -----------------------------------------------------------------------------
# 2. Appointment Queue Listing (GET /agent-appointments and /agents/appointments)
# -----------------------------------------------------------------------------
@router.get(
    "/agent-appointments",
    response_model=AppointmentListResponse,
    summary="List agent appointments for caller community",
)
@router.get(
    "/agents/appointments",
    response_model=AppointmentListResponse,
    summary="List agent appointments for caller community (alias)",
)
def list_agent_appointments(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.APPROVAL_READ))],
    session: Annotated[Session, Depends(get_db)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> AppointmentListResponse:
    tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else None
    return appointment_service.list_appointments(
        session=session,
        tenant_id=tenant_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


# -----------------------------------------------------------------------------
# 3. Appointment Detail (GET /agent-appointments/{id} and /agents/appointments/{id})
# -----------------------------------------------------------------------------
@router.get(
    "/agent-appointments/{appointment_id}",
    response_model=AppointmentSummary,
    summary="Get agent appointment details",
)
@router.get(
    "/agents/appointments/{appointment_id}",
    response_model=AppointmentSummary,
    summary="Get agent appointment details (alias)",
)
def get_agent_appointment(
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.APPROVAL_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> AppointmentSummary:
    caller_tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else None
    return appointment_service.get_appointment(
        session=session,
        appointment_id=appointment_id,
        caller_tenant_id=caller_tenant_id,
    )


# -----------------------------------------------------------------------------
# 4. Approve Appointment (POST /agent-appointments/{id}/approve)
# -----------------------------------------------------------------------------
@router.post(
    "/agent-appointments/{appointment_id}/approve",
    response_model=AppointmentSummary,
    summary="Approve an eligible agent appointment",
)
@router.post(
    "/agents/appointments/{appointment_id}/approve",
    response_model=AppointmentSummary,
    summary="Approve an eligible agent appointment (alias)",
)
def approve_agent_appointment(
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_APPROVE))],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    caller_tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else None
    return appointment_service.approve_appointment(
        session=session,
        appointment_id=appointment_id,
        reviewer_user_id=caller.user_id,
        reviewer_membership_id=caller.membership_id,
        caller_tenant_id=caller_tenant_id,
        data=body,
    )


# -----------------------------------------------------------------------------
# 5. Reject Appointment (POST /agent-appointments/{id}/reject)
# -----------------------------------------------------------------------------
@router.post(
    "/agent-appointments/{appointment_id}/reject",
    response_model=AppointmentSummary,
    summary="Reject an eligible agent appointment",
)
@router.post(
    "/agents/appointments/{appointment_id}/reject",
    response_model=AppointmentSummary,
    summary="Reject an eligible agent appointment (alias)",
)
def reject_agent_appointment(
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_REJECT))],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    caller_tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else None
    return appointment_service.reject_appointment(
        session=session,
        appointment_id=appointment_id,
        reviewer_user_id=caller.user_id,
        reviewer_membership_id=caller.membership_id,
        caller_tenant_id=caller_tenant_id,
        data=body,
    )


# -----------------------------------------------------------------------------
# 6. Cancel Appointment (POST /agent-appointments/{id}/cancel)
# -----------------------------------------------------------------------------
@router.post(
    "/agent-appointments/{appointment_id}/cancel",
    response_model=AppointmentSummary,
    summary="Cancel a pending agent appointment",
)
@router.post(
    "/agents/appointments/{appointment_id}/cancel",
    response_model=AppointmentSummary,
    summary="Cancel a pending agent appointment (alias)",
)
def cancel_agent_appointment(
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_CANCEL))],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    caller_tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else None
    return appointment_service.cancel_appointment(
        session=session,
        appointment_id=appointment_id,
        user_id=caller.user_id,
        caller_tenant_id=caller_tenant_id,
        data=body,
    )


# -----------------------------------------------------------------------------
# 7. Super Admin Platform Routes (/platform/communities/{community_id}/appointments)
# -----------------------------------------------------------------------------
@router.post(
    "/platform/communities/{community_id}/appointments",
    response_model=AppointmentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Super Admin: Submit an agent appointment for a community",
)
def platform_submit_agent_appointment(
    community_id: UUID,
    body: AppointmentSubmitRequest,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> AppointmentSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    settings = getattr(request.app.state, "settings", None)
    return appointment_service.submit_appointment(
        session=session,
        tenant_id=community_id,
        requested_by_user_id=caller.user_id,
        requested_by_membership_id=caller.membership_id,
        data=body,
        settings=settings,
    )


@router.get(
    "/platform/communities/{community_id}/appointments",
    response_model=AppointmentListResponse,
    summary="Super Admin: List appointments for a community",
)
def platform_list_agent_appointments(
    community_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> AppointmentListResponse:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    return appointment_service.list_appointments(
        session=session,
        tenant_id=community_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/platform/communities/{community_id}/appointments/{appointment_id}",
    response_model=AppointmentSummary,
    summary="Super Admin: Get appointment details in a community",
)
def platform_get_agent_appointment(
    community_id: UUID,
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AppointmentSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    return appointment_service.get_appointment(
        session=session,
        appointment_id=appointment_id,
        caller_tenant_id=community_id,
    )


@router.post(
    "/platform/communities/{community_id}/appointments/{appointment_id}/approve",
    response_model=AppointmentSummary,
    summary="Super Admin: Approve an appointment in a community",
)
def platform_approve_agent_appointment(
    community_id: UUID,
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    return appointment_service.approve_appointment(
        session=session,
        appointment_id=appointment_id,
        reviewer_user_id=caller.user_id,
        reviewer_membership_id=caller.membership_id,
        caller_tenant_id=community_id,
        data=body,
    )


@router.post(
    "/platform/communities/{community_id}/appointments/{appointment_id}/reject",
    response_model=AppointmentSummary,
    summary="Super Admin: Reject an appointment in a community",
)
def platform_reject_agent_appointment(
    community_id: UUID,
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    return appointment_service.reject_appointment(
        session=session,
        appointment_id=appointment_id,
        reviewer_user_id=caller.user_id,
        reviewer_membership_id=caller.membership_id,
        caller_tenant_id=community_id,
        data=body,
    )


@router.post(
    "/platform/communities/{community_id}/appointments/{appointment_id}/cancel",
    response_model=AppointmentSummary,
    summary="Super Admin: Cancel an appointment in a community",
)
def platform_cancel_agent_appointment(
    community_id: UUID,
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    return appointment_service.cancel_appointment(
        session=session,
        appointment_id=appointment_id,
        user_id=caller.user_id,
        caller_tenant_id=community_id,
        data=body,
    )

