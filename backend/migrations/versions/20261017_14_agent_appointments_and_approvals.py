"""Phase 2: Agent appointments and approval decisions.

Revision ID: 20261017_14
Revises: 20261017_13
Create Date: 2026-10-17
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261017_14"
down_revision: Union[str, None] = "20261017_13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_appointments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("agent_profile_id", sa.Uuid(as_uuid=True), sa.ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), sa.ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("proposed_commission_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', 'EXPIRED')",
            name="ck_agent_appointments_status",
        ),
    )
    op.create_index(
        "ix_agent_appointments_tenant_status",
        "agent_appointments",
        ["tenant_id", "status"],
    )
    op.create_index("ix_agent_appointments_user", "agent_appointments", ["user_id"])
    op.create_index(
        "ix_agent_appointments_profile", "agent_appointments", ["agent_profile_id"]
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("appointment_id", sa.Uuid(as_uuid=True), sa.ForeignKey("agent_appointments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_approval_decisions_decision",
        ),
    )
    op.create_index(
        "ix_approval_decisions_tenant_id", "approval_decisions", ["tenant_id"]
    )
    op.create_index(
        "ix_approval_decisions_appointment_id", "approval_decisions", ["appointment_id"]
    )
    op.create_index(
        "ix_approval_decisions_decided_by",
        "approval_decisions",
        ["decided_by_user_id"],
    )


def downgrade() -> None:
    op.drop_table("approval_decisions")
    op.drop_table("agent_appointments")
