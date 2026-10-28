"""Phase 4 Stage 4.4: Agent Payout Governance, Workflow & Ledger Integration.

Revision ID: 20261021_20
Revises: 20261021_19
Create Date: 2026-10-21
"""

from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa


revision: str = "20261021_20"
down_revision: Union[str, None] = "20261021_19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create payout_requests table
    op.create_table(
        "payout_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_profile_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING_REVIEW", nullable=False),
        sa.Column("payout_hold_journal_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("disbursement_journal_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("reversal_journal_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("bank_account_snapshot", sa.JSON(), nullable=True),
        sa.Column("request_note", sa.String(length=255), nullable=True),
        sa.Column("rejection_reason", sa.String(length=255), nullable=True),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("reviewed_by_membership_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.String(length=255), nullable=True),
        sa.Column("disbursed_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("disbursed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disbursement_reference", sa.String(length=128), nullable=True),
        sa.Column("disbursement_method", sa.String(length=32), nullable=True),
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
        sa.CheckConstraint("amount > 0.00", name="ck_payout_requests_amount"),
        sa.CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED', 'PROCESSING', 'PAID', 'FAILED', 'CANCELLED')",
            name="ck_payout_requests_status",
        ),
        sa.CheckConstraint("length(currency) = 3", name="ck_payout_requests_currency"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_payout_requests_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_membership_id"],
            ["tenant_memberships.id"],
            name="fk_payout_requests_agent_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            name="fk_payout_requests_agent_profile",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["commission_accounts.id"],
            name="fk_payout_requests_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_payout_requests_created_by_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_membership_id"],
            ["tenant_memberships.id"],
            name="fk_payout_requests_reviewed_by_membership",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["disbursed_by_user_id"],
            ["users.id"],
            name="fk_payout_requests_disbursed_by_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["payout_hold_journal_id"],
            ["commission_journals.id"],
            name="fk_payout_requests_hold_journal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["disbursement_journal_id"],
            ["commission_journals.id"],
            name="fk_payout_requests_disburse_journal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_journal_id"],
            ["commission_journals.id"],
            name="fk_payout_requests_reversal_journal",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_payout_requests_idempotency"),
    )

    op.create_index(
        "ix_payout_requests_tenant_status",
        "payout_requests",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_payout_requests_agent",
        "payout_requests",
        ["tenant_id", "agent_membership_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_payout_requests_agent", table_name="payout_requests")
    op.drop_index("ix_payout_requests_tenant_status", table_name="payout_requests")
    op.drop_table("payout_requests")

