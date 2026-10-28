from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentProfile(Base):
    """Authoritative agent profile bound 1-to-1 to a tenant membership."""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        CheckConstraint(
            "commission_rate_override IS NULL OR (commission_rate_override >= 0 AND commission_rate_override <= 100)",
            name="ck_agent_profiles_commission_override",
        ),
        Index("ix_agent_profiles_tenant_id", "tenant_id"),
        Index("ix_agent_profiles_membership_id", "membership_id", unique=True),
        Index("ix_agent_profiles_user_id", "user_id", unique=True),
        Index("ix_agent_profiles_ref_id", "ref_id", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    whatsapp_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mandal: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("states.id", ondelete="RESTRICT"), nullable=True
    )
    district_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("districts.id", ondelete="RESTRICT"), nullable=True
    )
    mandal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mandals.id", ondelete="RESTRICT"), nullable=True
    )
    manager_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    manager_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True
    )
    parent_agent_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True
    )
    ref_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    commission_rate_override: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    profile_photo_key: Mapped[str] = mapped_column(String(64), nullable=False)
    aadhaar_number_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    aadhaar_number_masked: Mapped[str] = mapped_column(String(16), nullable=False)
    aadhaar_document_key: Mapped[str] = mapped_column(String(64), nullable=False)
    pan_number_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    pan_number_masked: Mapped[str] = mapped_column(String(16), nullable=False)
    pan_document_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SubscriberProfile(Base):
    """End-user subscriber registered by an agent."""

    __tablename__ = "subscriber_profiles"
    __table_args__ = (
        Index("ix_subscriber_profiles_tenant_id", "tenant_id"),
        Index("ix_subscriber_profiles_membership_id", "membership_id", unique=True),
        Index("ix_subscriber_profiles_agent_membership_id", "registered_by_agent_membership_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    registered_by_agent_membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    whatsapp_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    whatsapp_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    whatsapp_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    photo_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_plan: Mapped[str | None] = mapped_column(String(50), nullable=True)
    billing_cycle: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

