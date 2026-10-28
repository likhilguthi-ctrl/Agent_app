"""Add operator-mediated tenant onboarding and DNS proof state.

Revision ID: 20261012_06
Revises: 20261012_05
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_06"
down_revision: Union[str, None] = "20261012_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLATFORM_PERMISSION_ADDITIONS = (
    ("platform.domain.challenge", "Issue and rotate tenant domain ownership challenges."),
    ("platform.onboarding.cancel", "Cancel eligible tenant onboarding requests."),
)


def upgrade() -> None:
    op.bulk_insert(
        sa.table(
            "platform_permissions",
            sa.column("code", sa.String(length=100)),
            sa.column("description", sa.String(length=255)),
        ),
        [{"code": code, "description": description} for code, description in PLATFORM_PERMISSION_ADDITIONS],
    )
    op.create_index(
        "ix_audit_events_actor_type_occurred",
        "audit_events",
        ["actor_category", "actor_ref", "event_type", "occurred_at"],
    )

    op.create_table(
        "tenant_onboarding_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_domain_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_by_operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("normalized_domain", sa.String(length=253), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW', 'REJECTED', 'EXPIRED', 'CANCELLED')",
            name="ck_tenant_onboarding_requests_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_tenant_onboarding_requests_version"),
        sa.CheckConstraint(
            "length(normalized_domain) BETWEEN 1 AND 253",
            name="ck_tenant_onboarding_requests_domain_length",
        ),
        sa.CheckConstraint(
            "state != 'PENDING_ACTIVATION_REVIEW' OR tenant_domain_id IS NOT NULL",
            name="ck_tenant_onboarding_requests_review_requires_domain",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_domain_id"], ["tenant_domains.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_onboarding_requests_tenant"),
        sa.UniqueConstraint(
            "id", "normalized_domain", name="uq_tenant_onboarding_requests_id_domain"
        ),
        sa.UniqueConstraint(
            "tenant_domain_id", name="uq_tenant_onboarding_requests_domain_link"
        ),
        sa.UniqueConstraint(
            "created_by_operator_id",
            "idempotency_key",
            name="uq_tenant_onboarding_requests_operator_idempotency",
        ),
    )
    op.create_index(
        "uq_tenant_onboarding_requests_pending_domain",
        "tenant_onboarding_requests",
        ["normalized_domain"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW')"
        ),
        sqlite_where=sa.text(
            "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW')"
        ),
    )
    op.create_index(
        "ix_tenant_onboarding_requests_state_expiry",
        "tenant_onboarding_requests",
        ["state", "expires_at"],
    )
    op.create_table(
        "domain_verification_challenges",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("issued_by_operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "purpose",
            sa.String(length=40),
            server_default="tenant_domain_ownership_v1",
            nullable=False,
        ),
        sa.Column("normalized_domain", sa.String(length=253), nullable=False),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_lease_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'checking', 'consumed', 'expired', 'superseded', 'locked')",
            name="ck_domain_verification_challenges_status",
        ),
        sa.CheckConstraint(
            "purpose = 'tenant_domain_ownership_v1'",
            name="ck_domain_verification_challenges_purpose",
        ),
        sa.CheckConstraint(
            "length(challenge_hash) = 64",
            name="ck_domain_verification_challenges_hash_length",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_domain_verification_challenges_attempt_count"
        ),
        sa.CheckConstraint(
            "(status = 'checking' AND verification_lease_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'checking' AND verification_lease_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_domain_verification_challenges_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL) OR "
            "(status != 'consumed' AND consumed_at IS NULL)",
            name="ck_domain_verification_challenges_consumption_state",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "normalized_domain"],
            ["tenant_onboarding_requests.id", "tenant_onboarding_requests.normalized_domain"],
            ondelete="RESTRICT",
            name="fk_domain_verification_challenges_request_domain",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id", "idempotency_key", name="uq_domain_verification_challenges_idempotency"
        ),
    )
    op.create_index(
        "ix_domain_verification_challenges_request_issued",
        "domain_verification_challenges",
        ["request_id", "issued_at"],
    )
    op.create_index(
        "uq_domain_verification_challenges_current",
        "domain_verification_challenges",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'checking')"),
        sqlite_where=sa.text("status IN ('active', 'checking')"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_actor_type_occurred", table_name="audit_events")
    op.drop_index(
        "uq_domain_verification_challenges_current",
        table_name="domain_verification_challenges",
    )
    op.drop_index(
        "ix_domain_verification_challenges_request_issued",
        table_name="domain_verification_challenges",
    )
    op.drop_table("domain_verification_challenges")
    op.drop_index(
        "ix_tenant_onboarding_requests_state_expiry",
        table_name="tenant_onboarding_requests",
    )
    op.drop_index(
        "uq_tenant_onboarding_requests_pending_domain",
        table_name="tenant_onboarding_requests",
    )
    op.drop_table("tenant_onboarding_requests")
    op.execute(
        "DELETE FROM platform_permissions WHERE code IN "
        "('platform.domain.challenge', 'platform.onboarding.cancel')"
    )
