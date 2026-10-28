from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PayoutStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PROCESSING = "PROCESSING"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_PAYOUT_STATUSES = frozenset({
    PayoutStatus.PAID.value,
    PayoutStatus.REJECTED.value,
    PayoutStatus.FAILED.value,
    PayoutStatus.CANCELLED.value,
})


class PayoutRequest(Base):
    """Authoritative agent payout request record governed by Community Admin and Platform Super Admin."""

    __tablename__ = "payout_requests"
    __table_args__ = (
        CheckConstraint("amount > 0.00", name="ck_payout_requests_amount"),
        CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED', 'PROCESSING', 'PAID', 'FAILED', 'CANCELLED')",
            name="ck_payout_requests_status",
        ),
        CheckConstraint("length(currency) = 3", name="ck_payout_requests_currency"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_payout_requests_idempotency"),
        Index("ix_payout_requests_tenant_status", "tenant_id", "status"),
        Index("ix_payout_requests_agent", "tenant_id", "agent_membership_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    agent_membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False
    )
    agent_profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commission_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=PayoutStatus.PENDING_REVIEW.value, nullable=False
    )

    # Financial Ledger Journal Links
    payout_hold_journal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commission_journals.id", ondelete="RESTRICT"), nullable=True
    )
    disbursement_journal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commission_journals.id", ondelete="RESTRICT"), nullable=True
    )
    reversal_journal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commission_journals.id", ondelete="RESTRICT"), nullable=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    bank_account_snapshot: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    request_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Community Admin Review Audit
    reviewed_by_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Platform Super Admin Disbursement Audit
    disbursed_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    disbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disbursement_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    disbursement_method: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

