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
from app.core.encryption import decrypt_identity_number
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.registration_photos.models import StoredObject
from app.modules.registration_photos.storage import MockedR2Storage
from app.modules.tenants.models import CommissionAuditLog, Tenant, TenantDomain
from app.modules.agents.models import AgentProfile, SubscriberProfile


def create_test_image_bytes(format: str = "JPEG") -> bytes:
    buf = BytesIO()
    img = Image.new("RGB", (100, 100), color=(73, 109, 137))
    img.save(buf, format=format)
    return buf.getvalue()


@pytest.fixture
def phase5_harness(tmp_path):
    database_path = tmp_path / "phase5-test.sqlite"
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
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    now = datetime.now(UTC)

    # 1. Platform Operator (Super Admin)
    super_op_id = uuid4()
    super_token = "test-superadmin-token-phase5"
    super_digest = __import__("hashlib").sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = __import__("hashlib").sha256(b"superadmin-csrf-phase5").hexdigest()

    # 2. Community A and Community B
    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    # 3. Community A Admin
    admin_a_user_id = uuid4()
    admin_a_mem_id = uuid4()
    admin_a_token = "test-admin-a-token-phase5"
    admin_a_digest = __import__("hashlib").sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = __import__("hashlib").sha256(b"admin-a-csrf-phase5").hexdigest()

    # 4. Community B Admin
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()
    admin_b_token = "test-admin-b-token-phase5"
    admin_b_digest = __import__("hashlib").sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = __import__("hashlib").sha256(b"admin-b-csrf-phase5").hexdigest()

    with factory.begin() as session:
        # Platform Operator setup
        root_op = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        super_op = PlatformOperator(
            id=super_op_id,
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
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                username="superadmin_p5",
                password_hash=hash_password("SuperSecureP5!"),
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

        # Admin A
        user_a = User(id=admin_a_user_id, status="active")
        session.add(user_a)
        session.flush()

        mem_a = TenantMembership(
            id=admin_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a_user_id,
            mobile_e164="+919876543210",
            status="active",
        )
        from app.modules.authorization.seeding import seed_tenant_role_permissions
        role_a, role_agent_a = seed_tenant_role_permissions(session, tenant_a_id)
        session.add(mem_a)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a_mem_id,
                role_id=role_a.id,
                assigned_by_membership_id=admin_a_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a_mem_id,
                token_digest=admin_a_digest,
                csrf_token=admin_a_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                username="admin_springfield",
                password_hash=hash_password("AdminPassSpringfield1!"),
                account_type="tenant_user",
                user_id=admin_a_user_id,
                status="active",
            )
        )

        # Admin B
        user_b = User(id=admin_b_user_id, status="active")
        session.add(user_b)
        session.flush()

        mem_b = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919876543211",
            status="active",
        )
        role_b, role_agent_b = seed_tenant_role_permissions(session, tenant_b_id)
        session.add(mem_b)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=admin_b_mem_id,
                role_id=role_b.id,
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
            "super_token": super_token,
            "super_csrf": super_csrf,
            "admin_a_token": admin_a_token,
            "admin_a_csrf": admin_a_csrf,
            "admin_b_token": admin_b_token,
            "admin_b_csrf": admin_b_csrf,
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


