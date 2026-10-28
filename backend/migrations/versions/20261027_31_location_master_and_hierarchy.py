"""Location master schema, manager location assignments, and agent hierarchy approval workflow.

Revision ID: 20261027_31
Revises: 20261027_30
Create Date: 2026-10-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261027_31"
down_revision: Union[str, None] = "20261027_30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. States table
    op.create_table(
        "states",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_states_name"),
        sa.UniqueConstraint("code", name="uq_states_code"),
    )
    op.create_index("ix_states_name", "states", ["name"])
    op.create_index("ix_states_code", "states", ["code"])

    # 2. Districts table
    op.create_table(
        "districts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("state_id", sa.Uuid(as_uuid=True), sa.ForeignKey("states.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("district_code", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("district_code", name="uq_districts_district_code"),
        sa.UniqueConstraint("state_id", "name", name="uq_districts_state_name"),
    )
    op.create_index("ix_districts_state_id", "districts", ["state_id"])
    op.create_index("ix_districts_district_code", "districts", ["district_code"])
    op.create_index("ix_districts_state_active", "districts", ["state_id", "is_active"])

    # 3. Mandals table
    op.create_table(
        "mandals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("district_id", sa.Uuid(as_uuid=True), sa.ForeignKey("districts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("mandal_code", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("mandal_code", name="uq_mandals_mandal_code"),
        sa.UniqueConstraint("district_id", "name", name="uq_mandals_district_name"),
    )
    op.create_index("ix_mandals_district_id", "mandals", ["district_id"])
    op.create_index("ix_mandals_mandal_code", "mandals", ["mandal_code"])
    op.create_index("ix_mandals_district_active", "mandals", ["district_id", "is_active"])

    # 4. Villages table
    op.create_table(
        "villages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("mandal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("mandals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("native_name", sa.String(200), nullable=True),
        sa.Column("village_code", sa.String(50), nullable=False),
        sa.Column("pincode", sa.String(16), nullable=True),
        sa.Column("native_source", sa.String(50), nullable=True),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("village_code", name="uq_villages_village_code"),
    )
    op.create_index("ix_villages_mandal_id", "villages", ["mandal_id"])
    op.create_index("ix_villages_village_code", "villages", ["village_code"])
    op.create_index("ix_villages_pincode", "villages", ["pincode"])

    # 5. Manager location assignments
    op.create_table(
        "manager_location_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state_id", sa.Uuid(as_uuid=True), sa.ForeignKey("states.id", ondelete="CASCADE"), nullable=False),
        sa.Column("all_districts", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("district_id", sa.Uuid(as_uuid=True), sa.ForeignKey("districts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("all_mandals", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("mandal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("mandals.id", ondelete="CASCADE"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("assigned_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_manager_loc_tenant_user", "manager_location_assignments", ["tenant_id", "user_id", "status"]
    )
    op.create_index(
        "ix_manager_loc_lookup",
        "manager_location_assignments",
        ["tenant_id", "state_id", "district_id", "mandal_id", "status"],
    )

    # 6. Add columns to agent_profiles
    op.add_column("agent_profiles", sa.Column("mandal", sa.String(100), nullable=True))
    op.add_column("agent_profiles", sa.Column("state_id", sa.Uuid(as_uuid=True), sa.ForeignKey("states.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("agent_profiles", sa.Column("district_id", sa.Uuid(as_uuid=True), sa.ForeignKey("districts.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("agent_profiles", sa.Column("mandal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("mandals.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("agent_profiles", sa.Column("manager_user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("agent_profiles", sa.Column("manager_membership_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True))
    op.add_column("agent_profiles", sa.Column("parent_agent_membership_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_agent_profiles_manager_user", "agent_profiles", ["tenant_id", "manager_user_id"])
    op.create_index("ix_agent_profiles_location", "agent_profiles", ["tenant_id", "state_id", "district_id", "mandal_id"])

    # 7. Agent creation requests table
    op.create_table(
        "agent_creation_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("request_number", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requesting_agent_membership_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant_memberships.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requesting_agent_user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manager_membership_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True),
        sa.Column("manager_user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("state_id", sa.Uuid(as_uuid=True), sa.ForeignKey("states.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("district_id", sa.Uuid(as_uuid=True), sa.ForeignKey("districts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mandal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("mandals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state_name", sa.String(100), nullable=False),
        sa.Column("district_name", sa.String(100), nullable=False),
        sa.Column("mandal_name", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING_MANAGER"),
        sa.Column("manager_decision", sa.String(32), nullable=True),
        sa.Column("manager_decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manager_decision_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("manager_comment", sa.Text(), nullable=True),
        sa.Column("superadmin_decision", sa.String(32), nullable=True),
        sa.Column("superadmin_decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superadmin_decision_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("superadmin_comment", sa.Text(), nullable=True),
        sa.Column("fulfilled_agent_profile_id", sa.Uuid(as_uuid=True), sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("request_number", name="uq_agent_creation_requests_number"),
        sa.CheckConstraint(
            "status IN ('PENDING_MANAGER', 'PENDING_SUPER_ADMIN', 'APPROVED', 'REJECTED', 'FULFILLED')",
            name="ck_agent_creation_requests_status",
        ),
    )
    op.create_index("ix_agent_creation_req_tenant_status", "agent_creation_requests", ["tenant_id", "status"])
    op.create_index("ix_agent_creation_req_requesting_agent", "agent_creation_requests", ["tenant_id", "requesting_agent_user_id"])
    op.create_index("ix_agent_creation_req_manager", "agent_creation_requests", ["tenant_id", "manager_user_id", "status"])


def downgrade() -> None:
    op.drop_table("agent_creation_requests")
    op.drop_index("ix_agent_profiles_location", table_name="agent_profiles")
    op.drop_index("ix_agent_profiles_manager_user", table_name="agent_profiles")
    op.drop_column("agent_profiles", "parent_agent_membership_id")
    op.drop_column("agent_profiles", "manager_membership_id")
    op.drop_column("agent_profiles", "manager_user_id")
    op.drop_column("agent_profiles", "mandal_id")
    op.drop_column("agent_profiles", "district_id")
    op.drop_column("agent_profiles", "state_id")
    op.drop_column("agent_profiles", "mandal")
    op.drop_table("manager_location_assignments")
    op.drop_table("villages")
    op.drop_table("mandals")
    op.drop_table("districts")
    op.drop_table("states")
