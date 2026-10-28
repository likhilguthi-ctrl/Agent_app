"""Phase 5 schema: Agent profiles, subscribers, R2 stored objects, photo policy, and commission.

Revision ID: 20261016_11
Revises: 20261015_10
Create Date: 2026-10-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261016_11"
down_revision: Union[str, None] = "20261015_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update tenants table with photo_requirement, default_commission_rate, policy_version
    op.add_column(
        "tenants",
        sa.Column(
            "photo_requirement",
            sa.String(length=16),
            server_default="OPTIONAL",
            nullable=False,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "default_commission_rate",
            sa.Numeric(precision=5, scale=2),
            server_default="10.00",
            nullable=False,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "policy_version",
            sa.String(length=16),
            server_default="v1",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_tenants_photo_requirement",
        "tenants",
        "photo_requirement IN ('OPTIONAL', 'REQUIRED')",
    )
    op.create_check_constraint(
        "ck_tenants_default_commission_rate",
        "tenants",
        "default_commission_rate >= 0 AND default_commission_rate <= 100",
    )

    # 2. Commission audit logs
    op.create_table(
        "commission_audit_logs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_profile_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("changed_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("changed_by_username", sa.String(length=100), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("previous_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("new_rate", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_commission_audit_logs_tenant"
        ),
    )

    # 3. Stored objects table for Cloudflare R2 metadata
    op.create_table(
        "stored_objects",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("object_key", sa.String(length=64), nullable=False),
        sa.Column("bucket_name", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("uploader_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IN ('agent-photo', 'aadhaar-document', 'pan-document', 'user-photo')",
            name="ck_stored_objects_category",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_stored_objects_tenant"
        ),
        sa.UniqueConstraint("object_key", name="uq_stored_objects_key"),
    )
    op.create_index("ix_stored_objects_object_key", "stored_objects", ["object_key"])
    op.create_index("ix_stored_objects_tenant_id", "stored_objects", ["tenant_id"])

    # 4. Agent profiles table
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("ref_id", sa.String(length=16), nullable=False),
        sa.Column("commission_rate_override", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("profile_photo_key", sa.String(length=64), nullable=False),
        sa.Column("aadhaar_number_ciphertext", sa.Text(), nullable=False),
        sa.Column("aadhaar_number_masked", sa.String(length=16), nullable=False),
        sa.Column("aadhaar_document_key", sa.String(length=64), nullable=False),
        sa.Column("pan_number_ciphertext", sa.Text(), nullable=False),
        sa.Column("pan_number_masked", sa.String(length=16), nullable=False),
        sa.Column("pan_document_key", sa.String(length=64), nullable=False),
        sa.Column("created_by_membership_id", sa.Uuid(as_uuid=True), nullable=True),
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
        sa.CheckConstraint(
            "commission_rate_override IS NULL OR (commission_rate_override >= 0 AND commission_rate_override <= 100)",
            name="ck_agent_profiles_commission_override",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_agent_profiles_tenant"
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_agent_profiles_membership",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_agent_profiles_user"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_membership_id"],
            ["tenant_memberships.id"],
            ondelete="SET NULL",
            name="fk_agent_profiles_creator",
        ),
        sa.UniqueConstraint("membership_id", name="uq_agent_profiles_membership"),
        sa.UniqueConstraint("user_id", name="uq_agent_profiles_user"),
        sa.UniqueConstraint("ref_id", name="uq_agent_profiles_ref_id"),
    )
    op.create_index("ix_agent_profiles_tenant_id", "agent_profiles", ["tenant_id"])
    op.create_index("ix_agent_profiles_ref_id", "agent_profiles", ["ref_id"])

    # 5. Subscriber profiles table
    op.create_table(
        "subscriber_profiles",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("registered_by_agent_membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("photo_key", sa.String(length=64), nullable=True),
        sa.Column("package_plan", sa.String(length=50), nullable=True),
        sa.Column("billing_cycle", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_subscriber_profiles_tenant"
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_subscriber_profiles_membership",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_subscriber_profiles_user"
        ),
        sa.ForeignKeyConstraint(
            ["registered_by_agent_membership_id"],
            ["tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_subscriber_profiles_agent",
        ),
        sa.UniqueConstraint("membership_id", name="uq_subscriber_profiles_membership"),
        sa.UniqueConstraint("user_id", name="uq_subscriber_profiles_user"),
    )
    op.create_index("ix_subscriber_profiles_tenant_id", "subscriber_profiles", ["tenant_id"])
    op.create_index(
        "ix_subscriber_profiles_agent_membership_id",
        "subscriber_profiles",
        ["registered_by_agent_membership_id"],
    )


def downgrade() -> None:
    op.drop_table("subscriber_profiles")
    op.drop_table("agent_profiles")
    op.drop_table("stored_objects")
    op.drop_table("commission_audit_logs")
    op.drop_constraint("ck_tenants_default_commission_rate", "tenants", type_="check")
    op.drop_constraint("ck_tenants_photo_requirement", "tenants", type_="check")
    op.drop_column("tenants", "policy_version")
    op.drop_column("tenants", "default_commission_rate")
    op.drop_column("tenants", "photo_requirement")

