from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db
from app.modules.agents.api import _create_agent_in_community
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.agents.schemas import AgentCreateRequest, AgentSummary
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.models import AccountCredential, EmailOtpChallenge
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability, require_super_admin
from app.modules.hierarchy.schemas import (
    AgentCreationRequestListResponse,
    AgentCreationRequestReview,
    AgentCreationRequestSubmit,
    AgentCreationRequestSummary,
    HierarchyAgentItem,
    HierarchyAgentListResponse,
    HierarchyManagerItem,
    HierarchyManagerListResponse,
    HierarchySubscriberItem,
    HierarchySubscriberListResponse,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.locations.models import (
    AgentCreationRequest,
    District,
    Mandal,
    ManagerLocationAssignment,
    State,
)
from app.modules.locations.service import LocationService
from app.modules.tenants.models import AdminProfile, Tenant

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Hierarchy & Approval Workflow"])


# ===========================================================================
# 1. Super Admin Hierarchy Drill-Down
# ===========================================================================

@router.get(
    "/platform/communities/{community_id}/managers",
    response_model=HierarchyManagerListResponse,
    summary="Super Admin: List Managers in a community with Agent and Subscriber counts (Level 1)",
)
def list_community_managers(
    community_id: UUID,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(description="Search by Manager name, username, or mobile")] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> HierarchyManagerListResponse:
    tenant = session.execute(select(Tenant).where(Tenant.id == community_id)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "COMMUNITY_NOT_FOUND"})

    # Query admin profiles in this community
    stmt = (
        select(
            AdminProfile,
            TenantMembership,
            AccountCredential.username,
        )
        .join(TenantMembership, TenantMembership.user_id == AdminProfile.user_id)
        .outerjoin(AccountCredential, AccountCredential.user_id == AdminProfile.user_id)
        .where(
            AdminProfile.tenant_id == community_id,
            TenantMembership.tenant_id == community_id,
        )
    )

    if status_filter:
        stmt = stmt.where(AdminProfile.status == status_filter)

    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            AdminProfile.first_name.ilike(term)
            | AdminProfile.last_name.ilike(term)
            | TenantMembership.mobile_e164.ilike(term)
            | AccountCredential.username.ilike(term)
        )

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    records = session.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()

    items: list[HierarchyManagerItem] = []
    for admin_prof, membership, username in records:
        fn = (admin_prof.first_name or "").strip()
        ln = (admin_prof.last_name or "").strip()
        full_name = f"{fn} {ln}".strip() or username or "Manager"

        # Assigned states and districts
        loc_assignments = session.execute(
            select(ManagerLocationAssignment).where(
                ManagerLocationAssignment.tenant_id == community_id,
                ManagerLocationAssignment.user_id == admin_prof.user_id,
                ManagerLocationAssignment.status == "active",
            )
        ).scalars().all()

        state_names = []
        if loc_assignments:
            st_ids = {a.state_id for a in loc_assignments}
            state_objs = session.execute(select(State).where(State.id.in_(st_ids))).scalars().all()
            state_names = [s.name for s in state_objs]

        # Districts summary
        has_all_districts = any(a.all_districts for a in loc_assignments)
        if has_all_districts:
            dist_summary = "All districts"
        elif loc_assignments:
            d_ids = {a.district_id for a in loc_assignments if a.district_id}
            dist_objs = session.execute(select(District).where(District.id.in_(d_ids))).scalars().all() if d_ids else []
            dist_summary = ", ".join(d.name for d in dist_objs) or "All districts"
        else:
            dist_summary = admin_prof.state or "All districts"

        # Count agents under this manager
        agent_count = session.execute(
            select(func.count(AgentProfile.id)).where(
                AgentProfile.tenant_id == community_id,
                AgentProfile.manager_user_id == admin_prof.user_id,
            )
        ).scalar() or 0

        # Count subscribers under agents of this manager
        agent_mem_ids = session.execute(
            select(AgentProfile.membership_id).where(
                AgentProfile.tenant_id == community_id,
                AgentProfile.manager_user_id == admin_prof.user_id,
            )
        ).scalars().all()

        sub_count = 0
        if agent_mem_ids:
            sub_count = session.execute(
                select(func.count(SubscriberProfile.id)).where(
                    SubscriberProfile.tenant_id == community_id,
                    SubscriberProfile.registered_by_agent_membership_id.in_(agent_mem_ids),
                )
            ).scalar() or 0

        items.append(
            HierarchyManagerItem(
                id=admin_prof.id,
                user_id=admin_prof.user_id,
                membership_id=membership.id,
                name=full_name,
                username=username or "",
                mobile_e164=membership.mobile_e164,
                email=admin_prof.email,
                assigned_states=state_names or ([admin_prof.state] if admin_prof.state else []),
                assigned_districts_summary=dist_summary,
                agent_count=agent_count,
                subscriber_count=sub_count,
                status=admin_prof.status,
                created_at=admin_prof.created_at,
            )
        )

    return HierarchyManagerListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/platform/communities/{community_id}/managers/{manager_id}/agents",
    response_model=HierarchyAgentListResponse,
    summary="Super Admin: List Agents under a specific Manager (Level 2)",
)
def list_manager_agents(
    community_id: UUID,
    manager_id: UUID,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
) -> HierarchyAgentListResponse:
    # Resolve manager
    manager_prof = session.execute(
        select(AdminProfile).where(
            (AdminProfile.id == manager_id) | (AdminProfile.user_id == manager_id),
            AdminProfile.tenant_id == community_id,
        )
    ).scalar_one_or_none()
    if not manager_prof:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "MANAGER_NOT_FOUND"})

    m_fn = (manager_prof.first_name or "").strip()
    m_ln = (manager_prof.last_name or "").strip()
    manager_display_name = f"{m_fn} {m_ln}".strip() or "Manager"

    stmt = (
        select(
            AgentProfile,
            TenantMembership,
            AccountCredential.username,
        )
        .join(TenantMembership, TenantMembership.id == AgentProfile.membership_id)
        .outerjoin(AccountCredential, AccountCredential.user_id == AgentProfile.user_id)
        .where(
            AgentProfile.tenant_id == community_id,
            AgentProfile.manager_user_id == manager_prof.user_id,
        )
    )

    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            AgentProfile.first_name.ilike(term)
            | AgentProfile.last_name.ilike(term)
            | TenantMembership.mobile_e164.ilike(term)
            | AccountCredential.username.ilike(term)
            | AgentProfile.district.ilike(term)
            | AgentProfile.mandal.ilike(term)
        )

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    records = session.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()

    items: list[HierarchyAgentItem] = []
    for agent_prof, membership, username in records:
        fn = (agent_prof.first_name or "").strip()
        ln = (agent_prof.last_name or "").strip()
        full_name = f"{fn} {ln}".strip() or username or "Agent"

        sub_count = session.execute(
            select(func.count(SubscriberProfile.id)).where(
                SubscriberProfile.tenant_id == community_id,
                SubscriberProfile.registered_by_agent_membership_id == agent_prof.membership_id,
            )
        ).scalar() or 0

        items.append(
            HierarchyAgentItem(
                id=agent_prof.id,
                user_id=agent_prof.user_id,
                membership_id=membership.id,
                name=full_name,
                username=username or "",
                mobile_e164=membership.mobile_e164,
                email=agent_prof.email,
                state=agent_prof.state,
                district=agent_prof.district,
                mandal=agent_prof.mandal,
                manager_name=manager_display_name,
                subscriber_count=sub_count,
                status="active",
                created_at=agent_prof.created_at,
            )
        )

    return HierarchyAgentListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/platform/communities/{community_id}/agents/{agent_id}/subscribers",
    response_model=HierarchySubscriberListResponse,
    summary="Super Admin: List Subscribers registered by an Agent (Level 3)",
)
def list_agent_subscribers(
    community_id: UUID,
    agent_id: UUID,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
) -> HierarchySubscriberListResponse:
    agent_prof = session.execute(
        select(AgentProfile).where(
            (AgentProfile.id == agent_id) | (AgentProfile.membership_id == agent_id),
            AgentProfile.tenant_id == community_id,
        )
    ).scalar_one_or_none()
    if not agent_prof:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "AGENT_NOT_FOUND"})

    stmt = (
        select(SubscriberProfile, TenantMembership.mobile_e164)
        .join(TenantMembership, TenantMembership.id == SubscriberProfile.membership_id)
        .where(
            SubscriberProfile.tenant_id == community_id,
            SubscriberProfile.registered_by_agent_membership_id == agent_prof.membership_id,
        )
    )

    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            SubscriberProfile.full_name.ilike(term)
            | TenantMembership.mobile_e164.ilike(term)
            | SubscriberProfile.email.ilike(term)
        )

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    records = session.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()

    items: list[HierarchySubscriberItem] = []
    for sub, mobile in records:
        items.append(
            HierarchySubscriberItem(
                id=sub.id,
                full_name=sub.full_name,
                mobile_e164=mobile,
                email=sub.email,
                address=sub.address,
                package_plan=sub.package_plan,
                payment_status=sub.status,
                subscription_status=sub.status,
                created_at=sub.created_at,
            )
        )

    return HierarchySubscriberListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ===========================================================================
