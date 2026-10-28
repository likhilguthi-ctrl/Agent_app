from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PlanStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class SubscriptionStatus(StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class SubscriptionPaymentStatus(StrEnum):
    UNPAID = "UNPAID"
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class SubscriptionPlan(Base):
    """Community-specific subscription plan definition managed by Super Admin / Manager."""

    __tablename__ = "subscription_plans"
    __table_args__ = (
        CheckConstraint("duration_days > 0", name="ck_sub_plan_duration"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_sub_plan_status"),
        CheckConstraint("nominal_price IS NULL OR nominal_price >= 0.00", name="ck_sub_plan_price"),
        CheckConstraint("length(currency) = 3", name="ck_sub_plan_currency"),
        Index("ix_subscription_plans_community_id", "community_id"),
        Index("ix_subscription_plans_community_status", "community_id", "status"),
        Index("ix_subscription_plans_status", "status"),
        UniqueConstraint("community_id", "code", name="uq_subscription_plans_community_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    community_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    benefits: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=PlanStatus.ACTIVE.value, nullable=False)
    nominal_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    created_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Backward-compatible property alias
    @property
    def tenant_id(self) -> UUID:
        return self.community_id

    @tenant_id.setter
    def tenant_id(self, value: UUID) -> None:
        self.community_id = value

    # Relationships
    community: Mapped[Any] = relationship(
        "Tenant", foreign_keys=[community_id], lazy="joined"
    )
    subscriptions: Mapped[list[MemberSubscription]] = relationship(
        "MemberSubscription", back_populates="plan", passive_deletes="all"
    )


class MemberSubscription(Base):
    """Tenant-scoped subscription agreement for a subscriber."""

    __tablename__ = "member_subscriptions"
    __table_args__ = (
        CheckConstraint("agreed_amount >= 0.00", name="ck_mem_sub_amount"),
        CheckConstraint("length(currency) = 3", name="ck_mem_sub_currency"),
        CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'ACTIVE', 'EXPIRED', 'CANCELLED')",
            name="ck_mem_sub_status",
        ),
        CheckConstraint(
            "payment_status IN ('UNPAID', 'PENDING', 'SETTLED', 'FAILED', 'REFUNDED')",
            name="ck_mem_sub_payment",
        ),
        CheckConstraint("expires_at > starts_at", name="ck_mem_sub_dates"),
        CheckConstraint("snapshot_duration_days > 0", name="ck_mem_sub_duration"),
        UniqueConstraint("tenant_id", "id", name="uq_member_subscriptions_tenant_id_id"),
        Index("ix_member_subscriptions_tenant_id", "tenant_id"),
        Index("ix_member_subscriptions_subscriber", "tenant_id", "subscriber_membership_id"),
        Index("ix_member_subscriptions_plan_id", "plan_id"),
        Index("ix_member_subscriptions_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    subscriber_membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False
    )

    # Manually entered agreed subscription amount (immutable historical snapshot)
    agreed_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), default=SubscriptionStatus.PENDING_PAYMENT.value, nullable=False
    )
    payment_status: Mapped[str] = mapped_column(
        String(20), default=SubscriptionPaymentStatus.UNPAID.value, nullable=False
    )

    # Historical plan snapshots to protect agreed terms against future plan modifications
    snapshot_plan_name: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_duration_days: Mapped[int] = mapped_column(Integer, nullable=False)

    notes: Mapped[Any | None] = mapped_column(
        sa.JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=True
    )

    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    plan: Mapped[SubscriptionPlan] = relationship("SubscriptionPlan", back_populates="subscriptions")
