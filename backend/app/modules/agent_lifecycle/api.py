from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.agent_appointments.api import require_super_admin
from app.modules.agent_lifecycle.schemas import (
    AgentLifecycleDecisionRequest,
    AgentLifecycleListResponse,
    AgentLifecycleSubmitRequest,
    AgentLifecycleSummary,
)
from app.modules.agent_lifecycle.service import (
    AgentLifecycleService,
    get_agent_lifecycle_service,
)
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability


community_lifecycle_router = APIRouter(prefix="/agent/lifecycle-requests", tags=["agent-lifecycle"])
agent_item_lifecycle_router = APIRouter(prefix="/agents", tags=["agent-lifecycle"])
platform_lifecycle_router = APIRouter(prefix="/platform/agent-lifecycle-requests", tags=["platform-agent-lifecycle"])


# -----------------------------------------------------------------------------
# 1. Community Admin: Submit Lifecycle Request
# -----------------------------------------------------------------------------
@agent_item_lifecycle_router.post(
    "/{agent_id}/lifecycle-requests",
    response_model=AgentLifecycleSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Community Admin: Submit an agent lifecycle request (suspend, deactivate, reactivate, revoke)",
)
def submit_agent_lifecycle_request(
    agent_id: UUID,
    body: AgentLifecycleSubmitRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_UPDATE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[AgentLifecycleService, Depends(get_agent_lifecycle_service)],
) -> AgentLifecycleSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    req = service.submit_lifecycle_request(
        session,
        tenant_id=caller.tenant_id,
        agent_profile_id=agent_id,
        action=body.action,
        reason=body.reason,
        requester_user_id=caller.user_id,
    )
    session.commit()
    return req


@community_lifecycle_router.get(
    "",
    response_model=AgentLifecycleListResponse,
    summary="Community Admin: List lifecycle requests for this community",
)
def list_community_lifecycle_requests(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[AgentLifecycleService, Depends(get_agent_lifecycle_service)],
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> AgentLifecycleListResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    return service.list_tenant_requests(
        session,
        tenant_id=caller.tenant_id,
        status_filter=status,
        limit=limit,
        offset=offset,
    )


# -----------------------------------------------------------------------------
# 2. Platform Super Admin: Review and Decision
# -----------------------------------------------------------------------------
@platform_lifecycle_router.get(
    "",
    response_model=AgentLifecycleListResponse,
    summary="Platform Super Admin: List agent lifecycle requests across communities",
)
def list_platform_lifecycle_requests(
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[AgentLifecycleService, Depends(get_agent_lifecycle_service)],
    community_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> AgentLifecycleListResponse:
    return service.list_platform_requests(
        session,
        tenant_id=community_id,
        status_filter=status,
        limit=limit,
        offset=offset,
    )


@platform_lifecycle_router.post(
    "/{request_id}/decision",
    response_model=AgentLifecycleSummary,
    summary="Platform Super Admin: Review and decide an agent lifecycle request",
)
def decide_agent_lifecycle_request(
    request_id: UUID,
    body: AgentLifecycleDecisionRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[AgentLifecycleService, Depends(get_agent_lifecycle_service)],
) -> AgentLifecycleSummary:
    summary = service.decide_lifecycle_request(
        session,
        request_id=request_id,
        reviewer_user_id=operator.user_id,
        decision=body.decision,
        decision_reason=body.decision_reason,
    )
    session.commit()
    return summary