# 2. Agent Creation Request Approval Workflow
# ===========================================================================

@router.post(
    "/agent/creation-requests",
    response_model=AgentCreationRequestSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Agent: Request permission to create a lower-tier Agent",
)
def request_agent_creation_permission(
    body: AgentCreationRequestSubmit,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentCreationRequestSummary:
    caller, _ = session_data
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "MEMBERSHIP_REQUIRED"})

    # Validate location
    state, district, mandal = LocationService.validate_location_hierarchy(
        session=session,
        state_id=body.state_id,
        district_id=body.district_id,
        mandal_id=body.mandal_id,
    )

    # Check duplicate pending requests
    dup = session.execute(
        select(AgentCreationRequest).where(
            AgentCreationRequest.tenant_id == caller.tenant_id,
            AgentCreationRequest.requesting_agent_user_id == caller.user_id,
            AgentCreationRequest.mandal_id == body.mandal_id,
            AgentCreationRequest.status.in_(["PENDING_MANAGER", "PENDING_SUPER_ADMIN"]),
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "DUPLICATE_REQUEST", "message": "You already have an active request for this mandal."},
        )

    # Find the Agent's manager
    agent_prof = session.execute(
        select(AgentProfile).where(
            AgentProfile.tenant_id == caller.tenant_id,
            AgentProfile.user_id == caller.user_id,
        )
    ).scalar_one_or_none()

    manager_user_id = agent_prof.manager_user_id if agent_prof else None
    manager_mem_id = agent_prof.manager_membership_id if agent_prof else None
    manager_name = None

    if not manager_user_id:
        # Check if an eligible manager covers this requested location
        eligible = LocationService.find_eligible_managers_for_location(
            session=session,
            tenant_id=caller.tenant_id,
            state_id=state.id,
            district_id=district.id,
            mandal_id=mandal.id,
        )
        if eligible:
            manager_user_id = eligible[0][0].id
            manager_mem_id = eligible[0][1].id
            manager_name = eligible[0][2]
    else:
        m_admin = session.execute(
            select(AdminProfile).where(
                AdminProfile.tenant_id == caller.tenant_id,
                AdminProfile.user_id == manager_user_id,
            )
        ).scalar_one_or_none()
        if m_admin:
            manager_name = f"{m_admin.first_name or ''} {m_admin.last_name or ''}".strip()

    req_num = f"REQ-AG-{datetime.now(UTC).strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
    req = AgentCreationRequest(
        id=uuid4(),
        request_number=req_num,
        tenant_id=caller.tenant_id,
        requesting_agent_membership_id=caller.membership_id,
        requesting_agent_user_id=caller.user_id,
        manager_membership_id=manager_mem_id,
        manager_user_id=manager_user_id,
        state_id=state.id,
        district_id=district.id,
        mandal_id=mandal.id,
        state_name=state.name,
        district_name=district.name,
        mandal_name=mandal.name,
        reason=body.reason.strip(),
        status="PENDING_MANAGER",
    )
    session.add(req)
    session.commit()

    agent_name = f"{agent_prof.first_name} {agent_prof.last_name}" if agent_prof else (caller.username or "Agent")

    return AgentCreationRequestSummary(
        id=req.id,
        request_number=req.request_number,
        tenant_id=req.tenant_id,
        requesting_agent_user_id=req.requesting_agent_user_id,
        requesting_agent_membership_id=req.requesting_agent_membership_id,
        requesting_agent_name=agent_name,
        manager_user_id=req.manager_user_id,
        manager_name=manager_name,
        state_id=req.state_id,
        district_id=req.district_id,
        mandal_id=req.mandal_id,
        state_name=req.state_name,
        district_name=req.district_name,
        mandal_name=req.mandal_name,
        reason=req.reason,
        status=req.status,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


@router.get(
    "/agent/creation-requests",
    response_model=AgentCreationRequestListResponse,
    summary="Agent: View submitted creation requests",
)
def list_agent_creation_requests(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentCreationRequestListResponse:
    caller, _ = session_data
    if not caller.tenant_id:
        return AgentCreationRequestListResponse(items=[], total=0)

    requests = session.execute(
        select(AgentCreationRequest)
        .where(
            AgentCreationRequest.tenant_id == caller.tenant_id,
            AgentCreationRequest.requesting_agent_user_id == caller.user_id,
        )
        .order_by(AgentCreationRequest.created_at.desc())
    ).scalars().all()

    items = [_to_request_summary(session, r) for r in requests]
    return AgentCreationRequestListResponse(items=items, total=len(items))


@router.get(
    "/manager/creation-requests",
    response_model=AgentCreationRequestListResponse,
    summary="Manager: View pending agent creation requests requiring review",
)
def list_manager_creation_requests(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentCreationRequestListResponse:
    caller, _ = session_data
    if not caller.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "NO_COMMUNITY_CONTEXT"})

    # Retrieve requests assigned to this manager or within their scope
    requests = session.execute(
        select(AgentCreationRequest)
        .where(
            AgentCreationRequest.tenant_id == caller.tenant_id,
            (
                (AgentCreationRequest.manager_user_id == caller.user_id)
                | (AgentCreationRequest.manager_user_id.is_(None))
            ),
        )
        .order_by(AgentCreationRequest.created_at.desc())
    ).scalars().all()

    # Filter to only requests within this Manager's assigned geographic scope
    filtered = []
    for r in requests:
        if LocationService.is_manager_authorized_for_location(
            session=session,
            tenant_id=caller.tenant_id,
            manager_user_id=caller.user_id,
            state_id=r.state_id,
            district_id=r.district_id,
            mandal_id=r.mandal_id,
        ):
            filtered.append(r)

    items = [_to_request_summary(session, r) for r in filtered]
    return AgentCreationRequestListResponse(items=items, total=len(items))


@router.post(
    "/manager/creation-requests/{request_id}/review",
    response_model=AgentCreationRequestSummary,
    summary="Manager: Approve or Reject an agent creation request (moves to PENDING_SUPER_ADMIN or REJECTED)",
)
def manager_review_creation_request(
    request_id: UUID,
    body: AgentCreationRequestReview,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentCreationRequestSummary:
    caller, _ = session_data
    req = session.execute(
        select(AgentCreationRequest).where(
            AgentCreationRequest.id == request_id,
            AgentCreationRequest.tenant_id == caller.tenant_id,
        )
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "REQUEST_NOT_FOUND"})

    if req.status != "PENDING_MANAGER":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "INVALID_STATE", "message": f"Request cannot be reviewed in status '{req.status}'."},
        )

    # Validate Manager scope
    if not LocationService.is_manager_authorized_for_location(
        session=session,
        tenant_id=caller.tenant_id,
        manager_user_id=caller.user_id,
        state_id=req.state_id,
        district_id=req.district_id,
        mandal_id=req.mandal_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "LOCATION_OUT_OF_SCOPE", "message": "Manager does not have authority over this location."},
        )

    now = datetime.now(UTC)
    req.manager_decision = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    req.manager_decision_at = now
    req.manager_decision_by_user_id = caller.user_id
    req.manager_comment = body.comment
    req.updated_at = now

    if body.decision == "APPROVE":
        req.status = "PENDING_SUPER_ADMIN"
    else:
        req.status = "REJECTED"

    session.commit()
    return _to_request_summary(session, req)


