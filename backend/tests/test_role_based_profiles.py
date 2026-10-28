"""Comprehensive Automated Tests for Role-Based Profile Viewing and Editing.

Verifies:
1. Super Admin view & update self (full_name, email, profile_photo_key, mobile_e164 via OTP).
2. Super Admin can view & update subordinate profiles (Managers, Agents, Child Agents, Subscribers).
3. Manager view & update self (first_name, last_name, email, whatsapp_number, address, profile_photo_key).
4. Manager can edit Agents assigned directly or located within Manager's geographic territory (Mandal).
5. Manager CANNOT edit Agents of other Managers outside territory (403 FORBIDDEN).
6. Manager CANNOT edit Super Admins or unrelated Managers (403 FORBIDDEN).
7. Agent view & update self.
8. Agent can view & edit Subscribers registered under them (registered_by_agent_membership_id == agent.membership_id).
9. Agent CANNOT edit Subscribers registered by other agents (403 FORBIDDEN).
10. Agent CANNOT edit Managers, Super Admins, or other Agents (403 FORBIDDEN).
11. Child Agent hierarchy: Child Agent can edit self; Parent Agent can edit Child Agent; Manager over Child Agent can edit Child Agent.
12. Subscriber can view self and edit permitted personal fields (first_name, last_name, email, address, photo).
13. Sensitive field tampering protection: attempting to edit role, status, tenant_id, mandal_id, commission_rate_override fails with 422.
14. Mobile number changes strictly require valid single-use OTP verification token.
15. Replaying or tampering with verification token fails with 400.
16. Duplicate mobile numbers in community/platform scope are rejected with 409 Conflict.
17. Audit logging: every successful profile update records an audit event with action="auth.profile_updated".
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.main import app
from app.db.session import get_db, get_session_factory
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.repository import ResolvedSession
from app.modules.identity.models import User, TenantMembership
from app.modules.platform_identity.models import PlatformOperator
from app.modules.tenants.models import Tenant, AdminProfile
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.locations.models import State, District, Mandal, ManagerLocationAssignment
from app.modules.profiles.schemas import (
    ProfileUpdateRequest,
    MobileChangeRequest,
    MobileChangeVerifyRequest,
)
from app.modules.audit.models import AuditEvent


@pytest.fixture(scope="module")
def db_session_factory():
    return get_session_factory()


@pytest.fixture
def db(db_session_factory):
    session = db_session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test Data Fixtures & Helpers
# ---------------------------------------------------------------------------

def create_platform_operator(
    db: Session,
    *,
    full_name: str = "Platform Super Admin",
    email: str | None = None,
    mobile_e164: str | None = None,
) -> tuple[PlatformOperator, ResolvedSession]:
    mobile = mobile_e164 or f"+9199{uuid4().int % 100000000:08d}"
    em = email or f"sa_{uuid4().hex[:6]}@platform.test"
    op = PlatformOperator(
        id=uuid4(),
        issuer="https://platform.internal",
        subject=f"sub_{uuid4().hex[:12]}",
        status="active",
        mobile_e164=mobile,
        full_name=full_name,
        email=em,
        profile_photo_key=f"photo_{uuid4().hex[:8]}.jpg",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(op)
    db.flush()

    sess = ResolvedSession(
        session_id=uuid4(),
        account_type="platform_operator",
        user_id=op.id,
        username="superadmin",
        role="SUPER_ADMIN",
        status="active",
        user_status="active",
        csrf_token=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
        tenant_id=None,
        membership_id=None,
    )
    return op, sess


def create_tenant(db: Session, *, slug_prefix: str = "comm") -> Tenant:
    slug = f"{slug_prefix}_{uuid4().hex[:8]}"
    t = Tenant(
        id=uuid4(),
        slug=slug,
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(t)
    db.flush()
    return t


def create_manager(
    db: Session,
    tenant: Tenant,
    *,
    first_name: str = "Community",
    last_name: str = "Manager",
    mobile_e164: str | None = None,
) -> tuple[AdminProfile, ResolvedSession]:
    mobile = mobile_e164 or f"+9198{uuid4().int % 100000000:08d}"
    user = User(id=uuid4(), status="active")
    db.add(user)
    db.flush()

    mem = TenantMembership(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        mobile_e164=mobile,
        status="active",
    )
    db.add(mem)
    db.flush()

    admin = AdminProfile(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        first_name=first_name,
        last_name=last_name,
        email=f"{first_name.lower()}_{uuid4().hex[:6]}@community.test",
        whatsapp_number=mobile,
        address="Manager Headquarters, Suite 100",
        profile_photo_key=f"manager_photo_{uuid4().hex[:8]}.jpg",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(admin)
    db.flush()

    sess = ResolvedSession(
        session_id=uuid4(),
        account_type="tenant_user",
        user_id=user.id,
        username=f"admin_{uuid4().hex[:6]}",
        role="ADMIN",
        status="active",
        user_status="active",
        tenant_id=tenant.id,
        membership_id=mem.id,
        tenant_status="active",
        membership_status="active",
        csrf_token=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )
    return admin, sess


def create_agent(
    db: Session,
    tenant: Tenant,
    *,
    first_name: str = "Field",
    last_name: str = "Agent",
    manager_user_id: UUID | None = None,
    parent_agent_membership_id: UUID | None = None,
    role: str = "AGENT",
    state_id: int | None = None,
    district_id: int | None = None,
    mandal_id: int | None = None,
    mobile_e164: str | None = None,
) -> tuple[AgentProfile, ResolvedSession]:
    mobile = mobile_e164 or f"+9197{uuid4().int % 100000000:08d}"
    user = User(id=uuid4(), status="active")
    db.add(user)
    db.flush()

    mem = TenantMembership(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        mobile_e164=mobile,
        status="active",
    )
    db.add(mem)
    db.flush()

    agent = AgentProfile(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=mem.id,
        first_name=first_name,
        last_name=last_name,
        email=f"{first_name.lower()}_{uuid4().hex[:6]}@agent.test",
        whatsapp_number=mobile,
        address="Agent Field Post 42",
        ref_id=f"AG-{uuid4().hex[:6].upper()}",
        manager_user_id=manager_user_id,
        parent_agent_membership_id=parent_agent_membership_id,
        state_id=state_id,
        district_id=district_id,
        mandal_id=mandal_id,
        profile_photo_key=f"agent_photo_{uuid4().hex[:8]}.jpg",
        aadhaar_number_ciphertext="enc_aadhaar",
        aadhaar_number_masked="XXXX-XXXX-1122",
        aadhaar_document_key=f"aadhaar_{uuid4().hex[:8]}.jpg",
        pan_number_ciphertext="enc_pan",
        pan_number_masked="ABCDE1122F",
        pan_document_key=f"pan_{uuid4().hex[:8]}.jpg",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(agent)
    db.flush()

    sess = ResolvedSession(
        session_id=uuid4(),
        account_type="tenant_user",
        user_id=user.id,
        username=f"agent_{uuid4().hex[:6]}",
        role=role,
        status="active",
        user_status="active",
        tenant_id=tenant.id,
        membership_id=mem.id,
        tenant_status="active",
        membership_status="active",
        csrf_token=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )
    return agent, sess


def create_subscriber(
    db: Session,
    tenant: Tenant,
    registering_agent_mem_id: UUID,
    *,
    first_name: str = "Test",
    last_name: str = "Subscriber",
    mobile_e164: str | None = None,
) -> tuple[SubscriberProfile, ResolvedSession]:
    mobile = mobile_e164 or f"+9196{uuid4().int % 100000000:08d}"
    user = User(id=uuid4(), status="active")
    db.add(user)
    db.flush()

    mem = TenantMembership(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        mobile_e164=mobile,
        status="active",
    )
    db.add(mem)
    db.flush()

    sub = SubscriberProfile(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=mem.id,
        registered_by_agent_membership_id=registering_agent_mem_id,
        full_name=f"{first_name} {last_name}",
        email=f"{first_name.lower()}_{uuid4().hex[:6]}@subscriber.test",
        whatsapp_number=mobile,
        address="House 123, Cross Road",
        photo_key=f"sub_photo_{uuid4().hex[:8]}.jpg",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(sub)
    db.flush()

    sess = ResolvedSession(
        session_id=uuid4(),
        account_type="tenant_user",
        user_id=user.id,
        username=f"sub_{uuid4().hex[:6]}",
        role="MEMBER",
        status="active",
        user_status="active",
        tenant_id=tenant.id,
        membership_id=mem.id,
        tenant_status="active",
        membership_status="active",
        csrf_token=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )
    return sub, sess


def create_verified_mobile_otp_token(
    db: Session,
    mobile_e164: str,
    *,
    purpose: str = "profile_mobile_change",
) -> str:
    token = f"tok_verified_{uuid4().hex}"
    challenge = OtpChallenge(
        id=uuid4(),
        mobile_e164=mobile_e164,
        otp_hash="verified",
        otp_salt="verified",
        purpose=purpose,
        failed_attempts=0,
        resend_count=0,
        dispatch_id=uuid4(),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        verified_at=datetime.now(UTC),
        verification_token=token,
    )
    db.add(challenge)
    db.flush()
    return token


def set_auth(session: ResolvedSession):
    app.dependency_overrides[get_current_resolved_session] = lambda: (session, "test_raw_token")


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_super_admin_view_and_edit_self(db: Session, client: TestClient):
    """Super Admin can view and update their own full_name, email, and photo."""
    op, session = create_platform_operator(db, full_name="Original Super Admin", email="original_sa@test.com")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(session)
    try:
        # 1. View /me
        res = client.get("/api/v1/profile/me")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["role"] == "SUPER_ADMIN"
        assert data["full_name"] == "Original Super Admin"
        assert data["email"] == "original_sa@test.com"
        assert "full_name" in data["editable_fields"]
        assert "email" in data["editable_fields"]
        assert "profile_photo_key" in data["editable_fields"]

        # 2. Update /me
        new_photo = f"new_op_photo_{uuid4().hex[:8]}.jpg"
        update_res = client.patch(
            "/api/v1/profile/me",
            json={
                "full_name": "Updated Super Admin",
                "email": "updated_sa@test.com",
                "profile_photo_key": new_photo,
            },
        )
        assert update_res.status_code == 200, update_res.text
        updated_data = update_res.json()
        assert updated_data["full_name"] == "Updated Super Admin"
        assert updated_data["email"] == "updated_sa@test.com"
        assert updated_data["profile_photo_key"] == new_photo

        # Verify DB persistence
        db.refresh(op)
        assert op.full_name == "Updated Super Admin"
        assert op.email == "updated_sa@test.com"
        assert op.profile_photo_key == new_photo
    finally:
        app.dependency_overrides.clear()


def test_super_admin_can_edit_manager_agent_subscriber(db: Session, client: TestClient):
    """Super Admin can view and edit subordinate Manager, Agent, and Subscriber."""
    op, sa_session = create_platform_operator(db)
    tenant = create_tenant(db)
    manager, _ = create_manager(db, tenant, first_name="Mgr1", last_name="Old")
    agent, _ = create_agent(db, tenant, first_name="Agt1", last_name="Old", manager_user_id=manager.user_id)
    subscriber, _ = create_subscriber(db, tenant, agent.membership_id, first_name="Sub1", last_name="Old")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(sa_session)
    try:
        # SA updates Manager
        res = client.patch(
            f"/api/v1/profile/{manager.id}",
            json={"first_name": "Mgr1Updated", "last_name": "New"},
        )
        assert res.status_code == 200, res.text
        db.refresh(manager)
        assert manager.first_name == "Mgr1Updated"
        assert manager.last_name == "New"

        # SA updates Agent
        res = client.patch(
            f"/api/v1/profile/{agent.id}",
            json={"first_name": "Agt1Updated", "address": "New Agency Office 9"},
        )
        assert res.status_code == 200, res.text
        db.refresh(agent)
        assert agent.first_name == "Agt1Updated"
        assert agent.address == "New Agency Office 9"

        # SA updates Subscriber
        res = client.patch(
            f"/api/v1/profile/{subscriber.id}",
            json={"first_name": "Sub1Updated", "address": "New Sub Villa 7"},
        )
        assert res.status_code == 200, res.text
        db.refresh(subscriber)
        assert "Sub1Updated" in subscriber.full_name
        assert subscriber.address == "New Sub Villa 7"
    finally:
        app.dependency_overrides.clear()


def test_manager_view_and_edit_self(db: Session, client: TestClient):
    """Manager can view and edit own profile details."""
    tenant = create_tenant(db)
    manager, m_session = create_manager(db, tenant, first_name="MgrSelf", last_name="Init")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(m_session)
    try:
        # View /me
        res = client.get("/api/v1/profile/me")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["role"] == "ADMIN"
        assert data["first_name"] == "MgrSelf"

        # Update /me
        res = client.patch(
            "/api/v1/profile/me",
            json={
                "first_name": "MgrSelfUpdated",
                "last_name": "Changed",
                "email": "mgr_new@test.com",
                "address": "New Office Blvd 5",
            },
        )
        assert res.status_code == 200, res.text
        db.refresh(manager)
        assert manager.first_name == "MgrSelfUpdated"
        assert manager.last_name == "Changed"
        assert manager.email == "mgr_new@test.com"
        assert manager.address == "New Office Blvd 5"
    finally:
        app.dependency_overrides.clear()


def test_manager_edit_assigned_agent_success(db: Session, client: TestClient):
    """Manager can edit Agent directly assigned to their manager_user_id."""
    tenant = create_tenant(db)
    manager, m_session = create_manager(db, tenant)
    agent, _ = create_agent(db, tenant, manager_user_id=manager.user_id, first_name="DirectAgent")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(m_session)
    try:
        res = client.patch(
            f"/api/v1/profile/{agent.id}",
            json={"first_name": "DirectAgentUpdated", "address": "North Sector 4"},
        )
        assert res.status_code == 200, res.text
        db.refresh(agent)
        assert agent.first_name == "DirectAgentUpdated"
        assert agent.address == "North Sector 4"
    finally:
        app.dependency_overrides.clear()


def test_manager_edit_territorial_agent_success(db: Session, client: TestClient):
    """Manager can edit Agent located in their assigned geographic territory (Mandal)."""
    tenant = create_tenant(db)
    manager, m_session = create_manager(db, tenant)

    # Setup location master: State -> District -> Mandal
    state = db.execute(select(State).limit(1)).scalar_one_or_none()
    if not state:
        state = State(id=9901, code="AP", name="Andhra Pradesh")
        db.add(state)
        db.flush()
    dist = db.execute(select(District).where(District.state_id == state.id).limit(1)).scalar_one_or_none()
    if not dist:
        dist = District(id=9902, state_id=state.id, code="VSKP", name="Visakhapatnam")
        db.add(dist)
        db.flush()
    mandal = db.execute(select(Mandal).where(Mandal.district_id == dist.id).limit(1)).scalar_one_or_none()
    if not mandal:
        mandal = Mandal(id=9903, district_id=dist.id, code="BML", name="Bheemili")
        db.add(mandal)
        db.flush()

    # Assign Manager to this Mandal
    assign = ManagerLocationAssignment(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=manager.user_id,
        state_id=state.id,
        district_id=dist.id,
        mandal_id=mandal.id,
        all_districts=False,
        all_mandals=False,
        status="active",
        created_at=datetime.now(UTC),
    )
    db.add(assign)
    db.flush()

    # Agent is in this mandal (even if manager_user_id was not explicitly set)
    agent, _ = create_agent(
        db,
        tenant,
        state_id=state.id,
        district_id=dist.id,
        mandal_id=mandal.id,
        first_name="TerritorialAgent",
    )

    app.dependency_overrides[get_db] = lambda: db
    set_auth(m_session)
    try:
        res = client.patch(
            f"/api/v1/profile/{agent.id}",
            json={"first_name": "TerritorialAgentUpdated"},
        )
        assert res.status_code == 200, res.text
        db.refresh(agent)
        assert agent.first_name == "TerritorialAgentUpdated"
    finally:
        app.dependency_overrides.clear()


def test_manager_forbidden_to_edit_unrelated_agent(db: Session, client: TestClient):
    """Manager CANNOT edit an Agent assigned to another manager in a different community or territory."""
    tenant_a = create_tenant(db, slug_prefix="comm_a")
    tenant_b = create_tenant(db, slug_prefix="comm_b")

    manager_a, m_session_a = create_manager(db, tenant_a)
    manager_b, _ = create_manager(db, tenant_b)
    agent_b, _ = create_agent(db, tenant_b, manager_user_id=manager_b.user_id)

    app.dependency_overrides[get_db] = lambda: db
    set_auth(m_session_a)
    try:
        # Cross-tenant agent edit must return 403
        res = client.patch(
            f"/api/v1/profile/{agent_b.id}",
            json={"first_name": "HackedName"},
        )
        assert res.status_code == 403, res.text
        assert "not authorized" in res.text.lower() or "forbidden" in res.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_manager_forbidden_to_edit_super_admin_or_peer_manager(db: Session, client: TestClient):
    """Manager CANNOT edit Super Admin or another Manager."""
    op, _ = create_platform_operator(db)
    tenant = create_tenant(db)
    manager_1, m1_session = create_manager(db, tenant, first_name="M1")
    manager_2, _ = create_manager(db, tenant, first_name="M2")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(m1_session)
    try:
        # Manager edits Super Admin -> 403
        res = client.patch(
            f"/api/v1/profile/{op.id}",
            json={"full_name": "HackedSA"},
        )
        assert res.status_code == 403, res.text

        # Manager edits another Manager -> 403
        res = client.patch(
            f"/api/v1/profile/{manager_2.id}",
            json={"first_name": "HackedM2"},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.clear()


def test_agent_view_and_edit_self(db: Session, client: TestClient):
    """Agent can view and edit own permitted profile fields."""
    tenant = create_tenant(db)
    agent, a_session = create_agent(db, tenant, first_name="AgentSelf")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a_session)
    try:
        res = client.get("/api/v1/profile/me")
        assert res.status_code == 200, res.text
        assert res.json()["first_name"] == "AgentSelf"

        res = client.patch(
            "/api/v1/profile/me",
            json={
                "first_name": "AgentSelfUpdated",
                "email": "agent_self_new@test.com",
                "address": "Booth 11, Green Hills",
            },
        )
        assert res.status_code == 200, res.text
        db.refresh(agent)
        assert agent.first_name == "AgentSelfUpdated"
        assert agent.email == "agent_self_new@test.com"
        assert agent.address == "Booth 11, Green Hills"
    finally:
        app.dependency_overrides.clear()


def test_agent_edit_own_subscriber_success(db: Session, client: TestClient):
    """Agent can edit Subscriber registered by them."""
    tenant = create_tenant(db)
    agent, a_session = create_agent(db, tenant)
    sub, _ = create_subscriber(db, tenant, agent.membership_id, first_name="SubMine")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a_session)
    try:
        res = client.patch(
            f"/api/v1/profile/{sub.id}",
            json={"first_name": "SubMineUpdated", "address": "Plot 99, Rose Garden"},
        )
        assert res.status_code == 200, res.text
        db.refresh(sub)
        assert "SubMineUpdated" in sub.full_name
        assert sub.address == "Plot 99, Rose Garden"
    finally:
        app.dependency_overrides.clear()


def test_agent_forbidden_to_edit_unrelated_subscriber(db: Session, client: TestClient):
    """Agent CANNOT edit a Subscriber registered by a different Agent."""
    tenant = create_tenant(db)
    agent_1, a1_session = create_agent(db, tenant, first_name="Agent1")
    agent_2, _ = create_agent(db, tenant, first_name="Agent2")
    sub_of_agent_2, _ = create_subscriber(db, tenant, agent_2.membership_id, first_name="SubOfAgt2")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a1_session)
    try:
        res = client.patch(
            f"/api/v1/profile/{sub_of_agent_2.id}",
            json={"first_name": "Tampered"},
        )
        assert res.status_code == 403, res.text
        assert "not authorized" in res.text.lower() or "forbidden" in res.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_agent_forbidden_to_edit_manager_or_peer_agent(db: Session, client: TestClient):
    """Agent CANNOT edit Manager or other Agents."""
    tenant = create_tenant(db)
    manager, _ = create_manager(db, tenant)
    agent_1, a1_session = create_agent(db, tenant, manager_user_id=manager.user_id)
    agent_2, _ = create_agent(db, tenant, manager_user_id=manager.user_id)

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a1_session)
    try:
        # Agent edits Manager -> 403
        res = client.patch(
            f"/api/v1/profile/{manager.id}",
            json={"first_name": "TamperedMgr"},
        )
        assert res.status_code == 403, res.text

        # Agent edits peer Agent -> 403
        res = client.patch(
            f"/api/v1/profile/{agent_2.id}",
            json={"first_name": "TamperedPeer"},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.clear()


def test_child_agent_hierarchy_permissions(db: Session, client: TestClient):
    """Child Agent can edit self; Parent Agent can edit Child; Manager over Child can edit."""
    tenant = create_tenant(db)
    manager, m_session = create_manager(db, tenant)
    parent_agent, p_session = create_agent(db, tenant, manager_user_id=manager.user_id, first_name="ParentAgt")
    child_agent, c_session = create_agent(
        db,
        tenant,
        first_name="ChildAgt",
        manager_user_id=manager.user_id,
        parent_agent_membership_id=parent_agent.membership_id,
        role="CHILD_AGENT",
    )
    unrelated_agent, u_session = create_agent(db, tenant, first_name="UnrelatedAgt")

    # 1. Child Agent edits self
    app.dependency_overrides[get_db] = lambda: db
    set_auth(c_session)
    res = client.patch("/api/v1/profile/me", json={"first_name": "ChildSelfUpdated"})
    assert res.status_code == 200, res.text

    # 2. Parent Agent edits Child Agent -> 200
    set_auth(p_session)
    res = client.patch(f"/api/v1/profile/{child_agent.id}", json={"address": "ParentAssignedHQ"})
    assert res.status_code == 200, res.text

    # 3. Manager edits Child Agent -> 200
    set_auth(m_session)
    res = client.patch(f"/api/v1/profile/{child_agent.id}", json={"last_name": "MgrUpdatedLastName"})
    assert res.status_code == 200, res.text

    # 4. Unrelated Agent edits Child Agent -> 403
    set_auth(u_session)
    res = client.patch(f"/api/v1/profile/{child_agent.id}", json={"first_name": "IllegalHacker"})
    assert res.status_code == 403, res.text

    app.dependency_overrides.clear()


def test_subscriber_permitted_fields_only(db: Session, client: TestClient):
    """Subscriber can edit permitted personal fields (first_name, last_name, email, address)."""
    tenant = create_tenant(db)
    agent, _ = create_agent(db, tenant)
    sub, sub_session = create_subscriber(db, tenant, agent.membership_id, first_name="SubSelf")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(sub_session)
    try:
        res = client.get("/api/v1/profile/me")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["role"] == "MEMBER"
        assert set(data["editable_fields"]).issubset({"first_name", "last_name", "full_name", "email", "whatsapp_number", "address", "profile_photo_key", "mobile_e164"})

        # Edit permitted fields
        res = client.patch(
            "/api/v1/profile/me",
            json={
                "first_name": "SubSelfUpdated",
                "last_name": "Kumar",
                "email": "sub_updated@test.com",
                "address": "Apartment 4B, Hill View",
            },
        )
        assert res.status_code == 200, res.text
        db.refresh(sub)
        assert sub.full_name == "SubSelfUpdated Kumar"
        assert sub.email == "sub_updated@test.com"
        assert sub.address == "Apartment 4B, Hill View"
    finally:
        app.dependency_overrides.clear()


def test_sensitive_fields_cannot_be_tampered(db: Session, client: TestClient):
    """ProfileUpdateRequest strictly rejects sensitive fields (extra='forbid') with 422."""
    tenant = create_tenant(db)
    agent, a_session = create_agent(db, tenant)

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a_session)
    try:
        # Attempting to tamper with role
        res = client.patch("/api/v1/profile/me", json={"role": "SUPER_ADMIN"})
        assert res.status_code == 422, res.text

        # Attempting to tamper with status
        res = client.patch("/api/v1/profile/me", json={"status": "inactive"})
        assert res.status_code == 422, res.text

        # Attempting to tamper with tenant_id
        res = client.patch("/api/v1/profile/me", json={"tenant_id": str(uuid4())})
        assert res.status_code == 422, res.text

        # Attempting to tamper with commission rate
        res = client.patch("/api/v1/profile/me", json={"commission_rate_override": "50.00"})
        assert res.status_code == 422, res.text

        # Attempting to tamper with mandal_id
        res = client.patch("/api/v1/profile/me", json={"mandal_id": 12345})
        assert res.status_code == 422, res.text
    finally:
        app.dependency_overrides.clear()


def test_mobile_change_otp_challenge_and_verification(db: Session, client: TestClient):
    """Mobile number changes require valid OTP challenge and token verification."""
    tenant = create_tenant(db)
    agent, a_session = create_agent(db, tenant)
    new_mobile = f"+9197{uuid4().int % 100000000:08d}"
    replay_mobile = f"+9197{uuid4().int % 100000000:08d}"

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a_session)
    try:
        # Case A: Attempting to update mobile without verification token -> 400
        res = client.patch("/api/v1/profile/me", json={"mobile_e164": new_mobile})
        assert res.status_code == 400, res.text
        assert "verification" in res.text.lower()

        # Case B: Attempting with fake/tampered token -> 400
        res = client.patch(
            "/api/v1/profile/me",
            json={"mobile_e164": new_mobile, "mobile_verification_token": "fake_tok_123"},
        )
        assert res.status_code == 400, res.text

        # Case C: Valid verified token flow
        valid_tok = create_verified_mobile_otp_token(db, new_mobile)
        res = client.patch(
            "/api/v1/profile/me",
            json={"mobile_e164": new_mobile, "mobile_verification_token": valid_tok},
        )
        assert res.status_code == 200, res.text
        assert res.json()["mobile_e164"] == new_mobile
        mem = db.get(TenantMembership, agent.membership_id)
        assert mem is not None
        assert mem.mobile_e164 == new_mobile

        # Case D: Token reuse / replay -> must fail with 400 (consumed single-use)
        res_replay = client.patch(
            "/api/v1/profile/me",
            json={"mobile_e164": replay_mobile, "mobile_verification_token": valid_tok},
        )
        assert res_replay.status_code == 400, res_replay.text
    finally:
        app.dependency_overrides.clear()


def test_duplicate_mobile_rejected(db: Session, client: TestClient):
    """Updating mobile to an already registered number in the community fails with 409 DUPLICATE_MOBILE."""
    tenant = create_tenant(db)
    agent_1, a1_session = create_agent(db, tenant, mobile_e164="+919711111111")
    agent_2, _ = create_agent(db, tenant, mobile_e164="+919722222222")

    # Create token for agent 1 attempting to take agent 2's mobile
    tok = create_verified_mobile_otp_token(db, "+919722222222")

    app.dependency_overrides[get_db] = lambda: db
    set_auth(a1_session)
    try:
        res = client.patch(
            "/api/v1/profile/me",
            json={"mobile_e164": "+919722222222", "mobile_verification_token": tok},
        )
        assert res.status_code == 409, res.text
        assert "already in use" in res.text.lower() or "duplicate_mobile" in res.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_audit_event_logged_on_profile_update(db: Session, client: TestClient):
    """Every profile update records an audit event with action='auth.profile_updated'."""
    tenant = create_tenant(db)
    manager, m_session = create_manager(db, tenant)

    app.dependency_overrides[get_db] = lambda: db
    set_auth(m_session)
    try:
        res = client.patch(
            "/api/v1/profile/me",
            json={"first_name": "AuditTestedName"},
        )
        assert res.status_code == 200, res.text

        # Verify AuditEvent
        stmt = (
            select(AuditEvent)
            .where(
                AuditEvent.event_type == "auth.profile_updated",
                AuditEvent.actor_ref == str(manager.user_id),
            )
            .order_by(AuditEvent.recorded_at.desc())
        )
        event = db.execute(stmt).scalars().first()
        assert event is not None
        assert event.outcome == "success"
        assert event.decision_metadata.get("reason_code") == "profile_updated"
    finally:
        app.dependency_overrides.clear()
