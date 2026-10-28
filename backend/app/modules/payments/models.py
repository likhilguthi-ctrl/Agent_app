from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PaymentOrderStatus(StrEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class PaymentTransactionStatus(StrEnum):
    CAPTURED = "captured"
    AUTHORIZED = "authorized"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentWebhookEventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class PaymentOrder(Base):
    """Payment order initiated for a subscription via payment gateway (Razorpay)."""

    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_payment_orders_amount_paise"),
        CheckConstraint("amount > 0.00", name="ck_payment_orders_amount"),
        CheckConstraint("length(currency) = 3", name="ck_payment_orders_currency"),
        CheckConstraint(
            "status IN ('created', 'attempted', 'paid', 'failed', 'expired')",
            name="ck_payment_orders_status",
        ),
        Index("ix_payment_orders_tenant_sub", "tenant_id", "subscription_id"),
        Index("ix_payment_orders_tenant_idempotency", "tenant_id", "idempotency_key"),
        Index("ix_payment_orders_gateway_order", "gateway", "gateway_order_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    subscription_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("member_subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    subscriber_membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False
    )

    gateway: Mapped[str] = mapped_column(String(32), default="razorpay", nullable=False)
    gateway_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_page_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    custom_amount: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=sa.text("false")
    )

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), default=PaymentOrderStatus.CREATED.value, nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)

    created_by_membership_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

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
    subscription: Mapped[Any] = relationship("MemberSubscription")
    transactions: Mapped[list[PaymentTransaction]] = relationship(
        "PaymentTransaction", back_populates="payment_order", passive_deletes="all"
    )


class PaymentTransaction(Base):
    """Authoritative record of a gateway payment attempt or settlement."""

    __tablename__ = "payment_transactions"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_payment_tx_amount_paise"),
        CheckConstraint("amount > 0.00", name="ck_payment_tx_amount"),
        CheckConstraint("length(currency) = 3", name="ck_payment_tx_currency"),
        CheckConstraint(
            "status IN ('captured', 'authorized', 'failed', 'refunded')",
            name="ck_payment_tx_status",
        ),
        Index("ix_payment_tx_order", "payment_order_id"),
        Index("ix_payment_tx_sub", "subscription_id"),
        Index("ix_payment_tx_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    payment_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False
    )
    subscription_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("member_subscriptions.id", ondelete="RESTRICT"), nullable=False
    )

    gateway: Mapped[str] = mapped_column(String(32), default="razorpay", nullable=False)
    gateway_payment_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), default=PaymentTransactionStatus.CAPTURED.value, nullable=False
    )
    gateway_signature: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    payment_order: Mapped[PaymentOrder] = relationship("PaymentOrder", back_populates="transactions")
    subscription: Mapped[Any] = relationship("MemberSubscription")


class PaymentWebhookEvent(Base):
    """Raw durable log of gateway webhook deliveries for idempotent processing."""

    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'processed', 'ignored', 'failed')",
            name="ck_payment_webhook_status",
        ),
        Index("ix_payment_webhook_status", "gateway", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    gateway: Mapped[str] = mapped_column(String(32), default="razorpay", nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=PaymentWebhookEventStatus.RECEIVED.value, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