@router.get(
    "/platform/creation-requests",
    response_model=AgentCreationRequestListResponse,
    summary="Super Admin: View all Agent creation requests across communities",
)
def list_superadmin_creation_requests(
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    community_id: Annotated[UUID | None, Query()] = None,
) -> AgentCreationRequestListResponse:
    stmt = select(AgentCreationRequest)
    if community_id:
        stmt = stmt.where(AgentCreationRequest.tenant_id == community_id)
    if status_filter:
        stmt = stmt.where(AgentCreationRequest.status == status_filter)
    stmt = stmt.order_by(AgentCreationRequest.created_at.desc())

    records = session.execute(stmt).scalars().all()
    items = [_to_request_summary(session, r) for r in records]
    return AgentCreationRequestListResponse(items=items, total=len(items))


@router.post(
    "/platform/creation-requests/{request_id}/review",
    response_model=AgentCreationRequestSummary,
    summary="Super Admin: Final Approval or Rejection of an Agent creation request",
)
def superadmin_review_creation_request(
    request_id: UUID,
    body: AgentCreationRequestReview,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentCreationRequestSummary:
    req = session.execute(
        select(AgentCreationRequest).where(AgentCreationRequest.id == request_id)
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "REQUEST_NOT_FOUND"})

    if req.status not in ("PENDING_SUPER_ADMIN", "PENDING_MANAGER"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "INVALID_STATE", "message": f"Request cannot be reviewed in status '{req.status}'."},
        )

    now = datetime.now(UTC)
    req.superadmin_decision = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    req.superadmin_decision_at = now
    req.superadmin_decision_by_user_id = operator.user_id
    req.superadmin_comment = body.comment
    req.updated_at = now

    if body.decision == "APPROVE":
        req.status = "APPROVED"
    else:
        req.status = "REJECTED"

    session.commit()
    return _to_request_summary(session, req)