@pytest.mark.anyio
async def test_multi_admin_management_and_tenant_isolation(phase5_harness):
    app = phase5_harness["app"]
    super_token = phase5_harness["super_token"]
    admin_a_token = phase5_harness["admin_a_token"]
    tenant_a_id = phase5_harness["tenant_a_id"]
    tenant_b_id = phase5_harness["tenant_b_id"]

    async with client_for(app) as client:
        # 1. Super Admin lists all administrators across all communities
        res = await client.get(
            "/api/v1/platform/administrators",
            cookies={"community_platform_session": super_token},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["total"] >= 2
        slugs = {item["community_slug"] for item in data["items"]}
        assert "springfield-ott" in slugs
        assert "shelbyville-ott" in slugs

        # 2. Non-super admin cannot access global administrators endpoint
        forbidden_res = await client.get(
            "/api/v1/platform/administrators",
            cookies={"community_session": admin_a_token},
        )
        assert forbidden_res.status_code == 403

        # 3. Admin A cannot create administrator for Community B
        cross_res = await client.post(
            f"/api/v1/platform/communities/{tenant_b_id}/administrators",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": phase5_harness["admin_a_csrf"], "Origin": "http://localhost:3000"},
            json={
                "username": "rogue_admin",
                "password": "RoguePassword123!",
                "mobile_e164": "+919999999999",
            },
        )
        assert cross_res.status_code == 403


@pytest.mark.anyio
async def test_r2_storage_upload_preview_and_isolation(phase5_harness):
    app = phase5_harness["app"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_b_token = phase5_harness["admin_b_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]

    image_bytes = create_test_image_bytes("JPEG")
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    async with client_for(app) as client:
        # 1. Upload via multipart form
        files = {"file": ("profile.jpg", image_bytes, "image/jpeg")}
        data = {"category": "agent-photo"}
        upload_res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files=files,
            data=data,
        )
        assert upload_res.status_code == 201
        upload_data = upload_res.json()
        photo_key = upload_data["object_key"]
        assert photo_key.startswith("agent-photos/")

        # 2. Upload via JSON base64
        json_upload = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            json={
                "category": "aadhaar-document",
                "image_base64": image_b64,
            },
        )
        assert json_upload.status_code == 201
        doc_key = json_upload.json()["object_key"]
        assert doc_key.startswith("aadhaar-documents/")

        # 3. Invalid image rejected
        corrupt_res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("corrupt.jpg", b"not-an-image-data", "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert corrupt_res.status_code == 422

        # 4. Presigned URL generation
        presigned_res = await client.get(
            f"/api/v1/storage/{photo_key}",
            cookies={"community_session": admin_a_token},
        )
        assert presigned_res.status_code == 200
        assert "presigned_url" in presigned_res.json()

        # 5. Direct preview stream
        preview_res = await client.get(
            f"/api/v1/storage/{photo_key}/preview",
            cookies={"community_session": admin_a_token},
        )
        assert preview_res.status_code == 200
        assert len(preview_res.content) > 0

        # 6. Tenant isolation: Admin B cannot access Admin A's stored object
        isolated_res = await client.get(
            f"/api/v1/storage/{photo_key}/preview",
            cookies={"community_session": admin_b_token},
        )
        assert isolated_res.status_code == 403


@pytest.mark.anyio
async def test_agent_registration_encryption_and_login(phase5_harness):
    app = phase5_harness["app"]
    factory = phase5_harness["factory"]
    super_token = phase5_harness["super_token"]
    super_csrf = phase5_harness["super_csrf"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    tenant_a_id = phase5_harness["tenant_a_id"]
    encryption_key = phase5_harness["settings"].identity_encryption_key

    # Upload test documents first
    image_bytes = create_test_image_bytes("JPEG")
    async with client_for(app) as client:
        # Upload profile photo, aadhaar doc, pan doc
        r1 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_key = r1.json()["object_key"]

        r2 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        aadhaar_doc_key = r2.json()["object_key"]

        r3 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        pan_doc_key = r3.json()["object_key"]

        # Register Agent
        agent_payload = {
            "first_name": "Ravi",
            "last_name": "Shankar",
            "username": "agent_ravi",
            "password": "RaviSecurePassword123!",
            "mobile_e164": "+919876543299",
            "email": "ravi@example.com",
            "address": "42 Market Street, Springfield",
            "aadhaar_number": "123456789012",
            "aadhaar_document_key": aadhaar_doc_key,
            "pan_number": "ABCDE1234F",
            "pan_document_key": pan_doc_key,
            "profile_photo_key": photo_key,
            "commission_rate_override": 14.50,
            "status": "active",
        }

        reg_res = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json=agent_payload,
        )
        assert reg_res.status_code == 201
        agent_data = reg_res.json()
        assert agent_data["username"] == "agent_ravi"
        assert agent_data["ref_id"] is not None
        assert len(agent_data["ref_id"]) == 8
        assert agent_data["aadhaar_number_masked"] in ("XXXXXXXX9012", "XXXX-XXXX-9012")
        assert agent_data["pan_number_masked"] == "XXXXX1234F"
        assert float(agent_data["effective_commission_rate"]) == 14.50

        # Verify DB encryption at rest
        with factory() as session:
            db_profile = session.execute(
                select(AgentProfile).where(AgentProfile.ref_id == agent_data["ref_id"])
            ).scalar_one()
            # The database column must NOT contain the plain text
            assert "123456789012" not in db_profile.aadhaar_number_ciphertext
            assert "ABCDE1234F" not in db_profile.pan_number_ciphertext
            # Decryption with encryption key yields original plain text
            decrypted_aadhaar = decrypt_identity_number(
                db_profile.aadhaar_number_ciphertext, encryption_key
            )
            assert decrypted_aadhaar == "123456789012"
            decrypted_pan = decrypt_identity_number(
                db_profile.pan_number_ciphertext, encryption_key
            )
            assert decrypted_pan == "ABCDE1234F"

        # Agent cannot log in before approval (SEC-02)
        unapproved_login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:3000"},
            json={
                "username": "agent_ravi",
                "password": "RaviSecurePassword123!",
            },
        )
        assert unapproved_login.status_code == 401

        # Super Admin approves the pending appointment
        with factory() as session:
            appt = session.execute(
                select(AgentAppointment).where(
                    AgentAppointment.tenant_id == UUID(str(tenant_a_id)),
                    AgentAppointment.status == "PENDING",
                )
            ).scalars().first()
            assert appt is not None
            assert appt.status == "PENDING"
            appt_id = appt.id

        appr_res = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{appt_id}/approve",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
        )
        assert appr_res.status_code == 200

        # Agent can now log in via /api/v1/auth/login and receives role AGENT
        login_res = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:3000"},
            json={
                "username": "agent_ravi",
                "password": "RaviSecurePassword123!",
            },
        )
        assert login_res.status_code == 200
        login_data = login_res.json()
        assert login_data["role"] == "AGENT"
        assert login_data["username"] == "agent_ravi"


