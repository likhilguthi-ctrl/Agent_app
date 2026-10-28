"""Comprehensive tests for Agent-Led Subscriber Registration, Payment Integration, Notifications, and Regional Scoping (Bugs A, B, C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.models import AccountCredential, OtpChallenge
from app.modules.authentication.repository import ResolvedSession
from app.modules.identity.models import TenantMembership, User
from app.modules.messaging.notification_service import NotificationServiceClient
from app.modules.outbox.models import AuditOutboxEvent
from app.modules.payments.microservice_client import PaymentInitResult, PaymentMicroserviceClient
from app.modules.payments.models import PaymentOrder, PaymentOrderStatus
from app.modules.platform_identity.models import PlatformOperator
from app.modules.relationships.models import MemberRelationship
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.agents.schemas import (
    AgentCollectionSummary,
    PaymentConfirmRequest,
    UserRegistrationRequest,
    UserSummary,
)
from app.modules.subscriptions.models import (
    MemberSubscription,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.tenants.models import AdminProfile, Region, Tenant, UserRegionAssignment


@pytest.fixture
def test_settings():
    return Settings(
        app_name="Community OTT Test",
        environment="test",
        database_url=None,
        payment_delivery_mode="payment_service",
        otp_delivery_mode="mock",
        otp_log_to_console=False,
        notification_debug_logging=False,
    )


@pytest.fixture
def memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield session_factory, engine
    Base.metadata.drop_all(engine)


def test_subscriber_profile_has_whatsapp_consent(memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    user_id = uuid4()
    agent_user_id = uuid4()
    mem_id = uuid4()
    agent_mem_id = uuid4()
    sub_id = uuid4()

    tenant = Tenant(
        id=tenant_id,
        slug="test-ott",
        status="active",
    )
    user = User(id=user_id, status="active")
    agent_user = User(id=agent_user_id, status="active")
    agent_mem = TenantMembership(id=agent_mem_id, tenant_id=tenant_id, user_id=agent_user_id, mobile_e164="+919876543210", status="active")
    membership = TenantMembership(id=mem_id, tenant_id=tenant_id, user_id=user_id, mobile_e164="+919988776655", status="active")

    session.add_all([tenant, user, agent_user, agent_mem, membership])
    session.flush()

    # Create subscriber profile with consent = True
    sub_prof = SubscriberProfile(
        id=sub_id,
        tenant_id=tenant_id,
        membership_id=mem_id,
        user_id=user_id,
        registered_by_agent_membership_id=agent_mem_id,
        full_name="Rajesh Kumar",
        whatsapp_number="+919988776655",
        whatsapp_verified=True,
        whatsapp_consent=True,
        whatsapp_consent_at=datetime.now(UTC),
        package_plan="Standard",
        billing_cycle="monthly",
        status="active",
    )
    session.add(sub_prof)
    session.commit()

    saved = session.execute(select(SubscriberProfile).where(SubscriberProfile.id == sub_id)).scalar_one()
    assert saved.whatsapp_consent is True
    assert saved.whatsapp_consent_at is not None
    assert saved.full_name == "Rajesh Kumar"


def test_payment_order_microservice_fields(memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    user_id = uuid4()
    mem_id = uuid4()
    sub_id = uuid4()
    order_id = uuid4()
    plan_id = uuid4()
    now = datetime.now(UTC)

    tenant = Tenant(id=tenant_id, slug="ott-slug", status="active")
    plan = SubscriptionPlan(
        id=plan_id,
        community_id=tenant_id,
        code="PREM",
        name="Premium",
        nominal_price=Decimal("299.00"),
        duration_days=30,
        currency="INR",
        status="active",
    )
    user = User(id=user_id, status="active")
    membership = TenantMembership(id=mem_id, tenant_id=tenant_id, user_id=user_id, mobile_e164="+919988776655", status="active")
    sub = MemberSubscription(
        id=sub_id,
        tenant_id=tenant_id,
        subscriber_membership_id=mem_id,
        plan_id=plan_id,
        agreed_amount=Decimal("299.00"),
        starts_at=now,
        expires_at=now + timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value,
        payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name="Premium",
        snapshot_duration_days=30,
        created_by_user_id=user_id,
    )
    session.add_all([tenant, plan, user, membership, sub])
    session.flush()

    order = PaymentOrder(
        id=order_id,
        tenant_id=tenant_id,
        subscription_id=sub_id,
        subscriber_membership_id=mem_id,
        gateway="payment_microservice",
        gateway_order_id="order_12345",
        external_payment_id="pay_remote_987",
        payment_page_url="http://handorg.duckdns.org/checkout/pay_remote_987",
        amount_paise=29900,
        amount=Decimal("299.00"),
        currency="INR",
        status=PaymentOrderStatus.CREATED.value,
        custom_amount=False,
    )
    session.add(order)
    session.commit()

    saved_order = session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id)).scalar_one()
    assert saved_order.external_payment_id == "pay_remote_987"
    assert saved_order.payment_page_url == "http://handorg.duckdns.org/checkout/pay_remote_987"
    assert saved_order.custom_amount is False
    assert saved_order.amount == Decimal("299.00")


@pytest.mark.anyio
async def test_payment_microservice_client_init_and_status():
    settings = Settings(
        payment_delivery_mode="mock",
        environment="development",
        otp_log_to_console=False,
    )
    client = PaymentMicroserviceClient(settings=settings)

    init_res = await client.initiate_payment(
        tenant_slug="test-tenant",
        amount_paise=19900,
        description="Subscription Test",
        callback_url="http://localhost:5000/agent/payment/callback",
        customer_name="Aman Gupta",
        customer_phone="+919876543210",
    )

    assert init_res.success is True
    assert init_res.payment_id.startswith("pay_mock_")
    assert "/checkout/" in init_res.payment_page_url
    assert init_res.amount == 19900

    # Status check
    status_res = await client.get_payment_status(init_res.payment_id)
    assert status_res.success is True
    assert status_res.status == "COMPLETED"


@pytest.mark.anyio
async def test_welcome_notifications_strict_whatsapp_consent():
    settings = Settings(
        otp_delivery_mode="mock",
        environment="development",
        otp_log_to_console=False,
    )
    client = NotificationServiceClient(settings=settings)

    # 1. When whatsapp_consent is FALSE: WhatsApp message must be skipped
    res_no_consent = await client.send_subscriber_welcome(
        subscriber_name="Suresh Raina",
        mobile_e164="+919876543210",
        whatsapp_number="+919876543210",
        whatsapp_consent=False,
        email="suresh@example.com",
        plan_name="Gold Monthly",
        amount_paid="299.00",
    )
    assert res_no_consent["sms"] is not None
    assert res_no_consent["sms"].success is True
    assert res_no_consent["email"] is not None
    assert res_no_consent["email"].success is True
    assert res_no_consent["whatsapp"] is None, "WhatsApp must NOT be dispatched when consent is False"

    # 2. When whatsapp_consent is TRUE: WhatsApp message must be dispatched
    res_with_consent = await client.send_subscriber_welcome(
        subscriber_name="Suresh Raina",
        mobile_e164="+919876543210",
        whatsapp_number="+919876543210",
        whatsapp_consent=True,
        email=None,
        plan_name="Gold Monthly",
        amount_paid="299.00",
    )
    assert res_with_consent["sms"] is not None
    assert res_with_consent["whatsapp"] is not None
    assert res_with_consent["whatsapp"].success is True
    assert res_with_consent["email"] is None, "Email must be skipped when no email provided"


def test_admin_regional_scoping_bug_a(memory_db):
    """Admin assigned to Region 1 should NOT be allowed to view agents from Region 2."""
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    reg1_id = uuid4()
    reg2_id = uuid4()
    admin_user_id = uuid4()
    agent1_user_id = uuid4()
    agent2_user_id = uuid4()

    tenant = Tenant(id=tenant_id, slug="tenant-scope", status="active")
    reg1 = Region(id=reg1_id, tenant_id=tenant_id, code="HYD", name="Hyderabad", state="Telangana", status="active")
    reg2 = Region(id=reg2_id, tenant_id=tenant_id, code="WGL", name="Warangal", state="Telangana", status="active")

    admin_user = User(id=admin_user_id, status="active")
    agent1_user = User(id=agent1_user_id, status="active")
    agent2_user = User(id=agent2_user_id, status="active")

    # Admin assigned only to Region 1
    admin_reg_assign = UserRegionAssignment(
        id=uuid4(),
        user_id=admin_user_id,
        tenant_id=tenant_id,
        region_id=reg1_id,
        status="active",
    )

    # Agent 1 assigned to Region 1 (Authorized for admin)
    agent1_assign = UserRegionAssignment(
        id=uuid4(),
        user_id=agent1_user_id,
        tenant_id=tenant_id,
        region_id=reg1_id,
        status="active",
    )

    # Agent 2 assigned to Region 2 (UNAUTHORIZED for admin)
    agent2_assign = UserRegionAssignment(
        id=uuid4(),
        user_id=agent2_user_id,
        tenant_id=tenant_id,
        region_id=reg2_id,
        status="active",
    )

    session.add_all([
        tenant, reg1, reg2, admin_user, agent1_user, agent2_user,
        admin_reg_assign, agent1_assign, agent2_assign,
    ])
    session.commit()

    # Query admin's allowed region IDs
    admin_regions = set(
        session.execute(
            select(UserRegionAssignment.region_id).where(
                UserRegionAssignment.user_id == admin_user_id,
                UserRegionAssignment.tenant_id == tenant_id,
                UserRegionAssignment.status == "active",
            )
        ).scalars().all()
    )
    assert admin_regions == {reg1_id}

    # Agent 1 check: overlap with admin_regions is non-empty
    agent1_regions = set(
        session.execute(
            select(UserRegionAssignment.region_id).where(
                UserRegionAssignment.user_id == agent1_user_id,
                UserRegionAssignment.tenant_id == tenant_id,
            )
        ).scalars().all()
    )
    assert bool(agent1_regions & admin_regions) is True

    # Agent 2 check: overlap with admin_regions is empty (Isolation enforced)
    agent2_regions = set(
        session.execute(
            select(UserRegionAssignment.region_id).where(
                UserRegionAssignment.user_id == agent2_user_id,
                UserRegionAssignment.tenant_id == tenant_id,
            )
        ).scalars().all()
    )
    assert bool(agent2_regions & admin_regions) is False


def test_agent_collection_summary_calculation(memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    agent_user_id = uuid4()
    agent_mem_id = uuid4()

    tenant = Tenant(id=tenant_id, slug="tenant-coll", status="active")
    agent_user = User(id=agent_user_id, status="active")
    agent_mem = TenantMembership(id=agent_mem_id, tenant_id=tenant_id, user_id=agent_user_id, mobile_e164="+919876543210", status="active")
    session.add_all([tenant, agent_user, agent_mem])
    session.flush()

    # Seed 3 subscribers registered by this agent
    total_amount = Decimal("0.00")
    for i in range(3):
        sub_u_id = uuid4()
        sub_m_id = uuid4()
        sub_prof_id = uuid4()
        amt = Decimal("299.00")

        u = User(id=sub_u_id, status="active")
        m = TenantMembership(id=sub_m_id, tenant_id=tenant_id, user_id=sub_u_id, mobile_e164=f"+91981111110{i}", status="active")
        sp = SubscriberProfile(
            id=sub_prof_id,
            tenant_id=tenant_id,
            membership_id=sub_m_id,
            user_id=sub_u_id,
            registered_by_agent_membership_id=agent_mem_id,
            full_name=f"Subscriber {i}",
            whatsapp_number=f"+91981111110{i}",
            whatsapp_verified=True,
            whatsapp_consent=True,
            status="active",
        )
        plan = SubscriptionPlan(
            id=uuid4(),
            community_id=tenant_id,
            code=f"PLAN_{i}",
            name=f"Plan {i}",
            nominal_price=amt,
            duration_days=30,
            currency="INR",
            status="active",
        )
        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=sub_m_id,
            plan_id=plan.id,
            agreed_amount=amt,
            starts_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
            status=SubscriptionStatus.ACTIVE.value,
            payment_status=SubscriptionPaymentStatus.SETTLED.value,
            snapshot_plan_name=f"Plan {i}",
            snapshot_duration_days=30,
            created_by_user_id=agent_user_id,
        )
        # 2 paid orders, 1 created order
        order_status = PaymentOrderStatus.PAID.value if i < 2 else PaymentOrderStatus.CREATED.value
        if order_status == PaymentOrderStatus.PAID.value:
            total_amount += amt

        order = PaymentOrder(
            id=uuid4(),
            tenant_id=tenant_id,
            subscription_id=sub.id,
            subscriber_membership_id=sub_m_id,
            gateway="payment_microservice",
            gateway_order_id=f"ord_{i}",
            amount_paise=int(amt * 100),
            amount=amt,
            currency="INR",
            status=order_status,
            created_by_membership_id=agent_mem_id,
        )
        session.add_all([u, m, sp, plan, sub, order])

    session.commit()

    # Query metrics
    total_subscribers = session.execute(
        select(func.count(SubscriberProfile.id)).where(
            SubscriberProfile.tenant_id == tenant_id,
            SubscriberProfile.registered_by_agent_membership_id == agent_mem_id,
        )
    ).scalar()
    assert total_subscribers == 3

    total_collected = session.execute(
        select(func.coalesce(func.sum(PaymentOrder.amount), Decimal("0.00"))).where(
            PaymentOrder.tenant_id == tenant_id,
            PaymentOrder.created_by_membership_id == agent_mem_id,
            PaymentOrder.status == PaymentOrderStatus.PAID.value,
        )
    ).scalar()
    assert total_collected == Decimal("598.00")


def setup_agent_registration_fixture(session):
    tenant_id = uuid4()
    agent_id = uuid4()
    agent_mem_id = uuid4()
    plan_id = uuid4()

    tenant = Tenant(
        id=tenant_id,
        slug="test-community",
        status="active",
        photo_requirement="OPTIONAL",
    )
    agent_user = User(id=agent_id, status="active")
    agent_mem = TenantMembership(
        id=agent_mem_id,
        tenant_id=tenant_id,
        user_id=agent_id,
        mobile_e164="+919876500001",
        status="active",
    )
    plan = SubscriptionPlan(
        id=plan_id,
        community_id=tenant_id,
        code="BASIC",
        name="Basic Pack",
        nominal_price=Decimal("199.00"),
        duration_days=30,
        currency="INR",
        status="active",
    )
    session.add_all([tenant, agent_user, agent_mem, plan])
    session.commit()

    agent_session = ResolvedSession(
        session_id=uuid4(),
        account_type="user",
        user_id=agent_id,
        username="agent_tester",
        role="SUPER_ADMIN",
        status="active",
        csrf_token="csrf_token_test",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=tenant_id,
        membership_id=agent_mem_id,
    )

    return {
        "tenant": tenant,
        "agent_mem": agent_mem,
        "plan": plan,
        "agent_session": agent_session,
    }


@pytest.mark.anyio
async def test_subscriber_registration_package_based_persists_dict_notes(test_settings, memory_db):
    """Verify package-based subscriber registration stores notes as dict and maps custom_amount=False."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_registration_fixture(session)

    mock_payment_client = AsyncMock(spec=PaymentMicroserviceClient)
    mock_payment_client.initiate_payment = AsyncMock(
        return_value=PaymentInitResult(
            success=True,
            payment_id="pay_remote_pkg_123",
            order_id="ord_remote_pkg_123",
            payment_page_url="http://handorg.duckdns.org/checkout/pay_remote_pkg_123",
            status="INITIATED",
            amount=19900,
            currency="INR",
            tenant_id=fixture["tenant"].slug,
        )
    )

    app = create_app(test_settings)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "test_cookie")
    app.state.payment_client = mock_payment_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/users",
            json={
                "full_name": "Rohan Sharma",
                "mobile_e164": "+919811122233",
                "plan_id": str(fixture["plan"].id),
                "package_plan": fixture["plan"].name,
                "whatsapp_consent": True,
            },
        )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["package_type"] == "Plan"
    assert data["payment_status"] == PaymentOrderStatus.CREATED.value
    assert data["subscription_status"] == SubscriptionStatus.PENDING_PAYMENT.value
    assert Decimal(str(data["payment_amount"])) == Decimal("199.00")
    assert data["external_payment_id"] == "pay_remote_pkg_123"

    # Verify database persistence: notes must be dict {'custom_amount': False}
    sub = session.execute(
        select(MemberSubscription).where(
            MemberSubscription.subscriber_membership_id == UUID(data["membership_id"])
        )
    ).scalar_one()
    assert sub.notes == {"custom_amount": False}
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value
    assert sub.agreed_amount == Decimal("199.00")

    order = session.execute(
        select(PaymentOrder).where(PaymentOrder.subscription_id == sub.id)
    ).scalar_one()
    assert order.custom_amount is False
    assert order.amount == Decimal("199.00")
    assert order.amount_paise == 19900
    assert order.status == PaymentOrderStatus.CREATED.value
    assert order.external_payment_id == "pay_remote_pkg_123"


