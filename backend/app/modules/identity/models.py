from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """Community-scoped authentication identity; platform operators are separate."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TenantMembership(Base):
    """Tenant relationship and community-specific mobile login identifier."""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_tenant_memberships_status"),
        CheckConstraint(
            "length(mobile_e164) BETWEEN 9 AND 16 AND substr(mobile_e164, 1, 1) = '+'",
            name="ck_tenant_memberships_mobile_e164_shape",
        ),
        UniqueConstraint("tenant_id", "mobile_e164", name="uq_tenant_memberships_tenant_mobile"),
        # Product policy: each community identity belongs to one community.
        UniqueConstraint("user_id", name="uq_tenant_memberships_user_identity"),
        # Supports tenant-consistent composite foreign keys from session records.
        UniqueConstraint("tenant_id", "id", name="uq_tenant_memberships_tenant_id_id"),
        Index("ix_tenant_memberships_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mobile_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