@router.post(
    "/agent/agents-from-request",
    response_model=AgentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Agent: Create a child Agent using an APPROVED creation request",
)
def create_child_agent_from_approved_request(
    creation_request_id: UUID,
    body: AgentCreateRequest,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> AgentSummary:
    caller, _ = session_data
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "MEMBERSHIP_REQUIRED"})

    req = session.execute(
        select(AgentCreationRequest)
        .where(
            AgentCreationRequest.id == creation_request_id,
            AgentCreationRequest.tenant_id == caller.tenant_id,
            AgentCreationRequest.requesting_agent_user_id == caller.user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "APPROVED_REQUEST_NOT_FOUND"})

    if req.status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "REQUEST_NOT_APPROVED", "message": f"Creation request is '{req.status}', must be 'APPROVED'."},
        )

    # Force location to match approved request
    body.state = req.state_name
    body.district = req.district_name
    body.mandal = req.mandal_name
    body.state_id = req.state_id
    body.district_id = req.district_id
    body.mandal_id = req.mandal_id

    # Auto-issue email challenge token for pre-approved dual-custody child agent if not provided
    if not body.email_verification_token:
        clean_email = (body.email or "").strip() or f"{body.username.strip()}@avanflix.local"
        body.email = clean_email
        token = f"approved_req_{uuid4().hex}"
        now_dt = datetime.now(UTC)
        session.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=caller.tenant_id,
                email=clean_email.lower(),
                purpose="email_verification",
                otp_hash="approved",
                otp_salt="approved",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now_dt,
                expires_at=now_dt + timedelta(minutes=15),
                verified_at=now_dt,
                verification_token=token,
            )
        )
        session.flush()
        body.email_verification_token = token

    tenant = session.execute(select(Tenant).where(Tenant.id == caller.tenant_id)).scalar_one()
    app_state = getattr(getattr(request, "app", None), "state", None)
    settings = getattr(app_state, "settings", None) or Settings(otp_delivery_mode="mock")
    enc_key = getattr(settings, "identity_encryption_key", None)

    # Create agent with parent linkage
    agent_summary = _create_agent_in_community(
        session=session,
        tenant=tenant,
        body=body,
        created_by_user_id=caller.user_id,
        created_by_username=caller.username or "agent",
        created_by_membership_id=caller.membership_id,
        encryption_key=enc_key,
        settings=settings,
        forced_manager_user_id=req.manager_user_id,
        forced_manager_membership_id=req.manager_membership_id,
        parent_agent_membership_id=caller.membership_id,
    )

    # Mark request fulfilled
    now = datetime.now(UTC)
    req.status = "FULFILLED"
    req.fulfilled_agent_profile_id = agent_summary.id
    req.fulfilled_at = now
    req.updated_at = now
    session.commit()

    return agent_summary