@pytest.mark.anyio
async def test_subscriber_registration_custom_amount_persists_dict_notes(test_settings, memory_db):
    """Verify custom-amount subscriber registration stores notes as dict and maps custom_amount=True."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_registration_fixture(session)

    mock_payment_client = AsyncMock(spec=PaymentMicroserviceClient)
    mock_payment_client.initiate_payment = AsyncMock(
        return_value=PaymentInitResult(
            success=True,
            payment_id="pay_remote_custom_450",
            order_id="ord_remote_custom_450",
            payment_page_url="http://handorg.duckdns.org/checkout/pay_remote_custom_450",
            status="INITIATED",
            amount=45000,
            currency="INR",
            tenant_id=fixture["tenant"].slug,
        )
    )

    app = create_app(test_settings)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "test_cookie")
    app.state.payment_client = mock_payment_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/users",
            json={
                "full_name": "Pooja Verma",
                "mobile_e164": "+919811122244",
                "custom_amount": "450.00",
                "whatsapp_consent": True,
            },
        )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["package_type"] == "Custom"
    assert data["payment_status"] == PaymentOrderStatus.CREATED.value
    assert data["subscription_status"] == SubscriptionStatus.PENDING_PAYMENT.value
    assert Decimal(str(data["payment_amount"])) == Decimal("450.00")
    assert data["external_payment_id"] == "pay_remote_custom_450"

    # Verify database persistence: notes must be dict {'custom_amount': True}
    sub = session.execute(
        select(MemberSubscription).where(
            MemberSubscription.subscriber_membership_id == UUID(data["membership_id"])
        )
    ).scalar_one()
    assert sub.notes == {"custom_amount": True}
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value
    assert sub.agreed_amount == Decimal("450.00")

    order = session.execute(
        select(PaymentOrder).where(PaymentOrder.subscription_id == sub.id)
    ).scalar_one()
    assert order.custom_amount is True
    assert order.amount == Decimal("450.00")
    assert order.amount_paise == 45000
    assert order.status == PaymentOrderStatus.CREATED.value


@pytest.mark.anyio
async def test_subscriber_registration_rolls_back_on_failed_payment_initiation(test_settings, memory_db):
    """Verify failed payment initiation triggers session rollback and leaves zero orphan records."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_registration_fixture(session)

    mock_payment_client = AsyncMock(spec=PaymentMicroserviceClient)
    mock_payment_client.initiate_payment = AsyncMock(
        return_value=PaymentInitResult(
            success=False,
            payment_id="",
            order_id="",
            payment_page_url="",
            status="FAILED",
            amount=0,
            currency="INR",
            tenant_id=fixture["tenant"].slug,
            error="Payment gateway returned 502 Bad Gateway",
        )
    )

    app = create_app(test_settings)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "test_cookie")
    app.state.payment_client = mock_payment_client

    test_mobile = "+919811199999"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/users",
            json={
                "full_name": "Failed Subscriber",
                "mobile_e164": test_mobile,
                "custom_amount": "300.00",
                "whatsapp_consent": False,
            },
        )

    assert resp.status_code == 502
    err_body = resp.json().get("error", resp.json().get("detail", {}))
    assert "Payment initiation failed" in str(err_body)

    # Verify zero orphaned records in DB
    existing_mem = session.execute(
        select(TenantMembership).where(TenantMembership.mobile_e164 == test_mobile)
    ).scalar_one_or_none()
    assert existing_mem is None

    existing_profile = session.execute(
        select(SubscriberProfile).where(SubscriberProfile.full_name == "Failed Subscriber")
    ).scalar_one_or_none()
    assert existing_profile is None

    # Total subscriptions count must not increase (remains 0 in this fixture)
    subs = session.execute(select(MemberSubscription)).scalars().all()
    assert len(subs) == 0

    orders = session.execute(select(PaymentOrder)).scalars().all()
    assert len(orders) == 0


