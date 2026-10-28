"""Phase 3 Implementation: Member Relationships, Agent Lifecycle Requests, and RBAC Seeding.

Revision ID: 20261018_16
Revises: 20261017_15
Create Date: 2026-10-18
"""

from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261018_16"
down_revision: Union[str, None] = "20261017_15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create member_relationships table
    op.create_table(
        "member_relationships",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_member_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("child_member_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("relationship_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("end_reason", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "parent_member_id != child_member_id",
            name="ck_member_relationships_no_self_link",
        ),
        sa.CheckConstraint(
            "relationship_type IN ('REPORTS_TO', 'REFERRED_BY', 'MANAGES')",
            name="ck_member_relationships_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_member_relationships_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_member_rel_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_member_rel_creator",
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_member_rel_ender",
        ),
    )

    # Composite foreign keys to tenant_memberships (enforcing tenant consistency at catalog level)
    if is_postgres:
        op.create_foreign_key(
            "fk_member_rel_parent",
            "member_relationships",
            "tenant_memberships",
            ["tenant_id", "parent_member_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_member_rel_child",
            "member_relationships",
            "tenant_memberships",
            ["tenant_id", "child_member_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
    else:
        # SQLite dialect fallback for single-column foreign keys
        op.create_foreign_key(
            "fk_member_rel_parent",
            "member_relationships",
            "tenant_memberships",
            ["parent_member_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_member_rel_child",
            "member_relationships",
            "tenant_memberships",
            ["child_member_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # Standard query B-Tree indexes
    op.create_index(
        "ix_member_rel_parent_active",
        "member_relationships",
        ["tenant_id", "parent_member_id", "relationship_type", "status"],
    )
    op.create_index(
        "ix_member_rel_child_active",
        "member_relationships",
        ["tenant_id", "child_member_id", "relationship_type", "status"],
    )

    # Partial unique indexes for active relationships
    if is_postgres:
        op.execute(
            """
            CREATE UNIQUE INDEX uq_member_rel_active_link
            ON member_relationships (tenant_id, parent_member_id, child_member_id, relationship_type)
            WHERE status = 'ACTIVE';
            """
        )
        op.execute(
            """
            CREATE UNIQUE INDEX uq_member_rel_single_active_parent
            ON member_relationships (tenant_id, child_member_id, relationship_type)
            WHERE status = 'ACTIVE' AND relationship_type IN ('REPORTS_TO', 'REFERRED_BY');
            """
        )
    else:
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_member_rel_active_link
            ON member_relationships (tenant_id, parent_member_id, child_member_id, relationship_type)
            WHERE status = 'ACTIVE';
            """
        )
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_member_rel_single_active_parent
            ON member_relationships (tenant_id, child_member_id, relationship_type)
            WHERE status = 'ACTIVE' AND relationship_type IN ('REPORTS_TO', 'REFERRED_BY');
            """
        )

    # 2. Create agent_lifecycle_requests table (Phase 3F Super Admin Governance)
    op.create_table(
        "agent_lifecycle_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_profile_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
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
        sa.Column("reviewed_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('SUSPEND', 'DEACTIVATE', 'REACTIVATE', 'REVOKE')",
            name="ck_alr_action",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_alr_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_alr_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            ondelete="RESTRICT",
            name="fk_alr_agent_profile",
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_alr_membership",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_alr_user",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_alr_requester",
        ),
    )

    op.create_index(
        "ix_alr_tenant_status",
        "agent_lifecycle_requests",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_alr_agent_profile",
        "agent_lifecycle_requests",
        ["agent_profile_id", "status"],
    )

    # 3. Seed Phase 3 Catalog Permissions and Role Assignments
    permissions = [
        ("member.relationship.manage", "Manage tenant member relationships."),
        ("member.stats.read", "Read permitted member statistics."),
        ("member.descendants.read", "Read permitted descendant information."),
        ("member.descendants.create", "Create an allowed descendant relationship."),
    ]
    for code, desc in permissions:
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (code, description, created_at)
                SELECT CAST(:code AS VARCHAR(100)), CAST(:desc AS VARCHAR(255)), CURRENT_TIMESTAMP
                WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code = CAST(:code AS VARCHAR(100)))
                """
            ),
            {"code": code, "desc": desc},
        )

    # Assign Phase 3 permissions to existing tenant Admin and Agent roles
    admin_perms = [
        "member.relationship.manage",
        "member.stats.read",
        "member.descendants.read",
        "member.descendants.create",
    ]
    agent_perms = [
        "member.stats.read",
        "member.descendants.read",
        "member.descendants.create",
    ]

    for p in admin_perms:
        bind.execute(
            sa.text(
                """
                INSERT INTO role_permissions (tenant_id, role_id, permission_code, status)
                SELECT r.tenant_id, r.id, CAST(:perm AS VARCHAR(100)), 'active'
                FROM roles r
                WHERE r.code = 'admin'
                  AND NOT EXISTS (
                      SELECT 1 FROM role_permissions rp
                      WHERE rp.tenant_id = r.tenant_id
                        AND rp.role_id = r.id
                        AND rp.permission_code = CAST(:perm AS VARCHAR(100))
                  )
                """
            ),
            {"perm": p},
        )

    for p in agent_perms:
        bind.execute(
            sa.text(
                """
                INSERT INTO role_permissions (tenant_id, role_id, permission_code, status)
                SELECT r.tenant_id, r.id, CAST(:perm AS VARCHAR(100)), 'active'
                FROM roles r
                WHERE r.code = 'agent'
                  AND NOT EXISTS (
                      SELECT 1 FROM role_permissions rp
                      WHERE rp.tenant_id = r.tenant_id
                        AND rp.role_id = r.id
                        AND rp.permission_code = CAST(:perm AS VARCHAR(100))
                  )
                """
            ),
            {"perm": p},
        )

    # 4. Legacy Data Backfill: Backfill existing SubscriberProfile records as REFERRED_BY relationships
    if is_postgres:
        bind.execute(
            sa.text(
                """
                INSERT INTO member_relationships (
                    id,
                    tenant_id,
                    parent_member_id,
                    child_member_id,
                    relationship_type,
                    status,
                    created_by_user_id,
                    created_at
                )
                SELECT 
                    gen_random_uuid(),
                    sp.tenant_id,
                    sp.registered_by_agent_membership_id,
                    sp.membership_id,
                    'REFERRED_BY',
                    'ACTIVE',
                    COALESCE(parent_tm.user_id, sp.user_id),
                    sp.created_at
                FROM subscriber_profiles sp
                JOIN tenant_memberships parent_tm 
                  ON parent_tm.id = sp.registered_by_agent_membership_id 
                 AND parent_tm.tenant_id = sp.tenant_id
                JOIN tenant_memberships child_tm 
                  ON child_tm.id = sp.membership_id 
                 AND child_tm.tenant_id = sp.tenant_id
                WHERE sp.registered_by_agent_membership_id != sp.membership_id
                  AND NOT EXISTS (
                      SELECT 1 FROM member_relationships mr
                      WHERE mr.tenant_id = sp.tenant_id
                        AND mr.parent_member_id = sp.registered_by_agent_membership_id
                        AND mr.child_member_id = sp.membership_id
                        AND mr.relationship_type = 'REFERRED_BY'
                        AND mr.status = 'ACTIVE'
                  );
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Drop agent_lifecycle_requests
    op.drop_index("ix_alr_agent_profile", table_name="agent_lifecycle_requests")
    op.drop_index("ix_alr_tenant_status", table_name="agent_lifecycle_requests")
    op.drop_table("agent_lifecycle_requests")

    # 2. Drop member_relationships indexes and table
    if is_postgres:
        op.execute("DROP INDEX IF EXISTS uq_member_rel_single_active_parent;")
        op.execute("DROP INDEX IF EXISTS uq_member_rel_active_link;")
    else:
        op.execute("DROP INDEX IF EXISTS uq_member_rel_single_active_parent;")
        op.execute("DROP INDEX IF EXISTS uq_member_rel_active_link;")

    op.drop_index("ix_member_rel_child_active", table_name="member_relationships")
    op.drop_index("ix_member_rel_parent_active", table_name="member_relationships")
    op.drop_table("member_relationships")

    # 3. Unseed Phase 3 permissions
    codes = [
        "member.relationship.manage",
        "member.stats.read",
        "member.descendants.read",
        "member.descendants.create",
    ]
    for c in codes:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_code = :code"),
            {"code": c},
        )
        bind.execute(
            sa.text("DELETE FROM permissions WHERE code = :code"),
            {"code": c},
        )

