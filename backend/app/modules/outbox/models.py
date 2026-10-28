from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditOutboxEvent(Base):
    """Immutable audit payload plus mutable delivery/retry state."""

    __tablename__ = "audit_outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'delivering', 'retryable_failure', 'delivered', 'terminal_failure')",
            name="ck_audit_outbox_events_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_audit_outbox_events_attempt_count"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_audit_outbox_events_digest_length"),
        CheckConstraint(
            "(status = 'delivering' AND worker_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'delivering' AND worker_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_audit_outbox_events_lease_state",
        ),
        CheckConstraint(
            "(status = 'delivered' AND acknowledged_at IS NOT NULL AND acknowledgement_ref IS NOT NULL) OR "
            "(status != 'delivered' AND acknowledged_at IS NULL AND acknowledgement_ref IS NULL)",
            name="ck_audit_outbox_events_ack_state",
        ),
        Index("ix_audit_outbox_events_dispatch", "status", "next_attempt_at", "created_at"),
        Index("ix_audit_outbox_events_lease", "status", "lease_expires_at"),
        UniqueConstraint(
            "idempotency_key", name="uq_audit_outbox_events_idempotency_key"
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("audit_events.id", ondelete="RESTRICT"), primary_key=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    acknowledgement_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuditOutboxAttempt(Base):
    """Durable lifecycle record for a single leased delivery attempt."""

    __tablename__ = "audit_outbox_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'delivered', 'retryable_failure', 'terminal_failure')",
            name="ck_audit_outbox_attempts_status",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_audit_outbox_attempts_number"),
        CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status != 'in_progress' AND completed_at IS NOT NULL)",
            name="ck_audit_outbox_attempts_completion_state",
        ),
        UniqueConstraint("event_id", "attempt_number", name="uq_audit_outbox_attempts_number"),
        Index("ix_audit_outbox_attempts_event_started", "event_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("audit_outbox_events.event_id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    acknowledgement_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuditOutboxWorkerHeartbeat(Base):
    """Recent proof that a configured worker reached a verified sink contract."""

    __tablename__ = "audit_outbox_worker_heartbeats"
    __table_args__ = (
        Index("ix_audit_outbox_worker_heartbeats_last_seen", "last_seen_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    adapter_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sink_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
