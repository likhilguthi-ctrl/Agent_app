"""Gate tenant resolution on the external activation outcome receipt.

Revision ID: 20261012_08
Revises: 20261012_07
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_08"
down_revision: Union[str, None] = "20261012_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_activation_attempts") as batch:
        batch.add_column(
            sa.Column("completion_audit_event_id", sa.Uuid(as_uuid=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_tenant_activation_attempts_completion_audit_event",
            "audit_events",
            ["completion_audit_event_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_tenant_activation_attempts_completion_event", ["completion_audit_event_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("tenant_activation_attempts") as batch:
        batch.drop_constraint("uq_tenant_activation_attempts_completion_event", type_="unique")
        batch.drop_constraint(
            "fk_tenant_activation_attempts_completion_audit_event", type_="foreignkey"
        )
        batch.drop_column("completion_audit_event_id")
