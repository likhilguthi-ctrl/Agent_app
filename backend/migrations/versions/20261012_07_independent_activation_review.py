"""Add independent review and audit-gated activation records.

Revision ID: 20261012_07
Revises: 20261012_06
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_07"
down_revision: Union[str, None] = "20261012_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ACTIVE is only written by the activation service after the matching
    # approval and activation-intent audit receipts have been verified.
    with op.batch_alter_table("tenant_onboarding_requests") as batch:
        batch.drop_constraint("ck_tenant_onboarding_requests_state", type_="check")
        batch.create_check_constraint(
            "ck_tenant_onboarding_requests_state",
            "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW', "
            "'REJECTED', 'EXPIRED', 'CANCELLED', 'ACTIVE')",
        )
        batch.create_unique_constraint(
            "uq_tenant_onboarding_requests_id_creator", ["id", "created_by_operator_id"]
        )

    op.create_table(
        "tenant_onboarding_reviews",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("requester_operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reviewer_operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("audit_event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=12), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')", name="ck_onboarding_reviews_decision"
        ),
        sa.CheckConstraint(
            "requester_operator_id != reviewer_operator_id",
            name="ck_onboarding_reviews_independent_reviewer",
        ),
        sa.CheckConstraint(
            "length(reason_code) BETWEEN 2 AND 80", name="ck_onboarding_reviews_reason_length"
        ),
        sa.CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 64",
            name="ck_onboarding_reviews_policy_version_length",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "requester_operator_id"],
            ["tenant_onboarding_requests.id", "tenant_onboarding_requests.created_by_operator_id"],
            ondelete="RESTRICT",
            name="fk_onboarding_reviews_requester",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_tenant_onboarding_reviews_request"),
        sa.UniqueConstraint(
            "request_id", "idempotency_key", name="uq_tenant_onboarding_reviews_idempotency"
        ),
        sa.UniqueConstraint("audit_event_id", name="uq_tenant_onboarding_reviews_audit_event"),
    )
    op.create_index(
        "ix_tenant_onboarding_reviews_reviewer_decision",
        "tenant_onboarding_reviews",
        ["reviewer_operator_id", "decision_at"],
    )

    op.create_table(
        "tenant_activation_attempts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("initiated_by_operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("intent_audit_event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING_AUDIT_ACK', 'ACTIVATED', 'CANCELLED')",
            name="ck_tenant_activation_attempts_status",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVATED' AND completed_at IS NOT NULL) OR "
            "(status != 'ACTIVATED' AND completed_at IS NULL)",
            name="ck_tenant_activation_attempts_completion",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 128",
            name="ck_tenant_activation_attempts_idempotency_length",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["tenant_onboarding_requests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["initiated_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["intent_audit_event_id"], ["audit_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id", "idempotency_key", name="uq_tenant_activation_attempts_idempotency"
        ),
        sa.UniqueConstraint(
            "intent_audit_event_id", name="uq_tenant_activation_attempts_intent_event"
        ),
    )
    op.create_index(
        "uq_tenant_activation_attempts_pending",
        "tenant_activation_attempts",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING_AUDIT_ACK'"),
        sqlite_where=sa.text("status = 'PENDING_AUDIT_ACK'"),
    )
    op.create_index(
        "ix_tenant_activation_attempts_status_created",
        "tenant_activation_attempts",
        ["status", "created_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    active_count = connection.execute(
        sa.text("SELECT count(*) FROM tenant_onboarding_requests WHERE state = 'ACTIVE'")
    ).scalar_one()
    if active_count:
        raise RuntimeError(
            "Cannot downgrade while active onboarding requests exist; preserve activation history."
        )

    op.drop_index(
        "ix_tenant_activation_attempts_status_created", table_name="tenant_activation_attempts"
    )
    op.drop_index(
        "uq_tenant_activation_attempts_pending", table_name="tenant_activation_attempts"
    )
    op.drop_table("tenant_activation_attempts")
    op.drop_index(
        "ix_tenant_onboarding_reviews_reviewer_decision", table_name="tenant_onboarding_reviews"
    )
    op.drop_table("tenant_onboarding_reviews")
    with op.batch_alter_table("tenant_onboarding_requests") as batch:
        batch.drop_constraint("uq_tenant_onboarding_requests_id_creator", type_="unique")
        batch.drop_constraint("ck_tenant_onboarding_requests_state", type_="check")
        batch.create_check_constraint(
            "ck_tenant_onboarding_requests_state",
            "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW', "
            "'REJECTED', 'EXPIRED', 'CANCELLED')",
        )
