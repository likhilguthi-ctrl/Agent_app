"""Create the shared tenant and verified tenant-domain registry.

Revision ID: 20261011_01
Revises:
Create Date: 2026-10-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261011_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_tenants_status"),
        sa.CheckConstraint("slug = lower(slug)", name="ck_tenants_slug_lowercase"),
        sa.CheckConstraint("length(slug) BETWEEN 1 AND 63", name="ck_tenants_slug_length"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_tenants_slug_normalized",
        "tenants",
        [sa.text("lower(slug)")],
        unique=True,
    )

    op.create_table(
        "tenant_domains",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column(
            "verification_status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "verification_status IN ('pending', 'verified', 'revoked')",
            name="ck_tenant_domains_verification_status",
        ),
        sa.CheckConstraint(
            "verification_status != 'verified' OR verified_at IS NOT NULL",
            name="ck_tenant_domains_verified_timestamp",
        ),
        sa.CheckConstraint(
            "hostname = lower(hostname)", name="ck_tenant_domains_hostname_lowercase"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_tenant_domains_hostname_normalized",
        "tenant_domains",
        [sa.text("lower(hostname)")],
        unique=True,
    )
    op.create_index("ix_tenant_domains_tenant_id", "tenant_domains", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_domains_tenant_id", table_name="tenant_domains")
    op.drop_index("uq_tenant_domains_hostname_normalized", table_name="tenant_domains")
    op.drop_table("tenant_domains")
    op.drop_index("uq_tenants_slug_normalized", table_name="tenants")
    op.drop_table("tenants")
