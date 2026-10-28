"""Add subscriber whatsapp consent and payment microservice integration fields.

Revision ID: 20261026_29
Revises: 20261026_28
Create Date: 2026-10-26
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261026_29"
down_revision: Union[str, None] = "20261026_28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. subscriber_profiles columns
    op.add_column(
        "subscriber_profiles",
        sa.Column("whatsapp_consent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("whatsapp_consent_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. payment_orders columns
    op.add_column(
        "payment_orders",
        sa.Column("external_payment_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "payment_orders",
        sa.Column("payment_page_url", sa.String(1024), nullable=True),
    )
    op.add_column(
        "payment_orders",
        sa.Column("custom_amount", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("payment_orders", "custom_amount")
    op.drop_column("payment_orders", "payment_page_url")
    op.drop_column("payment_orders", "external_payment_id")
    op.drop_column("subscriber_profiles", "whatsapp_consent_at")
    op.drop_column("subscriber_profiles", "whatsapp_consent")

