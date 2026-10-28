"""Phase 4 Stage 4.3: Razorpay Payments, Verified Subscription Settlement & Webhooks.

Revision ID: 20261021_19
Revises: 20261021_18
Create Date: 2026-10-21
"""

from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa


revision: str = "20261021_19"
down_revision: Union[str, None] = "20261021_18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create payment_orders table
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscriber_membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("gateway", sa.String(length=32), server_default="razorpay", nullable=False),
        sa.Column("gateway_order_id", sa.String(length=128), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="created", nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("created_by_membership_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_payment_orders_tenant"),
        sa.ForeignKeyConstraint(["subscription_id"], ["member_subscriptions.id"], ondelete="RESTRICT", name="fk_payment_orders_sub"),
        sa.ForeignKeyConstraint(["subscriber_membership_id"], ["tenant_memberships.id"], ondelete="RESTRICT", name="fk_payment_orders_subscriber"),
        sa.CheckConstraint("amount_paise > 0", name="ck_payment_orders_amount_paise"),
        sa.CheckConstraint("amount > 0.00", name="ck_payment_orders_amount"),
        sa.CheckConstraint("length(currency) = 3", name="ck_payment_orders_currency"),
        sa.CheckConstraint("status IN ('created', 'attempted', 'paid', 'failed', 'expired')", name="ck_payment_orders_status"),
        sa.UniqueConstraint("gateway", "gateway_order_id", name="uq_payment_orders_gateway_order"),
    )

    if is_postgres:
        op.create_foreign_key(
            "fk_payment_orders_tenant_sub",
            "payment_orders",
            "member_subscriptions",
            ["tenant_id", "subscription_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )

    op.create_index(
        "ix_payment_orders_tenant_sub",
        "payment_orders",
        ["tenant_id", "subscription_id"],
    )
    op.create_index(
        "ix_payment_orders_tenant_idempotency",
        "payment_orders",
        ["tenant_id", "idempotency_key"],
    )
    op.create_index(
        "ix_payment_orders_gateway_order",
        "payment_orders",
        ["gateway", "gateway_order_id"],
    )

    # 2. Create payment_transactions table
    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("payment_order_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("gateway", sa.String(length=32), server_default="razorpay", nullable=False),
        sa.Column("gateway_payment_id", sa.String(length=128), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="captured", nullable=False),
        sa.Column("gateway_signature", sa.String(length=256), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_description", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_payment_tx_tenant"),
        sa.ForeignKeyConstraint(["payment_order_id"], ["payment_orders.id"], ondelete="RESTRICT", name="fk_payment_tx_order"),
        sa.ForeignKeyConstraint(["subscription_id"], ["member_subscriptions.id"], ondelete="RESTRICT", name="fk_payment_tx_sub"),
        sa.CheckConstraint("amount_paise > 0", name="ck_payment_tx_amount_paise"),
        sa.CheckConstraint("amount > 0.00", name="ck_payment_tx_amount"),
        sa.CheckConstraint("length(currency) = 3", name="ck_payment_tx_currency"),
        sa.CheckConstraint("status IN ('captured', 'authorized', 'failed', 'refunded')", name="ck_payment_tx_status"),
        sa.UniqueConstraint("gateway", "gateway_payment_id", name="uq_payment_tx_gateway_payment"),
    )

    op.create_index(
        "ix_payment_tx_order",
        "payment_transactions",
        ["payment_order_id"],
    )
    op.create_index(
        "ix_payment_tx_sub",
        "payment_transactions",
        ["subscription_id"],
    )
    op.create_index(
        "ix_payment_tx_tenant_status",
        "payment_transactions",
        ["tenant_id", "status"],
    )

    # 3. Create payment_webhook_events table
    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("gateway", sa.String(length=32), server_default="razorpay", nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="received", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('received', 'processed', 'ignored', 'failed')", name="ck_payment_webhook_status"),
        sa.UniqueConstraint("gateway", "event_id", name="uq_payment_webhook_event"),
    )

    op.create_index(
        "ix_payment_webhook_status",
        "payment_webhook_events",
        ["gateway", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_payment_webhook_status", table_name="payment_webhook_events")
    op.drop_table("payment_webhook_events")

    op.drop_index("ix_payment_tx_tenant_status", table_name="payment_transactions")
    op.drop_index("ix_payment_tx_sub", table_name="payment_transactions")
    op.drop_index("ix_payment_tx_order", table_name="payment_transactions")
    op.drop_table("payment_transactions")

    op.drop_index("ix_payment_orders_gateway_order", table_name="payment_orders")
    op.drop_index("ix_payment_orders_tenant_idempotency", table_name="payment_orders")
    op.drop_index("ix_payment_orders_tenant_sub", table_name="payment_orders")
    op.drop_table("payment_orders")

