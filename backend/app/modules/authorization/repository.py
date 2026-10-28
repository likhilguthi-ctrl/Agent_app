from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.errors import AuthorizationDataUnavailable
from app.modules.authorization.models import Permission, Role, RolePermission, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.tenants.models import Tenant


@dataclass(frozen=True, slots=True)
class AuthorizationState:
    permissions: frozenset[PermissionCode]
    delegable_permissions: frozenset[PermissionCode]


@dataclass(frozen=True, slots=True)
class TenantRoleSummary:
    id: UUID
    code: str
    display_name: str
    status: str
    permissions: tuple[PermissionCode, ...]


class SQLAlchemyAuthorizationRepository:
    """Loads authorization state through tenant-consistent SQL joins."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        try:
            self._session_factory = session_factory or get_session_factory()
        except RuntimeError as exc:
            raise AuthorizationDataUnavailable from exc

    def load_state(
        self, *, tenant_id: UUID, user_id: UUID, membership_id: UUID
    ) -> AuthorizationState | None:
        now = datetime.now(UTC)
        try:
            with self._session_factory() as session:
                active_membership = session.execute(
                    select(TenantMembership.id)
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        TenantMembership.id == membership_id,
                        TenantMembership.tenant_id == tenant_id,
                        TenantMembership.user_id == user_id,
                        TenantMembership.status == "active",
                        User.status == "active",
                        Tenant.status == "active",
                    )
                ).scalar_one_or_none()
                if active_membership is None:
                    return None

                rows = session.execute(
                    select(Permission.code, RolePermission.is_delegable)
                    .select_from(UserRoleAssignment)
                    .join(
                        TenantMembership,
                        (TenantMembership.tenant_id == UserRoleAssignment.tenant_id)
                        & (TenantMembership.id == UserRoleAssignment.membership_id),
                    )
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == UserRoleAssignment.tenant_id)
                    .join(
                        Role,
                        (Role.tenant_id == UserRoleAssignment.tenant_id)
                        & (Role.id == UserRoleAssignment.role_id),
                    )
                    .join(
                        RolePermission,
                        (RolePermission.tenant_id == Role.tenant_id)
                        & (RolePermission.role_id == Role.id),
                    )
                    .join(Permission, Permission.code == RolePermission.permission_code)
                    .where(
                        UserRoleAssignment.tenant_id == tenant_id,
                        UserRoleAssignment.membership_id == membership_id,
                        UserRoleAssignment.status == "active",
                        UserRoleAssignment.valid_from <= now,
                        or_(
                            UserRoleAssignment.valid_until.is_(None),
                            UserRoleAssignment.valid_until > now,
                        ),
                        TenantMembership.status == "active",
                        User.status == "active",
                        Tenant.status == "active",
                        Role.status == "active",
                        RolePermission.status == "active",
                    )
                ).all()

                permissions: set[PermissionCode] = set()
                delegable: set[PermissionCode] = set()
                for row in rows:
                    try:
                        code = PermissionCode(row.code)
                    except ValueError as exc:
                        # A DB/catalog drift is an unavailable policy, never an allow.
                        raise AuthorizationDataUnavailable from exc
                    permissions.add(code)
                    if row.is_delegable:
                        delegable.add(code)
                return AuthorizationState(frozenset(permissions), frozenset(delegable))
        except AuthorizationDataUnavailable:
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthorizationDataUnavailable from exc

    def active_role_permissions(self, *, tenant_id: UUID, role_id: UUID) -> frozenset[PermissionCode] | None:
        """Return permissions only for an active role owned by the resolved tenant."""
        try:
            with self._session_factory() as session:
                role = session.execute(
                    select(Role.id).where(
                        Role.id == role_id,
                        Role.tenant_id == tenant_id,
                        Role.status == "active",
                    )
                ).scalar_one_or_none()
                if role is None:
                    return None
                rows = session.execute(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_code == Permission.code)
                    .where(
                        RolePermission.tenant_id == tenant_id,
                        RolePermission.role_id == role_id,
                        RolePermission.status == "active",
                    )
                ).scalars().all()
                try:
                    return frozenset(PermissionCode(code) for code in rows)
                except ValueError as exc:
                    raise AuthorizationDataUnavailable from exc
        except AuthorizationDataUnavailable:
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthorizationDataUnavailable from exc

    def is_active_membership(self, *, tenant_id: UUID, membership_id: UUID) -> bool:
        try:
            with self._session_factory() as session:
                result = session.execute(
                    select(TenantMembership.id)
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        TenantMembership.id == membership_id,
                        TenantMembership.tenant_id == tenant_id,
                        TenantMembership.status == "active",
                        User.status == "active",
                        Tenant.status == "active",
                    )
                ).scalar_one_or_none()
                return result is not None
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthorizationDataUnavailable from exc

    def list_roles_for_tenant(
        self, *, tenant_id: UUID, limit: int, offset: int
    ) -> list[TenantRoleSummary]:
        """List tenant-owned roles and their active registered permissions."""
        try:
            with self._session_factory() as session:
                roles = session.execute(
                    select(Role.id, Role.code, Role.display_name, Role.status)
                    .where(Role.tenant_id == tenant_id)
                    .order_by(Role.code, Role.id)
                    .limit(limit)
                    .offset(offset)
                ).mappings().all()
                if not roles:
                    return []

                role_ids = [role["id"] for role in roles]
                permission_rows = session.execute(
                    select(RolePermission.role_id, Permission.code)
                    .join(Permission, Permission.code == RolePermission.permission_code)
                    .where(
                        RolePermission.tenant_id == tenant_id,
                        RolePermission.role_id.in_(role_ids),
                        RolePermission.status == "active",
                    )
                    .order_by(RolePermission.role_id, Permission.code)
                ).all()

                permissions_by_role: dict[UUID, list[PermissionCode]] = {
                    role_id: [] for role_id in role_ids
                }
                for role_id, code in permission_rows:
                    try:
                        permissions_by_role[role_id].append(PermissionCode(code))
                    except ValueError as exc:
                        raise AuthorizationDataUnavailable from exc

                return [
                    TenantRoleSummary(
                        id=role["id"],
                        code=role["code"],
                        display_name=role["display_name"],
                        status=role["status"],
                        permissions=tuple(permissions_by_role[role["id"]]),
                    )
                    for role in roles
                ]
        except AuthorizationDataUnavailable:
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthorizationDataUnavailable from exc


def get_authorization_repository() -> SQLAlchemyAuthorizationRepository:
    return SQLAlchemyAuthorizationRepository()
