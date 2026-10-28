from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.relationships.models import MemberRelationship
from app.modules.subscriptions.models import MemberSubscription
from app.modules.targets.models import (
    TargetAchievementAdjustment,
    TargetAssignment,
    TargetType,
)
from app.modules.tenants.models import AdminProfile, Tenant


@pytest.fixture
def targets_harness(tmp_path):
    database_path = tmp_path / "targets-test.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    # Session tokens & digests
    super_token = "super_target_token_123"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_csrf").hexdigest()

    admin_a_token = "admin_a_target_token_123"
    admin_a_digest = hashlib.sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = hashlib.sha256(b"admin_a_csrf").hexdigest()

    admin_b_token = "admin_b_target_token_456"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf").hexdigest()

    agent_token = "agent_target_token_789"
    agent_digest = hashlib.sha256(agent_token.encode("utf-8")).hexdigest()
    agent_csrf = hashlib.sha256(b"agent_csrf").hexdigest()

    super_op_id = uuid4()
    admin_a_user_id = uuid4()
    admin_a_mem_id = uuid4()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()
    agent_user_id = uuid4()
    agent_mem_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # 1. Seed candidate target types
        target_types = [
            TargetType(
                id=uuid4(),
                code="AGENTS_RECRUITED",
                name="Agents Recruited",
                description="Community agents appointed and approved",
                unit="agents",
                calculation_type="AUTOMATIC",
                applicable_role="ADMIN",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
            TargetType(
                id=uuid4(),
                code="USERS_REGISTERED",
                name="Users Registered",
                description="OTT subscribers registered and activated",
                unit="users",
                calculation_type="AUTOMATIC",
                applicable_role="BOTH",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
            TargetType(
                id=uuid4(),
                code="CONTENT_UPLOADED",
                name="Content Uploaded",
                description="Community content videos uploaded",
                unit="videos",
                calculation_type="MANUAL",
                applicable_role="BOTH",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
            TargetType(
                id=uuid4(),
                code="SUBSCRIPTIONS_GENERATED",
                name="Subscriptions Generated",
                description="Paid subscriptions initiated and active",
                unit="subscriptions",
                calculation_type="AUTOMATIC",
                applicable_role="BOTH",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
        ]
        session.add_all(target_types)

        # 2. Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="tenant-alpha",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="tenant-beta",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        # 3. Super Admin
        root_op_id = uuid4()
        root_op = PlatformOperator(
            id=root_op_id,
            issuer="https://identity.community-ott.local",
            subject="operator:root_system",
            status="active",
        )
        super_op = PlatformOperator(
            id=super_op_id,
            issuer="https://identity.community-ott.local",
            subject="operator:super_target",
            status="active",
        )
        super_role = PlatformRole(
            id=uuid4(),
            code="super_admin",
            display_name="Super Administrator",
            status="active",
        )
        session.add_all([root_op, super_op, super_role])
        session.flush()

        session.add(
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=super_op_id,
                role_id=super_role.id,
                assigned_by_operator_id=root_op_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_op_id,
                token_digest=super_digest,
                csrf_token=super_csrf,
                authenticated_at=now,
                acr="urn:community:pwd",
                amr=["pwd"],
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # 4. Roles & Permissions for tenants
        admin_role_a, agent_role_a = seed_tenant_role_permissions(session, tenant_a_id)
        admin_role_b, agent_role_b = seed_tenant_role_permissions(session, tenant_b_id)

        # 5. Admin A (Tenant Alpha)
        user_admin_a = User(id=admin_a_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_admin_a)
        session.flush()

        mem_admin_a = TenantMembership(
            id=admin_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a_user_id,
            mobile_e164="+919000000001",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_admin_a)
        session.flush()

        prof_admin_a = AdminProfile(
            id=uuid4(),
            tenant_id=tenant_a_id,
            user_id=admin_a_user_id,
            first_name="Alpha",
            last_name="Administrator",
            email="admin.alpha@ott.com",
            address="1 Alpha Way",
            created_at=now,
            updated_at=now,
        )
        cred_admin_a = AccountCredential(
            id=uuid4(),
            user_id=admin_a_user_id,
            username="admin_alpha",
            password_hash="mock_hash",
            account_type="tenant_user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        assign_admin_a = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=admin_a_mem_id,
            role_id=admin_role_a.id,
            assigned_by_membership_id=admin_a_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        session_admin_a = AuthSession(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=admin_a_mem_id,
            token_digest=admin_a_digest,
            csrf_token=admin_a_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([prof_admin_a, cred_admin_a, assign_admin_a, session_admin_a])
        session.flush()

        # 6. Admin B (Tenant Beta)
        user_admin_b = User(id=admin_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_admin_b)
        session.flush()

        mem_admin_b = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919000000002",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_admin_b)
        session.flush()

        prof_admin_b = AdminProfile(
            id=uuid4(),
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            first_name="Beta",
            last_name="Administrator",
            email="admin.beta@ott.com",
            address="2 Beta Blvd",
            created_at=now,
            updated_at=now,
        )
        cred_admin_b = AccountCredential(
            id=uuid4(),
            user_id=admin_b_user_id,
            username="admin_beta",
            password_hash="mock_hash",
            account_type="tenant_user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        assign_admin_b = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_b_id,
            membership_id=admin_b_mem_id,
            role_id=admin_role_b.id,
            assigned_by_membership_id=admin_b_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        session_admin_b = AuthSession(
            id=uuid4(),
            tenant_id=tenant_b_id,
            membership_id=admin_b_mem_id,
            token_digest=admin_b_digest,
            csrf_token=admin_b_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([prof_admin_b, cred_admin_b, assign_admin_b, session_admin_b])
        session.flush()

        # 7. Agent (under Admin A in Tenant Alpha)
        user_agent = User(id=agent_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_agent)
        session.flush()

        mem_agent = TenantMembership(
            id=agent_mem_id,
            tenant_id=tenant_a_id,
            user_id=agent_user_id,
            mobile_e164="+919000000003",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_agent)
        session.flush()

        prof_agent = AgentProfile(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent_mem_id,
            user_id=agent_user_id,
            first_name="Field",
            last_name="Agent",
            email="agent.field@ott.com",
            address="3 Field Road",
            ref_id="AGT-1001",
            commission_rate_override=None,
            profile_photo_key="agent_photo_key",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-1122",
            aadhaar_document_key="doc_aadhaar",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE1234F",
            pan_document_key="doc_pan",
            created_at=now,
            updated_at=now,
        )
        cred_agent = AccountCredential(
            id=uuid4(),
            user_id=agent_user_id,
            username="field_agent_1",
            password_hash="mock_hash",
            account_type="tenant_user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        assign_agent = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent_mem_id,
            role_id=agent_role_a.id,
            assigned_by_membership_id=admin_a_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        session_agent = AuthSession(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent_mem_id,
            token_digest=agent_digest,
            csrf_token=agent_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        rel_agent = MemberRelationship(
            id=uuid4(),
            tenant_id=tenant_a_id,
            parent_member_id=admin_a_mem_id,
            child_member_id=agent_mem_id,
            relationship_type="REPORTS_TO",
            status="ACTIVE",
            created_by_user_id=admin_a_user_id,
            created_at=now,
        )
        session.add_all([prof_agent, cred_agent, assign_agent, session_agent, rel_agent])
        session.commit()

    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000,http://testserver",
        auth_session_cookie_secure=False,
        identity_encryption_key="12345678901234567890123456789012",
    )
    app = create_app(test_settings)

    def db_override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_authentication_repository] = lambda: SQLAlchemyAuthenticationRepository(factory)
    app.dependency_overrides[get_authorization_repository] = lambda: SQLAlchemyAuthorizationRepository(factory)

    client = TestClient(app, base_url="http://testserver")

    return {
        "client": client,
        "factory": factory,
        "tokens": {
            "super": super_token,
            "admin_a": admin_a_token,
            "admin_b": admin_b_token,
            "agent": agent_token,
        },
        "users": {
            "super_op_id": super_op_id,
            "admin_a_id": admin_a_user_id,
            "admin_a_mem_id": admin_a_mem_id,
            "admin_b_id": admin_b_user_id,
            "admin_b_mem_id": admin_b_mem_id,
            "agent_id": agent_user_id,
            "agent_mem_id": agent_mem_id,
        },
        "tenants": {
            "tenant_a_id": tenant_a_id,
            "tenant_b_id": tenant_b_id,
        },
    }


def test_list_target_types(targets_harness):
    client = targets_harness["client"]
    token = targets_harness["tokens"]["super"]

    res = client.get(
        "/api/v1/targets/types",
        cookies={"community_session": token},
    )
    assert res.status_code == 200
    data = res.json()
    codes = {t["code"] for t in data}
    assert "AGENTS_RECRUITED" in codes
    assert "USERS_REGISTERED" in codes
    assert "CONTENT_UPLOADED" in codes
    assert "SUBSCRIPTIONS_GENERATED" in codes


def test_super_admin_create_admin_target(targets_harness):
    client = targets_harness["client"]
    super_token = targets_harness["tokens"]["super"]
    admin_token = targets_harness["tokens"]["admin_a"]
    admin_id = str(targets_harness["users"]["admin_a_id"])

    today = date.today()
    deadline = today + timedelta(days=30)

    # 1. Non-super admin fails with 403
    fail_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_id,
            "target_type_code": "AGENTS_RECRUITED",
            "target_quantity": 25,
            "period": "MONTHLY",
            "start_date": str(today),
            "deadline": str(deadline),
            "notes": "Q4 Target",
        },
        cookies={"community_session": admin_token},
    )
    assert fail_res.status_code == 403

    # 2. Invalid date order fails with 422
    invalid_dates_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_id,
            "target_type_code": "AGENTS_RECRUITED",
            "target_quantity": 25,
            "period": "MONTHLY",
            "start_date": str(deadline),
            "deadline": str(today),
        },
        cookies={"community_session": super_token},
    )
    assert invalid_dates_res.status_code == 422
    assert "start_date" in str(invalid_dates_res.json()).lower() or "deadline" in str(invalid_dates_res.json()).lower()

    # 3. Super admin succeeds
    res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_id,
            "target_type_code": "AGENTS_RECRUITED",
            "target_quantity": 25,
            "period": "MONTHLY",
            "start_date": str(today),
            "deadline": str(deadline),
            "notes": "Q4 Agent Recruitment Goal",
        },
        cookies={"community_session": super_token},
    )
    assert res.status_code == 201
    target = res.json()
    assert target["target_type_code"] == "AGENTS_RECRUITED"
    assert target["target_quantity"] == 25
    assert target["achieved_quantity"] == 0
    assert target["remaining_quantity"] == 25
    assert target["progress_percentage"] == 0.0
    assert target["status"] == "ACTIVE"
    assert target["assigned_to_role"] == "ADMIN"
    assert "Alpha Administrator" in target["assigned_to_name"]


def test_hierarchical_target_allocation_and_quota_limits(targets_harness):
    client = targets_harness["client"]
    super_token = targets_harness["tokens"]["super"]
    admin_a_token = targets_harness["tokens"]["admin_a"]
    admin_a_id = str(targets_harness["users"]["admin_a_id"])
    agent_id = str(targets_harness["users"]["agent_id"])

    today = date.today()
    deadline = today + timedelta(days=30)

    # 1. Super Admin creates parent target for Admin A for USERS_REGISTERED (quota: 50)
    parent_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_a_id,
            "target_type_code": "USERS_REGISTERED",
            "target_quantity": 50,
            "period": "MONTHLY",
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": super_token},
    )
    assert parent_res.status_code == 201
    parent_id = parent_res.json()["id"]

    # 2. Admin A allocates 30 to Agent (valid, <= 50)
    child_res = client.post(
        "/api/v1/targets/allocate",
        json={
            "parent_target_id": parent_id,
            "agent_user_id": agent_id,
            "target_quantity": 30,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": admin_a_token},
    )
    assert child_res.status_code == 201
    child = child_res.json()
    assert child["parent_target_id"] == parent_id
    assert child["target_quantity"] == 30
    assert child["assigned_to_role"] == "AGENT"

    # 3. Check parent target now reflects allocated_to_children = 30, remaining_to_allocate = 20
    parent_check = client.get(
        f"/api/v1/targets/{parent_id}",
        cookies={"community_session": admin_a_token},
    )
    assert parent_check.status_code == 200
    assert parent_check.json()["allocated_to_children"] == 30
    assert parent_check.json()["remaining_to_allocate"] == 20

    # 4. Over-allocation: Admin A tries to allocate another 25 to Agent (30 + 25 = 55 > 50)
    over_res = client.post(
        "/api/v1/targets/allocate",
        json={
            "parent_target_id": parent_id,
            "agent_user_id": agent_id,
            "target_quantity": 25,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": admin_a_token},
    )
    assert over_res.status_code == 422
    assert "exceed" in str(over_res.json()).lower() or "allocation" in str(over_res.json()).lower()

    # 5. Out of bounds dates: child deadline > parent deadline
    bad_dates_res = client.post(
        "/api/v1/targets/allocate",
        json={
            "parent_target_id": parent_id,
            "agent_user_id": agent_id,
            "target_quantity": 10,
            "start_date": str(today),
            "deadline": str(deadline + timedelta(days=5)),
        },
        cookies={"community_session": admin_a_token},
    )
    assert bad_dates_res.status_code == 422
    assert "bounds" in str(bad_dates_res.json()).lower() or "range" in str(bad_dates_res.json()).lower()


def test_cross_tenant_isolation(targets_harness):
    client = targets_harness["client"]
    super_token = targets_harness["tokens"]["super"]
    admin_a_token = targets_harness["tokens"]["admin_a"]
    admin_b_token = targets_harness["tokens"]["admin_b"]
    admin_a_id = str(targets_harness["users"]["admin_a_id"])
    agent_id = str(targets_harness["users"]["agent_id"])

    today = date.today()
    deadline = today + timedelta(days=30)

    # Super Admin creates target for Admin A in Tenant Alpha
    parent_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_a_id,
            "target_type_code": "SUBSCRIPTIONS_GENERATED",
            "target_quantity": 100,
            "period": "MONTHLY",
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": super_token},
    )
    parent_id = parent_res.json()["id"]

    # Admin B (Tenant Beta) cannot see Admin A's target in list
    list_b = client.get(
        "/api/v1/targets/admin",
        cookies={"community_session": admin_b_token},
    )
    assert list_b.status_code == 200
    ids = [t["id"] for t in list_b.json()]
    assert parent_id not in ids

    # Admin B cannot allocate from Admin A's parent target
    allocate_b = client.post(
        "/api/v1/targets/allocate",
        json={
            "parent_target_id": parent_id,
            "agent_user_id": agent_id,
            "target_quantity": 10,
        },
        cookies={"community_session": admin_b_token},
    )
    assert allocate_b.status_code == 403
    assert "target" in str(allocate_b.json()).lower() or "denied" in str(allocate_b.json()).lower()


def test_automatic_achievement_calculation(targets_harness):
    client = targets_harness["client"]
    factory = targets_harness["factory"]
    super_token = targets_harness["tokens"]["super"]
    admin_a_token = targets_harness["tokens"]["admin_a"]
    agent_token = targets_harness["tokens"]["agent"]
    admin_a_id = targets_harness["users"]["admin_a_id"]
    agent_id = targets_harness["users"]["agent_id"]
    agent_mem_id = targets_harness["users"]["agent_mem_id"]
    tenant_a_id = targets_harness["tenants"]["tenant_a_id"]

    today = date.today()
    deadline = today + timedelta(days=30)

    # 1. Super Admin assigns AGENTS_RECRUITED target to Admin A (target: 2)
    admin_target_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": str(admin_a_id),
            "target_type_code": "AGENTS_RECRUITED",
            "target_quantity": 2,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": super_token},
    )
    admin_target_id = admin_target_res.json()["id"]
    assert admin_target_res.json()["achieved_quantity"] == 0

    # Simulate an approved AgentAppointment by Admin A
    now = datetime.now(UTC)
    with factory() as session:
        appt_user_id = uuid4()
        appt_mem_id = uuid4()
        appt_profile_id = uuid4()
        session.add(User(id=appt_user_id, status="active", created_at=now, updated_at=now))
        session.flush()
        session.add(
            TenantMembership(
                id=appt_mem_id,
                tenant_id=tenant_a_id,
                user_id=appt_user_id,
                mobile_e164="+919800000099",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            AgentProfile(
                id=appt_profile_id,
                tenant_id=tenant_a_id,
                membership_id=appt_mem_id,
                user_id=appt_user_id,
                first_name="Appointed",
                last_name="Agent",
                email="appointed@ott.com",
                address="10 Appointed Road",
                ref_id="AGT-2001",
                profile_photo_key="photo_appointed",
                aadhaar_number_ciphertext="enc_aadhaar",
                aadhaar_number_masked="XXXX-XXXX-9988",
                aadhaar_document_key="doc_aadhaar",
                pan_number_ciphertext="enc_pan",
                pan_number_masked="PQRSE5678G",
                pan_document_key="doc_pan",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        agent_role = session.scalar(
            select(Role).where(Role.tenant_id == tenant_a_id, Role.code == "agent")
        )
        session.add(
            AgentAppointment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                user_id=appt_user_id,
                membership_id=appt_mem_id,
                agent_profile_id=appt_profile_id,
                requested_by_user_id=admin_a_id,
                role_id=agent_role.id if agent_role else uuid4(),
                status="APPROVED",
                proposed_commission_rate=10.0,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    # Verify dynamically calculated achievement for AGENTS_RECRUITED: achieved = 1, remaining = 1, progress = 50.0%
    updated_admin_target = client.get(
        f"/api/v1/targets/{admin_target_id}",
        cookies={"community_session": admin_a_token},
    ).json()
    assert updated_admin_target["achieved_quantity"] == 1
    assert updated_admin_target["remaining_quantity"] == 1
    assert updated_admin_target["progress_percentage"] == 50.0
    assert updated_admin_target["status"] == "ACTIVE"

    # 2. Test USERS_REGISTERED for Agent
    # Admin A creates parent USERS_REGISTERED target (quota: 10)
    parent_users_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": str(admin_a_id),
            "target_type_code": "USERS_REGISTERED",
            "target_quantity": 10,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": super_token},
    )
    parent_users_id = parent_users_res.json()["id"]

    # Admin A allocates 5 to Agent
    child_users_res = client.post(
        "/api/v1/targets/allocate",
        json={
            "parent_target_id": parent_users_id,
            "agent_user_id": str(agent_id),
            "target_quantity": 5,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": admin_a_token},
    )
    agent_target_id = child_users_res.json()["id"]

    # Insert 6 SubscriberProfiles registered by this agent (overachieving!)
    with factory() as session:
        for i in range(6):
            sub_u = uuid4()
            sub_m = uuid4()
            session.add(User(id=sub_u, status="active", created_at=now, updated_at=now))
            session.flush()
            session.add(
                TenantMembership(
                    id=sub_m,
                    tenant_id=tenant_a_id,
                    user_id=sub_u,
                    mobile_e164=f"+91980000001{i}",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                SubscriberProfile(
                    id=uuid4(),
                    tenant_id=tenant_a_id,
                    membership_id=sub_m,
                    user_id=sub_u,
                    registered_by_agent_membership_id=agent_mem_id,
                    full_name=f"Subscriber {i}",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()

    # Agent checks their personal target
    agent_check = client.get(
        f"/api/v1/targets/{agent_target_id}",
        cookies={"community_session": agent_token},
    ).json()
    assert agent_check["achieved_quantity"] == 6
    assert agent_check["remaining_quantity"] == 0
    # Overachievement is visible (uncapped)
    assert agent_check["progress_percentage"] == 120.0
    assert agent_check["status"] == "COMPLETED"


def test_manual_adjustment_and_content_uploaded(targets_harness):
    client = targets_harness["client"]
    super_token = targets_harness["tokens"]["super"]
    admin_a_token = targets_harness["tokens"]["admin_a"]
    admin_a_id = str(targets_harness["users"]["admin_a_id"])

    today = date.today()
    deadline = today + timedelta(days=30)

    # Super Admin creates CONTENT_UPLOADED target (calculation_type: MANUAL)
    content_target_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_a_id,
            "target_type_code": "CONTENT_UPLOADED",
            "target_quantity": 8,
            "start_date": str(today),
            "deadline": str(deadline),
            "notes": "Catalog 8 community documentaries",
        },
        cookies={"community_session": super_token},
    )
    target_id = content_target_res.json()["id"]
    assert content_target_res.json()["calculation_type"] == "MANUAL"
    assert content_target_res.json()["achieved_quantity"] == 0

    # Super Admin records an auditable adjustment of +5
    adj_res = client.post(
        f"/api/v1/targets/{target_id}/adjust",
        json={
            "adjustment_quantity": 5,
            "reason": "Verified 5 festival highlights uploaded to regional server.",
        },
        cookies={"community_session": super_token},
    )
    assert adj_res.status_code == 201
    adj = adj_res.json()
    assert adj["adjustment_quantity"] == 5

    # Check updated target
    target_check = client.get(
        f"/api/v1/targets/{target_id}",
        cookies={"community_session": admin_a_token},
    ).json()
    assert target_check["achieved_quantity"] == 5
    assert target_check["remaining_quantity"] == 3
    assert target_check["progress_percentage"] == 62.5
    assert target_check["adjustments_count"] == 1

    # Check adjustment history
    adj_list = client.get(
        f"/api/v1/targets/{target_id}/adjustments",
        cookies={"community_session": admin_a_token},
    ).json()
    assert len(adj_list) == 1
    assert "festival highlights" in adj_list[0]["reason"]


def test_overdue_status_transition(targets_harness):
    client = targets_harness["client"]
    super_token = targets_harness["tokens"]["super"]
    admin_a_token = targets_harness["tokens"]["admin_a"]
    admin_a_id = str(targets_harness["users"]["admin_a_id"])

    # Target in the past with unreached quota
    past_start = date.today() - timedelta(days=60)
    past_deadline = date.today() - timedelta(days=10)

    target_res = client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_a_id,
            "target_type_code": "SUBSCRIPTIONS_GENERATED",
            "target_quantity": 50,
            "start_date": str(past_start),
            "deadline": str(past_deadline),
        },
        cookies={"community_session": super_token},
    )
    target_id = target_res.json()["id"]

    target_detail = client.get(
        f"/api/v1/targets/{target_id}",
        cookies={"community_session": admin_a_token},
    ).json()
    assert target_detail["status"] == "OVERDUE"
    assert target_detail["achieved_quantity"] == 0


def test_dashboard_summary_endpoint(targets_harness):
    client = targets_harness["client"]
    super_token = targets_harness["tokens"]["super"]
    admin_a_token = targets_harness["tokens"]["admin_a"]
    admin_a_id = str(targets_harness["users"]["admin_a_id"])

    today = date.today()
    deadline = today + timedelta(days=30)

    # Super Admin creates 2 targets for Admin A
    client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_a_id,
            "target_type_code": "AGENTS_RECRUITED",
            "target_quantity": 10,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": super_token},
    )
    client.post(
        "/api/v1/targets/admin",
        json={
            "admin_user_id": admin_a_id,
            "target_type_code": "USERS_REGISTERED",
            "target_quantity": 20,
            "start_date": str(today),
            "deadline": str(deadline),
        },
        cookies={"community_session": super_token},
    )

    summary_res = client.get(
        "/api/v1/targets/summary",
        cookies={"community_session": admin_a_token},
    )
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["total_targets"] == 2
    assert summary["total_target_quantity"] == 30
    assert summary["active_targets"] == 2