@pytest.mark.anyio
async def test_commission_inheritance_and_audit_trail(phase5_harness):
    app = phase5_harness["app"]
    super_token = phase5_harness["super_token"]
    super_csrf = phase5_harness["super_csrf"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    tenant_a_id = phase5_harness["tenant_a_id"]

    image_bytes = create_test_image_bytes("JPEG")
    async with client_for(app) as client:
        # Upload placeholder documents
        r1 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_key = r1.json()["object_key"]

        r2 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        aadhaar_key = r2.json()["object_key"]

        r3 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        pan_key = r3.json()["object_key"]

        # Register Agent without commission override
        reg_res = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Suresh",
                "last_name": "Rao",
                "username": "agent_suresh",
                "password": "SureshPassword123!",
                "mobile_e164": "+919876543288",
                "aadhaar_number": "987654321098",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "ZXCVB9876Q",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "commission_rate_override": None,
                "status": "active",
            },
        )
        assert reg_res.status_code == 201
        agent_data = reg_res.json()
        agent_id = agent_data["id"]
        # Community A default is 10.00%
        assert float(agent_data["effective_commission_rate"]) == 10.00
        assert agent_data["commission_rate_override"] is None

        # Super Admin updates Community A default commission rate to 11.50%
        comm_update = await client.patch(
            f"/api/v1/platform/communities/{tenant_a_id}/commission",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
            json={
                "default_commission_rate": 11.50,
                "reason": "Q4 Promotional rate increase",
            },
        )
        assert comm_update.status_code == 200

        # Agent Suresh now inherits the new 11.50% rate
        agent_check = await client.get(
            f"/api/v1/agents/{agent_id}",
            cookies={"community_session": admin_a_token},
        )
        assert agent_check.status_code == 200
        assert float(agent_check.json()["effective_commission_rate"]) == 11.50

        # Check commission audit log
        logs_res = await client.get(
            f"/api/v1/platform/communities/{tenant_a_id}/commission/audit-logs",
            cookies={"community_platform_session": super_token},
        )
        assert logs_res.status_code == 200
        logs = logs_res.json()["items"]
        assert len(logs) >= 1
        assert float(logs[0]["new_rate"]) == 11.50
        assert logs[0]["scope"] == "community"
        assert logs[0]["changed_by_username"] == "superadmin_p5"

        # Super Admin sets an agent-specific override to 16.00%
        override_res = await client.patch(
            f"/api/v1/agents/{agent_id}",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
            json={"commission_rate_override": 16.00},
        )
        assert override_res.status_code == 200
        assert float(override_res.json()["effective_commission_rate"]) == 16.00
        assert float(override_res.json()["commission_rate_override"]) == 16.00


