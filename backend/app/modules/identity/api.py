from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query, Response
from pydantic import BaseModel

from app.api.tenant_router import create_tenant_router
from app.core.tenant_context import TenantContext
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_permission
from app.modules.authorization.service import AuthorizationRequest
from app.modules.identity.inventory import (
    SQLAlchemyMembershipInventoryRepository,
    get_membership_inventory_repository,
)

router = create_tenant_router(prefix="/memberships", tags=["identity"])


class MembershipSummaryResponse(BaseModel):
    id: UUID
    user_id: UUID
    status: str
    user_status: str
    created_at: datetime
    updated_at: datetime


class MembershipListResponse(BaseModel):
    memberships: list[MembershipSummaryResponse]


@router.get("", response_model=MembershipListResponse)
async def list_memberships(
    response: Response,
    authorized: Annotated[
        AuthorizationRequest, Depends(require_permission(PermissionCode.MEMBER_READ))
    ],
    repository: Annotated[
        SQLAlchemyMembershipInventoryRepository,
        Depends(get_membership_inventory_repository),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> MembershipListResponse:
    """Return minimal tenant membership records for authorized member readers."""
    response.headers["Cache-Control"] = "no-store"
    tenant: TenantContext = authorized.tenant
    memberships = repository.list_for_tenant(
        tenant_id=tenant.tenant_id,
        limit=limit,
        offset=offset,
    )
    return MembershipListResponse(
        memberships=[
            MembershipSummaryResponse(
                id=membership.id,
                user_id=membership.user_id,
                status=membership.status,
                user_status=membership.user_status,
                created_at=membership.created_at,
                updated_at=membership.updated_at,
            )
            for membership in memberships
        ]
    )
