"""Create community identities and tenant-scoped mobile memberships.

Revision ID: 20261012_02
Revises: 20261011_01
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_02"
down_revision: Union[str, None] = "20261011_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("mobile_e164", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_tenant_memberships_status"),
        sa.CheckConstraint(
            "length(mobile_e164) BETWEEN 9 AND 16 AND substr(mobile_e164, 1, 1) = '+'",
            name="ck_tenant_memberships_mobile_e164_shape",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "mobile_e164", name="uq_tenant_memberships_tenant_mobile"),
        sa.UniqueConstraint("user_id", name="uq_tenant_memberships_user_identity"),
    )
    op.create_index(
        "ix_tenant_memberships_tenant_status",
        "tenant_memberships",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_memberships_tenant_status", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")
    op.drop_table("users")