def _to_request_summary(session: Session, req: AgentCreationRequest) -> AgentCreationRequestSummary:
    agent_prof = session.execute(
        select(AgentProfile).where(
            AgentProfile.tenant_id == req.tenant_id,
            AgentProfile.user_id == req.requesting_agent_user_id,
        )
    ).scalar_one_or_none()
    agent_name = f"{agent_prof.first_name} {agent_prof.last_name}" if agent_prof else "Agent"

    manager_name = None
    if req.manager_user_id:
        m_admin = session.execute(
            select(AdminProfile).where(
                AdminProfile.tenant_id == req.tenant_id,
                AdminProfile.user_id == req.manager_user_id,
            )
        ).scalar_one_or_none()
        if m_admin:
            manager_name = f"{m_admin.first_name or ''} {m_admin.last_name or ''}".strip() or "Manager"

    return AgentCreationRequestSummary(
        id=req.id,
        request_number=req.request_number,
        tenant_id=req.tenant_id,
        requesting_agent_user_id=req.requesting_agent_user_id,
        requesting_agent_membership_id=req.requesting_agent_membership_id,
        requesting_agent_name=agent_name,
        manager_user_id=req.manager_user_id,
        manager_name=manager_name,
        state_id=req.state_id,
        district_id=req.district_id,
        mandal_id=req.mandal_id,
        state_name=req.state_name,
        district_name=req.district_name,
        mandal_name=req.mandal_name,
        reason=req.reason,
        status=req.status,
        manager_decision=req.manager_decision,
        manager_decision_at=req.manager_decision_at,
        manager_comment=req.manager_comment,
        superadmin_decision=req.superadmin_decision,
        superadmin_decision_at=req.superadmin_decision_at,
        superadmin_comment=req.superadmin_comment,
        fulfilled_agent_profile_id=req.fulfilled_agent_profile_id,
        fulfilled_at=req.fulfilled_at,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )
