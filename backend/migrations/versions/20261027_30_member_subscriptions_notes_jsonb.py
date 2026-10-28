"""Change member_subscriptions notes column to JSONB.

Revision ID: 20261027_30
Revises: 20261026_29
Create Date: 2026-10-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261027_30"
down_revision: Union[str, None] = "20261026_29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("""
            ALTER TABLE member_subscriptions 
            ALTER COLUMN notes TYPE JSONB 
            USING (
                CASE 
                    WHEN notes IS NULL THEN NULL 
                    WHEN notes ~ '^\\s*[\\{\\[]' THEN notes::jsonb 
                    ELSE to_jsonb(notes) 
                END
            );
        """)
    else:
        with op.batch_alter_table("member_subscriptions") as batch_op:
            batch_op.alter_column("notes", type_=sa.JSON(), existing_nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("""
            ALTER TABLE member_subscriptions 
            ALTER COLUMN notes TYPE TEXT 
            USING (
                CASE 
                    WHEN notes IS NULL THEN NULL 
                    WHEN jsonb_typeof(notes) = 'string' THEN notes #>> '{}' 
                    ELSE notes::text 
                END
            );
        """)
    else:
        with op.batch_alter_table("member_subscriptions") as batch_op:
            batch_op.alter_column("notes", type_=sa.Text(), existing_nullable=True)

