from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
import importlib.util
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.audit.models import AuditEvent
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.models import Role, RolePermission, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.commissions.models import CommissionJournal, CommissionLedgerEntry
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.subscriptions.models import (
    MemberSubscription,
    PlanStatus,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.subscriptions.schemas import (
    PlanCreateRequest,
    PlanUpdateRequest,
    SubscriptionCreateRequest,
)
from app.modules.subscriptions.service import SubscriptionService
from app.modules.tenants.models import Tenant


@pytest.fixture
def subscription_harness(tmp_path):
    database_path = tmp_path / "subscription-test.sqlite"
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

    # Platform Super Admin credentials
    super_user_id = uuid4()
    super_token = "superadmin_session_token_subs"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_csrf_subs").hexdigest()

    # Tenant A credentials
    admin_a_token = "admin_a_session_token_subs"
    admin_a_digest = hashlib.sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = hashlib.sha256(b"admin_a_csrf_subs").hexdigest()
    admin_a_user_id = uuid4()
    admin_a_mem_id = uuid4()

    agent_a_token = "agent_a_session_token_subs"
    agent_a_digest = hashlib.sha256(agent_a_token.encode("utf-8")).hexdigest()
    agent_a_csrf = hashlib.sha256(b"agent_a_csrf_subs").hexdigest()
    agent_a_user_id = uuid4()
    agent_a_mem_id = uuid4()

    # Tenant A Subscriber
    subscriber_a_user_id = uuid4()
    subscriber_a_mem_id = uuid4()

    # Tenant B credentials
    admin_b_token = "admin_b_session_token_subs"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf_subs").hexdigest()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()

    subscriber_b_user_id = uuid4()
    subscriber_b_mem_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Create Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="comm-subs-a",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="comm-subs-b",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
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

        # Seed RBAC for Tenants
        admin_role_a, agent_role_a = seed_tenant_role_permissions(session, tenant_a_id)
        admin_role_b, agent_role_b = seed_tenant_role_permissions(session, tenant_b_id)

        # Tenant A Admin
        admin_a_user = User(id=admin_a_user_id, status="active", created_at=now, updated_at=now)
        session.add(admin_a_user)
        session.flush()

        admin_a_mem = TenantMembership(
            id=admin_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a_user_id,
            mobile_e164="+919811111111",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(admin_a_mem)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a_mem_id,
                role_id=admin_role_a.id,
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
        session.flush()

        # Tenant A Agent
        agent_a_user = User(id=agent_a_user_id, status="active", created_at=now, updated_at=now)
        session.add(agent_a_user)
        session.flush()

        agent_a_mem = TenantMembership(
            id=agent_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=agent_a_user_id,
            mobile_e164="+919822222222",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_a_mem)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=agent_a_mem_id,
                role_id=agent_role_a.id,
                assigned_by_membership_id=admin_a_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=agent_a_mem_id,
                token_digest=agent_a_digest,
                csrf_token=agent_a_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )
        session.flush()

        agent_profile_id = uuid4()
        agent_profile = AgentProfile(
            id=agent_profile_id,
            tenant_id=tenant_a_id,
            membership_id=agent_a_mem_id,
            user_id=agent_a_user_id,
            first_name="Subs",
            last_name="Agent",
            ref_id="AGT001",
            profile_photo_key="photo1",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-1234",
            aadhaar_document_key="doc1",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE1234F",
            pan_document_key="doc2",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_profile)
        session.flush()

        agent_cred = AccountCredential(
            id=uuid4(),
            user_id=agent_a_user_id,
            username="activeagent01",
            password_hash="mock_hash",
            account_type="tenant_user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_cred)
        session.flush()

        # Tenant A Subscriber
        sub_a_user = User(id=subscriber_a_user_id, status="active", created_at=now, updated_at=now)
        session.add(sub_a_user)
        session.flush()

        sub_a_mem = TenantMembership(
            id=subscriber_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=subscriber_a_user_id,
            mobile_e164="+919833333333",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(sub_a_mem)
        session.flush()

        session.add(
            SubscriberProfile(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=subscriber_a_mem_id,
                user_id=subscriber_a_user_id,
                registered_by_agent_membership_id=agent_a_mem_id,
                full_name="Subscriber A",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()

        # Tenant B Admin & Subscriber
        admin_b_user = User(id=admin_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(admin_b_user)
        session.flush()

        admin_b_mem = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919844444444",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(admin_b_mem)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=admin_b_mem_id,
                role_id=admin_role_b.id,
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
        session.flush()

        sub_b_user = User(id=subscriber_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(sub_b_user)
        session.flush()

        sub_b_mem = TenantMembership(
            id=subscriber_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=subscriber_b_user_id,
            mobile_e164="+919855555555",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(sub_b_mem)
        session.flush()
        session.add_all([sub_b_user, sub_b_mem])
        session.flush()

        session.commit()

    settings = Settings(
        app_env="testing",
        jwt_secret_key="test-secret-key-for-subscriptions-testing",
        csrf_secret_key="test-csrf-secret-key-for-subs",
        backend_api_base_url="http://testserver",
    )
    app = create_app(settings=settings)

    def override_get_db():
        with factory() as session:
            yield session

    def override_auth_repo():
        return SQLAlchemyAuthenticationRepository(factory)

    def override_authz_repo():
        return SQLAlchemyAuthorizationRepository(factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_authentication_repository] = override_auth_repo
    app.dependency_overrides[get_authorization_repository] = override_authz_repo

    return {
        "engine": engine,
        "factory": factory,
        "app": app,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "super_token": super_token,
        "super_csrf": super_csrf,
        "admin_a_token": admin_a_token,
        "admin_a_csrf": admin_a_csrf,
        "agent_a_token": agent_a_token,
        "agent_a_csrf": agent_a_csrf,
        "subscriber_a_mem_id": subscriber_a_mem_id,
        "agent_profile_id": agent_profile_id,
        "admin_b_token": admin_b_token,
        "admin_b_csrf": admin_b_csrf,
        "subscriber_b_mem_id": subscriber_b_mem_id,
    }


# =============================================================================
# 1. Super Admin: Create, Edit, List, and Deactivate Subscription Plans
# =============================================================================
@pytest.mark.anyio
async def test_super_admin_create_and_edit_plans(subscription_harness):
    h = subscription_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as client:
        # 1. Create a plan
        create_res = await client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "BASIC_MONTHLY",
                "name": "Basic Monthly Plan",
                "description": "Standard monthly streaming",
                "benefits": ["1 Screen", "HD Quality"],
                "duration_days": 30,
                "nominal_price": "199.00",
                "currency": "INR",
                "status": "active",
            },
        )
        assert create_res.status_code == 201, create_res.text
        plan_data = create_res.json()
        plan_id = plan_data["id"]
        assert plan_data["code"] == "BASIC_MONTHLY"
        assert plan_data["duration_days"] == 30
        assert plan_data["status"] == "active"

        # 2. Edit plan terms
        edit_res = await client.patch(
            f"/api/v1/platform/subscription-plans/{plan_id}",
            json={
                "name": "Basic Monthly Plan (Updated)",
                "duration_days": 35,
                "nominal_price": "249.00",
            },
        )
        assert edit_res.status_code == 200, edit_res.text
        updated = edit_res.json()
        assert updated["name"] == "Basic Monthly Plan (Updated)"
        assert updated["duration_days"] == 35

        # 3. List plans
        list_res = await client.get("/api/v1/platform/subscription-plans")
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert len(items) >= 1
        assert any(p["id"] == plan_id for p in items)


# =============================================================================
# 2. Unauthorized Roles Cannot Modify Platform Plans
# =============================================================================
@pytest.mark.anyio
async def test_unauthorized_roles_cannot_modify_platform_plans(subscription_harness):
    h = subscription_harness

    # Tenant Admin cannot access platform plan creation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client:
        res = await client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "ILLEGAL_PLAN",
                "name": "Illegal Plan",
                "duration_days": 30,
            },
        )
        assert res.status_code == 403

    # Agent cannot access platform plan creation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a_token"]},
        headers={"x-csrf-token": h["agent_a_csrf"]},
    ) as client:
        res = await client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "ILLEGAL_PLAN_2",
                "name": "Illegal Plan 2",
                "duration_days": 30,
            },
        )
        assert res.status_code == 403


# =============================================================================
# 3. Inactive Plans Cannot Be Selected for New Subscriptions
# =============================================================================
@pytest.mark.anyio
async def test_inactive_plans_cannot_be_selected_for_new_subscriptions(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates inactive plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        create_res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "LEGACY_PLAN",
                "name": "Legacy Retired Plan",
                "duration_days": 30,
                "status": "inactive",
            },
        )
        assert create_res.status_code == 201
        inactive_plan_id = create_res.json()["id"]

    # 2. Tenant Admin attempts to subscribe to inactive plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as admin_client:
        # Inactive plan should NOT appear in available plans
        avail_res = await admin_client.get("/api/v1/subscription-plans")
        assert avail_res.status_code == 200
        avail_ids = [p["id"] for p in avail_res.json()["items"]]
        assert inactive_plan_id not in avail_ids

        # Attempting to subscribe to inactive plan fails with 400
        sub_res = await admin_client.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": inactive_plan_id,
                "agreed_amount": "150.00",
            },
        )
        assert sub_res.status_code == 400


