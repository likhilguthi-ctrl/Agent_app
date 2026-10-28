"""Add durable OIDC login transactions and operator sessions.

Revision ID: 20261013_09
Revises: 20261012_08
Create Date: 2026-10-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261013_09"
down_revision: Union[str, None] = "20261012_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_oidc_login_transactions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("browser_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("encrypted_flow_secrets", sa.String(length=2048), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(state_digest) = 64", name="ck_platform_oidc_state_digest_length"),
        sa.CheckConstraint(
            "length(browser_binding_digest) = 64",
            name="ck_platform_oidc_binding_digest_length",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_platform_oidc_login_expiry"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR exchange_started_at IS NOT NULL",
            name="ck_platform_oidc_login_consumption_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_digest", name="uq_platform_oidc_login_state_digest"),
    )
    op.create_index(
        "ix_platform_oidc_login_expiry",
        "platform_oidc_login_transactions",
        ["expires_at", "exchange_started_at"],
    )

    op.create_table(
        "platform_operator_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operator_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acr", sa.String(length=255), nullable=True),
        sa.Column("amr", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(token_digest) = 64", name="ck_platform_operator_session_digest_length"
        ),
        sa.CheckConstraint("length(csrf_token) = 64", name="ck_platform_operator_session_csrf_length"),
        sa.CheckConstraint("expires_at > created_at", name="ck_platform_operator_session_expiry"),
        sa.ForeignKeyConstraint(["operator_id"], ["platform_operators.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_platform_operator_sessions_token_digest"),
    )
    op.create_index(
        "ix_platform_operator_sessions_operator_expiry",
        "platform_operator_sessions",
        ["operator_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_operator_sessions_operator_expiry", table_name="platform_operator_sessions"
    )
    op.drop_table("platform_operator_sessions")
    op.drop_index("ix_platform_oidc_login_expiry", table_name="platform_oidc_login_transactions")
    op.drop_table("platform_oidc_login_transactions")
