"""Deduplicate otp_challenges and add unique constraint on (mobile_e164, purpose).

Revision ID: 20261024_25
Revises: 20261024_24
Create Date: 2026-10-24
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261024_25"
down_revision: Union[str, None] = "20261024_24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Deduplicate any existing duplicate (mobile_e164, purpose) records
    # First, query all (mobile_e164, purpose) groups having more than one record
    dup_groups_query = sa.text("""
        SELECT mobile_e164, purpose
        FROM otp_challenges
        GROUP BY mobile_e164, purpose
        HAVING COUNT(id) > 1
    """)
    dup_groups = bind.execute(dup_groups_query).fetchall()

    for row in dup_groups:
        mobile = row[0]
        purpose = row[1]

        # Query all records for this group, prioritizing active unconsumed challenges, then newest created_at
        records_query = sa.text("""
            SELECT id, created_at, expires_at, consumed_at, verified_at
            FROM otp_challenges
            WHERE mobile_e164 = :mobile AND purpose = :purpose
            ORDER BY
                CASE WHEN consumed_at IS NULL THEN 0 ELSE 1 END,
                created_at DESC
        """)
        records = bind.execute(records_query, {"mobile": mobile, "purpose": purpose}).fetchall()

        if len(records) > 1:
            # Retain records[0] (most recent or active), delete the older obsolete duplicates
            obsolete_ids = [r[0] for r in records[1:]]
            for obs_id in obsolete_ids:
                bind.execute(
                    sa.text("DELETE FROM otp_challenges WHERE id = :id"),
                    {"id": obs_id},
                )

    # 2. Drop existing non-unique index if present
    try:
        op.drop_index("ix_otp_challenges_mobile_purpose", table_name="otp_challenges")
    except Exception:
        pass

    # 3. Create unique constraint on (mobile_e164, purpose)
    op.create_unique_constraint(
        "uq_otp_challenges_mobile_purpose",
        "otp_challenges",
        ["mobile_e164", "purpose"],
    )


def downgrade() -> None:
    try:
        op.drop_constraint("uq_otp_challenges_mobile_purpose", "otp_challenges", type_="unique")
    except Exception:
        pass

    op.create_index("ix_otp_challenges_mobile_purpose", "otp_challenges", ["mobile_e164", "purpose"])