# =============================================================================
# 4. Valid Manually Entered Subscription Amounts are Persisted Accurately
# =============================================================================
@pytest.mark.anyio
async def test_valid_manually_entered_subscription_amounts(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates active plan with nominal price 199.00
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "STANDARD_30",
                "name": "Standard 30 Days",
                "duration_days": 30,
                "nominal_price": "199.00",
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    # 2. Operator enters custom agreed amount (250.75) manually
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as admin_client:
        sub_res = await admin_client.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "250.75",
                "notes": "Custom agreed rate for rural subscriber",
            },
        )
        assert sub_res.status_code == 201, sub_res.text
        sub_data = sub_res.json()
        assert sub_data["agreed_amount"] == "250.75"
        assert sub_data["snapshot_plan_name"] == "Standard 30 Days"
        assert sub_data["snapshot_duration_days"] == 30
        assert sub_data["status"] == "PENDING_PAYMENT"
        assert sub_data["payment_status"] == "UNPAID"


# =============================================================================
# 5. Invalid, Negative, or Excessive-Precision Amounts are Rejected
# =============================================================================
@pytest.mark.anyio
async def test_invalid_negative_or_excessive_precision_amounts(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates active plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "PRECISION_PLAN",
                "name": "Precision Test Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as admin_client:
        # Negative amount -> 422
        neg_res = await admin_client.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "-50.00",
            },
        )
        assert neg_res.status_code == 422

        # 3 decimal places -> 422
        prec_res = await admin_client.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "199.999",
            },
        )
        assert prec_res.status_code == 422


