"""Create super_admin_notifications table for in-app approval requests.

Revision ID: 20261024_23
Revises: 20261023_22
Create Date: 2026-10-24
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261024_23"
down_revision: Union[str, None] = "20261023_22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "super_admin_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_operator_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False, server_default="AGENT_APPOINTMENT_PENDING"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("appointment_id", sa.Uuid(), nullable=True),
        sa.Column("agent_profile_id", sa.Uuid(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "resolution_status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_super_admin_notifications_resolution_status",
        ),
        sa.ForeignKeyConstraint(["recipient_operator_id"], ["platform_operators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["appointment_id"], ["agent_appointments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_profile_id"], ["agent_profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recipient_operator_id",
            "appointment_id",
            "event_type",
            name="uq_super_admin_notifications_dedup",
        ),
    )
    op.create_index(
        "ix_super_admin_notifications_recipient_read",
        "super_admin_notifications",
        ["recipient_operator_id", "is_read"],
    )
    op.create_index(
        "ix_super_admin_notifications_appointment",
        "super_admin_notifications",
        ["appointment_id"],
    )
    op.create_index(
        "ix_super_admin_notifications_created",
        "super_admin_notifications",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_super_admin_notifications_created", table_name="super_admin_notifications")
    op.drop_index("ix_super_admin_notifications_appointment", table_name="super_admin_notifications")
    op.drop_index("ix_super_admin_notifications_recipient_read", table_name="super_admin_notifications")
    op.drop_table("super_admin_notifications")

