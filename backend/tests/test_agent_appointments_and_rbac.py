from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.approval_workflows.models import ApprovalDecision
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession, EmailOtpChallenge
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.models import Role, RolePermission, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from sqlalchemy.exc import IntegrityError
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.registration_photos.models import StoredObject
from app.modules.registration_photos.storage import MockedR2Storage
from app.modules.tenants.models import Region, Tenant, UserRegionAssignment


def create_test_image_bytes(format: str = "JPEG") -> bytes:
    buf = BytesIO()
    img = Image.new("RGB", (100, 100), color=(73, 109, 137))
    img.save(buf, format=format)
    return buf.getvalue()


@pytest.fixture
def appointment_harness(tmp_path):
    database_path = tmp_path / "appointment-test.sqlite"
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

    import hashlib
    super_token = "super_admin_session_token_123"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_admin_csrf_123").hexdigest()

    admin_a1_token = "admin_a1_session_token_123"
    admin_a1_digest = hashlib.sha256(admin_a1_token.encode("utf-8")).hexdigest()
    admin_a1_csrf = hashlib.sha256(b"admin_a1_csrf_123").hexdigest()

    admin_a2_token = "admin_a2_session_token_123"
    admin_a2_digest = hashlib.sha256(admin_a2_token.encode("utf-8")).hexdigest()
    admin_a2_csrf = hashlib.sha256(b"admin_a2_csrf_123").hexdigest()

    admin_b_token = "admin_b_session_token_123"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf_123").hexdigest()

    tenant_a_id = uuid4()
    tenant_b_id = uuid4()
    admin_a1_user_id = uuid4()
    admin_a1_mem_id = uuid4()
    admin_a2_user_id = uuid4()
    admin_a2_mem_id = uuid4()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()
    region_a_id = uuid4()
    now = datetime.now(UTC)

    with factory() as session:
        # Super Admin operator
        root_op = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        super_op = PlatformOperator(
            id=uuid4(),
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
                operator_id=super_op.id,
                role_id=super_role.id,
                assigned_by_operator_id=root_op.id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_op.id,
                token_digest=super_digest,
                csrf_token=super_csrf,
                authenticated_at=now,
                acr="urn:community:pwd",
                amr=["pwd"],
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                username="superadmin_test",
                password_hash=hash_password("SuperSecureP2!"),
                account_type="platform_operator",
                operator_id=super_op.id,
                status="active",
            )
        )

        # Communities
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="springfield-ott",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=Decimal("10.00"),
            policy_version=1,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="shelbyville-ott",
            status="active",
            photo_requirement="REQUIRED",
            default_commission_rate=Decimal("12.00"),
            policy_version=1,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        # Region in Community A
        region_a = Region(
            id=region_a_id,
            tenant_id=tenant_a_id,
            name="Visakhapatnam",
            code="AP_VSKP",
            state="Andhra Pradesh",
            status="active",
        )
        session.add(region_a)

        # Roles and permissions for Community A
        from app.modules.authorization.seeding import seed_tenant_role_permissions
        role_admin_a, role_agent_a = seed_tenant_role_permissions(session, tenant_a_id)

        # Admin A1 (Requester)
        user_a1 = User(id=admin_a1_user_id, status="active")
        session.add(user_a1)
        session.flush()
        mem_a1 = TenantMembership(
            id=admin_a1_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a1_user_id,
            mobile_e164="+919876543211",
            status="active",
        )
        session.add(mem_a1)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a1_mem_id,
                role_id=role_admin_a.id,
                assigned_by_membership_id=admin_a1_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a1_mem_id,
                token_digest=admin_a1_digest,
                csrf_token=admin_a1_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                username="admin_a1",
                password_hash=hash_password("AdminPassA1!"),
                account_type="tenant_user",
                user_id=admin_a1_user_id,
                status="active",
            )
        )
        session.add(
            UserRegionAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                user_id=admin_a1_user_id,
                region_id=region_a_id,
                status="active",
            )
        )

        # Admin A2 (Reviewer)
        user_a2 = User(id=admin_a2_user_id, status="active")
        session.add(user_a2)
        session.flush()
        mem_a2 = TenantMembership(
            id=admin_a2_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a2_user_id,
            mobile_e164="+919876543212",
            status="active",
        )
        session.add(mem_a2)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a2_mem_id,
                role_id=role_admin_a.id,
                assigned_by_membership_id=admin_a2_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a2_mem_id,
                token_digest=admin_a2_digest,
                csrf_token=admin_a2_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                username="admin_a2",
                password_hash=hash_password("AdminPassA2!"),
                account_type="tenant_user",
                user_id=admin_a2_user_id,
                status="active",
            )
        )

        # Roles and permissions for Community B
        role_admin_b, role_agent_b = seed_tenant_role_permissions(session, tenant_b_id)

        user_b = User(id=admin_b_user_id, status="active")
        session.add(user_b)
        session.flush()
        mem_b = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919876543220",
            status="active",
        )
        session.add(mem_b)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=admin_b_mem_id,
                role_id=role_admin_b.id,
                assigned_by_membership_id=admin_b_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=admin_b_mem_id,
                token_digest=admin_b_digest,
                csrf_token=admin_b_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                username="admin_shelbyville",
                password_hash=hash_password("AdminPassShelbyville1!"),
                account_type="tenant_user",
                user_id=admin_b_user_id,
                status="active",
            )
        )
        session.commit()

    encryption_key = "12345678901234567890123456789012"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
        identity_encryption_key=encryption_key,
    )
    app = create_app(settings)

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

    try:
        yield {
            "app": app,
            "factory": factory,
            "settings": settings,
            "tenant_a_id": str(tenant_a_id),
            "tenant_b_id": str(tenant_b_id),
            "region_a_id": str(region_a_id),
            "super_token": super_token,
            "super_csrf": super_csrf,
            "admin_a1_token": admin_a1_token,
            "admin_a1_csrf": admin_a1_csrf,
            "admin_a1_user_id": str(admin_a1_user_id),
            "admin_a2_token": admin_a2_token,
            "admin_a2_csrf": admin_a2_csrf,
            "admin_a2_user_id": str(admin_a2_user_id),
            "admin_b_token": admin_b_token,
            "admin_b_csrf": admin_b_csrf,
            "admin_b_user_id": str(admin_b_user_id),
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@asynccontextmanager
async def client_for(app, base_url="http://localhost:3000"):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client


async def upload_test_object(client, cookies, category, tenant_id=None):
    category_map = {
        "profile_photo": "agent-photo",
        "aadhaar": "aadhaar-document",
        "pan": "pan-document",
        "user_photo": "user-photo",
    }
    cat = category_map.get(category, category)
    img_bytes = create_test_image_bytes()
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    payload = {
        "category": cat,
        "image_base64": b64,
    }
    res = await client.post(
        "/api/v1/storage/upload",
        json=payload,
        cookies=cookies,
    )
    assert res.status_code == 201, res.text
    return res.json()["object_key"]


@pytest.mark.anyio
async def test_agent_appointment_submission_and_unapproved_access_control(appointment_harness):
    """Unapproved agent appointment is created in PENDING status and candidate cannot authenticate."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    tenant_a_id = appointment_harness["tenant_a_id"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}

    async with client_for(app) as client:
        # 1. Upload required verification docs
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        # 2. Admin A1 submits agent appointment
        payload = {
            "first_name": "Homer",
            "last_name": "Simpson",
            "username": "homer_agent",
            "password": "HomerPassword123!",
            "mobile_e164": "+919876543301",
            "email": "homer@springfield.test",
            "address": "742 Evergreen Terrace",
            "aadhaar_number": "234567890123",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "ABCDE1234F",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
            "proposed_commission_rate": "15.00",
            "region_ids": [region_a_id],
            "notes": "Proposed field agent for sector 7G",
        }

        res = await client.post(
            "/api/v1/agent-appointments",
            json=payload,
            cookies=cookies_a1,
        )
        assert res.status_code == 201, res.text
        appointment = res.json()
        assert appointment["status"] == "PENDING"
        assert appointment["candidate_name"] == "Homer Simpson"
        assert appointment["candidate_username"] == "homer_agent"
        assert appointment["proposed_commission_rate"] == "15.00"
        appointment_id = appointment["id"]

        # 3. Verify in database: User, Membership, Credential, RoleAssignment are inactive/PENDING
        with factory() as session:
            appt_db = session.execute(
                select(AgentAppointment).where(AgentAppointment.id == UUID(appointment_id))
            ).scalar_one()
            assert appt_db.status == "PENDING"

            user_db = session.execute(
                select(User).where(User.id == appt_db.user_id)
            ).scalar_one()
            assert user_db.status == "inactive"

            cred_db = session.execute(
                select(AccountCredential).where(AccountCredential.user_id == appt_db.user_id)
            ).scalar_one()
            assert cred_db.status == "inactive"

            role_assign_db = session.execute(
                select(UserRoleAssignment).where(UserRoleAssignment.membership_id == appt_db.membership_id)
            ).scalar_one()
            assert role_assign_db.status == "inactive"

            region_assign_db = session.execute(
                select(UserRegionAssignment).where(UserRegionAssignment.user_id == appt_db.user_id)
            ).scalar_one()
            assert region_assign_db.status == "inactive"

        # 4. Critical security check: Unapproved agent MUST NOT be able to log in
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": "homer_agent", "password": "HomerPassword123!"},
        )
        assert login_res.status_code == 401, "Pending agent was erroneously allowed to log in!"

        # 5. Critical security check: Unapproved agent cannot access agent endpoints
        unauth_res = await client.get("/api/v1/agent/policy")
        assert unauth_res.status_code == 401


@pytest.mark.anyio
async def test_self_approval_prohibited_and_cross_tenant_isolation(appointment_harness):
    """Requesters cannot approve their own appointments; other tenants cannot view/approve."""
    app = appointment_harness["app"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_b_token = appointment_harness["admin_b_token"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_b = {"community_session": admin_b_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        res = await client.post(
            "/api/v1/agent-appointments",
            json={
                "first_name": "Carl",
                "last_name": "Carlson",
                "username": "carl_agent",
                "password": "CarlPassword123!",
                "mobile_e164": "+919876543302",
                "aadhaar_number": "345678901234",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "BCDEF1234G",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "region_ids": [region_a_id],
            },
            cookies=cookies_a1,
        )
        assert res.status_code == 201, res.text
        appointment_id = res.json()["id"]

        # 1. Admin A1 (the requester) attempts to approve their own appointment -> 403 Forbidden
        self_approve_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/approve",
            json={"reason": "Self approval attempt"},
            cookies=cookies_a1,
        )
        assert self_approve_res.status_code == 403
        assert self_approve_res.json()["error"]["code"] == "HTTP_ERROR"

        # 2. Admin B (cross-tenant) attempts to view Tenant A's appointment -> 403 Forbidden
        cross_get_res = await client.get(
            f"/api/v1/agent-appointments/{appointment_id}",
            cookies=cookies_b,
        )
        assert cross_get_res.status_code == 403
        assert cross_get_res.json()["error"]["code"] == "HTTP_ERROR"

        # 3. Admin B (cross-tenant) attempts to approve Tenant A's appointment -> 403 Forbidden
        cross_approve_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/approve",
            json={"reason": "Cross-tenant intrusion"},
            cookies=cookies_b,
        )
        assert cross_approve_res.status_code == 403
        assert cross_approve_res.json()["error"]["code"] == "HTTP_ERROR"

        # 4. Admin B (cross-tenant) attempts to reject Tenant A's appointment -> 403 Forbidden
        cross_reject_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/reject",
            json={"reason": "Cross-tenant intrusion"},
            cookies=cookies_b,
        )
        assert cross_reject_res.status_code == 403
        assert cross_reject_res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_appointment_approval_workflow_activates_agent_and_enforces_idempotency(appointment_harness):
    """Peer reviewer approves appointment; records activate; agent can log in; terminal 409 conflict enforced."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_a2_token = appointment_harness["admin_a2_token"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_a2 = {"community_session": admin_a2_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        submit_res = await client.post(
            "/api/v1/agent-appointments",
            json={
                "first_name": "Lenny",
                "last_name": "Leonard",
                "username": "lenny_agent",
                "password": "LennyPassword123!",
                "mobile_e164": "+919876543303",
                "aadhaar_number": "456789012345",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "CDEFG1234H",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "proposed_commission_rate": "12.50",
                "region_ids": [region_a_id],
            },
            cookies=cookies_a1,
        )
        assert submit_res.status_code == 201
        appointment_id = submit_res.json()["id"]

        # 1. Admin A2 (peer reviewer in same tenant) approves appointment
        approve_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/approve",
            json={"reason": "Background verification passed."},
            cookies=cookies_a2,
        )
        assert approve_res.status_code == 200, approve_res.text
        approved_summary = approve_res.json()
        assert approved_summary["status"] == "APPROVED"
        assert len(approved_summary["decisions"]) == 1
        assert approved_summary["decisions"][0]["decision"] == "APPROVED"
        assert approved_summary["decisions"][0]["reason"] == "Background verification passed."

        # 2. Verify all records in database are now "active"
        with factory() as session:
            appt_db = session.execute(
                select(AgentAppointment).where(AgentAppointment.id == UUID(appointment_id))
            ).scalar_one()
            assert appt_db.status == "APPROVED"

            user_db = session.execute(
                select(User).where(User.id == appt_db.user_id)
            ).scalar_one()
            assert user_db.status == "active"

            cred_db = session.execute(
                select(AccountCredential).where(AccountCredential.user_id == appt_db.user_id)
            ).scalar_one()
            assert cred_db.status == "active"

            role_assign_db = session.execute(
                select(UserRoleAssignment).where(UserRoleAssignment.membership_id == appt_db.membership_id)
            ).scalar_one()
            assert role_assign_db.status == "active"

            region_assign_db = session.execute(
                select(UserRegionAssignment).where(UserRegionAssignment.user_id == appt_db.user_id)
            ).scalar_one()
            assert region_assign_db.status == "active"

            decision_db = session.execute(
                select(ApprovalDecision).where(ApprovalDecision.appointment_id == UUID(appointment_id))
            ).scalar_one()
            assert decision_db.decision == "APPROVED"

        # 3. Newly approved agent can now log in successfully!
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": "lenny_agent", "password": "LennyPassword123!"},
        )
        assert login_res.status_code == 200, login_res.text
        login_data = login_res.json()
        assert login_data["role"] == "AGENT"
        agent_session_cookie = login_res.headers["set-cookie"].split(";")[0].split("=")[1]

        # 4. Newly approved agent can now access agent policy with active session
        policy_res = await client.get(
            "/api/v1/agent/policy",
            cookies={"community_session": agent_session_cookie},
        )
        assert policy_res.status_code == 200, policy_res.text

        # 5. Terminal state transition idempotency / 409 conflict checks:
        # Re-approving already approved appointment -> 409 Conflict
        re_approve_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/approve",
            json={"reason": "Duplicate approve"},
            cookies=cookies_a2,
        )
        assert re_approve_res.status_code == 409
        assert re_approve_res.json()["error"]["code"] == "HTTP_ERROR"

        # Rejecting already approved appointment -> 409 Conflict
        reject_approved_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/reject",
            json={"reason": "Cannot reject approved"},
            cookies=cookies_a2,
        )
        assert reject_approved_res.status_code == 409
        assert reject_approved_res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_appointment_rejection_workflow(appointment_harness):
    """Reviewer rejects appointment; records stay inactive; candidate cannot log in; terminal 409 enforced."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_a2_token = appointment_harness["admin_a2_token"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_a2 = {"community_session": admin_a2_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        submit_res = await client.post(
            "/api/v1/agent-appointments",
            json={
                "first_name": "Barney",
                "last_name": "Gumble",
                "username": "barney_agent",
                "password": "BarneyPassword123!",
                "mobile_e164": "+919876543304",
                "aadhaar_number": "567890123456",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "DEFGH1234I",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "region_ids": [region_a_id],
            },
            cookies=cookies_a1,
        )
        assert submit_res.status_code == 201
        appointment_id = submit_res.json()["id"]

        # Admin A2 rejects appointment with reason
        reject_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/reject",
            json={"reason": "Failed address verification."},
            cookies=cookies_a2,
        )
        assert reject_res.status_code == 200, reject_res.text
        rejected_summary = reject_res.json()
        assert rejected_summary["status"] == "REJECTED"
        assert len(rejected_summary["decisions"]) == 1
        assert rejected_summary["decisions"][0]["decision"] == "REJECTED"
        assert rejected_summary["decisions"][0]["reason"] == "Failed address verification."

        # Verify DB state
        with factory() as session:
            appt_db = session.execute(
                select(AgentAppointment).where(AgentAppointment.id == UUID(appointment_id))
            ).scalar_one()
            assert appt_db.status == "REJECTED"

            cred_db = session.execute(
                select(AccountCredential).where(AccountCredential.user_id == appt_db.user_id)
            ).scalar_one()
            assert cred_db.status == "inactive"

        # Rejected candidate cannot log in
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": "barney_agent", "password": "BarneyPassword123!"},
        )
        assert login_res.status_code == 401

        # Approving already rejected appointment -> 409 Conflict
        approve_rejected_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/approve",
            json={"reason": "Cannot approve rejected"},
            cookies=cookies_a2,
        )
        assert approve_rejected_res.status_code == 409
        assert approve_rejected_res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_platform_super_admin_appointment_workflow(appointment_harness):
    """Super Admin can list, submit, and review appointments across communities."""
    app = appointment_harness["app"]
    super_token = appointment_harness["super_token"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    tenant_a_id = appointment_harness["tenant_a_id"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_super = {"community_platform_session": super_token}
    cookies_a1 = {"community_session": admin_a1_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        # Admin A1 submits appointment for Community A
        res = await client.post(
            "/api/v1/agent-appointments",
            json={
                "first_name": "Waylon",
                "last_name": "Smithers",
                "username": "smithers_agent",
                "password": "SmithersPassword123!",
                "mobile_e164": "+919876543305",
                "aadhaar_number": "678901234567",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "EFGHI1234J",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "region_ids": [region_a_id],
            },
            cookies=cookies_a1,
        )
        assert res.status_code == 201, res.text
        appointment_id = res.json()["id"]

        # Super Admin lists appointments for Community A
        list_res = await client.get(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments",
            cookies=cookies_super,
        )
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert any(item["id"] == appointment_id for item in items)

        # Super Admin approves Community A appointment
        approve_res = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{appointment_id}/approve",
            json={"reason": "Super Admin executive approval"},
            cookies=cookies_super,
        )
        assert approve_res.status_code == 200
        assert approve_res.json()["status"] == "APPROVED"

        # Super Admin directly submits an appointment for Community A
        res_super = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments",
            json={
                "first_name": "Ned",
                "last_name": "Flanders",
                "username": "ned_agent",
                "password": "NedPassword123!",
                "mobile_e164": "+919876543306",
                "aadhaar_number": "789012345678",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "FGHIJ1234K",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "region_ids": [region_a_id],
            },
            cookies=cookies_super,
        )
        assert res_super.status_code == 201
        super_appt_id = res_super.json()["id"]

        # Super Admin cannot self-approve their own submitted appointment -> 403 Forbidden
        self_approve_super = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{super_appt_id}/approve",
            json={"reason": "Self-approval attempt by super admin"},
            cookies=cookies_super,
        )
        assert self_approve_super.status_code == 403
        assert self_approve_super.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_sec01_platform_appointments_isolation_and_superadmin_guard(appointment_harness):
    """SEC-01 & SEC-06: Tenant admins cannot access platform appointment endpoints; superadmin required."""
    app = appointment_harness["app"]
    tenant_a_id = appointment_harness["tenant_a_id"]
    tenant_b_id = appointment_harness["tenant_b_id"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    super_token = appointment_harness["super_token"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_admin_a1 = {"community_session": admin_a1_token}
    cookies_super = {"community_platform_session": super_token}

    async with client_for(app) as client:
        # 1. Tenant admin attempts to list appointments via platform route -> 403
        list_res = await client.get(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments",
            cookies=cookies_admin_a1,
        )
        assert list_res.status_code == 403
        assert list_res.json()["error"]["code"] == "HTTP_ERROR"

        # 2. Tenant admin attempts cross-tenant list via platform route -> 403
        cross_list_res = await client.get(
            f"/api/v1/platform/communities/{tenant_b_id}/appointments",
            cookies=cookies_admin_a1,
        )
        assert cross_list_res.status_code == 403
        assert cross_list_res.json()["error"]["code"] == "HTTP_ERROR"

        # 3. Tenant admin attempts platform submit -> 403
        photo_key = await upload_test_object(client, cookies_admin_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_admin_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_admin_a1, "pan")

        submit_payload = {
            "first_name": "Test",
            "last_name": "Candidate",
            "username": "test_candidate_p1",
            "password": "Password123!",
            "mobile_e164": "+919876543999",
            "aadhaar_number": "999988887777",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "ZZZZZ1234Z",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
            "region_ids": [region_a_id],
        }
        submit_res = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments",
            json=submit_payload,
            cookies=cookies_admin_a1,
        )
        assert submit_res.status_code == 403
        assert submit_res.json()["error"]["code"] == "HTTP_ERROR"

        # 4. Tenant admin attempts platform approve/reject/cancel/get on another community -> 403
        dummy_appt_id = uuid4()
        cross_approve_res = await client.post(
            f"/api/v1/platform/communities/{tenant_b_id}/appointments/{dummy_appt_id}/approve",
            json={"reason": "Attacking other community"},
            cookies=cookies_admin_a1,
        )
        assert cross_approve_res.status_code == 403
        assert cross_approve_res.json()["error"]["code"] == "HTTP_ERROR"

        cross_reject_res = await client.post(
            f"/api/v1/platform/communities/{tenant_b_id}/appointments/{dummy_appt_id}/reject",
            json={"reason": "Attacking other community"},
            cookies=cookies_admin_a1,
        )
        assert cross_reject_res.status_code == 403
        assert cross_reject_res.json()["error"]["code"] == "HTTP_ERROR"

        cross_cancel_res = await client.post(
            f"/api/v1/platform/communities/{tenant_b_id}/appointments/{dummy_appt_id}/cancel",
            json={"reason": "Attacking other community"},
            cookies=cookies_admin_a1,
        )
        assert cross_cancel_res.status_code == 403
        assert cross_cancel_res.json()["error"]["code"] == "HTTP_ERROR"

        cross_get_res = await client.get(
            f"/api/v1/platform/communities/{tenant_b_id}/appointments/{dummy_appt_id}",
            cookies=cookies_admin_a1,
        )
        assert cross_get_res.status_code == 403
        assert cross_get_res.json()["error"]["code"] == "HTTP_ERROR"

        # 5. Super Admin querying a non-existent community returns 404
        random_comm_id = uuid4()
        not_found_res = await client.get(
            f"/api/v1/platform/communities/{random_comm_id}/appointments",
            cookies=cookies_super,
        )
        assert not_found_res.status_code == 404
        assert not_found_res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_sec02_legacy_agent_provisioning_inactive_pending_and_login_gated(appointment_harness):
    """SEC-02: POST /agents creates inactive candidate with PENDING appointment; candidate cannot log in until approved."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_a2_token = appointment_harness["admin_a2_token"]
    tenant_b_id = appointment_harness["tenant_b_id"]
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_a2 = {"community_session": admin_a2_token}

    async with client_for(app) as client:
        # 1. Non-superadmin cannot query regions of another community
        cross_reg_res = await client.get(
            f"/api/v1/agents/regions?community_id={tenant_b_id}",
            cookies=cookies_a1,
        )
        assert cross_reg_res.status_code == 403
        assert cross_reg_res.json()["error"]["code"] == "HTTP_ERROR"

        # 2. Upload verification documents
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        # 3. Create candidate via legacy /api/v1/agents
        burns_email = "burns@springfield.test"
        v_token = "burns-email-token-123"
        now_dt = datetime.now(UTC)
        with factory() as session:
            session.add(
                EmailOtpChallenge(
                    id=uuid4(),
                    tenant_id=UUID(appointment_harness["tenant_a_id"]),
                    email=burns_email,
                    purpose="email_verification",
                    otp_hash="hash",
                    otp_salt="salt",
                    failed_attempts=0,
                    resend_count=0,
                    dispatch_id=uuid4(),
                    created_at=now_dt,
                    expires_at=now_dt + timedelta(minutes=10),
                    verified_at=now_dt,
                    verification_token=v_token,
                )
            )
            session.commit()

        agent_payload = {
            "first_name": "Montgomery",
            "last_name": "Burns",
            "username": "burns_agent",
            "password": "BurnsPassword123!",
            "mobile_e164": "+919876543401",
            "email": burns_email,
            "email_verification_token": v_token,
            "state": "Andhra Pradesh",
            "district": "Visakhapatnam",
            "address": "Burns Manor",
            "aadhaar_number": "111122223333",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "AAAAA1234A",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
            "region_ids": [region_a_id],
        }
        res = await client.post("/api/v1/agents", json=agent_payload, cookies=cookies_a1)
        assert res.status_code == 201, res.text
        summary = res.json()
        assert summary["status"] == "pending"
        candidate_user_id = UUID(summary["user_id"])

        # 4. Verify candidate records in DB are inactive and appointment is PENDING
        with factory() as session:
            user_db = session.execute(select(User).where(User.id == candidate_user_id)).scalar_one()
            assert user_db.status == "inactive"

            cred_db = session.execute(
                select(AccountCredential).where(AccountCredential.user_id == candidate_user_id)
            ).scalar_one()
            assert cred_db.status == "inactive"

            mem_db = session.execute(
                select(TenantMembership).where(TenantMembership.user_id == candidate_user_id)
            ).scalar_one()
            assert mem_db.status == "inactive"

            role_db = session.execute(
                select(UserRoleAssignment).where(UserRoleAssignment.membership_id == mem_db.id)
            ).scalar_one()
            assert role_db.status == "inactive"

            appt_db = session.execute(
                select(AgentAppointment).where(AgentAppointment.user_id == candidate_user_id)
            ).scalar_one()
            assert appt_db.status == "PENDING"
            appointment_id = appt_db.id

            # Verify NO fabricated ApprovalDecision exists
            decisions = session.execute(
                select(ApprovalDecision).where(ApprovalDecision.appointment_id == appointment_id)
            ).scalars().all()
            assert len(decisions) == 0

        # 5. Candidate CANNOT log in while PENDING
        login_fail = await client.post(
            "/api/v1/auth/login",
            json={"username": "burns_agent", "password": "BurnsPassword123!"},
        )
        assert login_fail.status_code == 401

        # 6. Admin A2 (peer reviewer) approves the appointment
        approve_res = await client.post(
            f"/api/v1/agent-appointments/{appointment_id}/approve",
            json={"reason": "Approved candidate Burns."},
            cookies=cookies_a2,
        )
        assert approve_res.status_code == 200

        # 7. Candidate CAN now log in
        login_ok = await client.post(
            "/api/v1/auth/login",
            json={"username": "burns_agent", "password": "BurnsPassword123!"},
        )
        assert login_ok.status_code == 200
        assert login_ok.json()["role"] == "AGENT"


