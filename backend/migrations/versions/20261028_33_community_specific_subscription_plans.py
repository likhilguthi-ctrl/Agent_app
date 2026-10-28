"""Community-specific subscription plans with community_id foreign key.

Revision ID: 20261028_33
Revises: 20261027_32
Create Date: 2026-10-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261028_33"
down_revision: Union[str, None] = "20261027_32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Add community_id column as nullable first to allow backfill
    if is_postgres:
        op.add_column("subscription_plans", sa.Column("community_id", sa.Uuid(as_uuid=True), nullable=True))
    else:
        with op.batch_alter_table("subscription_plans") as batch_op:
            batch_op.add_column(sa.Column("community_id", sa.Uuid(as_uuid=True), nullable=True))

    # 2. Backfill existing subscription plans based on referencing member_subscriptions
    # For plans referenced by subscriptions, match their tenant_id
    op.execute("""
        UPDATE subscription_plans sp
        SET community_id = subq.tenant_id
        FROM (
            SELECT plan_id, tenant_id
            FROM member_subscriptions
            GROUP BY plan_id, tenant_id
        ) subq
        WHERE sp.id = subq.plan_id AND sp.community_id IS NULL;
    """)

    # If any unreferenced plan exists without subscriptions, assign it to the first active tenant
    op.execute("""
        UPDATE subscription_plans
        SET community_id = (SELECT id FROM tenants WHERE status = 'active' ORDER BY created_at ASC LIMIT 1)
        WHERE community_id IS NULL AND EXISTS (SELECT 1 FROM tenants WHERE status = 'active');
    """)

    # 3. Enforce NOT NULL, foreign key, and indexes
    if is_postgres:
        op.alter_column("subscription_plans", "community_id", nullable=False)
        op.create_foreign_key(
            "fk_subscription_plans_community_id_tenants",
            "subscription_plans",
            "tenants",
            ["community_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index("ix_subscription_plans_community_id", "subscription_plans", ["community_id"])
        op.create_index("ix_subscription_plans_community_status", "subscription_plans", ["community_id", "status"])

        # Update unique constraint: drop global uq_subscription_plans_code, add scoped uq_subscription_plans_community_code
        try:
            op.drop_constraint("uq_subscription_plans_code", "subscription_plans", type_="unique")
        except Exception:
            pass
        op.create_unique_constraint(
            "uq_subscription_plans_community_code",
            "subscription_plans",
            ["community_id", "code"],
        )
    else:
        with op.batch_alter_table("subscription_plans") as batch_op:
            batch_op.alter_column("community_id", nullable=False)
            batch_op.create_foreign_key(
                "fk_subscription_plans_community_id_tenants",
                "tenants",
                ["community_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_index("ix_subscription_plans_community_id", ["community_id"])
            batch_op.create_index("ix_subscription_plans_community_status", ["community_id", "status"])
            try:
                batch_op.drop_constraint("uq_subscription_plans_code", type_="unique")
            except Exception:
                pass
            batch_op.create_unique_constraint("uq_subscription_plans_community_code", ["community_id", "code"])


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.drop_constraint("uq_subscription_plans_community_code", "subscription_plans", type_="unique")
        op.create_unique_constraint("uq_subscription_plans_code", "subscription_plans", ["code"])
        op.drop_index("ix_subscription_plans_community_status", table_name="subscription_plans")
        op.drop_index("ix_subscription_plans_community_id", table_name="subscription_plans")
        op.drop_constraint("fk_subscription_plans_community_id_tenants", "subscription_plans", type_="foreignkey")
        op.drop_column("subscription_plans", "community_id")
    else:
        with op.batch_alter_table("subscription_plans") as batch_op:
            batch_op.drop_constraint("uq_subscription_plans_community_code", type_="unique")
            batch_op.create_unique_constraint("uq_subscription_plans_code", ["code"])
            batch_op.drop_index("ix_subscription_plans_community_status")
            batch_op.drop_index("ix_subscription_plans_community_id")
            batch_op.drop_constraint("fk_subscription_plans_community_id_tenants", type_="foreignkey")
            batch_op.drop_column("community_id")

