"""Phase 4 Stage 4.2: Subscription & Package Billing Foundation.

Revision ID: 20261021_18
Revises: 20261021_17
Create Date: 2026-10-21
"""

from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa


revision: str = "20261021_18"
down_revision: Union[str, None] = "20261021_17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create subscription_plans table (Platform-wide catalog managed by Super Admin)
    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("benefits", sa.JSON(), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("nominal_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
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
        sa.CheckConstraint("duration_days > 0", name="ck_sub_plan_duration"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_sub_plan_status"),
        sa.CheckConstraint("nominal_price IS NULL OR nominal_price >= 0.00", name="ck_sub_plan_price"),
        sa.CheckConstraint("length(currency) = 3", name="ck_sub_plan_currency"),
        sa.UniqueConstraint("code", name="uq_subscription_plans_code"),
    )

    op.create_index(
        "ix_subscription_plans_status",
        "subscription_plans",
        ["status"],
    )

    # 2. Create member_subscriptions table (Tenant-scoped subscriber subscriptions)
    op.create_table(
        "member_subscriptions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscriber_membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("plan_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agreed_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING_PAYMENT", nullable=False),
        sa.Column("payment_status", sa.String(length=20), server_default="UNPAID", nullable=False),
        sa.Column("snapshot_plan_name", sa.String(length=100), nullable=False),
        sa.Column("snapshot_duration_days", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_mem_subs_tenant"),
        sa.ForeignKeyConstraint(["subscriber_membership_id"], ["tenant_memberships.id"], ondelete="RESTRICT", name="fk_mem_subs_member"),
        sa.ForeignKeyConstraint(["plan_id"], ["subscription_plans.id"], ondelete="RESTRICT", name="fk_mem_subs_plan"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_mem_subs_creator"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_member_subscriptions_tenant_id_id"),
        sa.CheckConstraint("agreed_amount >= 0.00", name="ck_mem_sub_amount"),
        sa.CheckConstraint("length(currency) = 3", name="ck_mem_sub_currency"),
        sa.CheckConstraint("status IN ('PENDING_PAYMENT', 'ACTIVE', 'EXPIRED', 'CANCELLED')", name="ck_mem_sub_status"),
        sa.CheckConstraint("payment_status IN ('UNPAID', 'PENDING', 'SETTLED', 'FAILED', 'REFUNDED')", name="ck_mem_sub_payment"),
        sa.CheckConstraint("expires_at > starts_at", name="ck_mem_sub_dates"),
        sa.CheckConstraint("snapshot_duration_days > 0", name="ck_mem_sub_duration"),
    )

    if is_postgres:
        op.create_foreign_key(
            "fk_mem_subs_membership",
            "member_subscriptions",
            "tenant_memberships",
            ["tenant_id", "subscriber_membership_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )

    op.create_index(
        "ix_member_subscriptions_tenant_id",
        "member_subscriptions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_member_subscriptions_subscriber",
        "member_subscriptions",
        ["tenant_id", "subscriber_membership_id"],
    )
    op.create_index(
        "ix_member_subscriptions_plan_id",
        "member_subscriptions",
        ["plan_id"],
    )
    op.create_index(
        "ix_member_subscriptions_status",
        "member_subscriptions",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_member_subscriptions_status", table_name="member_subscriptions")
    op.drop_index("ix_member_subscriptions_plan_id", table_name="member_subscriptions")
    op.drop_index("ix_member_subscriptions_subscriber", table_name="member_subscriptions")
    op.drop_index("ix_member_subscriptions_tenant_id", table_name="member_subscriptions")
    op.drop_table("member_subscriptions")

    op.drop_index("ix_subscription_plans_status", table_name="subscription_plans")
    op.drop_table("subscription_plans")