# =============================================================================
# 6. Updating a Plan Does Not Alter Existing Subscription Terms
# =============================================================================
@pytest.mark.anyio
async def test_plan_update_does_not_alter_existing_subscriptions(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "IMMUTABLE_TEST_PLAN",
                "name": "Original Name",
                "duration_days": 30,
                "nominal_price": "100.00",
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    # 2. Operator records subscription
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as admin_client:
        sub_res = await admin_client.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "120.00",
            },
        )
        sub_id = sub_res.json()["id"]
        original_expires = sub_res.json()["expires_at"]

    # 3. Super Admin changes plan details drastically
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        await super_client.patch(
            f"/api/v1/platform/subscription-plans/{plan_id}",
            json={
                "name": "Completely Changed Name",
                "duration_days": 90,
                "nominal_price": "999.00",
            },
        )

    # 4. Verify existing subscription terms remain completely unchanged
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as admin_client:
        check_res = await admin_client.get(f"/api/v1/subscriptions/{sub_id}")
        assert check_res.status_code == 200
        sub_now = check_res.json()
        assert sub_now["agreed_amount"] == "120.00"
        assert sub_now["snapshot_plan_name"] == "Original Name"
        assert sub_now["snapshot_duration_days"] == 30
        assert sub_now["expires_at"] == original_expires