@pytest.mark.anyio
async def test_subscriber_registration_and_photo_policy_enforcement(phase5_harness):
    app = phase5_harness["app"]
    factory = phase5_harness["factory"]
    super_token = phase5_harness["super_token"]
    super_csrf = phase5_harness["super_csrf"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    tenant_a_id = phase5_harness["tenant_a_id"]

    image_bytes = create_test_image_bytes("JPEG")
    async with client_for(app) as client:
        # Register an agent first
        r1 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_key = r1.json()["object_key"]

        r2 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        aadhaar_key = r2.json()["object_key"]

        r3 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        pan_key = r3.json()["object_key"]

        await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Pooja",
                "last_name": "Verma",
                "username": "agent_pooja",
                "password": "PoojaSecurePass1!",
                "mobile_e164": "+919876543277",
                "aadhaar_number": "112233445566",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "QWERT1234Y",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "status": "active",
            },
        )

        # Super Admin approves Pooja's pending appointment
        with factory() as session:
            appt = session.execute(
                select(AgentAppointment).where(
                    AgentAppointment.tenant_id == UUID(str(tenant_a_id)),
                    AgentAppointment.status == "PENDING",
                )
            ).scalars().first()
            assert appt is not None
            appt_id = appt.id

        appr_res = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{appt_id}/approve",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
        )
        assert appr_res.status_code == 200

        # Log in as Agent Pooja
        login_res = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:3000"},
            json={
                "username": "agent_pooja",
                "password": "PoojaSecurePass1!",
            },
        )
        assert login_res.status_code == 200
        agent_session_cookie = login_res.headers["set-cookie"].split(";")[0].split("=")[1]
        agent_csrf = login_res.json()["csrf_token"]

        # Step 1: Verify current Community A policy is OPTIONAL
        policy_res = await client.get(
            "/api/v1/agent/policy",
            cookies={"community_session": agent_session_cookie},
        )
        assert policy_res.status_code == 200
        assert policy_res.json()["photo_requirement"] == "OPTIONAL"

        # Agent registers a subscriber without photo -> SUCCESS
        sub_res_1 = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_session_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Amitabh Bachchan",
                "mobile_e164": "+919811122233",
                "email": "amitabh@example.com",
                "address": "Jalsa, Mumbai",
                "photo_key": None,
            },
        )
        assert sub_res_1.status_code == 201
        sub_1 = sub_res_1.json()
        assert sub_1["full_name"] == "Amitabh Bachchan"
        assert sub_1["id"] is not None
        assert sub_1["photo_key"] is None

        # Step 2: Super Admin toggles policy to REQUIRED
        policy_update = await client.patch(
            f"/api/v1/platform/communities/{tenant_a_id}/photo-policy",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
            json={"photo_requirement": "REQUIRED"},
        )
        assert policy_update.status_code == 200
        assert policy_update.json()["photo_requirement"] == "REQUIRED"

        # Agent checks policy -> now REQUIRED
        policy_res_2 = await client.get(
            "/api/v1/agent/policy",
            cookies={"community_session": agent_session_cookie},
        )
        assert policy_res_2.json()["photo_requirement"] == "REQUIRED"

        # Step 3: Agent attempts to register subscriber without photo -> REJECTED (422)
        sub_fail = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_session_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Shah Rukh Khan",
                "mobile_e164": "+919822233344",
                "photo_key": None,
            },
        )
        assert sub_fail.status_code == 422

        # Step 4: Agent uploads subscriber photo and registers -> SUCCESS
        r_user_photo = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": agent_session_cookie},
            files={"file": ("subscriber.jpg", image_bytes, "image/jpeg")},
            data={"category": "user-photo"},
        )
        user_photo_key = r_user_photo.json()["object_key"]

        sub_success = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_session_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Shah Rukh Khan",
                "mobile_e164": "+919822233344",
                "photo_key": user_photo_key,
            },
        )
        assert sub_success.status_code == 201
        sub_2 = sub_success.json()
        assert sub_2["full_name"] == "Shah Rukh Khan"
        assert sub_2["photo_key"] == user_photo_key

        # Step 5: Agent lists their registered subscribers
        list_res = await client.get(
            "/api/v1/agent/users",
            cookies={"community_session": agent_session_cookie},
        )
        assert list_res.status_code == 200
        subs_list = list_res.json()["items"]
        assert len(subs_list) == 2
        names = {s["full_name"] for s in subs_list}
        assert "Amitabh Bachchan" in names
        assert "Shah Rukh Khan" in names


