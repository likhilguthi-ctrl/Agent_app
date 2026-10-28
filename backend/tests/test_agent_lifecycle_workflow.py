from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agents.models import AgentProfile
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agent_lifecycle.models import (
    AgentLifecycleRequest,
    LifecycleAction,
    LifecycleRequestStatus,
)
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
from app.modules.relationships.models import MemberRelationship, RelationshipStatus
from app.modules.tenants.models import Tenant


@pytest.fixture
def lifecycle_harness(tmp_path):
    database_path = tmp_path / "lifecycle-test.sqlite"
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

    tenant_id = uuid4()
    admin_token = "admin_lifecycle_token_123"
    admin_digest = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    admin_csrf = hashlib.sha256(b"admin_lifecycle_csrf_123").hexdigest()

    super_token = "super_admin_lifecycle_token_123"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_admin_lifecycle_csrf_123").hexdigest()

    admin_user_id = uuid4()
    admin_mem_id = uuid4()
    agent_user_id = uuid4()
    agent_mem_id = uuid4()
    agent_profile_id = uuid4()
    super_user_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Tenant
        tenant = Tenant(
            id=tenant_id,
            slug="community-lifecycle",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        # Platform Super Admin
        root_op = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        super_op = PlatformOperator(
            id=super_user_id,
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin",
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
                operator_id=super_user_id,
                role_id=super_role.id,
                assigned_by_operator_id=root_op.id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_user_id,
                token_digest=super_digest,
                csrf_token=super_csrf,
                authenticated_at=now,
                acr="urn:community:pwd",
                amr=["pwd"],
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Seed tenant RBAC
        admin_role, agent_role = seed_tenant_role_permissions(session, tenant_id)

        # Admin
        admin_user = User(id=admin_user_id, status="active", created_at=now, updated_at=now)
        session.add(admin_user)
        session.flush()

        admin_mem = TenantMembership(
            id=admin_mem_id,
            tenant_id=tenant_id,
            user_id=admin_user_id,
            mobile_e164="+919900000001",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(admin_mem)
        session.flush()

        admin_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_id,
            membership_id=admin_mem_id,
            role_id=admin_role.id,
            assigned_by_membership_id=admin_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        admin_session = AuthSession(
            id=uuid4(),
            tenant_id=tenant_id,
            membership_id=admin_mem_id,
            token_digest=admin_digest,
            csrf_token=admin_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([admin_assign, admin_session])
        session.flush()

        # Active Agent (with approved appointment history)
        agent_user = User(id=agent_user_id, status="active", created_at=now, updated_at=now)
        session.add(agent_user)
        session.flush()

        agent_mem = TenantMembership(
            id=agent_mem_id,
            tenant_id=tenant_id,
            user_id=agent_user_id,
            mobile_e164="+919900000002",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_mem)
        session.flush()

        agent_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_id,
            membership_id=agent_mem_id,
            role_id=agent_role.id,
            assigned_by_membership_id=admin_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        agent_profile = AgentProfile(
            id=agent_profile_id,
            tenant_id=tenant_id,
            membership_id=agent_mem_id,
            user_id=agent_user_id,
            first_name="Active",
            last_name="Agent",
            ref_id="ACT001",
            profile_photo_key="photo_act",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-9999",
            aadhaar_document_key="doc_aadhaar",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE9999Z",
            pan_document_key="doc_pan",
            created_at=now,
            updated_at=now,
        )
        agent_cred = AccountCredential(
            id=uuid4(),
            user_id=agent_user_id,
            username="activeagent01",
            password_hash="mock_hash",
            account_type="tenant_user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add_all([agent_assign, agent_profile, agent_cred])
        session.flush()

        agent_apt = AgentAppointment(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=agent_user_id,
            membership_id=agent_mem_id,
            agent_profile_id=agent_profile_id,
            requested_by_user_id=admin_user_id,
            role_id=agent_role.id,
            status="APPROVED",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_apt)
        session.flush()
        session.commit()

    encryption_key = "12345678901234567890123456789012"
    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
        identity_encryption_key=encryption_key,
    )
    app = create_app(test_settings)

    def db_override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_authentication_repository] = lambda: SQLAlchemyAuthenticationRepository(
        factory
    )
    app.dependency_overrides[get_authorization_repository] = lambda: SQLAlchemyAuthorizationRepository(
        factory
    )

    return {
        "factory": factory,
        "app": app,
        "tenant_id": tenant_id,
        "admin_token": admin_token,
        "admin_csrf": admin_csrf,
        "admin_user_id": admin_user_id,
        "admin_mem_id": admin_mem_id,
        "super_token": super_token,
        "super_csrf": super_csrf,
        "super_user_id": super_user_id,
        "agent_profile_id": agent_profile_id,
        "agent_user_id": agent_user_id,
        "agent_mem_id": agent_mem_id,
    }


@pytest.mark.anyio
async def test_lifecycle_submission_preserves_state_and_enforces_superadmin_gate(lifecycle_harness):
    h = lifecycle_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Admin submits SUSPEND request
        sub_res = await client.post(
            f"/api/v1/agents/{h['agent_profile_id']}/lifecycle-requests",
            json={
                "action": "SUSPEND",
                "reason": "Temporary suspension for territory compliance review.",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert sub_res.status_code == 201
        req_data = sub_res.json()
        assert req_data["status"] == "PENDING"
        req_id = req_data["id"]

        # 2. Verify agent state is STILL active in database
        with h["factory"]() as session:
            mem = session.execute(
                select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])
            ).scalar_one()
            assert mem.status == "active"

        # 3. Community Admin attempts to call Super Admin decision endpoint -> 403 Forbidden
        dec_forbidden = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Admin attempting self-approval"},
            cookies={"community_session": h["admin_token"]},
        )
        assert dec_forbidden.status_code == 403

        # 4. Authorized Super Admin approves
        dec_ok = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Approved by superadmin after compliance review"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert dec_ok.status_code == 200
        assert dec_ok.json()["status"] == "APPROVED"

        # 5. Verify agent state in database is now inactive (suspended)
        with h["factory"]() as session:
            mem = session.execute(
                select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])
            ).scalar_one()
            assert mem.status == "inactive"
            usr = session.execute(
                select(User).where(User.id == h["agent_user_id"])
            ).scalar_one()
            assert usr.status == "inactive"


