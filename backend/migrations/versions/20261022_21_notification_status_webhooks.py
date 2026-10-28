"""Phase 5: Notification status webhooks, message persistence, and event deduplication.

Revision ID: 20261022_21
Revises: 20261021_20
Create Date: 2026-10-22
"""

from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa


revision: str = "20261022_21"
down_revision: Union[str, None] = "20261021_20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create notification_messages table
    op.create_table(
        "notification_messages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("recipient", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="QUEUED", nullable=False),
        sa.Column("priority", sa.String(length=8), server_default="P0", nullable=False),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("purpose", sa.String(length=64), nullable=True),
        sa.Column("challenge_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=256), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
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
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_notification_messages_tenant"
        ),
        sa.UniqueConstraint("message_id", name="uq_notification_messages_message_id"),
        sa.CheckConstraint(
            "channel IN ('SMS', 'WHATSAPP', 'EMAIL')",
            name="ck_notification_messages_channel",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'PROVIDER_ACCEPTED', 'DELIVERED', 'READ', 'RETRY_PENDING', 'MOCK_SENT', 'FAILED', 'EXPIRED', 'UNKNOWN_OUTCOME')",
            name="ck_notification_messages_status",
        ),
    )

    op.create_index(
        "ix_notification_messages_tenant_status",
        "notification_messages",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_notification_messages_message_id",
        "notification_messages",
        ["message_id"],
    )
    op.create_index(
        "ix_notification_messages_challenge_id",
        "notification_messages",
        ["challenge_id"],
    )

    # 2. Create notification_webhook_events table
    op.create_table(
        "notification_webhook_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processing_status", sa.String(length=32), server_default="received", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_id", name="uq_notification_webhook_event_id"),
        sa.CheckConstraint(
            "processing_status IN ('received', 'processed', 'duplicate', 'ignored', 'quarantined', 'failed')",
            name="ck_notification_webhook_proc_status",
        ),
    )

    op.create_index(
        "ix_notification_webhook_event_id",
        "notification_webhook_events",
        ["event_id"],
    )
    op.create_index(
        "ix_notification_webhook_message_id",
        "notification_webhook_events",
        ["message_id"],
    )
    op.create_index(
        "ix_notification_webhook_status",
        "notification_webhook_events",
        ["processing_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_webhook_status", table_name="notification_webhook_events")
    op.drop_index("ix_notification_webhook_message_id", table_name="notification_webhook_events")
    op.drop_index("ix_notification_webhook_event_id", table_name="notification_webhook_events")
    op.drop_table("notification_webhook_events")

    op.drop_index("ix_notification_messages_challenge_id", table_name="notification_messages")
    op.drop_index("ix_notification_messages_message_id", table_name="notification_messages")
    op.drop_index("ix_notification_messages_tenant_status", table_name="notification_messages")
    op.drop_table("notification_messages")

