"""Add platform identity, local audit records, and audit delivery outbox.

Revision ID: 20261012_05
Revises: 20261012_04
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_05"
down_revision: Union[str, None] = "20261012_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLATFORM_PERMISSION_SEED = (
    ("platform.audit.read", "Read platform security audit events."),
    ("platform.identity.manage", "Manage explicitly provisioned platform operator identities."),
    ("platform.operator.manage", "Manage reviewed platform operator role assignments."),
    ("platform.onboarding.create", "Create operator-mediated tenant onboarding requests."),
    ("platform.onboarding.read", "Read tenant onboarding requests."),
    ("platform.tenant.approve", "Approve eligible tenant onboarding requests."),
    ("platform.tenant.reject", "Reject eligible tenant onboarding requests."),
    ("platform.tenant.activate", "Activate a tenant after required audit acknowledgement."),
    ("platform.tenant.suspend", "Suspend tenant access."),
    ("platform.tenant.reactivate", "Reactivate a suspended tenant after required checks."),
    ("platform.domain.verify", "Verify ownership of a tenant domain."),
    ("platform.domain.revoke", "Revoke a tenant domain."),
)


def upgrade() -> None:
    op.create_table(
        "platform_operators",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_platform_operators_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_platform_operators_issuer_subject"),
    )
    op.create_index("ix_platform_operators_status", "platform_operators", ["status"])

    op.create_table(
        "platform_permissions",
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("code = lower(code)", name="ck_platform_permissions_code_lowercase"),
        sa.CheckConstraint("length(code) BETWEEN 3 AND 100", name="ck_platform_permissions_code_length"),
        sa.PrimaryKeyConstraint("code"),
    )
    permission_table = sa.table(
        "platform_permissions",
        sa.column("code", sa.String(length=100)),
        sa.column("description", sa.String(length=255)),
    )
    op.bulk_insert(
        permission_table,
        [{"code": code, "description": description} for code, description in PLATFORM_PERMISSION_SEED],
    )

    op.create_table(
        "platform_roles",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_platform_roles_status"),
        sa.CheckConstraint("code = lower(code)", name="ck_platform_roles_code_lowercase"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_platform_roles_code"),
    )
    op.create_table(
        "platform_role_permissions",
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("permission_code", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')", name="ck_platform_role_permissions_status"
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'inactive') AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_platform_role_permissions_revocation_state",
        ),
        sa.ForeignKeyConstraint(["permission_code"], ["platform_permissions.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["role_id"], ["platform_roles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("role_id", "permission_code"),
    )
    op.create_index(
        "ix_platform_role_permissions_role_status",
        "platform_role_permissions",
        ["role_id", "status"],
    )
    op.create_table(
        "platform_operator_role_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("assigned_by_operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="inactive", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'revoked')",
            name="ck_platform_operator_role_assignments_status",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_platform_operator_role_assignments_validity_window",
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status != 'revoked' AND revoked_at IS NULL)",
            name="ck_platform_operator_role_assignments_revocation_state",
        ),
        sa.CheckConstraint(
            "operator_id != assigned_by_operator_id",
            name="ck_platform_operator_role_assignments_not_self_assigned",
        ),
        sa.ForeignKeyConstraint(["assigned_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operator_id"], ["platform_operators.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["role_id"], ["platform_roles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_operator_role_assignments_operator_status",
        "platform_operator_role_assignments",
        ["operator_id", "status", "valid_from", "valid_until"],
    )
    op.create_index(
        "uq_platform_operator_role_assignments_active",
        "platform_operator_role_assignments",
        ["operator_id", "role_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor_category", sa.String(length=32), nullable=False),
        sa.Column("actor_ref", sa.String(length=200), nullable=True),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("target_entity_type", sa.String(length=100), nullable=True),
        sa.Column("target_entity_ref", sa.String(length=200), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("decision_metadata", sa.JSON(), nullable=False),
        sa.CheckConstraint("schema_version >= 1", name="ck_audit_events_schema_version"),
        sa.CheckConstraint(
            "actor_category IN ('platform_operator', 'tenant_member', 'system', 'unauthenticated')",
            name="ck_audit_events_actor_category",
        ),
        sa.CheckConstraint(
            "(actor_category = 'unauthenticated' AND actor_ref IS NULL) OR "
            "(actor_category != 'unauthenticated' AND actor_ref IS NOT NULL)",
            name="ck_audit_events_actor_reference",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'failure', 'pending')",
            name="ck_audit_events_outcome",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_tenant_recorded", "audit_events", ["tenant_id", "recorded_at", "id"]
    )
    op.create_index(
        "ix_audit_events_type_recorded", "audit_events", ["event_type", "recorded_at"]
    )
    op.create_index(
        "ix_audit_events_actor_recorded",
        "audit_events",
        ["actor_category", "actor_ref", "recorded_at"],
    )
    op.create_table(
        "audit_outbox_events",
        sa.Column("event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("acknowledgement_ref", sa.String(length=255), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'delivering', 'retryable_failure', 'delivered', 'terminal_failure')",
            name="ck_audit_outbox_events_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_audit_outbox_events_attempt_count"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_audit_outbox_events_digest_length"),
        sa.CheckConstraint(
            "(status = 'delivering' AND worker_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'delivering' AND worker_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_audit_outbox_events_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'delivered' AND acknowledged_at IS NOT NULL AND acknowledgement_ref IS NOT NULL) OR "
            "(status != 'delivered' AND acknowledged_at IS NULL AND acknowledgement_ref IS NULL)",
            name="ck_audit_outbox_events_ack_state",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["audit_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_audit_outbox_events_idempotency_key"),
    )
    op.create_index(
        "ix_audit_outbox_events_dispatch",
        "audit_outbox_events",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_audit_outbox_events_lease", "audit_outbox_events", ["status", "lease_expires_at"]
    )
    op.create_table(
        "audit_outbox_attempts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("acknowledgement_ref", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "status IN ('in_progress', 'delivered', 'retryable_failure', 'terminal_failure')",
            name="ck_audit_outbox_attempts_status",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_audit_outbox_attempts_number"),
        sa.CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status != 'in_progress' AND completed_at IS NOT NULL)",
            name="ck_audit_outbox_attempts_completion_state",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["audit_outbox_events.event_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "attempt_number", name="uq_audit_outbox_attempts_number"),
    )
    op.create_index(
        "ix_audit_outbox_attempts_event_started",
        "audit_outbox_attempts",
        ["event_id", "started_at"],
    )
    op.create_table(
        "audit_outbox_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("adapter_name", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sink_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_audit_outbox_worker_heartbeats_last_seen",
        "audit_outbox_worker_heartbeats",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_outbox_worker_heartbeats_last_seen",
        table_name="audit_outbox_worker_heartbeats",
    )
    op.drop_table("audit_outbox_worker_heartbeats")
    op.drop_index("ix_audit_outbox_attempts_event_started", table_name="audit_outbox_attempts")
    op.drop_table("audit_outbox_attempts")
    op.drop_index("ix_audit_outbox_events_lease", table_name="audit_outbox_events")
    op.drop_index("ix_audit_outbox_events_dispatch", table_name="audit_outbox_events")
    op.drop_table("audit_outbox_events")
    op.drop_index("ix_audit_events_actor_recorded", table_name="audit_events")
    op.drop_index("ix_audit_events_type_recorded", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_recorded", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(
        "uq_platform_operator_role_assignments_active", table_name="platform_operator_role_assignments"
    )
    op.drop_index(
        "ix_platform_operator_role_assignments_operator_status",
        table_name="platform_operator_role_assignments",
    )
    op.drop_table("platform_operator_role_assignments")
    op.drop_index("ix_platform_role_permissions_role_status", table_name="platform_role_permissions")
    op.drop_table("platform_role_permissions")
    op.drop_table("platform_roles")
    op.drop_table("platform_permissions")
    op.drop_index("ix_platform_operators_status", table_name="platform_operators")
    op.drop_table("platform_operators")
