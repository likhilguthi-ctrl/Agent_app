from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NotificationMessage(Base):
    """Authoritative record of notification messages dispatched by the platform."""

    __tablename__ = "notification_messages"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        UniqueConstraint("message_id", name="uq_notification_messages_message_id"),
        Index("ix_notification_messages_tenant_status", "tenant_id", "status"),
        Index("ix_notification_messages_message_id", "message_id"),
        Index("ix_notification_messages_challenge_id", "challenge_id"),
        CheckConstraint(
            "channel IN ('SMS', 'WHATSAPP', 'EMAIL')",
            name="ck_notification_messages_channel",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'PROVIDER_ACCEPTED', 'DELIVERED', 'READ', 'RETRY_PENDING', 'MOCK_SENT', 'FAILED', 'EXPIRED', 'UNKNOWN_OUTCOME')",
            name="ck_notification_messages_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="P0")
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenge_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class NotificationWebhookEvent(Base):
    """Immutable audit and deduplication log of inbound status webhooks."""

    __tablename__ = "notification_webhook_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_notification_webhook_event_id"),
        Index("ix_notification_webhook_event_id", "event_id"),
        Index("ix_notification_webhook_message_id", "message_id"),
        Index("ix_notification_webhook_status", "processing_status"),
        CheckConstraint(
            "processing_status IN ('received', 'processed', 'duplicate', 'ignored', 'quarantined', 'failed')",
            name="ck_notification_webhook_proc_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

