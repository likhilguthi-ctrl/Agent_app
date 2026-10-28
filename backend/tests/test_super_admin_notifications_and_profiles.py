from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agents.models import AgentProfile
from app.modules.agent_appointments.models import AgentAppointment
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
from app.modules.notifications.models import SuperAdminNotification
from app.modules.notifications.service import InAppNotificationService
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.tenants.models import AdminProfile, Region, Tenant, UserRegionAssignment


@pytest.fixture
def notifications_harness(tmp_path):
    database_path = tmp_path / "notifications-test.sqlite"
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
    admin_token = "admin_notif_token_123"
    admin_digest = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    admin_csrf = hashlib.sha256(b"admin_notif_csrf_123").hexdigest()

    super_token = "super_notif_token_123"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_notif_csrf_123").hexdigest()

    super_token_2 = "super_notif_token_456"
    super_digest_2 = hashlib.sha256(super_token_2.encode("utf-8")).hexdigest()
    super_csrf_2 = hashlib.sha256(b"super_notif_csrf_456").hexdigest()

    admin_user_id = uuid4()
    admin_mem_id = uuid4()
    super_op_id_1 = uuid4()
    super_op_id_2 = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Tenant
        tenant = Tenant(
            id=tenant_id,
            slug="notif-community",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        # Platform Super Admins (2 active operators)
        root_op = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        super_op_1 = PlatformOperator(
            id=super_op_id_1,
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin_1",
            status="active",
        )
        super_op_2 = PlatformOperator(
            id=super_op_id_2,
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin_2",
            status="active",
        )
        super_role = PlatformRole(
            id=uuid4(),
            code="super_admin",
            display_name="Super Administrator",
            status="active",
        )
        session.add_all([root_op, super_op_1, super_op_2, super_role])
        session.flush()

        # Role assignments
        session.add_all([
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=super_op_id_1,
                role_id=super_role.id,
                assigned_by_operator_id=root_op.id,
                status="active",
                valid_from=now - timedelta(days=1),
            ),
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=super_op_id_2,
                role_id=super_role.id,
                assigned_by_operator_id=root_op.id,
                status="active",
                valid_from=now - timedelta(days=1),
            ),
        ])

        # Operator sessions
        session.add_all([
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_op_id_1,
                token_digest=super_digest,
                csrf_token=super_csrf,
                authenticated_at=now,
                acr="urn:community:pwd",
                amr=["pwd"],
                created_at=now,
                expires_at=now + timedelta(hours=2),
            ),
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_op_id_2,
                token_digest=super_digest_2,
                csrf_token=super_csrf_2,
                authenticated_at=now,
                acr="urn:community:pwd",
                amr=["pwd"],
                created_at=now,
                expires_at=now + timedelta(hours=2),
            ),
        ])

        # Seed tenant RBAC
        admin_role, agent_role = seed_tenant_role_permissions(session, tenant_id)

        # Admin user and membership
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
        admin_cred = AccountCredential(
            id=uuid4(),
            user_id=admin_user_id,
            username="community_admin_1",
            password_hash="mock_hash",
            account_type="tenant_user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        admin_prof = AdminProfile(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=admin_user_id,
            first_name="Admin",
            last_name="One",
            address="123 Community Lane",
            profile_photo_key="admin_photo_key",
            created_at=now,
            updated_at=now,
        )
        session.add_all([admin_assign, admin_session, admin_cred, admin_prof])

        # Add region and assign to admin
        region = Region(
            id=uuid4(),
            tenant_id=tenant_id,
            code="NORTH",
            name="North District",
            description="Northern territory",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(region)
        session.flush()

        region_assign = UserRegionAssignment(
            id=uuid4(),
            user_id=admin_user_id,
            region_id=region.id,
            tenant_id=tenant_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(region_assign)
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
        "admin_user_id": admin_user_id,
        "admin_mem_id": admin_mem_id,
        "admin_token": admin_token,
        "admin_csrf": admin_csrf,
        "super_op_id_1": super_op_id_1,
        "super_op_id_2": super_op_id_2,
        "super_token_1": super_token,
        "super_csrf_1": super_csrf,
        "super_token_2": super_token_2,
        "super_csrf_2": super_csrf_2,
    }


@pytest.mark.anyio
async def test_super_admin_notification_workflow(notifications_harness):
    harness = notifications_harness
    app = harness["app"]
    factory = harness["factory"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Admin creates an agent appointment directly or via registration
        agent_user_id = uuid4()
        agent_mem_id = uuid4()
        agent_profile_id = uuid4()
        appointment_id = uuid4()
        now = datetime.now(UTC)

        with factory() as session:
            # Create user and membership
            u = User(id=agent_user_id, status="active", created_at=now, updated_at=now)
            session.add(u)
            session.flush()

            m = TenantMembership(
                id=agent_mem_id,
                tenant_id=harness["tenant_id"],
                user_id=agent_user_id,
                mobile_e164="+919900000088",
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(m)
            session.flush()

            ap = AgentProfile(
                id=agent_profile_id,
                tenant_id=harness["tenant_id"],
                membership_id=agent_mem_id,
                user_id=agent_user_id,
                first_name="Jane",
                last_name="Doe",
                ref_id="JND001",
                profile_photo_key="photo_jane",
                aadhaar_number_ciphertext="enc_aadhaar",
                aadhaar_number_masked="XXXX-XXXX-1122",
                aadhaar_document_key="doc_aadhaar",
                pan_number_ciphertext="enc_pan",
                pan_number_masked="ABCDE1122Z",
                pan_document_key="doc_pan",
                created_at=now,
            )
            session.add(ap)
            session.flush()

            # Find agent role
            stmt = select(Role).where(
                Role.tenant_id == harness["tenant_id"], Role.code == "agent"
            )
            agent_role = session.scalar(stmt)

            apt = AgentAppointment(
                id=appointment_id,
                tenant_id=harness["tenant_id"],
                user_id=agent_user_id,
                membership_id=agent_mem_id,
                agent_profile_id=agent_profile_id,
                requested_by_user_id=harness["admin_user_id"],
                role_id=agent_role.id,
                status="PENDING",
                created_at=now,
                updated_at=now,
            )
            session.add(apt)
            session.flush()

            # Trigger notification creation service
            tenant = session.get(Tenant, harness["tenant_id"])
            svc = InAppNotificationService()
            created = svc.create_appointment_approval_notifications(
                session,
                appointment=apt,
                profile=ap,
                tenant=tenant,
                requested_by_user_id=harness["admin_user_id"],
            )
            session.commit()
            assert len(created) == 3  # Notified all active super admins (root_op, super_op_1, super_op_2)

        # 2. Verify Super Admin 1 unread count
        headers_super1 = {
            "Cookie": f"community_platform_session={harness['super_token_1']}",
            "Host": "testserver",
        }
        res = await client.get("/api/v1/platform/notifications/unread-count", headers=headers_super1)
        assert res.status_code == 200
        assert res.json() == {"unread_count": 1}

        # 3. Super Admin 1 lists notifications
        res = await client.get("/api/v1/platform/notifications", headers=headers_super1)
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        notif = data["items"][0]
        assert notif["event_type"] == "AGENT_APPOINTMENT_PENDING"
        assert notif["resolution_status"] == "PENDING"
        assert notif["is_read"] is False
        assert notif["agent_profile_id"] == str(agent_profile_id)
        assert "Jane Doe" in notif["message"]

        notif_id = notif["id"]

        # 4. Super Admin 1 marks notification as read
        patch_headers = {
            **headers_super1,
            "X-CSRF-Token": harness["super_csrf_1"],
            "Origin": "http://testserver",
        }
        res = await client.patch(f"/api/v1/platform/notifications/{notif_id}/read", headers=patch_headers)
        assert res.status_code == 200
        assert res.json()["is_read"] is True

        # Unread count for Super Admin 1 is now 0
        res = await client.get("/api/v1/platform/notifications/unread-count", headers=headers_super1)
        assert res.status_code == 200
        assert res.json()["unread_count"] == 0

        # Unread count for Super Admin 2 is still 1 (recipient isolation)
        headers_super2 = {
            "Cookie": f"community_platform_session={harness['super_token_2']}",
            "Host": "testserver",
        }
        res = await client.get("/api/v1/platform/notifications/unread-count", headers=headers_super2)
        assert res.status_code == 200
        assert res.json()["unread_count"] == 1

        # 5. Approve appointment and verify notification resolution
        approve_headers = {
            **headers_super1,
            "X-CSRF-Token": harness["super_csrf_1"],
            "Origin": "http://testserver",
        }
        res = await client.post(
            f"/api/v1/agents/appointments/{appointment_id}/approve",
            headers=approve_headers,
            json={"reason": "Super Admin approved appointment"},
        )
        assert res.status_code == 200

        # Verify resolution status in database for both notifications
        with factory() as session:
            notifs = list(session.scalars(
                select(SuperAdminNotification).where(
                    SuperAdminNotification.appointment_id == appointment_id
                )
            ))
            assert len(notifs) == 3
            for n in notifs:
                assert n.resolution_status == "APPROVED"
                assert n.resolved_at is not None

        # Verify notification list returns updated resolution status
        res = await client.get("/api/v1/platform/notifications", headers=headers_super1)
        assert res.status_code == 200
        assert res.json()["items"][0]["resolution_status"] == "APPROVED"


@pytest.mark.anyio
async def test_super_admin_view_administrator_profile(notifications_harness):
    harness = notifications_harness
    app = harness["app"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers_super = {
            "Cookie": f"community_platform_session={harness['super_token_1']}",
            "Host": "testserver",
        }

        # 1. Fetch administrator via community endpoint
        res = await client.get(
            f"/api/v1/platform/communities/{harness['tenant_id']}/administrators/{harness['admin_user_id']}",
            headers=headers_super,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["user_id"] == str(harness["admin_user_id"])
        assert data["username"] == "community_admin_1"
        assert data["first_name"] == "Admin"
        assert data["last_name"] == "One"
        assert data["mobile_e164"] == "+919900000001"
        assert data["address"] == "123 Community Lane"
        assert data["profile_photo_key"] == "admin_photo_key"
        assert data["assigned_region_names"] == ["North District"]
        assert data["role"].lower() == "admin"
        assert data["status"] == "active"
        # Security assertions: sensitive fields must NEVER be returned
        assert "password" not in data
        assert "password_hash" not in data
        assert "salt" not in data

        # 2. Fetch administrator via global platform endpoint
        res_global = await client.get(
            f"/api/v1/platform/administrators/{harness['admin_user_id']}",
            headers=headers_super,
        )
        assert res_global.status_code == 200
        assert res_global.json()["username"] == "community_admin_1"

        # 3. Community Admin cannot access platform administrator profile endpoint (RBAC enforced)
        headers_admin = {
            "Cookie": f"community_session={harness['admin_token']}",
            "Host": "testserver",
        }
        res_unauth = await client.get(
            f"/api/v1/platform/administrators/{harness['admin_user_id']}",
            headers=headers_admin,
        )
        assert res_unauth.status_code in [401, 403]


@pytest.mark.anyio
async def test_super_admin_notification_rejection_and_mark_all(notifications_harness):
    harness = notifications_harness
    app = harness["app"]
    factory = harness["factory"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        agent_user_id = uuid4()
        agent_mem_id = uuid4()
        agent_profile_id = uuid4()
        appointment_id = uuid4()
        now = datetime.now(UTC)

        with factory() as session:
            u = User(id=agent_user_id, status="active", created_at=now, updated_at=now)
            session.add(u)
            session.flush()

            m = TenantMembership(
                id=agent_mem_id,
                tenant_id=harness["tenant_id"],
                user_id=agent_user_id,
                mobile_e164="+919900000089",
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(m)
            session.flush()

            ap = AgentProfile(
                id=agent_profile_id,
                tenant_id=harness["tenant_id"],
                membership_id=agent_mem_id,
                user_id=agent_user_id,
                first_name="Reject",
                last_name="Candidate",
                ref_id="REJ001",
                profile_photo_key="photo_rej",
                aadhaar_number_ciphertext="enc_aadhaar",
                aadhaar_number_masked="XXXX-XXXX-3344",
                aadhaar_document_key="doc_aadhaar",
                pan_number_ciphertext="enc_pan",
                pan_number_masked="ABCDE3344Z",
                pan_document_key="doc_pan",
                created_at=now,
            )
            session.add(ap)
            session.flush()

            agent_role = session.scalar(
                select(Role).where(
                    Role.tenant_id == harness["tenant_id"], Role.code == "agent"
                )
            )

            apt = AgentAppointment(
                id=appointment_id,
                tenant_id=harness["tenant_id"],
                user_id=agent_user_id,
                membership_id=agent_mem_id,
                agent_profile_id=agent_profile_id,
                requested_by_user_id=harness["admin_user_id"],
                role_id=agent_role.id,
                status="PENDING",
                created_at=now,
                updated_at=now,
            )
            session.add(apt)
            session.flush()

            tenant = session.get(Tenant, harness["tenant_id"])
            svc = InAppNotificationService()
            svc.create_appointment_approval_notifications(
                session,
                appointment=apt,
                profile=ap,
                tenant=tenant,
                requested_by_user_id=harness["admin_user_id"],
            )
            session.commit()

        headers_super1 = {
            "Cookie": f"community_platform_session={harness['super_token_1']}",
            "Host": "testserver",
            "X-CSRF-Token": harness["super_csrf_1"],
            "Origin": "http://localhost:3000",
        }

        # 1. Mark all read for Super Admin 1
        res = await client.post("/api/v1/platform/notifications/mark-all-read", headers=headers_super1)
        assert res.status_code == 200
        assert res.json()["marked_read"] >= 1

        # Unread count is now 0 for Super Admin 1
        res = await client.get("/api/v1/platform/notifications/unread-count", headers=headers_super1)
        assert res.status_code == 200
        assert res.json()["unread_count"] == 0

        # 2. Reject appointment
        res = await client.post(
            f"/api/v1/agents/appointments/{appointment_id}/reject",
            headers=headers_super1,
            json={"reason": "Incomplete background documentation"},
        )
        assert res.status_code == 200

        # 3. Verify notification resolution status updated to REJECTED
        with factory() as session:
            notifs = list(session.scalars(
                select(SuperAdminNotification).where(
                    SuperAdminNotification.appointment_id == appointment_id
                )
            ))
            assert len(notifs) >= 1
            for n in notifs:
                assert n.resolution_status == "REJECTED"
                assert n.resolved_at is not None


@pytest.mark.anyio
async def test_super_admin_sec03_and_idempotency(notifications_harness):
    harness = notifications_harness
    factory = harness["factory"]

    agent_user_id = uuid4()
    agent_mem_id = uuid4()
    agent_profile_id = uuid4()
    appointment_id = uuid4()
    now = datetime.now(UTC)

    with factory() as session:
        u = User(id=agent_user_id, status="active", created_at=now, updated_at=now)
        session.add(u)
        session.flush()

        m = TenantMembership(
            id=agent_mem_id,
            tenant_id=harness["tenant_id"],
            user_id=agent_user_id,
            mobile_e164="+919900000099",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m)
        session.flush()

        ap = AgentProfile(
            id=agent_profile_id,
            tenant_id=harness["tenant_id"],
            membership_id=agent_mem_id,
            user_id=agent_user_id,
            first_name="SEC03",
            last_name="Test",
            ref_id="SEC001",
            profile_photo_key="photo_sec",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-5566",
            aadhaar_document_key="doc_aadhaar",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE5566Z",
            pan_document_key="doc_pan",
            created_at=now,
        )
        session.add(ap)
        session.flush()

        agent_role = session.scalar(
            select(Role).where(
                Role.tenant_id == harness["tenant_id"], Role.code == "agent"
            )
        )

        # Appointment requested by super_op_id_1
        apt = AgentAppointment(
            id=appointment_id,
            tenant_id=harness["tenant_id"],
            user_id=agent_user_id,
            membership_id=agent_mem_id,
            agent_profile_id=agent_profile_id,
            requested_by_user_id=harness["super_op_id_1"],
            role_id=agent_role.id,
            status="PENDING",
            created_at=now,
            updated_at=now,
        )
        session.add(apt)
        session.flush()

        tenant = session.get(Tenant, harness["tenant_id"])
        svc = InAppNotificationService()

        # Call notification creation: super_op_id_1 should be excluded
        created = svc.create_appointment_approval_notifications(
            session,
            appointment=apt,
            profile=ap,
            tenant=tenant,
            requested_by_user_id=harness["super_op_id_1"],
        )
        session.commit()

        recipient_ids = [n.recipient_operator_id for n in created]
        assert harness["super_op_id_1"] not in recipient_ids, "Creator must be excluded as per SEC-03"
        assert harness["super_op_id_2"] in recipient_ids

        # Idempotency check: call again
        created_retry = svc.create_appointment_approval_notifications(
            session,
            appointment=apt,
            profile=ap,
            tenant=tenant,
            requested_by_user_id=harness["super_op_id_1"],
        )
        session.commit()

        # Verify retry returns empty or existing and total rows in DB did not increase
        total_in_db = session.scalar(
            select(func.count(SuperAdminNotification.id)).where(
                SuperAdminNotification.appointment_id == appointment_id
            )
        )
        assert total_in_db == len(created), "Duplicate notifications must not be inserted"