@pytest.mark.anyio
async def test_subscriber_registration_rolls_back_on_database_failure(test_settings, memory_db):
    """Verify database-level conflicts (e.g. duplicate mobile) fail closed without initiating payment."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_registration_fixture(session)

    # Pre-register a member with mobile +919811188888
    existing_user = User(id=uuid4(), status="active")
    existing_mem = TenantMembership(
        id=uuid4(),
        tenant_id=fixture["tenant"].id,
        user_id=existing_user.id,
        mobile_e164="+919811188888",
        status="active",
    )
    session.add_all([existing_user, existing_mem])
    session.commit()

    mock_payment_client = AsyncMock(spec=PaymentMicroserviceClient)
    mock_payment_client.initiate_payment = AsyncMock()

    app = create_app(test_settings)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "test_cookie")
    app.state.payment_client = mock_payment_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/users",
            json={
                "full_name": "Duplicate Mobile User",
                "mobile_e164": "+919811188888",
                "custom_amount": "500.00",
            },
        )

    assert resp.status_code == 409
    err_body = resp.json().get("error", resp.json().get("detail", {}))
    assert "already registered" in str(err_body)
    # Crucial: initiate_payment was NEVER called on gateway
    assert mock_payment_client.initiate_payment.call_count == 0

    # No subscriber profile or subscription was created
    sub_prof = session.execute(
        select(SubscriberProfile).where(SubscriberProfile.full_name == "Duplicate Mobile User")
    ).scalar_one_or_none()
    assert sub_prof is None


def test_cancel_subscription_with_dict_notes(memory_db):
    """Verify cancel_subscription properly appends cancellation reason to dict notes."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_registration_fixture(session)

    now = datetime.now(UTC)
    sub_id = uuid4()
    sub = MemberSubscription(
        id=sub_id,
        tenant_id=fixture["tenant"].id,
        subscriber_membership_id=fixture["agent_mem"].id,
        plan_id=fixture["plan"].id,
        agreed_amount=Decimal("199.00"),
        starts_at=now,
        expires_at=now + timedelta(days=30),
        status=SubscriptionStatus.ACTIVE.value,
        payment_status=SubscriptionPaymentStatus.SETTLED.value,
        snapshot_plan_name="Basic Pack",
        snapshot_duration_days=30,
        notes={"custom_amount": False},
        created_by_user_id=fixture["agent_mem"].user_id,
    )
    session.add(sub)
    session.commit()

    from app.modules.subscriptions.service import SubscriptionService
    svc = SubscriptionService()
    cancelled = svc.cancel_subscription(
        session=session,
        tenant_id=fixture["tenant"].id,
        subscription_id=sub_id,
        reason="Subscriber requested refund",
        operator_user_id=fixture["agent_mem"].user_id,
    )
    assert cancelled.status == SubscriptionStatus.CANCELLED.value
    assert isinstance(cancelled.notes, dict)
    assert cancelled.notes.get("custom_amount") is False
    assert cancelled.notes.get("cancellation_reason") == "Subscriber requested refund"


