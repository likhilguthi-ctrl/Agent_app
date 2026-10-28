"""Phase 4 Stage 4.1: Financial Foundation - Accounts, Journals, and Double-Entry Ledger.

Revision ID: 20261021_17
Revises: 20261018_16
Create Date: 2026-10-21
"""

from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa


revision: str = "20261021_17"
down_revision: Union[str, None] = "20261018_16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create commission_accounts table
    op.create_table(
        "commission_accounts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_membership_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("agent_profile_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("account_number", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "current_balance",
            sa.Numeric(precision=12, scale=2),
            server_default="0.00",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("allow_negative", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_comm_accts_tenant"),
        sa.ForeignKeyConstraint(["agent_profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="fk_comm_accts_profile"),
        sa.CheckConstraint(
            "allow_negative = TRUE OR current_balance >= 0.00",
            name="ck_comm_acct_balance",
        ),
        sa.CheckConstraint(
            "account_type IN ('AGENT_COMMISSION', 'COMMUNITY_EXPENSE', 'COMMUNITY_CLEARING', 'PAYOUT_CLEARING', 'ADJUSTMENT_CLEARING')",
            name="ck_comm_acct_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'frozen', 'closed')",
            name="ck_comm_acct_status",
        ),
        sa.CheckConstraint(
            "length(currency) = 3",
            name="ck_comm_acct_currency",
        ),
        sa.UniqueConstraint("tenant_id", "account_number", name="uq_comm_accts_number"),
    )

    if is_postgres:
        op.create_foreign_key(
            "fk_comm_accts_membership",
            "commission_accounts",
            "tenant_memberships",
            ["tenant_id", "agent_membership_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )

    op.create_index(
        "ix_comm_accts_tenant_id",
        "commission_accounts",
        ["tenant_id"],
    )
    op.create_index(
        "uq_comm_accts_agent",
        "commission_accounts",
        ["tenant_id", "agent_membership_id", "currency"],
        unique=True,
        postgresql_where=sa.text("agent_membership_id IS NOT NULL"),
        sqlite_where=sa.text("agent_membership_id IS NOT NULL"),
    )
    op.create_index(
        "uq_comm_accts_system",
        "commission_accounts",
        ["tenant_id", "account_type", "currency"],
        unique=True,
        postgresql_where=sa.text("agent_membership_id IS NULL"),
        sqlite_where=sa.text("agent_membership_id IS NULL"),
    )

    # 2. Create commission_journals table (Transaction Header)
    op.create_table(
        "commission_journals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("journal_number", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("reference_type", sa.String(length=50), nullable=False),
        sa.Column("reference_id", sa.String(length=100), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="POSTED", nullable=False),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_comm_journals_tenant"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_comm_journals_creator"),
        sa.CheckConstraint("total_amount > 0", name="ck_comm_journal_amount"),
        sa.CheckConstraint("status IN ('POSTED', 'REVERSED')", name="ck_comm_journal_status"),
        sa.CheckConstraint("length(currency) = 3", name="ck_comm_journal_currency"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_comm_journals_idempotency"),
        sa.UniqueConstraint("tenant_id", "journal_number", name="uq_comm_journals_number"),
    )

    op.create_index(
        "ix_comm_journals_tenant_posted",
        "commission_journals",
        ["tenant_id", "posted_at"],
    )

    # 3. Create commission_ledger_entries table (Line Items)
    op.create_table(
        "commission_ledger_entries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("journal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("entry_type", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_comm_ledger_tenant"),
        sa.ForeignKeyConstraint(["journal_id"], ["commission_journals.id"], ondelete="RESTRICT", name="fk_comm_ledger_journal"),
        sa.ForeignKeyConstraint(["account_id"], ["commission_accounts.id"], ondelete="RESTRICT", name="fk_comm_ledger_account"),
        sa.CheckConstraint("entry_type IN ('DEBIT', 'CREDIT')", name="ck_comm_ledger_type"),
        sa.CheckConstraint("amount > 0.00", name="ck_comm_ledger_amount"),
        sa.CheckConstraint("length(currency) = 3", name="ck_comm_ledger_currency"),
    )

    op.create_index(
        "ix_comm_ledger_acct_created",
        "commission_ledger_entries",
        ["tenant_id", "account_id", "created_at"],
    )
    op.create_index(
        "ix_comm_ledger_journal_id",
        "commission_ledger_entries",
        ["journal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_comm_ledger_journal_id", table_name="commission_ledger_entries")
    op.drop_index("ix_comm_ledger_acct_created", table_name="commission_ledger_entries")
    op.drop_table("commission_ledger_entries")

    op.drop_index("ix_comm_journals_tenant_posted", table_name="commission_journals")
    op.drop_table("commission_journals")

    op.drop_index("uq_comm_accts_system", table_name="commission_accounts")
    op.drop_index("uq_comm_accts_agent", table_name="commission_accounts")
    op.drop_index("ix_comm_accts_tenant_id", table_name="commission_accounts")
    op.drop_table("commission_accounts")
