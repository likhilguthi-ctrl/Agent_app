"""Add tenant-scoped RBAC records and the reviewed permission catalog.

Revision ID: 20261012_04
Revises: 20261012_03
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_04"
down_revision: Union[str, None] = "20261012_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Keep this seed immutable with this revision. The catalog has no role grants.
PERMISSION_SEED = (
    ("agent.create", "Submit an agent appointment."),
    ("agent.read", "Read agents within applicable tenant and scope policies."),
    ("agent.update", "Update permitted agent profile fields."),
    ("agent.cancel", "Cancel a pending appointment subject to policy."),
    ("agent.approve", "Approve an eligible appointment."),
    ("agent.reject", "Reject an eligible appointment."),
    ("agent.suspend", "Suspend an active agent assignment."),
    ("agent.revoke", "Revoke an agent assignment."),
    ("role.create", "Create a tenant role."),
    ("role.read", "Read tenant roles."),
    ("role.update", "Update a tenant role."),
    ("role.delete", "Delete a tenant role when policy permits."),
    ("role.assign", "Assign an authorized tenant role to a membership."),
    ("role.revoke", "Revoke a tenant role assignment."),
    ("permission.read", "Read the platform permission catalog."),
    ("role_permission.assign", "Assign an authorized permission to a tenant role."),
    ("role_permission.revoke", "Revoke a permission from a tenant role."),
    ("region.create", "Create a tenant region."),
    ("region.read", "Read tenant regions."),
    ("region.update", "Update a tenant region."),
    ("region.manage", "Manage tenant regions within applicable scope policy."),
    ("region.assign", "Assign a membership to a tenant region."),
    ("member.create", "Create a tenant member."),
    ("member.read", "Read members within applicable tenant and scope policies."),
    ("member.update", "Update permitted member fields."),
    ("member.deactivate", "Deactivate a tenant member."),
    ("member.relationship.manage", "Manage tenant member relationships."),
    ("member.stats.read", "Read permitted member statistics."),
    ("member.descendants.read", "Read permitted descendant information."),
    ("member.descendants.create", "Create an allowed descendant relationship."),
    ("approval.read", "Read approval requests within applicable scope policies."),
    ("audit.read", "Read permitted tenant audit records."),
    ("notification.read", "Read tenant notifications."),
    ("notification.manage", "Manage tenant notifications."),
    ("commission.read", "Read financial commission accounts and ledger statements."),
    ("commission.manage", "Manage financial accounts and post ledger adjustments."),
    ("subscription.read", "Read subscriptions and available subscription plans."),
    ("subscription.manage", "Create and manage subscriber subscriptions within community context."),
    ("payment.read", "Read payment transactions and gateway payment orders."),
    ("payment.manage", "Initiate and verify subscription payment transactions."),
    ("payout.request", "Submit a payout request to withdraw earned commissions."),
    ("payout.review", "Review and approve or reject community agent payout requests."),
    ("payout.read", "Read payout requests and statement history."),
    ("payout.disburse", "Authorize and execute fund disbursement for approved payouts."),
)


def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("code = lower(code)", name="ck_permissions_code_lowercase"),
        sa.CheckConstraint("length(code) BETWEEN 3 AND 100", name="ck_permissions_code_length"),
        sa.PrimaryKeyConstraint("code"),
    )
    permission_table = sa.table(
        "permissions",
        sa.column("code", sa.String(length=100)),
        sa.column("description", sa.String(length=255)),
    )
    op.bulk_insert(
        permission_table,
        [{"code": code, "description": description} for code, description in PERMISSION_SEED],
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_roles_status"),
        sa.CheckConstraint("code = lower(code)", name="ck_roles_code_lowercase"),
        sa.CheckConstraint("length(code) BETWEEN 1 AND 100", name="ck_roles_code_length"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_roles_tenant_id_id"),
    )
    op.create_index("ix_roles_tenant_status", "roles", ["tenant_id", "status"])

    op.create_table(
        "role_permissions",
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("permission_code", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("is_delegable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')", name="ck_role_permissions_status"
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'inactive') AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_role_permissions_revocation_state",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"], ["roles.tenant_id", "roles.id"],
            ondelete="RESTRICT", name="fk_role_permissions_tenant_role",
        ),
        sa.ForeignKeyConstraint(
            ["permission_code"], ["permissions.code"],
            ondelete="RESTRICT", name="fk_role_permissions_permission",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "role_id", "permission_code"),
    )
    op.create_index(
        "ix_role_permissions_tenant_role_status",
        "role_permissions",
        ["tenant_id", "role_id", "status"],
    )

    op.create_table(
        "user_role_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("assigned_by_membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')", name="ck_user_role_assignments_status"
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_user_role_assignments_validity_window",
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status != 'revoked' AND revoked_at IS NULL)",
            name="ck_user_role_assignments_revocation_state",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            ondelete="RESTRICT", name="fk_user_role_assignments_tenant_membership",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"], ["roles.tenant_id", "roles.id"],
            ondelete="RESTRICT", name="fk_user_role_assignments_tenant_role",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "assigned_by_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            ondelete="RESTRICT", name="fk_user_role_assignments_assigner_membership",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_role_assignments_tenant_membership_status",
        "user_role_assignments",
        ["tenant_id", "membership_id", "status"],
    )
    op.create_index(
        "uq_user_role_assignments_active",
        "user_role_assignments",
        ["tenant_id", "membership_id", "role_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_user_role_assignments_active", table_name="user_role_assignments")
    op.drop_index(
        "ix_user_role_assignments_tenant_membership_status", table_name="user_role_assignments"
    )
    op.drop_table("user_role_assignments")
    op.drop_index("ix_role_permissions_tenant_role_status", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_index("ix_roles_tenant_status", table_name="roles")
    op.drop_table("roles")
    op.drop_table("permissions")
