from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.tenant_context import TenantContext
from app.modules.authentication.api import get_authenticated_session, get_current_resolved_session
from app.modules.authentication.repository import AuthenticatedSession, ResolvedSession
from app.modules.authorization.catalog import PermissionCode, permission_code
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.service import (
    AuthorizationConstraint,
    AuthorizationRequest,
    AuthorizationService,
)
from app.modules.tenants.dependencies import get_tenant_context


def get_authorization_service(
    repository: Annotated[
        SQLAlchemyAuthorizationRepository, Depends(get_authorization_repository)
    ],
) -> AuthorizationService:
    return AuthorizationService(repository)


def require_permission(
    permission: PermissionCode | str,
    *,
    constraints: Sequence[AuthorizationConstraint] = (),
):
    """Build a reusable protected-route dependency for a registered permission."""
    required = permission_code(permission)

    def dependency(
        tenant: Annotated[TenantContext, Depends(get_tenant_context)],
        authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
        service: Annotated[AuthorizationService, Depends(get_authorization_service)],
    ) -> AuthorizationRequest:
        return service.require(
            tenant=tenant,
            authenticated=authenticated,
            permission=required,
            constraints=constraints,
        )

    dependency.__name__ = f"require_{required.value.replace('.', '_')}"
    return dependency


def require_session_capability(permission: PermissionCode | str):
    """Dependency that resolves session and verifies permission capability."""
    required = permission_code(permission)

    def dependency(
        session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
        authz_repo: Annotated[SQLAlchemyAuthorizationRepository, Depends(get_authorization_repository)],
    ) -> ResolvedSession:
        caller, _ = session_data
        if caller.role == "SUPER_ADMIN":
            return caller

        if not caller.tenant_id or not caller.membership_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "PERMISSION_DENIED", "message": "Tenant membership required."},
            )

        state = authz_repo.load_state(
            tenant_id=caller.tenant_id,
            user_id=caller.user_id,
            membership_id=caller.membership_id,
        )
        if state and required in state.permissions:
            return caller

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "PERMISSION_DENIED", "message": f"Permission '{required.value}' is required."},
        )

    dependency.__name__ = f"require_cap_{required.value.replace('.', '_')}"
    return dependency


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
