"""Canonical location definitions and seeding for Community OTT.

Provides standardized state and district definitions for:
- Andhra Pradesh (26 districts)
- Telangana (33 districts)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Tuple
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.tenants.models import Region

# Canonical mapping: State -> list of (code, name)
CANONICAL_LOCATIONS: Dict[str, List[Tuple[str, str]]] = {
    "Andhra Pradesh": [
        ("AP-ALLURI-SITHARAMA-RAJU", "Alluri Sitharama Raju"),
        ("AP-ANAKAPALLI", "Anakapalli"),
        ("AP-ANANTHAPURAMU", "Ananthapuramu"),
        ("AP-ANNAMAYYA", "Annamayya"),
        ("AP-BAPATLA", "Bapatla"),
        ("AP-CHITTOOR", "Chittoor"),
        ("AP-DR-BR-AMBEDKAR-KONASEEMA", "Dr. B. R. Ambedkar Konaseema"),
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
        ("TS-RANGA-REDDY", "Rangareddy"),
        ("TS-SANGAREDDY", "Sangareddy"),
        ("TS-SIDDIPET", "Siddipet"),
        ("TS-SURYAPET", "Suryapet"),
        ("TS-VIKARABAD", "Vikarabad"),
        ("TS-WANAPARTHY", "Wanaparthy"),
        ("TS-WARANGAL", "Warangal"),
        ("TS-YADADRI-BHUVANAGIRI", "Yadadri Bhuvanagiri"),
    ],
}

# Aliases to accommodate legacy spellings
DISTRICT_ALIASES: Dict[str, str] = {
    "ranga reddy": "Rangareddy",
    "dr. b.r. ambedkar konaseema": "Dr. B. R. Ambedkar Konaseema",
    "dr. b. r. ambedkar konaseema": "Dr. B. R. Ambedkar Konaseema",
}

SUPPORTED_STATES: List[str] = list(CANONICAL_LOCATIONS.keys())


def get_states() -> List[str]:
    """Returns the list of supported states: ['Andhra Pradesh', 'Telangana']."""
    return list(SUPPORTED_STATES)


def get_districts_for_state(state: str) -> List[str]:
    """Returns canonical district names for the given state, or empty list if unsupported."""
    clean_state = state.strip() if state else ""
    pairs = CANONICAL_LOCATIONS.get(clean_state, [])
    return [name for _, name in pairs]


def normalize_district_name(district: str) -> str:
    """Normalizes district string, resolving whitespace and legacy alias variations."""
    clean = district.strip()
    lower = clean.lower()
    if lower in DISTRICT_ALIASES:
        return DISTRICT_ALIASES[lower]
    return clean


def validate_state(state: str) -> bool:
    """Returns True if the state is one of the supported states."""
    return state.strip() in SUPPORTED_STATES if state else False


def validate_state_district(state: str, district: str) -> bool:
    """Returns True if the district belongs to the specified state."""
    clean_state = state.strip() if state else ""
    if clean_state not in CANONICAL_LOCATIONS:
        return False
    norm_dist = normalize_district_name(district).lower()
    valid_districts = {name.lower() for _, name in CANONICAL_LOCATIONS[clean_state]}
    return norm_dist in valid_districts


def seed_canonical_regions_for_tenant(session: Session, tenant_id: UUID) -> List[Region]:
    """Ensure all canonical districts for supported states are seeded for the tenant.

    Idempotent: skips regions whose code or district name already exists for the tenant.
    """
    existing_codes = set(
        session.execute(
            select(Region.code).where(Region.tenant_id == tenant_id)
        ).scalars().all()
    )
    existing_state_names = {
        (s, (n or "").lower())
        for s, n in session.execute(
            select(Region.state, Region.name).where(Region.tenant_id == tenant_id)
        ).all()
    }

    created_regions: List[Region] = []
    now = datetime.now(timezone.utc)

    for state, districts in CANONICAL_LOCATIONS.items():
        for code, name in districts:
            if code not in existing_codes and (state, name.lower()) not in existing_state_names:
                region = Region(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    code=code,
                    name=name,
                    state=state,
                    description=f"{name} district in {state}",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                session.add(region)
                created_regions.append(region)
                existing_codes.add(code)
                existing_state_names.add((state, name.lower()))

    if created_regions:
        session.flush()

    return created_regions


def get_or_create_region_for_district(session: Session, tenant_id: UUID, state: str, district: str) -> Region | None:
    """Finds or seeds the canonical Region for a given tenant, state, and district."""
    from sqlalchemy import func
    norm_dist = normalize_district_name(district)
    reg = session.execute(
        select(Region).where(
            Region.tenant_id == tenant_id,
            Region.state == state,
            func.lower(Region.name) == norm_dist.lower(),
        ).order_by(Region.code.asc())
    ).scalars().first()
    if reg is not None:
        return reg

    for s, dists in CANONICAL_LOCATIONS.items():
        if s == state:
            for code, name in dists:
                if name.lower() == norm_dist.lower():
                    now = datetime.now(timezone.utc)
                    new_reg = Region(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        code=code,
                        name=name,
                        state=state,
                        description=f"{name} district in {state}",
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(new_reg)
                    session.flush()
                    return new_reg
    return None

