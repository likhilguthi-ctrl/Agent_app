from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApprovalDecision(Base):
    """Immutable audit record of an administrative review decision."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_approval_decisions_decision",
        ),
        UniqueConstraint("appointment_id", name="uq_approval_decisions_appointment_id"),
        Index("ix_approval_decisions_tenant_id", "tenant_id"),
        Index("ix_approval_decisions_appointment_id", "appointment_id"),
        Index("ix_approval_decisions_decided_by", "decided_by_user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_appointments.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
