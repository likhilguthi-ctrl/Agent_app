"""Registration workflow upgrade: state, district, WhatsApp, email verification, and email OTP challenges.

Revision ID: 20261026_28
Revises: 20261024_27
Create Date: 2026-10-26
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261026_28"
down_revision: Union[str, None] = "20261024_27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. admin_profiles columns
    op.add_column("admin_profiles", sa.Column("state", sa.String(100), nullable=True))
    op.add_column("admin_profiles", sa.Column("whatsapp_number", sa.String(16), nullable=True))
    op.add_column(
        "admin_profiles",
        sa.Column("whatsapp_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "admin_profiles",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("admin_profiles", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))

    # 2. agent_profiles columns
    op.add_column("agent_profiles", sa.Column("state", sa.String(100), nullable=True))
    op.add_column("agent_profiles", sa.Column("district", sa.String(100), nullable=True))
    op.add_column("agent_profiles", sa.Column("whatsapp_number", sa.String(16), nullable=True))
    op.add_column(
        "agent_profiles",
        sa.Column("whatsapp_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "agent_profiles",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("agent_profiles", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))

    # 3. subscriber_profiles columns
    op.add_column("subscriber_profiles", sa.Column("whatsapp_number", sa.String(16), nullable=True))
    op.add_column(
        "subscriber_profiles",
        sa.Column("whatsapp_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("subscriber_profiles", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))

    # 4. email_otp_challenges table
    op.create_table(
        "email_otp_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="registration"),
        sa.Column("otp_hash", sa.String(128), nullable=False, server_default=""),
        sa.Column("otp_salt", sa.String(64), nullable=False, server_default=""),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resend_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_token", sa.String(64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_otp_challenges_email_purpose",
        "email_otp_challenges",
        ["email", "purpose"],
    )
    op.create_index(
        "ix_email_otp_challenges_verification_token",
        "email_otp_challenges",
        ["verification_token"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_otp_challenges_verification_token", table_name="email_otp_challenges")
    op.drop_index("ix_email_otp_challenges_email_purpose", table_name="email_otp_challenges")
    op.drop_table("email_otp_challenges")

    op.drop_column("subscriber_profiles", "email_verified_at")
    op.drop_column("subscriber_profiles", "email_verified")
    op.drop_column("subscriber_profiles", "whatsapp_verified")
    op.drop_column("subscriber_profiles", "whatsapp_number")

    op.drop_column("agent_profiles", "email_verified_at")
    op.drop_column("agent_profiles", "email_verified")
    op.drop_column("agent_profiles", "whatsapp_verified")
    op.drop_column("agent_profiles", "whatsapp_number")
    op.drop_column("agent_profiles", "district")
    op.drop_column("agent_profiles", "state")

    op.drop_column("admin_profiles", "email_verified_at")
    op.drop_column("admin_profiles", "email_verified")
    op.drop_column("admin_profiles", "whatsapp_verified")
    op.drop_column("admin_profiles", "whatsapp_number")
    op.drop_column("admin_profiles", "state")