@pytest.mark.anyio
async def test_sec03_privilege_escalation_and_schema_validation(appointment_harness):
    """SEC-03: Submitting appointment with admin role or extra fields is rejected; approval revalidates role."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_a2_token = appointment_harness["admin_a2_token"]
    tenant_a_id = UUID(appointment_harness["tenant_a_id"])
    tenant_b_id = UUID(appointment_harness["tenant_b_id"])
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_a2 = {"community_session": admin_a2_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        with factory() as session:
            admin_role_a = session.execute(
                select(Role).where(Role.tenant_id == tenant_a_id, Role.code == "admin")
            ).scalar_one()
            agent_role_b = session.execute(
                select(Role).where(Role.tenant_id == tenant_b_id, Role.code == "agent")
            ).scalar_one()
            admin_role_a_id = admin_role_a.id
            agent_role_b_id = agent_role_b.id

        base_payload = {
            "first_name": "Milhouse",
            "last_name": "VanHouten",
            "username": "milhouse_agent",
            "password": "MilhousePass123!",
            "mobile_e164": "+919876543402",
            "aadhaar_number": "222233334444",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "BBBBB1234B",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
            "region_ids": [region_a_id],
        }

        # 1. Attempt to submit with admin role ID -> 400
        escalation_payload = dict(base_payload, role_id=str(admin_role_a_id))
        res_esc = await client.post("/api/v1/agent-appointments", json=escalation_payload, cookies=cookies_a1)
        assert res_esc.status_code == 400
        assert res_esc.json()["error"]["code"] == "HTTP_ERROR"

        # 2. Attempt to submit with another tenant's role ID -> 404 (not found in caller tenant)
        cross_payload = dict(base_payload, role_id=str(agent_role_b_id))
        res_cross = await client.post("/api/v1/agent-appointments", json=cross_payload, cookies=cookies_a1)
        assert res_cross.status_code == 404
        assert res_cross.json()["error"]["code"] == "HTTP_ERROR"

        # 3. Attempt to submit with extra unexpected fields -> 422 Unprocessable Entity
        extra_payload = dict(base_payload, is_admin=True, superuser="yes")
        res_extra = await client.post("/api/v1/agent-appointments", json=extra_payload, cookies=cookies_a1)
        assert res_extra.status_code == 422
        assert res_extra.json()["error"]["code"] == "VALIDATION_ERROR"

        # 4. Attempt to submit decision with extra unexpected fields -> 422 Unprocessable Entity
        valid_res = await client.post("/api/v1/agent-appointments", json=base_payload, cookies=cookies_a1)
        assert valid_res.status_code == 201
        appt_id = valid_res.json()["id"]

        decision_extra = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/approve",
            json={"reason": "ok", "grant_superadmin": True},
            cookies=cookies_a2,
        )
        assert decision_extra.status_code == 422
        assert decision_extra.json()["error"]["code"] == "VALIDATION_ERROR"

        # 5. Tamper appointment in DB to point to admin role; approve should catch it and reject with 400
        with factory() as session:
            appt_db = session.execute(
                select(AgentAppointment).where(AgentAppointment.id == UUID(appt_id))
            ).scalar_one()
            appt_db.role_id = admin_role_a_id
            session.commit()

        tampered_approve = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/approve",
            json={"reason": "Approving tampered appointment."},
            cookies=cookies_a2,
        )
        assert tampered_approve.status_code == 400
        assert tampered_approve.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_sec04_fail_closed_database_rbac_and_permission_revocation(appointment_harness):
    """SEC-04: Capability check strictly fails closed without hardcoded fallbacks; revoking permission takes effect immediately."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_a2_token = appointment_harness["admin_a2_token"]
    tenant_a_id = UUID(appointment_harness["tenant_a_id"])
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_a2 = {"community_session": admin_a2_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        submit_res = await client.post(
            "/api/v1/agent-appointments",
            json={
                "first_name": "Nelson",
                "last_name": "Muntz",
                "username": "nelson_agent",
                "password": "NelsonPassword123!",
                "mobile_e164": "+919876543403",
                "aadhaar_number": "333344445555",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "CCCCC1234C",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "region_ids": [region_a_id],
            },
            cookies=cookies_a1,
        )
        assert submit_res.status_code == 201
        appt_id = submit_res.json()["id"]

        # Revoke AGENT_APPROVE from the admin role in the DB
        with factory() as session:
            admin_role_a = session.execute(
                select(Role).where(Role.tenant_id == tenant_a_id, Role.code == "admin")
            ).scalar_one()
            admin_role_a_id = admin_role_a.id
            session.execute(
                select(RolePermission).where(
                    RolePermission.tenant_id == tenant_a_id,
                    RolePermission.role_id == admin_role_a_id,
                    RolePermission.permission_code == PermissionCode.AGENT_APPROVE.value,
                )
            ).scalar_one()
            from sqlalchemy import delete
            session.execute(
                delete(RolePermission).where(
                    RolePermission.tenant_id == tenant_a_id,
                    RolePermission.role_id == admin_role_a_id,
                    RolePermission.permission_code == PermissionCode.AGENT_APPROVE.value,
                )
            )
            session.commit()

        # Admin A2 attempts to approve -> MUST fail closed with 403 Forbidden
        revoked_res = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/approve",
            json={"reason": "Should be denied."},
            cookies=cookies_a2,
        )
        assert revoked_res.status_code == 403
        assert revoked_res.json()["error"]["code"] == "HTTP_ERROR"

        # Restore permission
        with factory() as session:
            session.add(
                RolePermission(
                    tenant_id=tenant_a_id,
                    role_id=admin_role_a_id,
                    permission_code=PermissionCode.AGENT_APPROVE.value,
                    status="active",
                )
            )
            session.commit()

        # Admin A2 can now approve
        restored_res = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/approve",
            json={"reason": "Approved after permission restored."},
            cookies=cookies_a2,
        )
        assert restored_res.status_code == 200