@pytest.mark.anyio
async def test_agent_registration_stored_object_validation_and_rejections(phase5_harness):
    app = phase5_harness["app"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    admin_b_token = phase5_harness["admin_b_token"]
    admin_b_csrf = phase5_harness["admin_b_csrf"]

    image_bytes = create_test_image_bytes("JPEG")
    async with client_for(app) as client:
        # Admin A uploads valid photos
        r_photo_a = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_a = r_photo_a.json()["object_key"]

        r_aadhaar_a = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        aadhaar_a = r_aadhaar_a.json()["object_key"]

        r_pan_a = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        pan_a = r_pan_a.json()["object_key"]

        # Admin B uploads a photo in community B
        r_photo_b = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_b_token},
            files={"file": ("photo_b.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_b = r_photo_b.json()["object_key"]

        # 1. Fake photo key rejected (422)
        res_fake_photo = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Test",
                "last_name": "Agent",
                "username": "agent_test_fake",
                "password": "Password123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "123456789012",
                "aadhaar_document_key": aadhaar_a,
                "pan_number": "ABCDE1234F",
                "pan_document_key": pan_a,
                "profile_photo_key": "agent-photos/non-existent-key.jpg",
            },
        )
        assert res_fake_photo.status_code == 422

        # 2. Fake aadhaar key rejected (422)
        res_fake_aadhaar = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Test",
                "last_name": "Agent",
                "username": "agent_test_fake",
                "password": "Password123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "123456789012",
                "aadhaar_document_key": "aadhaar-documents/non-existent-key.jpg",
                "pan_number": "ABCDE1234F",
                "pan_document_key": pan_a,
                "profile_photo_key": photo_a,
            },
        )
        assert res_fake_aadhaar.status_code == 422

        # 3. Wrong category rejected (422) - passing pan key as aadhaar document
        res_wrong_cat = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Test",
                "last_name": "Agent",
                "username": "agent_test_fake",
                "password": "Password123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "123456789012",
                "aadhaar_document_key": pan_a,
                "pan_number": "ABCDE1234F",
                "pan_document_key": pan_a,
                "profile_photo_key": photo_a,
            },
        )
        assert res_wrong_cat.status_code == 422

        # 4. Cross-tenant object rejected (403) - Admin A attempts to use photo_b uploaded in Community B
        res_cross_tenant = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Test",
                "last_name": "Agent",
                "username": "agent_test_fake",
                "password": "Password123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "123456789012",
                "aadhaar_document_key": aadhaar_a,
                "pan_number": "ABCDE1234F",
                "pan_document_key": pan_a,
                "profile_photo_key": photo_b,
            },
        )
        assert res_cross_tenant.status_code == 403

        # 5. Missing mandatory photo rejected (422)
        res_missing_photo = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Test",
                "last_name": "Agent",
                "username": "agent_test_fake",
                "password": "Password123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "123456789012",
                "aadhaar_document_key": aadhaar_a,
                "pan_number": "ABCDE1234F",
                "pan_document_key": pan_a,
                "profile_photo_key": "",
            },
        )
        assert res_missing_photo.status_code == 422

        # 6. Successful registration with valid keys
        res_success = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Valid",
                "last_name": "Agent",
                "username": "agent_valid_p5",
                "password": "ValidPassword123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "123456789012",
                "aadhaar_document_key": aadhaar_a,
                "pan_number": "ABCDE1234F",
                "pan_document_key": pan_a,
                "profile_photo_key": photo_a,
            },
        )
        assert res_success.status_code == 201

        # 7. Duplicate username rejected (409)
        res_dup_user = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Another",
                "last_name": "Agent",
                "username": "agent_valid_p5",
                "password": "ValidPassword123!",
                "mobile_e164": "+919876540002",
                "aadhaar_number": "999988887777",
                "aadhaar_document_key": aadhaar_a,
                "pan_number": "ZZZZZ9999Z",
                "pan_document_key": pan_a,
                "profile_photo_key": photo_a,
            },
        )
        assert res_dup_user.status_code == 409

        # 8. Duplicate mobile in community rejected (409)
        res_dup_mobile = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Another",
                "last_name": "Agent",
                "username": "agent_diff_user",
                "password": "ValidPassword123!",
                "mobile_e164": "+919876540001",
                "aadhaar_number": "999988887777",
                "aadhaar_document_key": aadhaar_a,
                "pan_number": "ZZZZZ9999Z",
                "pan_document_key": pan_a,
                "profile_photo_key": photo_a,
            },
        )
        assert res_dup_mobile.status_code == 409


