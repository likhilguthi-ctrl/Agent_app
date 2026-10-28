"""Comprehensive Automated Tests for:
1. Location Master Integrity & Idempotent Seeding
2. Manager Location Assignment & Scope Enforcement
3. Super Admin & Manager Agent Creation with Manager Allocation Rules
4. Three-Level Hierarchy Drill-Down (Managers -> Agents -> Subscribers)
5. Two-Stage Agent Creation Approval Workflow (Dual Custody)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.testclient import TestClient
from app.main import app

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.locations.models import (
    State,
    District,
    Mandal,
    ManagerLocationAssignment,
    AgentCreationRequest,
)
from app.modules.locations.service import LocationService
from app.modules.locations.schemas import StateScopeConfig, MandalScopeConfig
from scripts.seed_location_master import run_seeder
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.agents.schemas import AgentCreateRequest
from app.modules.agents.api import _create_agent_in_community
from app.modules.tenants.models import Tenant, AdminProfile
from app.modules.identity.models import User, TenantMembership
from app.modules.authentication.models import EmailOtpChallenge
from app.modules.registration_photos.models import StoredObject
from app.modules.relationships.models import MemberRelationship, RelationshipType, RelationshipStatus
from app.modules.hierarchy.api import (
    list_community_managers,
    list_manager_agents,
    list_agent_subscribers,
    request_agent_creation_permission,
    manager_review_creation_request,
    superadmin_review_creation_request,
    create_child_agent_from_approved_request,
)
from app.modules.hierarchy.schemas import (
    AgentCreationRequestSubmit,
    AgentCreationRequestReview,
)
from app.modules.authentication.repository import ResolvedSession


@pytest.fixture(scope="module")
def db_session_factory():
    """Authoritative session factory connected to the live PostgreSQL database."""
    return get_session_factory()


@pytest.fixture
def db(db_session_factory):
    session = db_session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_email_token(session: Session, email: str, tenant_id: UUID | None = None) -> str:
    token = f"tok_{uuid4().hex}"
    session.add(
        EmailOtpChallenge(
            id=uuid4(),
            tenant_id=tenant_id,
            email=email.strip().lower(),
            purpose="email_verification",
            otp_hash="approved",
            otp_salt="approved",
            failed_attempts=0,
            resend_count=0,
            dispatch_id=uuid4(),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            verified_at=datetime.now(UTC),
            verification_token=token,
        )
    )
    session.flush()
    return token


def seed_test_stored_objects(session: Session, tenant_id: UUID) -> tuple[str, str, str]:
    uid = uuid4().hex[:8]
    p_key = f"photo_{uid}.jpg"
    a_key = f"aadhaar_{uid}.jpg"
    pan_key = f"pan_{uid}.jpg"
    now = datetime.now(UTC)
    for k, cat in [(p_key, "agent-photo"), (a_key, "aadhaar-document"), (pan_key, "pan-document")]:
        session.add(
            StoredObject(
                id=uuid4(),
                tenant_id=tenant_id,
                object_key=k,
                category=cat,
                bucket_name="r2-test-bucket",
                content_type="image/jpeg",
                file_size=1024,
                created_at=now,
            )
        )
    session.flush()
    return p_key, a_key, pan_key


def test_location_master_integrity_and_seeder_idempotency(db: Session):
    """Test 1: Verify location master dataset loaded and seeder idempotency."""
    # 1. Verify States
    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one_or_none()
    ts_state = db.execute(select(State).where(State.code == "TS")).scalar_one_or_none()
    assert ap_state is not None, "Andhra Pradesh state must exist"
    assert ts_state is not None, "Telangana state must exist"
    assert ap_state.name == "Andhra Pradesh"
    assert ts_state.name == "Telangana"

    # 2. Verify Districts
    ap_districts = db.execute(select(District).where(District.state_id == ap_state.id)).scalars().all()
    ts_districts = db.execute(select(District).where(District.state_id == ts_state.id)).scalars().all()
    total_districts = db.execute(select(func.count(District.id))).scalar_one()

    assert len(ap_districts) == 28, f"Expected 28 AP districts, got {len(ap_districts)}"
    assert len(ts_districts) == 33, f"Expected 33 TS districts, got {len(ts_districts)}"
    assert total_districts == 61, f"Expected 61 total districts, got {total_districts}"

    # 3. Verify Mandals
    total_mandals = db.execute(select(func.count(Mandal.id))).scalar_one()
    assert total_mandals == 1305, f"Expected 1,305 total mandals, got {total_mandals}"

    # 4. Verify Villages table does not exist in PostgreSQL schema
    table_exists = db.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'villages';")
    ).scalar()
    assert table_exists is None, "Villages table must not exist in PostgreSQL schema"

    # 5. Idempotent seeder re-run
    run_seeder()
    # Verify counts remained invariant
    recheck_districts = db.execute(select(func.count(District.id))).scalar_one()
    recheck_mandals = db.execute(select(func.count(Mandal.id))).scalar_one()
    assert recheck_districts == 61
    assert recheck_mandals == 1305


def test_manager_location_scope_assignment_and_validation(db: Session):
    """Test 2: Manager Scope Enforcement (single-state, dual-state, specific districts/mandals)."""
    tenant_id = uuid4()
    manager_user_id = uuid4()
    tenant = Tenant(id=tenant_id, slug=f"test-comm-{uuid4().hex[:6]}", status="active")
    manager_user = User(id=manager_user_id, status="active")
    db.add(tenant)
    db.add(manager_user)
    db.flush()
    db.add(TenantMembership(id=uuid4(), tenant_id=tenant_id, user_id=manager_user_id, mobile_e164="+919876543201", status="active"))
    db.flush()

    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one()
    ts_state = db.execute(select(State).where(State.code == "TS")).scalar_one()

    vizag_district = db.execute(
        select(District).where(District.state_id == ap_state.id, District.name == "Visakhapatnam")
    ).scalar_one()
    krishna_district = db.execute(
        select(District).where(District.state_id == ap_state.id, District.name == "Krishna")
    ).scalar_one()
    hyderabad_district = db.execute(
        select(District).where(District.state_id == ts_state.id, District.name == "Hyderabad")
    ).scalar_one()

    # Manager assigned to:
    # 1. AP: Visakhapatnam district ONLY (all_districts=False)
    # 2. TS: ALL districts (all_districts=True)
    configs = [
        StateScopeConfig(
            state_id=ap_state.id,
            all_districts=False,
            district_ids=[vizag_district.id],
        ),
        StateScopeConfig(
            state_id=ts_state.id,
            all_districts=True,
            district_ids=[],
        ),
    ]

    LocationService.save_manager_location_assignments(
        session=db,
        tenant_id=tenant_id,
        user_id=manager_user_id,
        configs=configs,
    )
    db.flush()
    assignments = db.execute(
        select(ManagerLocationAssignment).where(ManagerLocationAssignment.user_id == manager_user_id)
    ).scalars().all()
    assert len(assignments) >= 2

    # Check Visakhapatnam, AP (authorized)
    assert LocationService.is_manager_authorized_for_location(
        session=db,
        tenant_id=tenant_id,
        manager_user_id=manager_user_id,
        state_id=ap_state.id,
        district_id=vizag_district.id,
    ) is True

    # Check Krishna, AP (NOT authorized)
    assert LocationService.is_manager_authorized_for_location(
        session=db,
        tenant_id=tenant_id,
        manager_user_id=manager_user_id,
        state_id=ap_state.id,
        district_id=krishna_district.id,
    ) is False

    # Check Hyderabad, TS (authorized via all_districts)
    assert LocationService.is_manager_authorized_for_location(
        session=db,
        tenant_id=tenant_id,
        manager_user_id=manager_user_id,
        state_id=ts_state.id,
        district_id=hyderabad_district.id,
    ) is True

    # Test find_eligible_managers_for_location
    eligible_vizag = LocationService.find_eligible_managers_for_location(
        session=db,
        tenant_id=tenant_id,
        state_id=ap_state.id,
        district_id=vizag_district.id,
    )
    eligible_vizag_ids = [u.id for u, m, n in eligible_vizag]
    assert manager_user_id in eligible_vizag_ids

    eligible_krishna = LocationService.find_eligible_managers_for_location(
        session=db,
        tenant_id=tenant_id,
        state_id=ap_state.id,
        district_id=krishna_district.id,
    )
    eligible_krishna_ids = [u.id for u, m, n in eligible_krishna]
    assert manager_user_id not in eligible_krishna_ids

    # Get Manager Scope response
    scope = LocationService.get_manager_scope(
        session=db,
        tenant_id=tenant_id,
        manager_user_id=manager_user_id,
    )
    assert len(scope.states) == 2
    assert str(ap_state.id) in scope.all_districts_by_state
    assert scope.all_districts_by_state[str(ap_state.id)] is False
    assert scope.all_districts_by_state[str(ts_state.id)] is True


def test_manager_agent_creation_scope_enforcement(db: Session):
    """Test 3: Manager creates Agent within permitted scope vs. outside permitted scope."""
    tenant_id = uuid4()
    manager_user_id = uuid4()
    manager_mem_id = uuid4()

    tenant = Tenant(id=tenant_id, slug=f"test-mgr-comm-{uuid4().hex[:6]}", status="active")
    manager_user = User(id=manager_user_id, status="active")
    db.add(tenant)
    db.add(manager_user)
    db.flush()

    manager_mem = TenantMembership(id=manager_mem_id, tenant_id=tenant_id, user_id=manager_user_id, mobile_e164="+919876543299", status="active")
    db.add(manager_mem)
    db.flush()

    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one()
    vizag_district = db.execute(
        select(District).where(District.state_id == ap_state.id, District.name == "Visakhapatnam")
    ).scalar_one()
    krishna_district = db.execute(
        select(District).where(District.state_id == ap_state.id, District.name == "Krishna")
    ).scalar_one()

    # Assign Manager to Visakhapatnam only
    LocationService.save_manager_location_assignments(
        session=db,
        tenant_id=tenant_id,
        user_id=manager_user_id,
        configs=[
            StateScopeConfig(
                state_id=ap_state.id,
                all_districts=False,
                district_ids=[vizag_district.id],
            )
        ],
    )
    db.flush()

    # 1. Manager attempts to create agent in Krishna (out of scope) -> 403 LOCATION_OUT_OF_SCOPE
    out_of_scope_req = AgentCreateRequest(
        first_name="Ravi",
        last_name="Teja",
        username=f"ravi_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"ravi_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9198{uuid4().int % 100000000:08d}",
        state="Andhra Pradesh",
        state_id=ap_state.id,
        district="Krishna",
        district_id=krishna_district.id,
        aadhaar_number="123456789012",
        pan_number="ABCDE1234F",
        profile_photo_key="photos/p1.jpg",
        aadhaar_document_key="docs/a1.jpg",
        pan_document_key="docs/pan1.jpg",
    )

    with pytest.raises(HTTPException) as exc_info:
        _create_agent_in_community(
            session=db,
            tenant=tenant,
            body=out_of_scope_req,
            created_by_user_id=manager_user_id,
            created_by_username="test_manager",
            created_by_membership_id=manager_mem_id,
            caller_role="admin",
            settings=Settings(otp_delivery_mode="mock"),
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "LOCATION_OUT_OF_SCOPE"

    # 2. Manager creates agent in Visakhapatnam (in scope) -> Succeeds and auto-assigns manager
    p_key, a_key, pan_key = seed_test_stored_objects(db, tenant.id)
    in_scope_req = AgentCreateRequest(
        first_name="Suresh",
        last_name="Raina",
        username=f"suresh_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"suresh_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9197{uuid4().int % 100000000:08d}",
        state="Andhra Pradesh",
        state_id=ap_state.id,
        district="Visakhapatnam",
        district_id=vizag_district.id,
        aadhaar_number="234567890123",
        pan_number="BCDEF2345G",
        profile_photo_key=p_key,
        aadhaar_document_key=a_key,
        pan_document_key=pan_key,
    )
    in_scope_req.email_verification_token = make_email_token(db, in_scope_req.email, tenant.id)

    agent_summary = _create_agent_in_community(
        session=db,
        tenant=tenant,
        body=in_scope_req,
        created_by_user_id=manager_user_id,
        created_by_username="test_manager",
        created_by_membership_id=manager_mem_id,
        caller_role="admin",
        settings=Settings(otp_delivery_mode="mock"),
    )
    db.flush()

    assert agent_summary.first_name == "Suresh"
    # Verify manager_user_id on AgentProfile
    agent_profile = db.execute(
        select(AgentProfile).where(AgentProfile.user_id == agent_summary.user_id)
    ).scalar_one()
    assert agent_profile.manager_user_id == manager_user_id
    assert agent_profile.district_id == vizag_district.id

    # Verify MANAGES relationship exists
    rel = db.execute(
        select(MemberRelationship).where(
            MemberRelationship.tenant_id == tenant_id,
            MemberRelationship.parent_member_id == manager_mem_id,
            MemberRelationship.child_member_id == agent_profile.membership_id,
            MemberRelationship.relationship_type == RelationshipType.MANAGES.value,
        )
    ).scalar_one_or_none()
    assert rel is not None
    assert rel.status == RelationshipStatus.ACTIVE.value


def test_super_admin_agent_creation_manager_allocation_rules(db: Session):
    """Test 4: Super Admin Agent creation manager allocation:
    - 1 match: auto-assigns
    - >1 matches: 400 AMBIGUOUS_MANAGER_ASSIGNMENT
    - 0 matches: 400 NO_ELIGIBLE_MANAGER
    - Scope mismatch: 400 MANAGER_SCOPE_MISMATCH
    """
    tenant_id = uuid4()
    tenant = Tenant(id=tenant_id, slug=f"sa-comm-{uuid4().hex[:6]}", status="active")
    db.add(tenant)
    db.flush()

    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one()
    vizag_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Visakhapatnam")).scalar_one()
    guntur_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Guntur")).scalar_one()
    chittoor_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Chittoor")).scalar_one()

    # Manager 1: Assigned to Visakhapatnam AND Guntur
    m1_user_id = uuid4()
    m1_mem_id = uuid4()
    db.add(User(id=m1_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=m1_mem_id, tenant_id=tenant_id, user_id=m1_user_id, mobile_e164="+919811111111", status="active"))
    db.add(AdminProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=m1_user_id,
        first_name="Manager",
        last_name="One",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    LocationService.save_manager_location_assignments(
        session=db,
        tenant_id=tenant_id,
        user_id=m1_user_id,
        configs=[StateScopeConfig(state_id=ap_state.id, all_districts=False, district_ids=[vizag_dist.id, guntur_dist.id])],
    )

    # Manager 2: Also assigned to Guntur (overlap on Guntur!)
    m2_user_id = uuid4()
    m2_mem_id = uuid4()
    db.add(User(id=m2_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=m2_mem_id, tenant_id=tenant_id, user_id=m2_user_id, mobile_e164="+919822222222", status="active"))
    db.add(AdminProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=m2_user_id,
        first_name="Manager",
        last_name="Two",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    LocationService.save_manager_location_assignments(
        session=db,
        tenant_id=tenant_id,
        user_id=m2_user_id,
        configs=[StateScopeConfig(state_id=ap_state.id, all_districts=False, district_ids=[guntur_dist.id])],
    )
    db.flush()

    super_admin_id = uuid4()
    db.add(User(id=super_admin_id, status="active"))
    db.flush()

    # Case A: Exactly 1 Manager covers Visakhapatnam (Manager 1) -> Auto-allocates to Manager 1
    pa_key, aa_key, pana_key = seed_test_stored_objects(db, tenant.id)
    req_a = AgentCreateRequest(
        first_name="Agent",
        last_name="A",
        username=f"agent_a_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"agent_a_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9191{uuid4().int % 100000000:08d}",
        state="Andhra Pradesh",
        state_id=ap_state.id,
        district="Visakhapatnam",
        district_id=vizag_dist.id,
        aadhaar_number="345678901234",
        pan_number="CDEFG3456H",
        profile_photo_key=pa_key,
        aadhaar_document_key=aa_key,
        pan_document_key=pana_key,
    )
    req_a.email_verification_token = make_email_token(db, req_a.email, tenant.id)
    created_a = _create_agent_in_community(
        session=db,
        tenant=tenant,
        body=req_a,
        created_by_user_id=super_admin_id,
        created_by_username="superadmin",
        caller_role="SUPER_ADMIN",
        settings=Settings(otp_delivery_mode="mock"),
    )
    db.flush()
    prof_a = db.execute(select(AgentProfile).where(AgentProfile.user_id == created_a.user_id)).scalar_one()
    assert prof_a.manager_user_id == m1_user_id, "Should auto-allocate to Manager 1"

    # Case B: Multiple managers cover Guntur (Manager 1 & 2) without explicit manager -> 400 AMBIGUOUS_MANAGER_ASSIGNMENT
    pb_key, ab_key, panb_key = seed_test_stored_objects(db, tenant.id)
    req_b = AgentCreateRequest(
        first_name="Agent",
        last_name="B",
        username=f"agent_b_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"agent_b_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9192{uuid4().int % 100000000:08d}",
        state="Andhra Pradesh",
        state_id=ap_state.id,
        district="Guntur",
        district_id=guntur_dist.id,
        aadhaar_number="456789012345",
        pan_number="DEFGH4567I",
        profile_photo_key=pb_key,
        aadhaar_document_key=ab_key,
        pan_document_key=panb_key,
    )
    with pytest.raises(HTTPException) as exc_b:
        _create_agent_in_community(
            session=db,
            tenant=tenant,
            body=req_b,
            created_by_user_id=super_admin_id,
            created_by_username="superadmin",
            caller_role="SUPER_ADMIN",
            settings=Settings(otp_delivery_mode="mock"),
        )
    assert exc_b.value.status_code == 400
    assert exc_b.value.detail["error"] == "AMBIGUOUS_MANAGER_ASSIGNMENT"

    # Case C: Multiple managers cover Guntur, but Super Admin explicitly picks Manager 2 -> Succeeds
    req_b.manager_user_id = m2_user_id
    req_b.email_verification_token = make_email_token(db, req_b.email, tenant.id)
    created_b = _create_agent_in_community(
        session=db,
        tenant=tenant,
        body=req_b,
        created_by_user_id=super_admin_id,
        created_by_username="superadmin",
        caller_role="SUPER_ADMIN",
        settings=Settings(otp_delivery_mode="mock"),
    )
    db.flush()
    prof_b = db.execute(select(AgentProfile).where(AgentProfile.user_id == created_b.user_id)).scalar_one()
    assert prof_b.manager_user_id == m2_user_id, "Should be assigned to Manager 2"

    # Case D: Super Admin explicitly picks Manager 2 for Visakhapatnam (where M2 has no scope) -> 400 MANAGER_SCOPE_MISMATCH
    req_d = AgentCreateRequest(
        first_name="Agent",
        last_name="D",
        username=f"agent_d_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"agent_d_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9193{uuid4().int % 100000000:08d}",
        state="Andhra Pradesh",
        state_id=ap_state.id,
        district="Visakhapatnam",
        district_id=vizag_dist.id,
        manager_user_id=m2_user_id,  # M2 only covers Guntur!
        aadhaar_number="567890123456",
        pan_number="EFGHI5678J",
        profile_photo_key="p_d.jpg",
        aadhaar_document_key="a_d.jpg",
        pan_document_key="pan_d.jpg",
    )
    with pytest.raises(HTTPException) as exc_d:
        _create_agent_in_community(
            session=db,
            tenant=tenant,
            body=req_d,
            created_by_user_id=super_admin_id,
            created_by_username="superadmin",
            caller_role="SUPER_ADMIN",
            settings=Settings(otp_delivery_mode="mock"),
        )
    assert exc_d.value.status_code == 400
    assert exc_d.value.detail["error"] == "MANAGER_SCOPE_MISMATCH"

    # Case E: No manager covers Chittoor -> 400 NO_ELIGIBLE_MANAGER
    req_e = AgentCreateRequest(
        first_name="Agent",
        last_name="E",
        username=f"agent_e_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"agent_e_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9194{uuid4().int % 100000000:08d}",
        state="Andhra Pradesh",
        state_id=ap_state.id,
        district="Chittoor",
        district_id=chittoor_dist.id,
        aadhaar_number="678901234567",
        pan_number="FGHIJ6789K",
        profile_photo_key="p_e.jpg",
        aadhaar_document_key="a_e.jpg",
        pan_document_key="pan_e.jpg",
    )
    with pytest.raises(HTTPException) as exc_e:
        _create_agent_in_community(
            session=db,
            tenant=tenant,
            body=req_e,
            created_by_user_id=super_admin_id,
            created_by_username="superadmin",
            caller_role="SUPER_ADMIN",
            settings=Settings(otp_delivery_mode="mock"),
        )
    assert exc_e.value.status_code == 400
    assert exc_e.value.detail["error"] == "NO_ELIGIBLE_MANAGER"


def test_three_level_hierarchy_drilldown(db: Session):
    """Test 5: Three-Level Hierarchy Drill-Down (Level 1 Managers -> Level 2 Agents -> Level 3 Subscribers)."""
    tenant_id = uuid4()
    tenant = Tenant(id=tenant_id, slug=f"drilldown-{uuid4().hex[:6]}", status="active")
    db.add(tenant)
    db.flush()

    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one()
    vizag_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Visakhapatnam")).scalar_one()

    # 1. Create Manager
    m_user_id = uuid4()
    m_mem_id = uuid4()
    db.add(User(id=m_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=m_mem_id, tenant_id=tenant_id, user_id=m_user_id, mobile_e164="+919800000001", status="active"))
    db.add(AdminProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=m_user_id,
        first_name="Hierarchy",
        last_name="Manager",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    LocationService.save_manager_location_assignments(
        session=db,
        tenant_id=tenant_id,
        user_id=m_user_id,
        configs=[StateScopeConfig(state_id=ap_state.id, all_districts=True, district_ids=[])],
    )
    db.flush()

    # 2. Create Agent under Manager
    a_user_id = uuid4()
    a_mem_id = uuid4()
    a_prof_id = uuid4()
    db.add(User(id=a_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=a_mem_id, tenant_id=tenant_id, user_id=a_user_id, mobile_e164="+919800000002", status="active"))
    db.flush()
    db.add(AgentProfile(
        id=a_prof_id,
        tenant_id=tenant_id,
        membership_id=a_mem_id,
        user_id=a_user_id,
        manager_user_id=m_user_id,
        first_name="Hierarchy",
        last_name="Agent",
        ref_id=f"AGT-{uuid4().hex[:6]}",
        profile_photo_key="photos/p.jpg",
        aadhaar_number_ciphertext="enc_aadhaar",
        aadhaar_number_masked="XXXXXXXX1234",
        aadhaar_document_key="docs/a.jpg",
        pan_number_ciphertext="enc_pan",
        pan_number_masked="XXXXX1234X",
        pan_document_key="docs/pan.jpg",
        state="Andhra Pradesh",
        district="Visakhapatnam",
        state_id=ap_state.id,
        district_id=vizag_dist.id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    # 3. Create Subscribers under Agent
    s1_user_id = uuid4()
    s1_mem_id = uuid4()
    s2_user_id = uuid4()
    s2_mem_id = uuid4()
    db.add(User(id=s1_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=s1_mem_id, tenant_id=tenant_id, user_id=s1_user_id, mobile_e164="+919800000003", status="active"))
    db.flush()
    db.add(SubscriberProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        membership_id=s1_mem_id,
        user_id=s1_user_id,
        registered_by_agent_membership_id=a_mem_id,
        full_name="Subscriber One",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    db.add(User(id=s2_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=s2_mem_id, tenant_id=tenant_id, user_id=s2_user_id, mobile_e164="+919800000004", status="active"))
    db.flush()
    db.add(SubscriberProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        membership_id=s2_mem_id,
        user_id=s2_user_id,
        registered_by_agent_membership_id=a_mem_id,
        full_name="Subscriber Two",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    operator = ResolvedSession(
        session_id=uuid4(),
        account_type="platform_operator",
        user_id=uuid4(),
        username="superadmin",
        role="SUPER_ADMIN",
        status="active",
        csrf_token="csrf123",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=None,
        membership_id=None,
    )

    # Level 1: List Managers for community
    mgr_result = list_community_managers(
        community_id=tenant_id,
        operator=operator,
        session=db,
    )
    assert mgr_result.total == 1
    manager_item = mgr_result.items[0]
    assert manager_item.user_id == m_user_id
    assert manager_item.agent_count == 1
    assert manager_item.subscriber_count == 2

    # Level 2: List Agents under Manager
    agent_result = list_manager_agents(
        community_id=tenant_id,
        manager_id=m_user_id,
        operator=operator,
        session=db,
    )
    assert agent_result.total == 1
    agent_item = agent_result.items[0]
    assert agent_item.user_id == a_user_id
    assert agent_item.subscriber_count == 2
    assert agent_item.state == "Andhra Pradesh"
    assert agent_item.district == "Visakhapatnam"

    # Level 3: List Subscribers under Agent
    sub_result = list_agent_subscribers(
        community_id=tenant_id,
        agent_id=a_prof_id,
        operator=operator,
        session=db,
    )
    assert sub_result.total == 2
    sub_names = {s.full_name for s in sub_result.items}
    assert "Subscriber One" in sub_names
    assert "Subscriber Two" in sub_names


def test_two_stage_agent_creation_approval_workflow(db: Session):
    """Test 6: Two-Stage Agent Creation Approval Workflow:
    Stage 1: Field Agent requests child agent -> PENDING_MANAGER
    Stage 2 (Manager): Manager approves -> PENDING_SUPER_ADMIN
    Stage 2 (Super Admin): Super Admin approves -> APPROVED
    Fulfillment: Child agent created -> FULFILLED, hierarchy linkages established.
    """
    tenant_id = uuid4()
    tenant = Tenant(id=tenant_id, slug=f"dual-custody-{uuid4().hex[:6]}", status="active")
    db.add(tenant)
    db.flush()

    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one()
    vizag_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Visakhapatnam")).scalar_one()
    mandal = db.execute(select(Mandal).where(Mandal.district_id == vizag_dist.id)).scalars().first()

    # Manager
    m_user_id = uuid4()
    m_mem_id = uuid4()
    db.add(User(id=m_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=m_mem_id, tenant_id=tenant_id, user_id=m_user_id, mobile_e164="+919855555551", status="active"))
    db.flush()

    LocationService.save_manager_location_assignments(
        session=db,
        tenant_id=tenant_id,
        user_id=m_user_id,
        configs=[StateScopeConfig(state_id=ap_state.id, all_districts=True, district_ids=[])],
    )
    db.flush()

    # Field Agent (Parent)
    a_user_id = uuid4()
    a_mem_id = uuid4()
    a_prof_id = uuid4()
    db.add(User(id=a_user_id, status="active"))
    db.flush()
    db.add(TenantMembership(id=a_mem_id, tenant_id=tenant_id, user_id=a_user_id, mobile_e164="+919855555552", status="active"))
    db.flush()
    db.add(AgentProfile(
        id=a_prof_id,
        tenant_id=tenant_id,
        membership_id=a_mem_id,
        user_id=a_user_id,
        manager_user_id=m_user_id,
        first_name="Field",
        last_name="Leader",
        ref_id=f"AGT-{uuid4().hex[:6]}",
        profile_photo_key="photos/leader.jpg",
        aadhaar_number_ciphertext="enc_aadhaar",
        aadhaar_number_masked="XXXXXXXX9999",
        aadhaar_document_key="docs/leader_a.jpg",
        pan_number_ciphertext="enc_pan",
        pan_number_masked="XXXXX9999X",
        pan_document_key="docs/leader_pan.jpg",
        state="Andhra Pradesh",
        district="Visakhapatnam",
        state_id=ap_state.id,
        district_id=vizag_dist.id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    db.flush()

    agent_session = ResolvedSession(
        session_id=uuid4(),
        account_type="community_member",
        user_id=a_user_id,
        username="field_leader",
        role="agent",
        status="active",
        csrf_token="csrf123",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=tenant_id,
        membership_id=a_mem_id,
    )
    manager_session = ResolvedSession(
        session_id=uuid4(),
        account_type="community_member",
        user_id=m_user_id,
        username="territory_mgr",
        role="admin",
        status="active",
        csrf_token="csrf123",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=tenant_id,
        membership_id=m_mem_id,
    )
    sa_session = ResolvedSession(
        session_id=uuid4(),
        account_type="platform_operator",
        user_id=uuid4(),
        username="platform_admin",
        role="SUPER_ADMIN",
        status="active",
        csrf_token="csrf123",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=None,
        membership_id=None,
    )

    # 1. Agent submits creation request
    req_body = AgentCreationRequestSubmit(
        state_id=ap_state.id,
        district_id=vizag_dist.id,
        mandal_id=mandal.id,
        reason="Expansion in mandal territory to cover rural subscribers",
    )
    created_req = request_agent_creation_permission(
        body=req_body,
        session_data=(agent_session, "dummy_token"),
        session=db,
    )
    db.flush()

    assert created_req.status == "PENDING_MANAGER"
    assert created_req.state_name == ap_state.name
    assert created_req.district_name == vizag_dist.name
    assert created_req.mandal_name == mandal.name
    assert created_req.manager_user_id == m_user_id

    # 2. Manager Endorsement
    mgr_review = AgentCreationRequestReview(
        decision="APPROVE",
        comment="Endorsed. Territory requires coverage.",
    )
    endorsed_req = manager_review_creation_request(
        request_id=created_req.id,
        body=mgr_review,
        session_data=(manager_session, "dummy_token"),
        session=db,
    )
    db.flush()

    assert endorsed_req.status == "PENDING_SUPER_ADMIN"
    assert endorsed_req.manager_decision == "APPROVED"
    assert endorsed_req.manager_comment == "Endorsed. Territory requires coverage."

    # 3. Super Admin Dual-Custody Approval
    sa_review = AgentCreationRequestReview(
        decision="APPROVE",
        comment="Approved for onboarding.",
    )
    approved_req = superadmin_review_creation_request(
        request_id=created_req.id,
        body=sa_review,
        operator=sa_session,
        session=db,
    )
    db.flush()

    assert approved_req.status == "APPROVED"
    assert approved_req.superadmin_decision == "APPROVED"

    # 4. Field Agent registers Child Agent using approved request
    scope_mock_request = Request({"type": "http", "app": None})
    pc_key, ac_key, panc_key = seed_test_stored_objects(db, tenant_id)
    child_body = AgentCreateRequest(
        first_name="Kiran",
        last_name="Reddy",
        username=f"kiran_{uuid4().hex[:6]}",
        password="Password123!",
        email=f"kiran_{uuid4().hex[:6]}@example.com",
        mobile_e164=f"+9198{uuid4().int % 100000000:08d}",
        state=ap_state.name,
        state_id=ap_state.id,
        district=vizag_dist.name,
        district_id=vizag_dist.id,
        mandal=mandal.name,
        mandal_id=mandal.id,
        aadhaar_number="789012345678",
        pan_number="GHIJK7890L",
        profile_photo_key=pc_key,
        aadhaar_document_key=ac_key,
        pan_document_key=panc_key,
    )

    child_summary = create_child_agent_from_approved_request(
        creation_request_id=created_req.id,
        body=child_body,
        session_data=(agent_session, "dummy_token"),
        request=scope_mock_request,
        session=db,
    )
    db.flush()

    assert child_summary.first_name == "Kiran"
    assert child_summary.state == "Andhra Pradesh"
    assert child_summary.district == "Visakhapatnam"

    # Verify request status transitioned to FULFILLED
    final_req = db.execute(select(AgentCreationRequest).where(AgentCreationRequest.id == created_req.id)).scalar_one()
    assert final_req.status == "FULFILLED"
    assert final_req.fulfilled_agent_profile_id is not None

    # Verify child agent profile linkages
    child_profile = db.execute(select(AgentProfile).where(AgentProfile.user_id == child_summary.user_id)).scalar_one()
    assert child_profile.manager_user_id == m_user_id
    assert child_profile.parent_agent_membership_id == a_mem_id

    # Verify MemberRelationships
    child_to_parent_rel = db.execute(
        select(MemberRelationship).where(
            MemberRelationship.tenant_id == tenant_id,
            MemberRelationship.parent_member_id == a_mem_id,
            MemberRelationship.child_member_id == child_profile.membership_id,
            MemberRelationship.relationship_type == RelationshipType.REPORTS_TO.value,
        )
    ).scalar_one_or_none()
    assert child_to_parent_rel is not None
    assert child_to_parent_rel.status == RelationshipStatus.ACTIVE.value


def test_village_endpoint_removed_and_fails_closed():
    """Verify that village endpoints are completely removed from the FastAPI router and return 404."""
    client = TestClient(app)

    # 1. Villages endpoint must fail closed with 404 Not Found
    res = client.get("/api/v1/locations/villages")
    assert res.status_code == 404, f"Expected 404 Not Found for removed villages endpoint, got {res.status_code}"

    # 2. Supported hierarchy endpoints: States, Districts, Mandals must function
    states_res = client.get("/api/v1/locations/states")
    assert states_res.status_code == 200
    states = states_res.json()
    assert len(states) == 2
    ap_id = next(s["id"] for s in states if s["code"] == "AP")

    districts_res = client.get(f"/api/v1/locations/districts?state_id={ap_id}")
    assert districts_res.status_code == 200
    districts = districts_res.json()
    assert len(districts) == 28
    vizag_id = next(d["id"] for d in districts if d["name"] == "Visakhapatnam")

    mandals_res = client.get(f"/api/v1/locations/mandals?district_id={vizag_id}")
    assert mandals_res.status_code == 200
    mandals = mandals_res.json()
    assert len(mandals) > 0


def test_hierarchy_boundary_enforced_at_mandal_level(db: Session):
    """Verify that location validations enforce State -> District -> Mandal and reject missing or mismatched mandal."""
    ap_state = db.execute(select(State).where(State.code == "AP")).scalar_one()
    vizag_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Visakhapatnam")).scalar_one()
    vizag_mandal = db.execute(select(Mandal).where(Mandal.district_id == vizag_dist.id)).scalars().first()

    # Valid hierarchy: State -> District -> Mandal
    state, district, mandal = LocationService.validate_location_hierarchy(
        session=db,
        state_id=ap_state.id,
        district_id=vizag_dist.id,
        mandal_id=vizag_mandal.id,
    )
    assert state.name == "Andhra Pradesh"
    assert district.name == "Visakhapatnam"
    assert mandal.id == vizag_mandal.id

    # Incompatible Mandal -> District fails with 400 INVALID_MANDAL
    guntur_dist = db.execute(select(District).where(District.state_id == ap_state.id, District.name == "Guntur")).scalar_one()
    with pytest.raises(HTTPException) as exc_info:
        LocationService.validate_location_hierarchy(
            session=db,
            state_id=ap_state.id,
            district_id=guntur_dist.id,
            mandal_id=vizag_mandal.id,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "INVALID_MANDAL"

