"""Create target management schema and seed initial target types.

Revision ID: 20261024_26
Revises: 20261024_25
Create Date: 2026-10-24
"""

from datetime import UTC, datetime
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20261024_26"
down_revision: Union[str, None] = "20261024_25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. target_types table
    op.create_table(
        "target_types",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid4),
        sa.Column("code", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("calculation_type", sa.String(length=32), nullable=False),
        sa.Column("applicable_role", sa.String(length=32), nullable=False, server_default="BOTH"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "calculation_type IN ('AUTOMATIC', 'MANUAL')",
            name="ck_target_types_calculation_type",
        ),
        sa.CheckConstraint(
            "applicable_role IN ('ADMIN', 'AGENT', 'BOTH')",
            name="ck_target_types_applicable_role",
        ),
    )
    op.create_index("ix_target_types_code", "target_types", ["code"])

    # 2. target_assignments table
    op.create_table(
        "target_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid4),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("parent_target_id", sa.Uuid(as_uuid=True), sa.ForeignKey("target_assignments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_type_id", sa.Uuid(as_uuid=True), sa.ForeignKey("target_types.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assigned_to_user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_to_membership_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("assigned_role", sa.String(length=32), nullable=False),
        sa.Column("target_quantity", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column("period", sa.String(length=32), nullable=False, server_default="MONTHLY"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("target_quantity > 0", name="ck_target_assignments_target_quantity_pos"),
        sa.CheckConstraint("start_date <= deadline", name="ck_target_assignments_dates_valid"),
        sa.CheckConstraint("assigned_role IN ('ADMIN', 'AGENT')", name="ck_target_assignments_assigned_role"),
        sa.CheckConstraint("period IN ('WEEKLY', 'MONTHLY', 'QUARTERLY', 'ANNUAL', 'CUSTOM')", name="ck_target_assignments_period"),
        sa.CheckConstraint("status IN ('ACTIVE', 'COMPLETED', 'CANCELLED', 'OVERDUE')", name="ck_target_assignments_status"),
    )
    op.create_index("ix_target_assignments_tenant_assigned_role", "target_assignments", ["tenant_id", "assigned_role"])
    op.create_index("ix_target_assignments_assignee", "target_assignments", ["assigned_to_user_id"])
    op.create_index("ix_target_assignments_parent", "target_assignments", ["parent_target_id"])
    op.create_index("ix_target_assignments_period_dates", "target_assignments", ["start_date", "deadline"])

    # 3. target_achievement_adjustments table
    op.create_table(
        "target_achievement_adjustments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid4),
        sa.Column("target_id", sa.Uuid(as_uuid=True), sa.ForeignKey("target_assignments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("adjusted_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("adjustment_quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_target_adjustments_target", "target_achievement_adjustments", ["target_id"])

    # 4. Seed initial candidate target types
    bind = op.get_bind()
    now = datetime.now(UTC)
    target_types_table = sa.table(
        "target_types",
        sa.column("id", sa.Uuid),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("unit", sa.String),
        sa.column("calculation_type", sa.String),
        sa.column("applicable_role", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    seed_data = [
        {
            "id": uuid4(),
            "code": "AGENTS_RECRUITED",
            "name": "Agents Recruited",
            "description": "Number of community agents successfully recruited, appointed, and approved.",
            "unit": "agents",
            "calculation_type": "AUTOMATIC",
            "applicable_role": "ADMIN",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": uuid4(),
            "code": "USERS_REGISTERED",
            "name": "Users Registered",
            "description": "Number of community OTT subscribers registered and activated.",
            "unit": "users",
            "calculation_type": "AUTOMATIC",
            "applicable_role": "BOTH",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": uuid4(),
            "code": "CONTENT_UPLOADED",
            "name": "Content Uploaded",
            "description": "Number of local and community content videos uploaded and published.",
            "unit": "videos",
            "calculation_type": "MANUAL",
            "applicable_role": "BOTH",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": uuid4(),
            "code": "SUBSCRIPTIONS_GENERATED",
            "name": "Subscriptions Generated",
            "description": "Number of active, paid subscription plans initiated and settled.",
            "unit": "subscriptions",
            "calculation_type": "AUTOMATIC",
            "applicable_role": "BOTH",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    ]

    bind.execute(target_types_table.insert().values(seed_data))


def downgrade() -> None:
    op.drop_table("target_achievement_adjustments")
    op.drop_table("target_assignments")
    op.drop_table("target_types")
