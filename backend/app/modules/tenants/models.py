from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    column,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Tenant(Base):
    """Platform-level community record. New tenants are inactive by default."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_tenants_status"),
        CheckConstraint("slug = lower(slug)", name="ck_tenants_slug_lowercase"),
        CheckConstraint("length(slug) BETWEEN 1 AND 63", name="ck_tenants_slug_length"),
        CheckConstraint("photo_requirement IN ('OPTIONAL', 'REQUIRED')", name="ck_tenants_photo_requirement"),
        CheckConstraint("default_commission_rate >= 0 AND default_commission_rate <= 100", name="ck_tenants_default_commission_rate"),
        Index("uq_tenants_slug_normalized", func.lower(column("slug")), unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inactive", server_default="inactive"
    )
    photo_requirement: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OPTIONAL", server_default="OPTIONAL"
    )
    default_commission_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00"), server_default="10.00"
    )
    policy_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="v1", server_default="v1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    domains: Mapped[list[TenantDomain]] = relationship(
        back_populates="tenant", passive_deletes=True
    )


class TenantDomain(Base):
    """Hostname registry entry; pending and inactive entries never resolve."""

    __tablename__ = "tenant_domains"
    __table_args__ = (
        CheckConstraint(
            "verification_status IN ('pending', 'verified', 'revoked')",
            name="ck_tenant_domains_verification_status",
        ),
        CheckConstraint(
            "verification_status != 'verified' OR verified_at IS NOT NULL",
            name="ck_tenant_domains_verified_timestamp",
        ),
        CheckConstraint("hostname = lower(hostname)", name="ck_tenant_domains_hostname_lowercase"),
        Index(
            "uq_tenant_domains_hostname_normalized",
            func.lower(column("hostname")),
            unique=True,
        ),
        Index("ix_tenant_domains_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    tenant: Mapped[Tenant] = relationship(back_populates="domains")


class CommissionAuditLog(Base):
    """Audit record for community default or agent commission percentage changes."""

    __tablename__ = "commission_audit_logs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    agent_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    changed_by_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    changed_by_username: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)  # "community" or "agent"
    previous_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    new_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Region(Base):
    """Administrative operating region scoped to a community."""

    __tablename__ = "regions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_regions_tenant_code"),
        Index("ix_regions_tenant_status", "tenant_id", "status"),
        Index("ix_regions_tenant_state_status", "tenant_id", "state", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Andhra Pradesh", server_default="Andhra Pradesh"
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserRegionAssignment(Base):
    """Association binding an Admin or Agent to authorized operating regions."""

    __tablename__ = "user_region_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "region_id", name="uq_user_region_tenant_user_region"),
        Index("ix_user_region_assignments_lookup", "tenant_id", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    region_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("regions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    assigned_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RegionAuditLog(Base):
    """Audit log tracking regional assignments and revocations."""

    __tablename__ = "region_audit_logs"
    __table_args__ = (
        Index("ix_region_audit_logs_tenant", "tenant_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    region_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # "assigned", "revoked"
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminProfile(Base):
    """Identity details and identity documents for a community Administrator."""

    __tablename__ = "admin_profiles"
    __table_args__ = (
        Index("ix_admin_profiles_tenant", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile_photo_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aadhaar_document_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pan_document_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aadhaar_number_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pan_number_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    whatsapp_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
