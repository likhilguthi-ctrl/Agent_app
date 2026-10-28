from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.relationships.models import RelationshipStatus, RelationshipType
from app.modules.relationships.schemas import (
    DescendantRosterResponse,
    DescendantStatsResponse,
    RelationshipCreateRequest,
    RelationshipListResponse,
    RelationshipRevokeRequest,
    RelationshipSummary,
)
from app.modules.relationships.service import RelationshipService, get_relationship_service
from app.modules.agents.schemas import UserRegistrationRequest, UserSummary
from app.modules.agents.api import register_user


members_relationships_router = APIRouter(prefix="/members", tags=["members"])
agent_descendants_router = APIRouter(prefix="/agent/descendants", tags=["agent"])


# -----------------------------------------------------------------------------
# 1. Tenant Admin: Member Relationship Management
# -----------------------------------------------------------------------------
@members_relationships_router.post(
    "/relationships",
    response_model=RelationshipSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new member relationship in the community",
)
def create_member_relationship(
    body: RelationshipCreateRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_RELATIONSHIP_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[RelationshipService, Depends(get_relationship_service)],
) -> RelationshipSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    rel = service.create_relationship(
        session,
        tenant_id=caller.tenant_id,
        parent_member_id=body.parent_member_id,
        child_member_id=body.child_member_id,
        relationship_type=body.relationship_type.value,
        creator_user_id=caller.user_id,
    )
    session.commit()
    return rel


@members_relationships_router.get(
    "/{member_id}/relationships",
    response_model=RelationshipListResponse,
    summary="List direct relationships for a community member",
)
def list_member_relationships(
    member_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[RelationshipService, Depends(get_relationship_service)],
    include_revoked: Annotated[bool, Query()] = False,
) -> RelationshipListResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    # If caller is an agent (not admin) and requesting another member, restrict to self or descendant
    if caller.role == "AGENT" and caller.membership_id != member_id:
        # Check if member_id is in caller's descendant tree
        roster = service.get_descendant_roster(
            session,
            tenant_id=caller.tenant_id,
            root_member_id=caller.membership_id,
            max_depth=3,
            limit=1000,
            offset=0,
        )
        descendant_ids = {d.member_id for d in roster.descendants}
        if member_id not in descendant_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "MEMBER_NOT_FOUND", "message": "Member not found in your authorized network."},
            )

    results = service.list_relationships_for_member(
        session,
        tenant_id=caller.tenant_id,
        member_id=member_id,
        include_revoked=include_revoked,
    )
    return RelationshipListResponse(relationships=results, total=len(results))


@members_relationships_router.delete(
    "/relationships/{relationship_id}",
    response_model=RelationshipSummary,
    summary="Revoke an existing member relationship",
)
def revoke_member_relationship(
    relationship_id: UUID,
    body: RelationshipRevokeRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_RELATIONSHIP_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[RelationshipService, Depends(get_relationship_service)],
) -> RelationshipSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    rel = service.revoke_relationship(
        session,
        tenant_id=caller.tenant_id,
        relationship_id=relationship_id,
        revoker_user_id=caller.user_id,
        end_reason=body.end_reason,
    )
    session.commit()
    return rel


# -----------------------------------------------------------------------------
# 2. Agent: Descendant Roster and Network Statistics
# -----------------------------------------------------------------------------
@agent_descendants_router.get(
    "",
    response_model=DescendantRosterResponse,
    summary="Agent: Query recursive descendant members with bounded depth",
)
def get_agent_descendants(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_DESCENDANTS_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[RelationshipService, Depends(get_relationship_service)],
    relationship_type: Annotated[str, Query()] = "REPORTS_TO",
    max_depth: Annotated[int, Query(ge=1, le=3)] = 3,  # Hard cap at 3 for agents
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> DescendantRosterResponse:
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_AGENT_MEMBERSHIP", "message": "Caller has no active agent membership."},
        )

    if relationship_type not in (RelationshipType.REPORTS_TO.value, RelationshipType.REFERRED_BY.value, RelationshipType.MANAGES.value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_RELATIONSHIP_TYPE", "message": "Invalid relationship type requested."},
        )

    return service.get_descendant_roster(
        session,
        tenant_id=caller.tenant_id,
        root_member_id=caller.membership_id,
        relationship_type=relationship_type,
        max_depth=max_depth,
        limit=limit,
        offset=offset,
    )


@agent_descendants_router.get(
    "/stats",
    response_model=DescendantStatsResponse,
    summary="Agent: Query aggregate network statistics",
)
def get_agent_descendant_stats(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_STATS_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[RelationshipService, Depends(get_relationship_service)],
    relationship_type: Annotated[str, Query()] = "REPORTS_TO",
    max_depth: Annotated[int, Query(ge=1, le=3)] = 3,
) -> DescendantStatsResponse:
    if not caller.tenant_id or not caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_AGENT_MEMBERSHIP", "message": "Caller has no active agent membership."},
        )

    if relationship_type not in (RelationshipType.REPORTS_TO.value, RelationshipType.REFERRED_BY.value, RelationshipType.MANAGES.value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_RELATIONSHIP_TYPE", "message": "Invalid relationship type requested."},
        )

    return service.get_descendant_stats(
        session,
        tenant_id=caller.tenant_id,
        root_member_id=caller.membership_id,
        relationship_type=relationship_type,
        max_depth=max_depth,
    )


@agent_descendants_router.post(
    "",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Agent: Register a descendant member under this agent",
)
def create_agent_descendant(
    body: UserRegistrationRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_DESCENDANTS_CREATE))],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> UserSummary:
    # Delegates to the canonical registration function which binds the subscriber and creates relationships
    return register_user(body=body, caller=caller, request=request, session=session)

