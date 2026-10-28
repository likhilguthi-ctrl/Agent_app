from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence
from uuid import UUID

from app.core.tenant_context import TenantContext
from app.modules.authentication.repository import AuthenticatedSession
from app.modules.authorization.catalog import PermissionCode, permission_code
from app.modules.authorization.errors import PermissionDenied
from app.modules.authorization.repository import AuthorizationState, SQLAlchemyAuthorizationRepository


@dataclass(frozen=True, slots=True)
class AuthorizationPrincipal:
    """Identity identifiers taken from a validated server-side session."""

    tenant_id: UUID
    user_id: UUID
    membership_id: UUID


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Inputs to composable authorization constraints, all server-created."""

    tenant: TenantContext
    principal: AuthorizationPrincipal
    permission: PermissionCode
    active_permissions: frozenset[PermissionCode]
    resource: object | None = None


class AuthorizationConstraint(Protocol):
    """Composable resource/scope policy; region and relationship policies are future work."""

    def allows(self, request: AuthorizationRequest) -> bool: ...


class AuthorizationService:
    """Central permission and delegation evaluator. Missing data always denies."""

    def __init__(self, repository: SQLAlchemyAuthorizationRepository) -> None:
        self._repository = repository

    def require(
        self,
        *,
        tenant: TenantContext,
        authenticated: AuthenticatedSession,
        permission: PermissionCode | str,
        resource: object | None = None,
        constraints: Sequence[AuthorizationConstraint] = (),
    ) -> AuthorizationRequest:
        code = permission_code(permission)
        principal = self._principal_for(tenant, authenticated)
        state = self._active_state(principal)
        if code not in state.permissions:
            raise PermissionDenied

        request = AuthorizationRequest(
            tenant=tenant,
            principal=principal,
            permission=code,
            active_permissions=state.permissions,
            resource=resource,
        )
        for constraint in constraints:
            try:
                permitted = constraint.allows(request)
            except Exception as exc:
                # A failed scope/resource policy is an unavailable authorization decision.
                from app.modules.authorization.errors import AuthorizationDataUnavailable

                raise AuthorizationDataUnavailable from exc
            if not permitted:
                raise PermissionDenied
        return request

    def resolve_effective_permissions(
        self,
        *,
        tenant: TenantContext,
        authenticated: AuthenticatedSession,
    ) -> frozenset[PermissionCode]:
        """Resolve grants for a trusted tenant session without inventing a required code.

        An empty set is valid only when the active membership and authorization data were
        successfully loaded and no active grants exist. Repository failures propagate as
        ``AuthorizationDataUnavailable``.
        """
        principal = self._principal_for(tenant, authenticated)
        return self._active_state(principal).permissions

    def authorize_role_assignment(
        self,
        *,
        tenant: TenantContext,
        authenticated: AuthenticatedSession,
        recipient_membership_id: UUID,
        role_id: UUID,
    ) -> None:
        """Authorize assignment only within tenant and only for explicitly delegable codes."""
        principal = self._principal_for(tenant, authenticated)
        state = self._active_state(principal)
        if PermissionCode.ROLE_ASSIGN not in state.permissions:
            raise PermissionDenied
        if not self._repository.is_active_membership(
            tenant_id=tenant.tenant_id, membership_id=recipient_membership_id
        ):
            raise PermissionDenied
        target_permissions = self._repository.active_role_permissions(
            tenant_id=tenant.tenant_id, role_id=role_id
        )
        if target_permissions is None or not target_permissions.issubset(state.delegable_permissions):
            raise PermissionDenied

    def authorize_permission_assignment(
        self,
        *,
        tenant: TenantContext,
        authenticated: AuthenticatedSession,
        role_id: UUID,
        permission: PermissionCode | str,
    ) -> None:
        """Authorize a role-permission change without allowing arbitrary privilege grants."""
        code = permission_code(permission)
        principal = self._principal_for(tenant, authenticated)
        state = self._active_state(principal)
        if PermissionCode.ROLE_PERMISSION_ASSIGN not in state.permissions:
            raise PermissionDenied
        if code not in state.delegable_permissions:
            raise PermissionDenied
        if self._repository.active_role_permissions(
            tenant_id=tenant.tenant_id, role_id=role_id
        ) is None:
            raise PermissionDenied

    @staticmethod
    def _principal_for(
        tenant: TenantContext, authenticated: AuthenticatedSession
    ) -> AuthorizationPrincipal:
        if (
            authenticated.tenant_id != tenant.tenant_id
            or authenticated.user_status != "active"
            or authenticated.membership_status != "active"
            or authenticated.tenant_status != "active"
        ):
            raise PermissionDenied
        return AuthorizationPrincipal(
            tenant_id=tenant.tenant_id,
            user_id=authenticated.user_id,
            membership_id=authenticated.membership_id,
        )

    def _active_state(self, principal: AuthorizationPrincipal) -> AuthorizationState:
        state = self._repository.load_state(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            membership_id=principal.membership_id,
        )
        if state is None:
            raise PermissionDenied
        return state
