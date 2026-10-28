"""Add region state column, index, and seed canonical locations.

Revision ID: 20261024_24
Revises: 20261024_23
Create Date: 2026-10-24
"""

from typing import Sequence, Union
from uuid import uuid4
from alembic import op
import sqlalchemy as sa


revision: str = "20261024_24"
down_revision: Union[str, None] = "20261024_23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CANONICAL_LOCATIONS = {
    "Andhra Pradesh": [
        ("AP-ALLURI-SITHARAMA-RAJU", "Alluri Sitharama Raju"),
        ("AP-ANAKAPALLI", "Anakapalli"),
        ("AP-ANANTHAPURAMU", "Ananthapuramu"),
        ("AP-ANNAMAYYA", "Annamayya"),
        ("AP-BAPATLA", "Bapatla"),
        ("AP-CHITTOOR", "Chittoor"),
        ("AP-DR-BR-AMBEDKAR-KONASEEMA", "Dr. B.R. Ambedkar Konaseema"),
        ("AP-EAST-GODAVARI", "East Godavari"),
        ("AP-ELURU", "Eluru"),
        ("AP-GUNTUR", "Guntur"),
        ("AP-KAKINADA", "Kakinada"),
        ("AP-KRISHNA", "Krishna"),
        ("AP-KURNOOL", "Kurnool"),
        ("AP-NANDYAL", "Nandyal"),
        ("AP-NTR", "NTR"),
        ("AP-PALNADU", "Palnadu"),
        ("AP-PARVATHIPURAM-MANYAM", "Parvathipuram Manyam"),
        ("AP-PRAKASAM", "Prakasam"),
        ("AP-SRI-POTTI-SRIRAMULU-NELLORE", "Sri Potti Sriramulu Nellore"),
        ("AP-SRI-SATHYA-SAI", "Sri Sathya Sai"),
        ("AP-SRIKAKULAM", "Srikakulam"),
        ("AP-TIRUPATI", "Tirupati"),
        ("AP-VISAKHAPATNAM", "Visakhapatnam"),
        ("AP-VIZIANAGARAM", "Vizianagaram"),
        ("AP-WEST-GODAVARI", "West Godavari"),
        ("AP-YSR-KADAPA", "YSR Kadapa"),
    ],
    "Telangana": [
        ("TS-ADILABAD", "Adilabad"),
        ("TS-BHADRADRI-KOTHAGUDEM", "Bhadradri Kothagudem"),
        ("TS-HANUMAKONDA", "Hanumakonda"),
        ("TS-HYDERABAD", "Hyderabad"),
        ("TS-JAGTIAL", "Jagtial"),
        ("TS-JANGAON", "Jangaon"),
        ("TS-JAYASHANKAR-BHUPALPALLY", "Jayashankar Bhupalpally"),
        ("TS-JOGULAMBA-GADWAL", "Jogulamba Gadwal"),
        ("TS-KAMAREDDY", "Kamareddy"),
        ("TS-KARIMNAGAR", "Karimnagar"),
        ("TS-KHAMMAM", "Khammam"),
        ("TS-KUMURAM-BHEEM-ASIFABAD", "Kumuram Bheem Asifabad"),
        ("TS-MAHABUBABAD", "Mahabubabad"),
        ("TS-MAHABUBNAGAR", "Mahabubnagar"),
        ("TS-MANCHERIAL", "Mancherial"),
        ("TS-MEDAK", "Medak"),
        ("TS-MEDCHAL-MALKAJGIRI", "Medchal-Malkajgiri"),
        ("TS-MULUGU", "Mulugu"),
        ("TS-NAGARKURNOOL", "Nagarkurnool"),
        ("TS-NALGONDA", "Nalgonda"),
        ("TS-NARAYANPET", "Narayanpet"),
        ("TS-NIRMAL", "Nirmal"),
        ("TS-NIZAMABAD", "Nizamabad"),
        ("TS-PEDDAPALLI", "Peddapalli"),
        ("TS-RAJANNA-SIRCILLA", "Rajanna Sircilla"),
        ("TS-RANGA-REDDY", "Ranga Reddy"),
        ("TS-SANGAREDDY", "Sangareddy"),
        ("TS-SIDDIPET", "Siddipet"),
        ("TS-SURYAPET", "Suryapet"),
        ("TS-VIKARABAD", "Vikarabad"),
        ("TS-WANAPARTHY", "Wanaparthy"),
        ("TS-WARANGAL", "Warangal"),
        ("TS-YADADRI-BHUVANAGIRI", "Yadadri Bhuvanagiri"),
    ],
}


def upgrade() -> None:
    op.add_column(
        "regions",
        sa.Column(
            "state",
            sa.String(length=100),
            nullable=False,
            server_default="Andhra Pradesh",
        ),
    )
    op.create_index(
        "ix_regions_tenant_state_status",
        "regions",
        ["tenant_id", "state", "status"],
        unique=False,
    )

    # Seed canonical locations for all existing tenants
    conn = op.get_bind()
    tenants = conn.execute(sa.text("SELECT id FROM tenants")).fetchall()

    for (tenant_id,) in tenants:
        existing_codes_rows = conn.execute(
            sa.text("SELECT code FROM regions WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        ).fetchall()
        existing_codes = {r[0] for r in existing_codes_rows}

        for state, districts in CANONICAL_LOCATIONS.items():
            for code, name in districts:
                if code not in existing_codes:
                    conn.execute(
                        sa.text(
                            """
                            INSERT INTO regions (id, tenant_id, code, name, state, description, status, created_at, updated_at)
                            VALUES (:id, :tenant_id, :code, :name, :state, :description, 'active', NOW(), NOW())
                            """
                        ),
                        {
                            "id": uuid4(),
                            "tenant_id": tenant_id,
                            "code": code,
                            "name": name,
                            "state": state,
                            "description": f"{name} district in {state}",
                        },
                    )
                    existing_codes.add(code)


def downgrade() -> None:
    op.drop_index("ix_regions_tenant_state_status", table_name="regions")
    op.drop_column("regions", "state")

