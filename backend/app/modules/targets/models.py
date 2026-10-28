from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TargetType(Base):
    """Configurable target/KPI definition."""

    __tablename__ = "target_types"
    __table_args__ = (
        CheckConstraint(
            "calculation_type IN ('AUTOMATIC', 'MANUAL')",
            name="ck_target_types_calculation_type",
        ),
        CheckConstraint(
            "applicable_role IN ('ADMIN', 'AGENT', 'BOTH')",
            name="ck_target_types_applicable_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    calculation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    applicable_role: Mapped[str] = mapped_column(String(32), nullable=False, default="BOTH")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class TargetAssignment(Base):
    """Hierarchical target assignment to an Admin or Agent."""

    __tablename__ = "target_assignments"
    __table_args__ = (
        CheckConstraint("target_quantity > 0", name="ck_target_assignments_target_quantity_pos"),
        CheckConstraint("start_date <= deadline", name="ck_target_assignments_dates_valid"),
        CheckConstraint(
            "assigned_role IN ('ADMIN', 'AGENT')",
            name="ck_target_assignments_assigned_role",
        ),
        CheckConstraint(
            "period IN ('WEEKLY', 'MONTHLY', 'QUARTERLY', 'ANNUAL', 'CUSTOM')",
            name="ck_target_assignments_period",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'COMPLETED', 'CANCELLED', 'OVERDUE')",
            name="ck_target_assignments_status",
        ),
        Index("ix_target_assignments_tenant_assigned_role", "tenant_id", "assigned_role"),
        Index("ix_target_assignments_assignee", "assigned_to_user_id"),
        Index("ix_target_assignments_parent", "parent_target_id"),
        Index("ix_target_assignments_period_dates", "start_date", "deadline"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    parent_target_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("target_assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_type_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("target_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assigned_to_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_to_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant_memberships.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assigned_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    assigned_role: Mapped[str] = mapped_column(String(32), nullable=False)
    target_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    deadline: Mapped[date] = mapped_column(Date, nullable=False)
    period: Mapped[str] = mapped_column(String(32), nullable=False, default="MONTHLY")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    target_type: Mapped[TargetType] = relationship(lazy="joined")
    parent_target: Mapped[TargetAssignment | None] = relationship(
        remote_side=[id], backref="child_targets"
    )
    adjustments: Mapped[list[TargetAchievementAdjustment]] = relationship(
        back_populates="target", cascade="all, delete-orphan", order_by="TargetAchievementAdjustment.created_at"
    )


class TargetAchievementAdjustment(Base):
    """Auditable manual adjustment to a target's achievement count."""

    __tablename__ = "target_achievement_adjustments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("target_assignments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    adjusted_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    adjustment_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    # Relationship
    target: Mapped[TargetAssignment] = relationship(back_populates="adjustments")
