from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    """Append-only local security/business-decision audit record."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_audit_events_schema_version"),
        CheckConstraint(
            "actor_category IN ('platform_operator', 'tenant_member', 'system', 'unauthenticated')",
            name="ck_audit_events_actor_category",
        ),
        CheckConstraint(
            "(actor_category = 'unauthenticated' AND actor_ref IS NULL) OR "
            "(actor_category != 'unauthenticated' AND actor_ref IS NOT NULL)",
            name="ck_audit_events_actor_reference",
        ),
        CheckConstraint(
            "outcome IN ('success', 'denied', 'failure', 'pending')",
            name="ck_audit_events_outcome",
        ),
        Index("ix_audit_events_tenant_recorded", "tenant_id", "recorded_at", "id"),
        Index("ix_audit_events_type_recorded", "event_type", "recorded_at"),
        Index("ix_audit_events_actor_recorded", "actor_category", "actor_ref", "recorded_at"),
        Index(
            "ix_audit_events_actor_type_occurred",
            "actor_category",
            "actor_ref",
            "event_type",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_category: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    target_entity_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_entity_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_metadata: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