@pytest.mark.anyio
async def test_sec05_concurrency_protection_and_database_unique_constraint(appointment_harness):
    """SEC-05: Double decision triggers 409 Conflict; database unique constraint enforces one decision per appointment."""
    app = appointment_harness["app"]
    factory = appointment_harness["factory"]
    admin_a1_token = appointment_harness["admin_a1_token"]
    admin_a2_token = appointment_harness["admin_a2_token"]
    tenant_a_id = UUID(appointment_harness["tenant_a_id"])
    region_a_id = appointment_harness["region_a_id"]

    cookies_a1 = {"community_session": admin_a1_token}
    cookies_a2 = {"community_session": admin_a2_token}

    async with client_for(app) as client:
        photo_key = await upload_test_object(client, cookies_a1, "profile_photo")
        aadhaar_key = await upload_test_object(client, cookies_a1, "aadhaar")
        pan_key = await upload_test_object(client, cookies_a1, "pan")

        submit_res = await client.post(
            "/api/v1/agent-appointments",
            json={
                "first_name": "Ralph",
                "last_name": "Wiggum",
                "username": "ralph_agent",
                "password": "RalphPassword123!",
                "mobile_e164": "+919876543404",
                "aadhaar_number": "444455556666",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "DDDDD1234D",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "region_ids": [region_a_id],
            },
            cookies=cookies_a1,
        )
        assert submit_res.status_code == 201
        appt_id = submit_res.json()["id"]

        # First approval succeeds
        app_res1 = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/approve",
            json={"reason": "First approval."},
            cookies=cookies_a2,
        )
        assert app_res1.status_code == 200

        # Concurrent/subsequent approval fails with 409 Conflict
        app_res2 = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/approve",
            json={"reason": "Second approval attempt."},
            cookies=cookies_a2,
        )
        assert app_res2.status_code == 409
        assert app_res2.json()["error"]["code"] == "HTTP_ERROR"

        # Rejecting also fails with 409 Conflict
        rej_res = await client.post(
            f"/api/v1/agent-appointments/{appt_id}/reject",
            json={"reason": "Subsequent reject attempt."},
            cookies=cookies_a2,
        )
        assert rej_res.status_code == 409
        assert rej_res.json()["error"]["code"] == "HTTP_ERROR"

        # Database unique constraint test: directly inserting a duplicate ApprovalDecision raises IntegrityError
        with factory() as session:
            duplicate_decision = ApprovalDecision(
                id=uuid4(),
                tenant_id=tenant_a_id,
                appointment_id=UUID(appt_id),
                decided_by_user_id=UUID(appointment_harness["admin_a2_user_id"]),
                decision="APPROVED",
                reason="Direct insert duplicate bypass attempt",
                decided_at=datetime.now(UTC),
            )
            session.add(duplicate_decision)
            with pytest.raises(IntegrityError):
                session.commit()


