"""Add local account credentials for platform operators and tenant users.

Revision ID: 20261015_10
Revises: 20261013_09
Create Date: 2026-10-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261015_10"
down_revision: Union[str, None] = "20261013_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_credentials",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("operator_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "account_type IN ('platform_operator', 'tenant_user')",
            name="ck_account_credentials_account_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_account_credentials_status",
        ),
        sa.CheckConstraint(
            "(account_type = 'platform_operator' AND operator_id IS NOT NULL AND user_id IS NULL) OR "
            "(account_type = 'tenant_user' AND user_id IS NOT NULL AND operator_id IS NULL)",
            name="ck_account_credentials_target_presence",
        ),
        sa.CheckConstraint(
            "username = lower(username)",
            name="ck_account_credentials_username_lowercase",
        ),
        sa.CheckConstraint(
            "length(username) BETWEEN 3 AND 100",
            name="ck_account_credentials_username_length",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["platform_operators.id"], ondelete="CASCADE",
            name="fk_account_credentials_operator",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_account_credentials_user",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_account_credentials_username"),
    )
    op.create_index("ix_account_credentials_username", "account_credentials", ["username"])
    op.create_index("ix_account_credentials_operator_id", "account_credentials", ["operator_id"])
    op.create_index("ix_account_credentials_user_id", "account_credentials", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_account_credentials_user_id", table_name="account_credentials")
    op.drop_index("ix_account_credentials_operator_id", table_name="account_credentials")
    op.drop_index("ix_account_credentials_username", table_name="account_credentials")
    op.drop_table("account_credentials")

