"""Add tenant-bound sessions, OTP challenges, and shared abuse controls.

Revision ID: 20261012_03
Revises: 20261012_02
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_03"
down_revision: Union[str, None] = "20261012_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_tenant_memberships_tenant_id_id", "tenant_memberships", ["tenant_id", "id"]
    )
    op.create_table(
        "otp_challenges",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("mobile_e164", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(mobile_e164) BETWEEN 9 AND 16 AND substr(mobile_e164, 1, 1) = '+'",
            name="ck_otp_challenges_mobile_e164_shape",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "mobile_e164", name="uq_otp_challenges_tenant_mobile"),
    )
    op.create_index(
        "ix_otp_challenges_tenant_mobile_expiry",
        "otp_challenges",
        ["tenant_id", "mobile_e164", "expires_at"],
    )

    op.create_table(
        "otp_rate_buckets",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("mobile_e164", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("action IN ('send', 'verify')", name="ck_otp_rate_buckets_action"),
        sa.CheckConstraint("attempt_count >= 1", name="ck_otp_rate_buckets_attempt_count"),
        sa.CheckConstraint(
            "length(mobile_e164) BETWEEN 9 AND 16 AND substr(mobile_e164, 1, 1) = '+'",
            name="ck_otp_rate_buckets_mobile_e164_shape",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "mobile_e164", "action", name="uq_otp_rate_buckets_scope_action"
        ),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="ck_auth_sessions_token_digest_length"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_auth_sessions_tenant_membership",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_auth_sessions_token_digest"),
    )
    op.create_index(
        "ix_auth_sessions_membership_expiry",
        "auth_sessions",
        ["tenant_id", "membership_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_membership_expiry", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("otp_rate_buckets")
    op.drop_index("ix_otp_challenges_tenant_mobile_expiry", table_name="otp_challenges")
    op.drop_table("otp_challenges")
    op.drop_constraint(
        "uq_tenant_memberships_tenant_id_id", "tenant_memberships", type_="unique"
    )
