"""Add must_change_password to account_credentials.

Revision ID: 20261023_22
Revises: 20261022_21
Create Date: 2026-10-23
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261023_22"
down_revision: Union[str, None] = "20261022_21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "account_credentials",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("account_credentials", "must_change_password")