@pytest.mark.anyio
async def test_reason_validation_mandatory(lifecycle_harness):
    h = lifecycle_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Empty or short reason (< 5 chars) -> 422
        res = await client.post(
            f"/api/v1/agents/{h['agent_profile_id']}/lifecycle-requests",
            json={"action": "DEACTIVATE", "reason": "no"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert res.status_code == 422


@pytest.mark.anyio
async def test_reactivate_workflow(lifecycle_harness):
    h = lifecycle_harness
    app = h["app"]

    # First set agent to inactive (suspended/deactivated)
    with h["factory"]() as session:
        mem = session.execute(select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])).scalar_one()
        mem.status = "inactive"
        session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Submit REACTIVATE
        sub = await client.post(
            f"/api/v1/agents/{h['agent_profile_id']}/lifecycle-requests",
            json={"action": "REACTIVATE", "reason": "Completed audit and compliance training"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert sub.status_code == 201
        req_id = sub.json()["id"]

        # Super Admin approves
        dec = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Training verified"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert dec.status_code == 200

        with h["factory"]() as session:
            mem = session.execute(select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])).scalar_one()
            assert mem.status == "active"


@pytest.mark.anyio
async def test_rejection_preserves_state(lifecycle_harness):
    h = lifecycle_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sub = await client.post(
            f"/api/v1/agents/{h['agent_profile_id']}/lifecycle-requests",
            json={"action": "REVOKE", "reason": "Dispute under investigation"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert sub.status_code == 201
        req_id = sub.json()["id"]

        # Super Admin REJECTS
        dec = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "REJECTED", "decision_reason": "Insufficient evidence for revocation"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert dec.status_code == 200
        assert dec.json()["status"] == "REJECTED"

        # Agent status remains active
        with h["factory"]() as session:
            mem = session.execute(select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])).scalar_one()
            assert mem.status == "active"


