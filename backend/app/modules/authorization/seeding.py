from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.authorization.catalog import PERMISSION_DESCRIPTIONS, PermissionCode
from app.modules.authorization.models import Permission, Role, RolePermission

ADMIN_PERMISSIONS: tuple[PermissionCode, ...] = (
    PermissionCode.AGENT_CREATE,
    PermissionCode.AGENT_READ,
    PermissionCode.AGENT_UPDATE,
    PermissionCode.AGENT_CANCEL,
    PermissionCode.AGENT_APPROVE,
    PermissionCode.AGENT_REJECT,
    PermissionCode.AGENT_SUSPEND,
    PermissionCode.AGENT_REVOKE,
    PermissionCode.APPROVAL_READ,
    PermissionCode.REGION_CREATE,
    PermissionCode.REGION_READ,
    PermissionCode.REGION_UPDATE,
    PermissionCode.REGION_MANAGE,
    PermissionCode.REGION_ASSIGN,
    PermissionCode.MEMBER_CREATE,
    PermissionCode.MEMBER_READ,
    PermissionCode.MEMBER_UPDATE,
    PermissionCode.MEMBER_RELATIONSHIP_MANAGE,
    PermissionCode.MEMBER_STATS_READ,
    PermissionCode.MEMBER_DESCENDANTS_READ,
    PermissionCode.MEMBER_DESCENDANTS_CREATE,
    PermissionCode.AUDIT_READ,
    PermissionCode.NOTIFICATION_READ,
    PermissionCode.NOTIFICATION_MANAGE,
    PermissionCode.COMMISSION_READ,
    PermissionCode.COMMISSION_MANAGE,
    PermissionCode.SUBSCRIPTION_READ,
    PermissionCode.SUBSCRIPTION_MANAGE,
    PermissionCode.PAYMENT_READ,
    PermissionCode.PAYMENT_MANAGE,
    PermissionCode.PAYOUT_READ,
    PermissionCode.PAYOUT_REVIEW,
    PermissionCode.TARGET_READ,
    PermissionCode.TARGET_ALLOCATE,
)

AGENT_PERMISSIONS: tuple[PermissionCode, ...] = (
    PermissionCode.AGENT_READ,
    PermissionCode.REGION_READ,
    PermissionCode.MEMBER_CREATE,
    PermissionCode.MEMBER_READ,
    PermissionCode.MEMBER_STATS_READ,
    PermissionCode.MEMBER_DESCENDANTS_READ,
    PermissionCode.MEMBER_DESCENDANTS_CREATE,
    PermissionCode.COMMISSION_READ,
    PermissionCode.SUBSCRIPTION_READ,
    PermissionCode.SUBSCRIPTION_MANAGE,
    PermissionCode.PAYMENT_READ,
    PermissionCode.PAYMENT_MANAGE,
    PermissionCode.PAYOUT_READ,
    PermissionCode.PAYOUT_REQUEST,
    PermissionCode.TARGET_READ,
)


def seed_catalog_permissions(session: Session) -> None:
    """Ensure all registered catalog permissions exist in the permissions table."""
    now = datetime.now(UTC)
    for code, desc in PERMISSION_DESCRIPTIONS.items():
        existing = session.execute(
            select(Permission.code).where(Permission.code == code.value)
        ).scalar_one_or_none()
        if existing is None:
            session.add(Permission(code=code.value, description=desc, created_at=now))
    session.flush()


def seed_tenant_role_permissions(
    session: Session,
    tenant_id: UUID,
    admin_role_id: UUID | None = None,
    agent_role_id: UUID | None = None,
) -> tuple[Role, Role]:
    """Idempotently seed the standard admin and agent roles and role-permission mappings for a tenant."""
    seed_catalog_permissions(session)
    now = datetime.now(UTC)

    # 1. Tenant Admin Role
    admin_role: Role | None = None
    if admin_role_id is not None:
        admin_role = session.execute(
            select(Role).where(Role.id == admin_role_id, Role.tenant_id == tenant_id)
        ).scalar_one_or_none()

    if admin_role is None:
        admin_role = session.execute(
            select(Role).where(Role.tenant_id == tenant_id, Role.code == "admin")
        ).scalar_one_or_none()

    if admin_role is None:
        admin_role = Role(
            id=admin_role_id or uuid4(),
            tenant_id=tenant_id,
            code="admin",
            display_name="Tenant Administrator",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(admin_role)
        session.flush()

    for perm_code in ADMIN_PERMISSIONS:
        existing_rp = session.execute(
            select(RolePermission.permission_code).where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_id == admin_role.id,
                RolePermission.permission_code == perm_code.value,
            )
        ).scalar_one_or_none()
        if existing_rp is None:
            session.add(
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=admin_role.id,
                    permission_code=perm_code.value,
                    status="active",
                )
            )

    # 2. Tenant Agent Role
    agent_role: Role | None = None
    if agent_role_id is not None:
        agent_role = session.execute(
            select(Role).where(Role.id == agent_role_id, Role.tenant_id == tenant_id)
        ).scalar_one_or_none()

    if agent_role is None:
        agent_role = session.execute(
            select(Role).where(Role.tenant_id == tenant_id, Role.code == "agent")
        ).scalar_one_or_none()

    if agent_role is None:
        agent_role = Role(
            id=agent_role_id or uuid4(),
            tenant_id=tenant_id,
            code="agent",
            display_name="Tenant Agent",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_role)
        session.flush()

    for perm_code in AGENT_PERMISSIONS:
        existing_rp = session.execute(
            select(RolePermission.permission_code).where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_id == agent_role.id,
                RolePermission.permission_code == perm_code.value,
            )
        ).scalar_one_or_none()
        if existing_rp is None:
            session.add(
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=agent_role.id,
                    permission_code=perm_code.value,
                    status="active",
                )
            )

    session.flush()
    return admin_role, agent_role

