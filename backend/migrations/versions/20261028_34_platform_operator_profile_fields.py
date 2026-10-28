"""Add profile fields (full_name, email, profile_photo_key) to platform_operators.

Revision ID: 20261028_34
Revises: 20261028_33
Create Date: 2026-10-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261028_34"
down_revision: Union[str, None] = "20261028_33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.add_column("platform_operators", sa.Column("full_name", sa.String(200), nullable=True))
        op.add_column("platform_operators", sa.Column("email", sa.String(255), nullable=True))
        op.add_column("platform_operators", sa.Column("profile_photo_key", sa.String(64), nullable=True))
    else:
        with op.batch_alter_table("platform_operators") as batch_op:
            batch_op.add_column(sa.Column("full_name", sa.String(200), nullable=True))
            batch_op.add_column(sa.Column("email", sa.String(255), nullable=True))
            batch_op.add_column(sa.Column("profile_photo_key", sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.drop_column("platform_operators", "profile_photo_key")
        op.drop_column("platform_operators", "email")
        op.drop_column("platform_operators", "full_name")
    else:
        with op.batch_alter_table("platform_operators") as batch_op:
            batch_op.drop_column("profile_photo_key")
            batch_op.drop_column("email")
            batch_op.drop_column("full_name")

