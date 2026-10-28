from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Uuid,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Permission(Base):
    """Platform-wide permission reference; catalog entries grant no access alone."""

    __tablename__ = "permissions"
    __table_args__ = (
        CheckConstraint("code = lower(code)", name="ck_permissions_code_lowercase"),
        CheckConstraint("length(code) BETWEEN 3 AND 100", name="ck_permissions_code_length"),
    )

    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Role(Base):
    """Tenant-owned role. No built-in or privileged roles are seeded."""

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_roles_status"),
        CheckConstraint("code = lower(code)", name="ck_roles_code_lowercase"),
        CheckConstraint("length(code) BETWEEN 1 AND 100", name="ck_roles_code_length"),
        UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_code"),
        UniqueConstraint("tenant_id", "id", name="uq_roles_tenant_id_id"),
        Index("ix_roles_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RolePermission(Base):
    """Tenant-scoped link between one role and one catalog permission."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["roles.tenant_id", "roles.id"],
            ondelete="RESTRICT",
            name="fk_role_permissions_tenant_role",
        ),
        ForeignKeyConstraint(
            ["permission_code"], ["permissions.code"], ondelete="RESTRICT",
            name="fk_role_permissions_permission",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')", name="ck_role_permissions_status"
        ),
        CheckConstraint(
            "(status IN ('active', 'inactive') AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_role_permissions_revocation_state",
        ),
        Index("ix_role_permissions_tenant_role_status", "tenant_id", "role_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    role_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    permission_code: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    # Delegation is denied unless an explicitly reviewed provisioning flow opts in.
    is_delegable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserRoleAssignment(Base):
    """Auditable tenant-scoped membership-to-role assignment."""

    __tablename__ = "user_role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_user_role_assignments_tenant_membership",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["roles.tenant_id", "roles.id"],
            ondelete="RESTRICT",
            name="fk_user_role_assignments_tenant_role",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assigned_by_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_user_role_assignments_assigner_membership",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')", name="ck_user_role_assignments_status"
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_user_role_assignments_validity_window",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status != 'revoked' AND revoked_at IS NULL)",
            name="ck_user_role_assignments_revocation_state",
        ),
        Index(
            "ix_user_role_assignments_tenant_membership_status",
            "tenant_id", "membership_id", "status",
        ),
        Index(
            "uq_user_role_assignments_active",
            "tenant_id", "membership_id", "role_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assigned_by_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
