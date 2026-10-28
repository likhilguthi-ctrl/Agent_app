"""Remove village-level data and allocation schema.

Hierarchy stops at Mandal (State -> District -> Mandal).

Revision ID: 20261027_32
Revises: 20261027_31
Create Date: 2026-10-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261027_32"
down_revision: Union[str, None] = "20261027_31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop villages table, its constraints, and its indexes
    op.drop_table("villages")


def downgrade() -> None:
    # Recreate villages table and its indexes
    op.create_table(
        "villages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("mandal_id", sa.Uuid(as_uuid=True), sa.ForeignKey("mandals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("native_name", sa.String(200), nullable=True),
        sa.Column("village_code", sa.String(50), nullable=False),
        sa.Column("pincode", sa.String(16), nullable=True),
        sa.Column("native_source", sa.String(50), nullable=True),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("village_code", name="uq_villages_village_code"),
    )
    op.create_index("ix_villages_mandal_id", "villages", ["mandal_id"])
    op.create_index("ix_villages_village_code", "villages", ["village_code"])
    op.create_index("ix_villages_pincode", "villages", ["pincode"])

