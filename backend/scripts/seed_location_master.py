"""Idempotent seed script for loading official Location Master data (States, Districts, Mandals, Villages)
into PostgreSQL from Andhra Pradesh and Telangana dataset archives.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

# Setup project root import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.locations.models import District, Mandal, State

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "locations"
ZIP_AP = Path("D:/andhra_pradesh_data.zip")
ZIP_TG = Path("D:/telangana_data.zip")

DATA_SOURCES = [
    {
        "state_name": "Andhra Pradesh",
        "state_code": "AP",
        "csv_path": DATA_DIR / "andhra_pradesh" / "data" / "andhra_pradesh_villages.csv",
        "zip_path": ZIP_AP,
        "zip_internal_csv": "andhra_pradesh/data/andhra_pradesh_villages.csv",
    },
    {
        "state_name": "Telangana",
        "state_code": "TS",
        "csv_path": DATA_DIR / "telangana" / "data" / "telangana_villages.csv",
        "zip_path": ZIP_TG,
        "zip_internal_csv": "telangana/data/telangana_villages.csv",
    },
]


@dataclass
class ImportStats:
    state_name: str
    total_csv_rows: int = 0
    districts_inserted: int = 0
    districts_existing: int = 0
    mandals_inserted: int = 0
    mandals_existing: int = 0
    rejected_rows: list[str] = field(default_factory=list)


def ensure_csv_extracted(source: dict) -> Path:
    csv_file = source["csv_path"]
    if csv_file.exists():
        return csv_file
    zip_path = source["zip_path"]
    if not zip_path.exists():
        raise FileNotFoundError(f"Neither CSV ({csv_file}) nor ZIP archive ({zip_path}) found.")
    logger.info("Extracting %s from %s...", source["zip_internal_csv"], zip_path)
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extract(source["zip_internal_csv"], DATA_DIR)
    return csv_file


def seed_locations_for_source(session: Session, source: dict) -> ImportStats:
    state_name = source["state_name"]
    state_code = source["state_code"]
    csv_path = ensure_csv_extracted(source)
    stats = ImportStats(state_name=state_name)

    logger.info("--- Starting location master import for %s ---", state_name)

    # 1. Ensure State exists
    state = session.execute(select(State).where(State.name == state_name)).scalar_one_or_none()
    if state is None:
        state = State(id=uuid4(), name=state_name, code=state_code, is_active=True)
        session.add(state)
        session.flush()
        logger.info("Inserted State: %s (%s) [ID: %s]", state_name, state_code, state.id)
    else:
        logger.info("State already exists: %s (%s) [ID: %s]", state_name, state_code, state.id)

    # Load existing districts & mandals for this state
    existing_districts = {
        d.district_code: d
        for d in session.execute(select(District).where(District.state_id == state.id)).scalars().all()
    }
    existing_mandals = {
        m.mandal_code: m
        for m in session.execute(
            select(Mandal).join(District).where(District.state_id == state.id)
        ).scalars().all()
    }

    # Read CSV
    districts_to_insert: dict[str, dict] = {}
    mandals_to_insert: dict[str, dict] = {}

    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            stats.total_csv_rows += 1
            d_name = (row.get("District") or "").strip()
            d_code = (row.get("District Code") or "").strip()
            m_name = (row.get("Mandal") or "").strip()
            m_code = (row.get("Mandal Code") or "").strip()

            if not d_name or not d_code or not m_name or not m_code:
                stats.rejected_rows.append(f"Row {row_idx}: Missing required location identifiers: {row}")
                continue

            # District deduplication
            if d_code not in existing_districts and d_code not in districts_to_insert:
                districts_to_insert[d_code] = {
                    "id": uuid4(),
                    "state_id": state.id,
                    "name": d_name,
                    "district_code": d_code,
                    "is_active": True,
                }

            # Mandal deduplication
            if m_code not in existing_mandals and m_code not in mandals_to_insert:
                mandals_to_insert[m_code] = {
                    "id": uuid4(),
                    "district_code": d_code,
                    "name": m_name,
                    "mandal_code": m_code,
                    "is_active": True,
                }

    # Insert Districts
    if districts_to_insert:
        for d_dict in districts_to_insert.values():
            dist = District(
                id=d_dict["id"],
                state_id=d_dict["state_id"],
                name=d_dict["name"],
                district_code=d_dict["district_code"],
                is_active=True,
            )
            session.add(dist)
            existing_districts[d_dict["district_code"]] = dist
        session.flush()
        stats.districts_inserted = len(districts_to_insert)
    stats.districts_existing = len(existing_districts) - stats.districts_inserted

    # Insert Mandals
    if mandals_to_insert:
        for m_dict in mandals_to_insert.values():
            parent_dist = existing_districts[m_dict["district_code"]]
            mandal = Mandal(
                id=m_dict["id"],
                district_id=parent_dist.id,
                name=m_dict["name"],
                mandal_code=m_dict["mandal_code"],
                is_active=True,
            )
            session.add(mandal)
            existing_mandals[m_dict["mandal_code"]] = mandal
        session.flush()
        stats.mandals_inserted = len(mandals_to_insert)
    stats.mandals_existing = len(existing_mandals) - stats.mandals_inserted

    session.commit()
    logger.info("Committed %s: %d districts, %d mandals", state_name, stats.districts_inserted, stats.mandals_inserted)
    return stats


def run_seeder() -> None:
    start_time = time.time()
    factory = get_session_factory()
    total_stats: list[ImportStats] = []

    logger.info("Initializing Location Master Seeder...")
    with factory() as session:
        for src in DATA_SOURCES:
            stats = seed_locations_for_source(session, src)
            total_stats.append(stats)

    elapsed = time.time() - start_time
    logger.info("==================================================")
    logger.info("LOCATION MASTER IMPORT SUMMARY (Elapsed: %.2fs)", elapsed)
    logger.info("==================================================")
    for st in total_stats:
        logger.info(
            "%s: %d rows parsed | Districts: %d inserted, %d existing | Mandals: %d inserted, %d existing | Rejected: %d",
            st.state_name,
            st.total_csv_rows,
            st.districts_inserted,
            st.districts_existing,
            st.mandals_inserted,
            st.mandals_existing,
            len(st.rejected_rows),
        )
        if st.rejected_rows:
            logger.warning("First 5 rejections for %s: %s", st.state_name, st.rejected_rows[:5])
    logger.info("==================================================")


if __name__ == "__main__":
    run_seeder()
