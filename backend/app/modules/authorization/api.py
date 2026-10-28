from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query, Response
from pydantic import BaseModel

from app.api.tenant_router import create_tenant_router
from app.core.tenant_context import TenantContext
from app.modules.authorization.catalog import PERMISSION_DESCRIPTIONS, PermissionCode
from app.modules.authorization.dependencies import require_permission
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    TenantRoleSummary,
    get_authorization_repository,
)
from app.modules.authorization.service import AuthorizationRequest

permission_router = create_tenant_router(prefix="/permissions", tags=["authorization"])
role_router = create_tenant_router(prefix="/roles", tags=["authorization"])


class PermissionCatalogEntry(BaseModel):
    code: PermissionCode
    description: str


class PermissionCatalogResponse(BaseModel):
    permissions: list[PermissionCatalogEntry]


class TenantRoleResponse(BaseModel):
    id: UUID
    code: str
    display_name: str
    status: str
    permissions: list[PermissionCode]

    @classmethod
    def from_summary(cls, role: TenantRoleSummary) -> TenantRoleResponse:
        return cls(
            id=role.id,
            code=role.code,
            display_name=role.display_name,
            status=role.status,
            permissions=list(role.permissions),
        )


class TenantRoleListResponse(BaseModel):
    roles: list[TenantRoleResponse]


@permission_router.get("", response_model=PermissionCatalogResponse)
async def read_permission_catalog(
    response: Response,
    _: Annotated[
        AuthorizationRequest, Depends(require_permission(PermissionCode.PERMISSION_READ))
    ],
) -> PermissionCatalogResponse:
    """Return the registered permission catalog to authorized tenant members."""
    response.headers["Cache-Control"] = "no-store"
    return PermissionCatalogResponse(
        permissions=[
            PermissionCatalogEntry(code=code, description=PERMISSION_DESCRIPTIONS[code])
            for code in sorted(PERMISSION_DESCRIPTIONS, key=lambda item: item.value)
        ]
    )


@role_router.get("", response_model=TenantRoleListResponse)
async def list_tenant_roles(
    response: Response,
    authorized: Annotated[
        AuthorizationRequest, Depends(require_permission(PermissionCode.ROLE_READ))
    ],
    repository: Annotated[
        SQLAlchemyAuthorizationRepository, Depends(get_authorization_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> TenantRoleListResponse:
    """List tenant-owned role definitions without exposing membership data."""
    response.headers["Cache-Control"] = "no-store"
    tenant: TenantContext = authorized.tenant
    roles = repository.list_roles_for_tenant(
        tenant_id=tenant.tenant_id,
        limit=limit,
        offset=offset,
    )
    return TenantRoleListResponse(
        roles=[TenantRoleResponse.from_summary(role) for role in roles]
    )


router = permission_router