# =============================================================================
# 7. Cross-Tenant Subscription Access and Modification are Denied
# =============================================================================
@pytest.mark.anyio
async def test_cross_tenant_subscription_access_denied(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates active plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "TENANT_TEST_PLAN",
                "name": "Tenant Test Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    # 2. Tenant A records subscription
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client_a:
        sub_a = (
            await client_a.post(
                "/api/v1/subscriptions",
                json={
                    "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                    "plan_id": plan_id,
                    "agreed_amount": "199.00",
                },
            )
        ).json()
        sub_a_id = sub_a["id"]

    # 3. Tenant B attempts to read Tenant A's subscription -> 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_b_token"]},
        headers={"x-csrf-token": h["admin_b_csrf"]},
    ) as client_b:
        read_res = await client_b.get(f"/api/v1/subscriptions/{sub_a_id}")
        assert read_res.status_code == 404

        # Tenant B attempts to cancel Tenant A's subscription -> 404
        cancel_res = await client_b.post(
            f"/api/v1/subscriptions/{sub_a_id}/cancel",
            json={"reason": "Attempt cross-tenant cancel"},
        )
        assert cancel_res.status_code == 404

        # Tenant B attempts to create subscription with Tenant A's subscriber membership -> 404
        cross_create_res = await client_b.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "199.00",
            },
        )
        assert cross_create_res.status_code == 404


# =============================================================================
# 8. Subscription Lifecycle State Transitions Follow Approved Rules
# =============================================================================
@pytest.mark.anyio
async def test_subscription_lifecycle_state_transitions(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "LIFECYCLE_PLAN",
                "name": "Lifecycle Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client_a:
        # Create subscription -> PENDING_PAYMENT
        sub = (
            await client_a.post(
                "/api/v1/subscriptions",
                json={
                    "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                    "plan_id": plan_id,
                    "agreed_amount": "199.00",
                },
            )
        ).json()
        sub_id = sub["id"]
        assert sub["status"] == "PENDING_PAYMENT"

        # Explicitly activate -> ACTIVE
        act_res = await client_a.post(f"/api/v1/subscriptions/{sub_id}/activate")
        assert act_res.status_code == 200
        assert act_res.json()["status"] == "ACTIVE"

        # Activating already active subscription -> 409
        dup_act = await client_a.post(f"/api/v1/subscriptions/{sub_id}/activate")
        assert dup_act.status_code == 409

        # Cancel subscription -> CANCELLED
        cancel_res = await client_a.post(
            f"/api/v1/subscriptions/{sub_id}/cancel",
            json={"reason": "Subscriber relocated out of community"},
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "CANCELLED"

        # Activating cancelled subscription -> 409
        cant_act = await client_a.post(f"/api/v1/subscriptions/{sub_id}/activate")
        assert cant_act.status_code == 409


# =============================================================================
# 9. Manual Amount Entry Does Not Automatically Mark Payment as Settled
# =============================================================================
@pytest.mark.anyio
async def test_manual_amount_entry_does_not_settle_payment(subscription_harness):
    h = subscription_harness

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "SETTLEMENT_CHECK_PLAN",
                "name": "Settlement Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client_a:
        sub = (
            await client_a.post(
                "/api/v1/subscriptions",
                json={
                    "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                    "plan_id": plan_id,
                    "agreed_amount": "500.00",
                },
            )
        ).json()

        # Boundary check: Payment status must be UNPAID, not SETTLED
        assert sub["payment_status"] == "UNPAID"
        assert sub["status"] == "PENDING_PAYMENT"


# =============================================================================
# 10. Subscription Creation Does Not Create an Unsupported Financial Ledger Posting
# =============================================================================
@pytest.mark.anyio
async def test_no_unsupported_financial_ledger_posting(subscription_harness):
    h = subscription_harness

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "LEDGER_CHECK_PLAN",
                "name": "Ledger Check Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client_a:
        await client_a.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "300.00",
            },
        )

    # Inspect the financial foundation tables
    with h["factory"]() as session:
        journals_count = session.execute(select(CommissionJournal)).scalars().all()
        ledger_count = session.execute(select(CommissionLedgerEntry)).scalars().all()
        assert len(journals_count) == 0, "No financial journal should be posted on manual subscription creation"
        assert len(ledger_count) == 0, "No ledger entries should be posted on manual subscription creation"