@pytest.mark.anyio
async def test_terminal_state_conflict_prevention(lifecycle_harness):
    h = lifecycle_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sub = await client.post(
            f"/api/v1/agents/{h['agent_profile_id']}/lifecycle-requests",
            json={"action": "DEACTIVATE", "reason": "Voluntary leave of absence"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert sub.status_code == 201
        req_id = sub.json()["id"]

        # First decision: APPROVED
        d1 = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Approved"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert d1.status_code == 200

        # Repeated decision on decided request -> 409 Conflict
        d2 = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "REJECTED", "decision_reason": "Trying to change decision"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert d2.status_code == 409


@pytest.mark.anyio
async def test_sec07_legacy_agent_status_patch_bypass_strictly_prohibited(lifecycle_harness):
    """SEC-07: Verify that legacy PATCH /api/v1/agents/{id} strictly rejects direct status changes

    and requires the Super Admin lifecycle approval workflow.
    """
    h = lifecycle_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Tenant Admin attempts direct deactivation via legacy PATCH -> strictly rejected with 403
        deact_res = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={"status": "inactive"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert deact_res.status_code == 403

        # 2. Tenant Admin attempts direct reactivation via legacy PATCH -> strictly rejected with 403
        react_res = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={"status": "active"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert react_res.status_code == 403

        # 3. Super Admin calling legacy PATCH with status -> also rejected with 403 (all lifecycle transitions must use lifecycle workflow)
        super_patch_status = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={"status": "inactive"},
            cookies={"community_platform_session": h["super_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["super_csrf"]},
        )
        assert super_patch_status.status_code == 403

        # 4. Verify database state: User, TenantMembership, and AccountCredential statuses remain unchanged ('active')
        with h["factory"]() as session:
            mem = session.execute(select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])).scalar_one()
            usr = session.execute(select(User).where(User.id == h["agent_user_id"])).scalar_one()
            assert mem.status == "active"
            assert usr.status == "active"

        # 5. Legitimate agent profile updates (name, address, email) still succeed for Tenant Admin
        legit_res = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={
                "first_name": "UpdatedFirst",
                "last_name": "UpdatedLast",
                "address": "456 Updated St, Community Center",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert legit_res.status_code == 200
        assert legit_res.json()["first_name"] == "UpdatedFirst"
        assert legit_res.json()["last_name"] == "UpdatedLast"
        assert legit_res.json()["address"] == "456 Updated St, Community Center"

        # 6. Commission update authorization: Tenant Admin cannot update commission_rate_override
        comm_admin_res = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={"commission_rate_override": 12.50},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert comm_admin_res.status_code == 403

        # 7. Super Admin can update commission_rate_override
        comm_super_res = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={"commission_rate_override": 14.00},
            cookies={"community_platform_session": h["super_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["super_csrf"]},
        )
        assert comm_super_res.status_code == 200
        assert float(comm_super_res.json()["commission_rate_override"]) == 14.00

        # 8. Tenant Admin cannot decide lifecycle requests directly
        # First, submit a valid lifecycle request
        sub_req = await client.post(
            f"/api/v1/agents/{h['agent_profile_id']}/lifecycle-requests",
            json={"action": "SUSPEND", "reason": "Audit compliance check requested by tenant admin"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert sub_req.status_code == 201
        req_id = sub_req.json()["id"]

        # Tenant Admin attempts to approve via platform endpoint -> 403 Forbidden
        admin_dec_res = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Tenant admin self-approval attempt"},
            cookies={"community_session": h["admin_token"]},
        )
        assert admin_dec_res.status_code == 403

        # Super Admin approves lifecycle request -> 200 OK and atomic transition to inactive
        super_dec_res = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Super Admin approved suspension"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert super_dec_res.status_code == 200
        assert super_dec_res.json()["status"] == "APPROVED"

        with h["factory"]() as session:
            mem_post = session.execute(select(TenantMembership).where(TenantMembership.id == h["agent_mem_id"])).scalar_one()
            usr_post = session.execute(select(User).where(User.id == h["agent_user_id"])).scalar_one()
            assert mem_post.status == "inactive"
            assert usr_post.status == "inactive"


@pytest.mark.anyio
async def test_candidate_agent_activation_rejection_and_dual_custody_approval(lifecycle_harness):
    h = lifecycle_harness
    app = h["app"]
    factory = h["factory"]
    now = datetime.now(UTC)

    candidate_user_id = uuid4()
    candidate_mem_id = uuid4()
    candidate_profile_id = uuid4()
    candidate_apt_id = uuid4()

    with factory() as session:
        agent_role = session.execute(
            select(Role).where(Role.tenant_id == h["tenant_id"], Role.code == "agent")
        ).scalar_one()

        cand_user = User(id=candidate_user_id, status="inactive", created_at=now, updated_at=now)
        cand_mem = TenantMembership(
            id=candidate_mem_id,
            tenant_id=h["tenant_id"],
            user_id=candidate_user_id,
            mobile_e164="+919876543210",
            status="inactive",
            created_at=now,
            updated_at=now,
        )
        cand_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=h["tenant_id"],
            membership_id=candidate_mem_id,
            role_id=agent_role.id,
            assigned_by_membership_id=h["admin_mem_id"],
            status="inactive",
            valid_from=now,
            valid_until=None,
            created_at=now,
            updated_at=now,
        )
        cand_cred = AccountCredential(
            id=uuid4(),
            user_id=candidate_user_id,
            username="candidate_likhil",
            password_hash="hashed_pw_123",
            account_type="tenant_user",
            status="inactive",
            created_at=now,
            updated_at=now,
        )
        cand_profile = AgentProfile(
            id=candidate_profile_id,
            tenant_id=h["tenant_id"],
            membership_id=candidate_mem_id,
            user_id=candidate_user_id,
            first_name="Likhil",
            last_name="G",
            ref_id="XH3ZZSXX",
            profile_photo_key="agent-photos/photo.jpg",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-1234",
            aadhaar_document_key="agent-docs/aadhaar.pdf",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE1234F",
            pan_document_key="agent-docs/pan.pdf",
            created_at=now,
            updated_at=now,
        )
        session.add(cand_user)
        session.flush()
        session.add(cand_mem)
        session.flush()
        session.add_all([cand_assign, cand_cred, cand_profile])
        session.flush()

        cand_apt = AgentAppointment(
            id=candidate_apt_id,
            tenant_id=h["tenant_id"],
            user_id=candidate_user_id,
            membership_id=candidate_mem_id,
            agent_profile_id=candidate_profile_id,
            requested_by_user_id=h["admin_user_id"],
            role_id=agent_role.id,
            status="PENDING",
            created_at=now,
            updated_at=now,
        )
        session.add(cand_apt)
        session.flush()
        session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Inspect candidate via GET /api/v1/agents/{id} -> status is "pending", pending_appointment_id is set
        detail_res = await client.get(
            f"/api/v1/agents/{candidate_profile_id}",
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local"},
        )
        assert detail_res.status_code == 200
        detail_body = detail_res.json()
        assert detail_body["status"] == "pending"
        assert detail_body["pending_appointment_id"] == str(candidate_apt_id)

        # 2. Candidate activation via lifecycle request fails with 400 Bad Request
        # (This reproduces and verifies the exact 400 issue and ensures message is unmasked)
        fail_res = await client.post(
            f"/api/v1/agents/{candidate_profile_id}/lifecycle-requests",
            json={"action": "REACTIVATE", "reason": "Attempt activation without appointment approval"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert fail_res.status_code == 400
        err_body = fail_res.json()
        assert "error" in err_body
        assert err_body["error"]["code"] == "HTTP_ERROR"
        assert "Cannot reactivate an agent whose initial appointment was never approved." in err_body["error"]["message"]
        assert "requestId" in err_body["error"]

        # 3. Dual-custody / SEC-03 self-approval prevention:
        # Admin who requested appointment cannot self-approve -> 403 Forbidden
        self_approve_res = await client.post(
            f"/api/v1/agents/appointments/{candidate_apt_id}/approve",
            json={"reason": "Self-approval attempt by creator"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert self_approve_res.status_code == 403
        self_err_body = self_approve_res.json()
        assert "Reviewers cannot approve appointments they requested." in self_err_body["error"]["message"]

        # 4. Super Admin (or peer reviewer) approves appointment -> 200 OK
        super_approve_res = await client.post(
            f"/api/v1/agents/appointments/{candidate_apt_id}/approve",
            json={"reason": "Super Admin approved initial appointment"},
            cookies={"community_platform_session": h["super_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["super_csrf"]},
        )
        assert super_approve_res.status_code == 200
        assert super_approve_res.json()["status"] == "APPROVED"

        # 5. After approval, verify candidate User, Membership, Credential, Role are active
        with factory() as session:
            cand_usr_db = session.execute(select(User).where(User.id == candidate_user_id)).scalar_one()
            cand_mem_db = session.execute(select(TenantMembership).where(TenantMembership.id == candidate_mem_id)).scalar_one()
            cand_cred_db = session.execute(select(AccountCredential).where(AccountCredential.user_id == candidate_user_id)).scalar_one()
            assert cand_usr_db.status == "active"
            assert cand_mem_db.status == "active"
            assert cand_cred_db.status == "active"

        # 6. GET /api/v1/agents/{id} now reports status "active" and pending_appointment_id None
        active_res = await client.get(
            f"/api/v1/agents/{candidate_profile_id}",
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local"},
        )
        assert active_res.status_code == 200
        assert active_res.json()["status"] == "active"
        assert active_res.json()["pending_appointment_id"] is None

        # 7. Now lifecycle requests work: Tenant Admin submits DEACTIVATE
        deact_res = await client.post(
            f"/api/v1/agents/{candidate_profile_id}/lifecycle-requests",
            json={"action": "DEACTIVATE", "reason": "Deactivating for leave"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert deact_res.status_code == 201
        deact_req_id = deact_res.json()["id"]

        # Super Admin approves DEACTIVATE
        dec_res = await client.post(
            f"/api/v1/platform/agent-lifecycle-requests/{deact_req_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "Approved leave deactivation"},
            cookies={"community_platform_session": h["super_token"]},
        )
        assert dec_res.status_code == 200

        # 8. Now Tenant Admin can submit REACTIVATE successfully because initial appointment was approved!
        react_res = await client.post(
            f"/api/v1/agents/{candidate_profile_id}/lifecycle-requests",
            json={"action": "REACTIVATE", "reason": "Reactivating after leave"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-lifecycle.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert react_res.status_code == 201


