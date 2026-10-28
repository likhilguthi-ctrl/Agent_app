"""Phase 2 Security Remediation: Unique appointment constraint on approval decisions and role permission seeding.

Revision ID: 20261017_15
Revises: 20261017_14
Create Date: 2026-10-17
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision: str = "20261017_15"
down_revision: Union[str, None] = "20261017_14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Enforce unique final decision per appointment (SEC-05)
    with op.batch_alter_table("approval_decisions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_approval_decisions_appointment_id",
            ["appointment_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("approval_decisions") as batch_op:
        batch_op.drop_constraint("uq_approval_decisions_appointment_id", type_="unique")

