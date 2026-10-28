from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    JSON,
    Index,
    String,
    Uuid,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlatformOperator(Base):
    """An operator record bound to an external OIDC issuer and subject."""

    __tablename__ = "platform_operators"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_platform_operators_status"),
        UniqueConstraint("issuer", "subject", name="uq_platform_operators_issuer_subject"),
        Index("ix_platform_operators_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    mobile_e164: Mapped[str | None] = mapped_column(String(16), nullable=True, unique=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile_photo_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PlatformPermission(Base):
    """A platform-only permission definition; a catalog entry grants no authority."""

    __tablename__ = "platform_permissions"
    __table_args__ = (
        CheckConstraint("code = lower(code)", name="ck_platform_permissions_code_lowercase"),
        CheckConstraint("length(code) BETWEEN 3 AND 100", name="ck_platform_permissions_code_length"),
    )

    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformRole(Base):
    """Platform role definitions are separate from tenant roles."""

    __tablename__ = "platform_roles"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_platform_roles_status"),
        CheckConstraint("code = lower(code)", name="ck_platform_roles_code_lowercase"),
        UniqueConstraint("code", name="uq_platform_roles_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformRolePermission(Base):
    """Platform role-to-permission link, never consulted by tenant RBAC."""

    __tablename__ = "platform_role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(["role_id"], ["platform_roles.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["permission_code"], ["platform_permissions.code"], ondelete="RESTRICT"
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')", name="ck_platform_role_permissions_status"
        ),
        CheckConstraint(
            "(status IN ('active', 'inactive') AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_platform_role_permissions_revocation_state",
        ),
        Index("ix_platform_role_permissions_role_status", "role_id", "status"),
    )

    role_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    permission_code: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformOperatorRoleAssignment(Base):
    """Explicit operator authorization with separate grantor provenance."""

    __tablename__ = "platform_operator_role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(["role_id"], ["platform_roles.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["assigned_by_operator_id"],
            ["platform_operators.id"],
            ondelete="RESTRICT",
            name="fk_platform_role_assignments_assigner",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')",
            name="ck_platform_operator_role_assignments_status",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_platform_operator_role_assignments_validity_window",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status != 'revoked' AND revoked_at IS NULL)",
            name="ck_platform_operator_role_assignments_revocation_state",
        ),
        CheckConstraint(
            "operator_id != assigned_by_operator_id",
            name="ck_platform_operator_role_assignments_not_self_assigned",
        ),
        Index(
            "ix_platform_operator_role_assignments_operator_status",
            "operator_id", "status", "valid_from", "valid_until",
        ),
        Index(
            "uq_platform_operator_role_assignments_active",
            "operator_id", "role_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assigned_by_operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformOIDCLoginTransaction(Base):
    """Single-use server-side authorization-code flow state; secrets are encrypted."""

    __tablename__ = "platform_oidc_login_transactions"
    __table_args__ = (
        UniqueConstraint("state_digest", name="uq_platform_oidc_login_state_digest"),
        CheckConstraint("length(state_digest) = 64", name="ck_platform_oidc_state_digest_length"),
        CheckConstraint("length(browser_binding_digest) = 64", name="ck_platform_oidc_binding_digest_length"),
        CheckConstraint("expires_at > created_at", name="ck_platform_oidc_login_expiry"),
        CheckConstraint(
            "consumed_at IS NULL OR exchange_started_at IS NOT NULL",
            name="ck_platform_oidc_login_consumption_state",
        ),
        Index("ix_platform_oidc_login_expiry", "expires_at", "exchange_started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    browser_binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_flow_secrets: Mapped[str] = mapped_column(String(2048), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformOperatorSession(Base):
    """Opaque, revocable platform-operator session; only token digest is persisted."""

    __tablename__ = "platform_operator_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("token_digest", name="uq_platform_operator_sessions_token_digest"),
        CheckConstraint("length(token_digest) = 64", name="ck_platform_operator_session_digest_length"),
        CheckConstraint("length(csrf_token) = 64", name="ck_platform_operator_session_csrf_length"),
        CheckConstraint("expires_at > created_at", name="ck_platform_operator_session_expiry"),
        Index("ix_platform_operator_sessions_operator_expiry", "operator_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amr: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