# =============================================================================
# 11. Audit Events are Generated for Administrative Plan Changes
# =============================================================================
@pytest.mark.anyio
async def test_audit_events_generated_for_plan_and_subscription_changes(subscription_harness):
    h = subscription_harness

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "AUDIT_TEST_PLAN",
                "name": "Audit Test Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

        await super_client.patch(
            f"/api/v1/platform/subscription-plans/{plan_id}",
            json={"name": "Audit Test Plan Updated"},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client_a:
        await client_a.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "200.00",
            },
        )

    with h["factory"]() as session:
        events = session.execute(select(AuditEvent)).scalars().all()
        event_types = [e.event_type for e in events]
        assert "subscription.plan.created" in event_types
        assert "subscription.plan.updated" in event_types
        assert "subscription.created" in event_types


# =============================================================================
# 12. Plan Cannot Be Hard-Deleted If Referenced by Existing Subscriptions
# =============================================================================
@pytest.mark.anyio
async def test_plan_cannot_be_deleted_if_referenced(subscription_harness):
    h = subscription_harness

    # 1. Super Admin creates plan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        res = await super_client.post(
            "/api/v1/platform/subscription-plans",
            json={
                "code": "DELETE_TEST_PLAN",
                "name": "Delete Test Plan",
                "duration_days": 30,
                "status": "active",
            },
        )
        plan_id = res.json()["id"]

    # 2. Record subscription
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client_a:
        await client_a.post(
            "/api/v1/subscriptions",
            json={
                "subscriber_membership_id": str(h["subscriber_a_mem_id"]),
                "plan_id": plan_id,
                "agreed_amount": "100.00",
            },
        )

    # 3. Super Admin attempts to delete referenced plan -> 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as super_client:
        del_res = await super_client.delete(f"/api/v1/platform/subscription-plans/{plan_id}")
        assert del_res.status_code == 409

        # Deactivating it instead succeeds
        deact_res = await super_client.patch(
            f"/api/v1/platform/subscription-plans/{plan_id}",
            json={"status": "inactive"},
        )
        assert deact_res.status_code == 200
        assert deact_res.json()["status"] == "inactive"


# =============================================================================
# 13. Migration Upgrade and Downgrade Verification on Isolated SQLite DB
# =============================================================================
def test_subscription_migration_upgrade_and_downgrade(tmp_path: Path):
    db_file = tmp_path / "migration-test-stage42.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id CHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE tenants (id CHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE tenant_memberships (id CHAR(36) PRIMARY KEY, tenant_id CHAR(36))"))

    migration_file = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261021_18_subscription_and_package_billing.py"
    spec = importlib.util.spec_from_file_location("rev_18", migration_file)
    assert spec and spec.loader
    rev_18 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev_18)

    # 1. Test Upgrade
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            rev_18.upgrade()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"subscription_plans", "member_subscriptions"} <= tables

    # 2. Test Downgrade
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            rev_18.downgrade()

    inspector_down = inspect(engine)
    tables_down = set(inspector_down.get_table_names())
    assert "subscription_plans" not in tables_down
    assert "member_subscriptions" not in tables_down


# =============================================================================
# 14. Phase 2/3 Security Regressions & SEC-07 Closure Safeguard
# =============================================================================
@pytest.mark.anyio
async def test_security_regressions_and_sec07_closure(subscription_harness):
    h = subscription_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a_token"]},
        headers={"x-csrf-token": h["admin_a_csrf"]},
    ) as client:
        res = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            json={"status": "inactive"},
        )
        assert res.status_code == 403
        data = res.json()
        assert data["error"]["code"] in ("LIFECYCLE_WORKFLOW_REQUIRED", "HTTP_ERROR")