@pytest.mark.anyio
async def test_user_registration_stored_object_validation_and_cross_tenant(phase5_harness):
    app = phase5_harness["app"]
    factory = phase5_harness["factory"]
    super_token = phase5_harness["super_token"]
    super_csrf = phase5_harness["super_csrf"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    admin_b_token = phase5_harness["admin_b_token"]
    admin_b_csrf = phase5_harness["admin_b_csrf"]
    tenant_a_id = phase5_harness["tenant_a_id"]

    image_bytes = create_test_image_bytes("JPEG")
    async with client_for(app) as client:
        # Create an agent in Community A
        r1 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        r2 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        r3 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        agent_create = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Ramesh",
                "last_name": "Kumar",
                "username": "agent_ramesh",
                "password": "RameshPassword123!",
                "mobile_e164": "+919876540055",
                "aadhaar_number": "555566667777",
                "aadhaar_document_key": r2.json()["object_key"],
                "pan_number": "ABCDE5555F",
                "pan_document_key": r3.json()["object_key"],
                "profile_photo_key": r1.json()["object_key"],
            },
        )
        assert agent_create.status_code == 201

        # Super Admin approves Ramesh's pending appointment
        with factory() as session:
            appt = session.execute(
                select(AgentAppointment).where(
                    AgentAppointment.tenant_id == UUID(str(tenant_a_id)),
                    AgentAppointment.status == "PENDING",
                )
            ).scalars().first()
            assert appt is not None
            appt_id = appt.id

        appr_res = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{appt_id}/approve",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
        )
        assert appr_res.status_code == 200

        # Login as Agent Ramesh
        login_res = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:3000"},
            json={"username": "agent_ramesh", "password": "RameshPassword123!"},
        )
        assert login_res.status_code == 200
        agent_cookie = login_res.headers["set-cookie"].split(";")[0].split("=")[1]
        agent_csrf = login_res.json()["csrf_token"]

        # Admin B uploads a photo in Community B
        r_photo_b = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_b_token},
            files={"file": ("user_b.jpg", image_bytes, "image/jpeg")},
            data={"category": "user-photo"},
        )
        photo_b_key = r_photo_b.json()["object_key"]

        # 1. Agent Ramesh attempts to register subscriber with fake photo key -> 422 OBJECT_NOT_FOUND
        res_fake = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Test Subscriber",
                "mobile_e164": "+919876500001",
                "photo_key": "user-photos/non-existent.jpg",
            },
        )
        assert res_fake.status_code == 422

        # 2. Agent Ramesh attempts to use photo uploaded in Community B -> 403 CROSS_TENANT_OBJECT_FORBIDDEN
        res_cross = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Test Subscriber",
                "mobile_e164": "+919876500001",
                "photo_key": photo_b_key,
            },
        )
        assert res_cross.status_code == 403

        # 3. Agent Ramesh attempts to use an object with wrong category (e.g. agent-photo instead of user-photo) -> 422
        res_cat = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Test Subscriber",
                "mobile_e164": "+919876500001",
                "photo_key": r1.json()["object_key"],
            },
        )
        assert res_cat.status_code == 422

        # 4. Agent Ramesh uploads valid user photo in Community A -> SUCCESS
        r_user_photo = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": agent_cookie},
            files={"file": ("user_a.jpg", image_bytes, "image/jpeg")},
            data={"category": "user-photo"},
        )
        user_photo_key = r_user_photo.json()["object_key"]

        res_ok = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Gita Govind",
                "mobile_e164": "+919876500001",
                "photo_key": user_photo_key,
            },
        )
        assert res_ok.status_code == 201
        assert res_ok.json()["photo_key"] == user_photo_key

        # 5. Duplicate mobile registration in community -> 409 MOBILE_CONFLICT
        res_dup = await client.post(
            "/api/v1/agent/users",
            cookies={"community_session": agent_cookie},
            headers={"X-CSRF-Token": agent_csrf, "Origin": "http://localhost:3000"},
            json={
                "full_name": "Another Gita",
                "mobile_e164": "+919876500001",
                "photo_key": None,
            },
        )
        assert res_dup.status_code == 409
