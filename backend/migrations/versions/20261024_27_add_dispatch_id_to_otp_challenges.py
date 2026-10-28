"""Add dispatch_id to otp_challenges for outbound retry idempotency.

Revision ID: 20261024_27
Revises: 20261024_26
Create Date: 2026-10-24
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261024_27"
down_revision: Union[str, None] = "20261024_26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add dispatch_id as nullable first
    op.add_column(
        "otp_challenges",
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
    )

    # 2. Backfill existing challenges with their own challenge id
    op.execute("UPDATE otp_challenges SET dispatch_id = id WHERE dispatch_id IS NULL")

    # 3. Alter to NOT NULL
    op.alter_column(
        "otp_challenges",
        "dispatch_id",
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("otp_challenges", "dispatch_id")

