"""Phase 6: Mobile OTP Authentication, Registration Verification, and Regional Assignments.

Revision ID: 20261016_12
Revises: 20261016_11
Create Date: 2026-10-16
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261016_12"
down_revision: Union[str, None] = "20261016_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update otp_challenges table
    # Make tenant_id nullable for platform operators and pre-tenant registration challenges
    op.alter_column("otp_challenges", "tenant_id", existing_type=sa.Uuid(as_uuid=True), nullable=True)

    # Add purpose, otp_hash, otp_salt, failed_attempts, resend_count, verified_at, verification_token, metadata_json
    op.add_column("otp_challenges", sa.Column("purpose", sa.String(length=32), nullable=False, server_default="login"))
    op.add_column("otp_challenges", sa.Column("otp_hash", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("otp_challenges", sa.Column("otp_salt", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("otp_challenges", sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("otp_challenges", sa.Column("resend_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("otp_challenges", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("otp_challenges", sa.Column("verification_token", sa.String(length=64), nullable=True))
    op.add_column("otp_challenges", sa.Column("metadata_json", sa.JSON(), nullable=True))

    # Drop old unique constraint on (tenant_id, mobile_e164) if it exists
    try:
        op.drop_constraint("uq_otp_challenges_tenant_mobile", "otp_challenges", type_="unique")
    except Exception:
        pass

    # Create new indexes for OTP lookups
    op.create_index("ix_otp_challenges_mobile_purpose", "otp_challenges", ["mobile_e164", "purpose"])
    op.create_index("ix_otp_challenges_verification_token", "otp_challenges", ["verification_token"])

    # 2. Add mobile_e164 to platform_operators for Super Admin mobile OTP login
    op.add_column("platform_operators", sa.Column("mobile_e164", sa.String(length=16), nullable=True))
    op.create_unique_constraint("uq_platform_operators_mobile", "platform_operators", ["mobile_e164"])

    # Seed demo_superadmin with mobile +15550000001
    op.execute(
        "UPDATE platform_operators SET mobile_e164 = '+15550000001' WHERE subject = 'operator:demo_superadmin'"
    )

    # 3. Create regions table
    op.create_table(
        "regions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "code", name="uq_regions_tenant_code"),
    )
    op.create_index("ix_regions_tenant_status", "regions", ["tenant_id", "status"])

    # 4. Create user_region_assignments table
    op.create_table(
        "user_region_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("region_id", sa.Uuid(as_uuid=True), sa.ForeignKey("regions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("assigned_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "user_id", "region_id", name="uq_user_region_tenant_user_region"),
    )
    op.create_index("ix_user_region_assignments_lookup", "user_region_assignments", ["tenant_id", "user_id", "status"])

    # 5. Create region_audit_logs table
    op.create_table(
        "region_audit_logs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("region_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_region_audit_logs_tenant", "region_audit_logs", ["tenant_id", "user_id"])

    # 6. Create admin_profiles table for shared Admin/Agent identity details
    op.create_table(
        "admin_profiles",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("profile_photo_key", sa.String(length=64), nullable=True),
        sa.Column("aadhaar_document_key", sa.String(length=64), nullable=True),
        sa.Column("pan_document_key", sa.String(length=64), nullable=True),
        sa.Column("aadhaar_number_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("pan_number_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_profiles_tenant", "admin_profiles", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("admin_profiles")
    op.drop_table("region_audit_logs")
    op.drop_table("user_region_assignments")
    op.drop_table("regions")
    op.drop_constraint("uq_platform_operators_mobile", "platform_operators", type_="unique")
    op.drop_column("platform_operators", "mobile_e164")
    op.drop_index("ix_otp_challenges_verification_token", table_name="otp_challenges")
    op.drop_index("ix_otp_challenges_mobile_purpose", table_name="otp_challenges")
    op.drop_column("otp_challenges", "metadata_json")
    op.drop_column("otp_challenges", "verification_token")
    op.drop_column("otp_challenges", "verified_at")
    op.drop_column("otp_challenges", "resend_count")
    op.drop_column("otp_challenges", "failed_attempts")
    op.drop_column("otp_challenges", "otp_salt")
    op.drop_column("otp_challenges", "otp_hash")
    op.drop_column("otp_challenges", "purpose")

