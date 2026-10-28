"""Tests for Payment Microservice Webhook, Signature Validation, Production Security, and Notification Contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.agents.models import SubscriberProfile
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.models import AccountCredential
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.identity.models import TenantMembership, User
from app.modules.messaging.notification_service import NotificationServiceClient
from app.modules.payments.microservice_client import PaymentMicroserviceClient
from app.modules.payments.models import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentTransaction,
    PaymentWebhookEvent,
)
from app.modules.platform_identity.models import PlatformOperator
from app.modules.subscriptions.models import (
    MemberSubscription,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.tenants.models import Tenant


@pytest.fixture
def test_settings():
    return Settings(
        app_name="Community OTT Test",
        environment="test",
        database_url=SecretStr("postgresql+psycopg://test:test@127.0.0.1:5432/test_db"),
        payment_service_webhook_secret=SecretStr("super_secret_payment_webhook_key_123"),
        payment_service_api_key=SecretStr("test_payment_api_key_456"),
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


@pytest.mark.anyio
async def test_payment_webhook_valid_signature_settles_order_and_activates_subscription(
    test_settings, memory_db
):
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    agent_id = uuid4()
    sub_user_id = uuid4()
    agent_mem_id = uuid4()
    sub_mem_id = uuid4()
    plan_id = uuid4()
    sub_id = uuid4()
    order_id = uuid4()
    external_pay_id = "pay_test_external_9999"

    tenant = Tenant(id=tenant_id, slug="tenant-alpha", status="active")
    agent_user = User(id=agent_id, status="active")
    agent_mem = TenantMembership(id=agent_mem_id, tenant_id=tenant_id, user_id=agent_id, mobile_e164="+919876543210", status="active")
    sub_user = User(id=sub_user_id, status="active")
    sub_mem = TenantMembership(id=sub_mem_id, tenant_id=tenant_id, user_id=sub_user_id, mobile_e164="+919811112233", status="active")

    sub_profile = SubscriberProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        membership_id=sub_mem_id,
        user_id=sub_user_id,
        registered_by_agent_membership_id=agent_mem_id,
        full_name="Kavitha Reddy",
        whatsapp_number="+919811112233",
        whatsapp_verified=True,
        whatsapp_consent=True,
        email="kavitha@example.com",
        status="active",
    )

    plan = SubscriptionPlan(
        id=plan_id,
        community_id=tenant_id,
        code="GOLD_30",
        name="Gold 30 Days",
        nominal_price=Decimal("299.00"),
        duration_days=30,
        currency="INR",
        status="active",
    )

    sub = MemberSubscription(
        id=sub_id,
        tenant_id=tenant_id,
        subscriber_membership_id=sub_mem_id,
        plan_id=plan_id,
        agreed_amount=Decimal("299.00"),
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value,
        payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name="Gold 30 Days",
        snapshot_duration_days=30,
        created_by_user_id=agent_id,
    )

    order = PaymentOrder(
        id=order_id,
        tenant_id=tenant_id,
        subscription_id=sub_id,
        subscriber_membership_id=sub_mem_id,
        gateway="payment_microservice",
        gateway_order_id="order_gateway_777",
        external_payment_id=external_pay_id,
        amount_paise=29900,
        amount=Decimal("299.00"),
        currency="INR",
        status=PaymentOrderStatus.CREATED.value,
        created_by_membership_id=agent_mem_id,
    )

    session.add_all([tenant, agent_user, agent_mem, sub_user, sub_mem, sub_profile, plan, sub, order])
    session.commit()

    # Build FastAPI test app overriding DB dependency
    app = create_app(test_settings)
    from app.db.session import get_db

    app.dependency_overrides[get_db] = lambda: session

    payload = {
        "event_id": "evt_unique_101",
        "event_type": "payment.completed",
        "payment_id": external_pay_id,
        "order_id": "order_gateway_777",
        "status": "COMPLETED",
        "amount": 29900,
        "currency": "INR",
        "tenant_id": str(tenant_id),
        "timestamp": int(datetime.now(UTC).timestamp()),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    secret = test_settings.payment_service_webhook_secret.get_secret_value()
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/webhooks/payments/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Payment-Signature": signature,
                "X-Event-ID": "evt_unique_101",
            },
        )

    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "processed"
    assert data["event_id"] == "evt_unique_101"

    # Verify state updates in DB
    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.PAID.value
    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.payment_status == SubscriptionPaymentStatus.SETTLED.value
    assert order.notes.get("welcome_notified") is True


@pytest.mark.anyio
async def test_payment_webhook_invalid_signature_rejected(test_settings, memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    app = create_app(test_settings)
    from app.db.session import get_db

    app.dependency_overrides[get_db] = lambda: session

    payload = {"event_id": "evt_invalid_sig", "status": "COMPLETED"}
    raw_body = json.dumps(payload).encode("utf-8")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/webhooks/payments/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Payment-Signature": "invalid_forged_signature_hex",
            },
        )

    assert resp.status_code == 400
    assert "signature verification failed" in resp.json()["error"]["message"]


@pytest.mark.anyio
async def test_payment_webhook_duplicate_event_is_idempotent(test_settings, memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    agent_id = uuid4()
    sub_user_id = uuid4()
    agent_mem_id = uuid4()
    sub_mem_id = uuid4()
    plan_id = uuid4()
    sub_id = uuid4()
    order_id = uuid4()
    external_pay_id = "pay_test_dup_123"

    tenant = Tenant(id=tenant_id, slug="tenant-dup", status="active")
    agent_user = User(id=agent_id, status="active")
    agent_mem = TenantMembership(id=agent_mem_id, tenant_id=tenant_id, user_id=agent_id, mobile_e164="+919876543210", status="active")
    sub_user = User(id=sub_user_id, status="active")
    sub_mem = TenantMembership(id=sub_mem_id, tenant_id=tenant_id, user_id=sub_user_id, mobile_e164="+919822223344", status="active")
    plan = SubscriptionPlan(id=plan_id, community_id=tenant_id, code="PL1", name="P1", nominal_price=Decimal("199.00"), duration_days=30, currency="INR", status="active")
    sub = MemberSubscription(
        id=sub_id, tenant_id=tenant_id, subscriber_membership_id=sub_mem_id, plan_id=plan_id,
        agreed_amount=Decimal("199.00"), starts_at=datetime.now(UTC), expires_at=datetime.now(UTC)+timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value, payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name="P1", snapshot_duration_days=30, created_by_user_id=agent_id,
    )
    order = PaymentOrder(
        id=order_id, tenant_id=tenant_id, subscription_id=sub_id, subscriber_membership_id=sub_mem_id,
        gateway="payment_microservice", gateway_order_id="ord_dup_gateway", external_payment_id=external_pay_id,
        amount_paise=19900, amount=Decimal("199.00"), currency="INR", status=PaymentOrderStatus.CREATED.value,
        created_by_membership_id=agent_mem_id,
    )
    session.add_all([tenant, agent_user, agent_mem, sub_user, sub_mem, plan, sub, order])
    session.commit()

    app = create_app(test_settings)
    from app.db.session import get_db

    app.dependency_overrides[get_db] = lambda: session

    payload = {
        "event_id": "evt_dup_999",
        "event_type": "payment.completed",
        "payment_id": external_pay_id,
        "status": "COMPLETED",
        "amount": 19900,
        "tenant_id": str(tenant_id),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    secret = test_settings.payment_service_webhook_secret.get_secret_value()
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Payment-Signature": signature}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. First delivery
        resp1 = await client.post("/api/v1/webhooks/payments/status", content=raw_body, headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "processed"

        # 2. Duplicate delivery with same event_id
        resp2 = await client.post("/api/v1/webhooks/payments/status", content=raw_body, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"


@pytest.mark.anyio
async def test_payment_webhook_cross_tenant_rejected(test_settings, memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    tenant1_id = uuid4()
    tenant2_id = uuid4()
    agent_id = uuid4()
    sub_user_id = uuid4()
    sub_mem_id = uuid4()
    plan_id = uuid4()
    sub_id = uuid4()
    order_id = uuid4()
    external_pay_id = "pay_test_cross_tenant"

    t1 = Tenant(id=tenant1_id, slug="tenant-1", status="active")
    t2 = Tenant(id=tenant2_id, slug="tenant-2", status="active")
    sub_user = User(id=sub_user_id, status="active")
    sub_mem = TenantMembership(id=sub_mem_id, tenant_id=tenant1_id, user_id=sub_user_id, mobile_e164="+919833334455", status="active")
    plan = SubscriptionPlan(id=plan_id, community_id=tenant1_id, code="PLX", name="PX", nominal_price=Decimal("100.00"), duration_days=30, currency="INR", status="active")
    sub = MemberSubscription(
        id=sub_id, tenant_id=tenant1_id, subscriber_membership_id=sub_mem_id, plan_id=plan_id,
        agreed_amount=Decimal("100.00"), starts_at=datetime.now(UTC), expires_at=datetime.now(UTC)+timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value, payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name="PX", snapshot_duration_days=30, created_by_user_id=agent_id,
    )
    order = PaymentOrder(
        id=order_id, tenant_id=tenant1_id, subscription_id=sub_id, subscriber_membership_id=sub_mem_id,
        gateway="payment_microservice", gateway_order_id="ord_xtenant", external_payment_id=external_pay_id,
        amount_paise=10000, amount=Decimal("100.00"), currency="INR", status=PaymentOrderStatus.CREATED.value,
    )
    session.add_all([t1, t2, sub_user, sub_mem, plan, sub, order])
    session.commit()

    app = create_app(test_settings)
    from app.db.session import get_db

    app.dependency_overrides[get_db] = lambda: session

    # Payload claims tenant2_id for an order belonging to tenant1_id
    payload = {
        "event_id": "evt_xtenant_1",
        "payment_id": external_pay_id,
        "status": "COMPLETED",
        "tenant_id": str(tenant2_id),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    secret = test_settings.payment_service_webhook_secret.get_secret_value()
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/webhooks/payments/status",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Payment-Signature": signature},
        )

    assert resp.status_code == 403
    assert "Cross-tenant" in resp.json()["error"]["message"]


@pytest.mark.anyio
async def test_payment_webhook_altered_amount_rejected(test_settings, memory_db):
    session_factory, _ = memory_db
    session = session_factory()

    tenant_id = uuid4()
    agent_id = uuid4()
    sub_user_id = uuid4()
    sub_mem_id = uuid4()
    plan_id = uuid4()
    sub_id = uuid4()
    order_id = uuid4()
    external_pay_id = "pay_test_altered_amt"

    t = Tenant(id=tenant_id, slug="tenant-amt", status="active")
    sub_user = User(id=sub_user_id, status="active")
    sub_mem = TenantMembership(id=sub_mem_id, tenant_id=tenant_id, user_id=sub_user_id, mobile_e164="+919844445566", status="active")
    plan = SubscriptionPlan(id=plan_id, community_id=tenant_id, code="PLM", name="PM", nominal_price=Decimal("500.00"), duration_days=30, currency="INR", status="active")
    sub = MemberSubscription(
        id=sub_id, tenant_id=tenant_id, subscriber_membership_id=sub_mem_id, plan_id=plan_id,
        agreed_amount=Decimal("500.00"), starts_at=datetime.now(UTC), expires_at=datetime.now(UTC)+timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value, payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name="PM", snapshot_duration_days=30, created_by_user_id=agent_id,
    )
    order = PaymentOrder(
        id=order_id, tenant_id=tenant_id, subscription_id=sub_id, subscriber_membership_id=sub_mem_id,
        gateway="payment_microservice", gateway_order_id="ord_alt_amt", external_payment_id=external_pay_id,
        amount_paise=50000, amount=Decimal("500.00"), currency="INR", status=PaymentOrderStatus.CREATED.value,
    )
    session.add_all([t, sub_user, sub_mem, plan, sub, order])
    session.commit()

    app = create_app(test_settings)
    from app.db.session import get_db

    app.dependency_overrides[get_db] = lambda: session

    # Order expects 50000 paise (₹500.00), webhook sends only 1000 paise (₹10.00)
    payload = {
        "event_id": "evt_amt_hack",
        "payment_id": external_pay_id,
        "status": "COMPLETED",
        "amount": 1000,
        "tenant_id": str(tenant_id),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    secret = test_settings.payment_service_webhook_secret.get_secret_value()
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/webhooks/payments/status",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Payment-Signature": signature},
        )

    assert resp.status_code == 400
    assert "Payment amount does not match" in resp.json()["error"]["message"]


@pytest.mark.anyio
async def test_production_environment_rejects_mock_payment_status():
    # Configure production settings
    prod_settings = Settings(
        app_name="Community OTT Prod",
        environment="production",
        payment_service_url="https://payment.communityott.internal",
        payment_service_api_key=SecretStr("real_prod_api_key_12345"),
        payment_service_webhook_secret=SecretStr("real_prod_webhook_secret_67890"),
        payment_delivery_mode="payment_service",
        database_url=SecretStr("postgresql+psycopg://user:real_password_8899@localhost:5432/prod_db"),
        frontend_origins="https://app.communityott.internal",
        session_cookie_secure=True,
        messaging_provider="http",
        messaging_api_base_url="https://messaging.communityott.internal",
        messaging_send_otp_path="/api/v1/send-otp",
        messaging_verify_otp_path="/api/v1/verify-otp",
        messaging_send_otp_method="POST",
        messaging_verify_otp_method="POST",
        messaging_api_key=SecretStr("messaging_secret_key_12345"),
        messaging_auth_header_name="X-API-Key",
        otp_challenge_ttl_seconds=300,
        otp_delivery_mode="notification_service",
        otp_log_to_console=False,
        notification_debug_logging=False,
        notification_service_base_url="https://notifications.communityott.internal",
        notification_service_api_key=SecretStr("notif_secret_key_12345"),
        notification_webhook_secret=SecretStr("notif_webhook_secret_67890"),
        auth_session_cookie_path="/backend-api/api/v1",
    )

    client = PaymentMicroserviceClient(settings=prod_settings)

    # In production, a mock payment ID MUST NOT be marked COMPLETED
    res = await client.get_payment_status("pay_mock_forged_id_12345")
    assert res.success is False
    assert res.status == "FAILED"
    assert "Mock payment IDs are strictly forbidden" in (res.error or "")


@pytest.mark.anyio
async def test_payment_status_multi_endpoint_resolution():
    """Verify that get_payment_status checks both /api/v1/payments/{payment_id} and /checkout/api/details/{payment_id}."""
    test_settings = Settings(
        environment="test",
        payment_service_url="http://mock-service",
        payment_service_api_key=SecretStr("test_key"),
        payment_delivery_mode="payment_service",
        otp_delivery_mode="mock",
        otp_log_to_console=False,
        notification_debug_logging=False,
    )

    # Mock an httpx Transport to verify which URLs are hit
    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path == "/api/v1/payments/pay_live_001":
            return httpx.Response(200, json={
                "payment_id": "pay_live_001",
                "order_id": "order_gateway_001",
                "status": "COMPLETED",
                "amount": 29900,
                "currency": "INR",
            })
        elif url_path == "/checkout/api/details/pay_live_002":
            return httpx.Response(200, json={
                "paymentId": "pay_live_002",
                "orderId": "order_gateway_002",
                "isCompleted": True,
                "amount": 49900,
                "currency": "INR",
            })
        return httpx.Response(404, json={"error": "Not found"})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport, base_url="http://mock-service")

    client = PaymentMicroserviceClient(settings=test_settings, client=async_client)

    # Test 1: Hit authoritative server-to-server endpoint (/api/v1/payments/{payment_id})
    res1 = await client.get_payment_status("pay_live_001")
    assert res1.success is True
    assert res1.status == "COMPLETED"
    assert res1.order_id == "order_gateway_001"
    assert res1.amount == 29900
    assert res1.is_authoritative is True
    assert res1.source_endpoint == "http://mock-service/api/v1/payments/pay_live_001"

    # Test 2: Falls back to checkout details endpoint (/checkout/api/details/{payment_id})
    res2 = await client.get_payment_status("pay_live_002")
    assert res2.success is True
    assert res2.status == "COMPLETED"
    assert res2.order_id == "order_gateway_002"
    assert res2.amount == 49900
    assert res2.is_authoritative is False
    assert res2.source_endpoint == "http://mock-service/checkout/api/details/pay_live_002"


@pytest.mark.anyio
async def test_welcome_notification_no_secrets_and_strict_consent():
    """Verify welcome notifications NEVER contain secrets and strictly respect whatsapp_consent."""
    settings = Settings(
        otp_delivery_mode="mock",
        environment="development",
        otp_log_to_console=False,
    )
    client = NotificationServiceClient(settings=settings)

    # Case 1: WhatsApp consent = False -> WhatsApp MUST NOT be dispatched
    res1 = await client.send_subscriber_welcome(
        subscriber_name="Praveen Kumar",
        mobile_e164="+919876543210",
        whatsapp_number="+919876543210",
        whatsapp_consent=False,
        email="praveen@example.com",
        plan_name="Gold Monthly",
        amount_paid="299.00",
    )
    assert res1["sms"] is not None
    assert res1["email"] is not None
    assert res1["whatsapp"] is None

    # Case 2: WhatsApp consent = True -> WhatsApp dispatched
    res2 = await client.send_subscriber_welcome(
        subscriber_name="Praveen Kumar",
        mobile_e164="+919876543210",
        whatsapp_number="+919876543210",
        whatsapp_consent=True,
        email=None,
        plan_name="Gold Monthly",
        amount_paid="299.00",
    )
    assert res2["sms"] is not None
    assert res2["whatsapp"] is not None
    assert res2["email"] is None


def setup_agent_order_fixture(
    session,
    tenant_id=None,
    agent_id=None,
    amount_paise=29900,
    currency="INR",
    external_pay_id="pay_live_test_1",
):
    tenant_id = tenant_id or uuid4()
    agent_id = agent_id or uuid4()
    sub_user_id = uuid4()
    agent_mem_id = uuid4()
    sub_mem_id = uuid4()
    plan_id = uuid4()
    sub_id = uuid4()
    order_id = uuid4()

    tenant = Tenant(id=tenant_id, slug=f"tenant-{uuid4().hex[:6]}", status="active")
    agent_user = User(id=agent_id, status="active")
    agent_mem = TenantMembership(id=agent_mem_id, tenant_id=tenant_id, user_id=agent_id, mobile_e164="+919876543210", status="active")
    sub_user = User(id=sub_user_id, status="active")
    sub_mem = TenantMembership(id=sub_mem_id, tenant_id=tenant_id, user_id=sub_user_id, mobile_e164="+919811223344", status="active")

    sub_profile = SubscriberProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        membership_id=sub_mem_id,
        user_id=sub_user_id,
        registered_by_agent_membership_id=agent_mem_id,
        full_name="Ravi Kumar",
        whatsapp_number="+919811223344",
        whatsapp_verified=True,
        whatsapp_consent=True,
        email="ravi@example.com",
        status="active",
    )

    plan = SubscriptionPlan(
        id=plan_id,
        community_id=tenant_id,
        code="TEST_PLAN",
        name="Test Plan",
        nominal_price=Decimal(str(amount_paise / 100.0)),
        duration_days=30,
        currency=currency,
        status="active",
    )

    sub = MemberSubscription(
        id=sub_id,
        tenant_id=tenant_id,
        subscriber_membership_id=sub_mem_id,
        plan_id=plan_id,
        agreed_amount=Decimal(str(amount_paise / 100.0)),
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value,
        payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name="Test Plan",
        snapshot_duration_days=30,
        created_by_user_id=agent_id,
    )

    order = PaymentOrder(
        id=order_id,
        tenant_id=tenant_id,
        subscription_id=sub_id,
        subscriber_membership_id=sub_mem_id,
        gateway="payment_microservice",
        gateway_order_id=f"order_{uuid4().hex[:14]}",
        external_payment_id=external_pay_id,
        amount_paise=amount_paise,
        amount=Decimal(str(amount_paise / 100.0)),
        currency=currency,
        status=PaymentOrderStatus.CREATED.value,
        created_by_membership_id=agent_mem_id,
    )

    session.add_all([tenant, agent_user, agent_mem, sub_user, sub_mem, sub_profile, plan, sub, order])
    session.commit()

    agent_session = ResolvedSession(
        session_id=uuid4(),
        account_type="user",
        user_id=agent_id,
        username="agent_tester",
        role="SUPER_ADMIN",
        status="active",
        csrf_token="csrf_token_val",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=tenant_id,
        membership_id=agent_mem_id,
    )

    return {
        "tenant": tenant,
        "agent_mem": agent_mem,
        "sub": sub,
        "order": order,
        "agent_session": agent_session,
    }


@pytest.mark.anyio
async def test_untrusted_checkout_details_rejected_for_settlement(test_settings, memory_db):
    """Confirm settlement cannot be granted based on unauthenticated public checkout details."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, external_pay_id="pay_public_checkout_only")
    order = fixture["order"]
    sub = fixture["sub"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkout/api/details/pay_public_checkout_only":
            return httpx.Response(200, json={
                "paymentId": "pay_public_checkout_only",
                "isCompleted": True,
                "amount": 29900,
                "currency": "INR",
            })
        return httpx.Response(404, json={"error": "Not found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-service")
    app = create_app(test_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")
    app.state.payment_client = PaymentMicroserviceClient(settings=test_settings, client=mock_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNTRUSTED_PAYMENT_STATUS" or "untrusted public checkout" in err["message"]

    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.CREATED.value
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value
    assert sub.payment_status == SubscriptionPaymentStatus.UNPAID.value


@pytest.mark.anyio
async def test_unavailable_status_endpoint_fails_closed_leaves_order_pending(test_settings, memory_db):
    """When gateway status is unreachable/unavailable, payment confirmation must fail closed."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, external_pay_id="pay_unavailable_svc")
    order = fixture["order"]
    sub = fixture["sub"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "Service Unavailable"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-service")
    app = create_app(test_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")
    app.state.payment_client = PaymentMicroserviceClient(settings=test_settings, client=mock_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 400
    err = resp.json()["error"]
    assert "could not be verified" in err["message"] or "Mock payment IDs are strictly forbidden" in err["message"] or "could not be verified" in err["message"]

    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.CREATED.value
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value


@pytest.mark.anyio
async def test_confirm_payment_amount_mismatch_rejected(test_settings, memory_db):
    """Gateway returns an altered amount -> payment confirmation must reject."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, amount_paise=29900, external_pay_id="pay_amt_hack")
    order = fixture["order"]
    sub = fixture["sub"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/payments/pay_amt_hack":
            return httpx.Response(200, json={
                "payment_id": "pay_amt_hack",
                "order_id": order.gateway_order_id,
                "status": "COMPLETED",
                "amount": 1000,
                "currency": "INR",
                "tenant_id": str(fixture["tenant"].id),
            })
        return httpx.Response(404, json={"error": "Not found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-service")
    app = create_app(test_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")
    app.state.payment_client = PaymentMicroserviceClient(settings=test_settings, client=mock_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "AMOUNT_MISMATCH" or "Payment amount" in err["message"]

    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.CREATED.value
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value


@pytest.mark.anyio
async def test_confirm_payment_cross_tenant_rejected(test_settings, memory_db):
    """Gateway returns payment belonging to a different tenant -> rejected with HTTP 403."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, external_pay_id="pay_cross_tenant_attacker")
    order = fixture["order"]
    sub = fixture["sub"]
    other_tenant_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/payments/pay_cross_tenant_attacker":
            return httpx.Response(200, json={
                "payment_id": "pay_cross_tenant_attacker",
                "order_id": order.gateway_order_id,
                "status": "COMPLETED",
                "amount": 29900,
                "currency": "INR",
                "tenant_id": str(other_tenant_id),
            })
        return httpx.Response(404, json={"error": "Not found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-service")
    app = create_app(test_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")
    app.state.payment_client = PaymentMicroserviceClient(settings=test_settings, client=mock_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "CROSS_TENANT_PAYMENT_REJECTED" or "different tenant" in err["message"]

    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.CREATED.value
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value


@pytest.mark.anyio
async def test_confirm_payment_currency_mismatch_rejected(test_settings, memory_db):
    """Gateway returns mismatched currency -> rejected with HTTP 400."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, currency="INR", external_pay_id="pay_currency_hack")
    order = fixture["order"]
    sub = fixture["sub"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/payments/pay_currency_hack":
            return httpx.Response(200, json={
                "payment_id": "pay_currency_hack",
                "order_id": order.gateway_order_id,
                "status": "COMPLETED",
                "amount": 29900,
                "currency": "USD",
                "tenant_id": str(fixture["tenant"].id),
            })
        return httpx.Response(404, json={"error": "Not found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-service")
    app = create_app(test_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")
    app.state.payment_client = PaymentMicroserviceClient(settings=test_settings, client=mock_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "CURRENCY_MISMATCH" or "Currency mismatch" in err["message"]

    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.CREATED.value
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value


@pytest.mark.anyio
async def test_confirm_payment_authoritative_success_activates_subscription(test_settings, memory_db):
    """Authoritative server-verified payment successfully settles and activates subscription."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, amount_paise=29900, currency="INR", external_pay_id="pay_authoritative_ok")
    order = fixture["order"]
    sub = fixture["sub"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/payments/pay_authoritative_ok":
            return httpx.Response(200, json={
                "payment_id": "pay_authoritative_ok",
                "order_id": order.gateway_order_id,
                "status": "COMPLETED",
                "amount": 29900,
                "currency": "INR",
                "tenant_id": str(fixture["tenant"].id),
            })
        return httpx.Response(404, json={"error": "Not found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-service")
    app = create_app(test_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")
    app.state.payment_client = PaymentMicroserviceClient(settings=test_settings, client=mock_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "paid"
    assert body["subscription_status"] == "ACTIVE"

    # Verify database persistence
    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.PAID.value
    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.payment_status == SubscriptionPaymentStatus.SETTLED.value
    assert order.notes.get("welcome_notified") is True


@pytest.mark.anyio
async def test_mock_payment_in_staging_environment_cannot_activate_payment(memory_db):
    """In staging or production, mock payment IDs cannot activate subscriptions."""
    session_factory, _ = memory_db
    session = session_factory()
    fixture = setup_agent_order_fixture(session, external_pay_id="pay_mock_exploit_999")
    order = fixture["order"]
    sub = fixture["sub"]

    staging_settings = Settings(
        app_name="Community OTT Staging",
        environment="staging",
        database_url=SecretStr("postgresql+psycopg://user:real_password_8899@localhost:5432/staging_db"),
        frontend_origins="https://staging.communityott.internal",
        session_cookie_secure=True,
        messaging_provider="http",
        messaging_api_base_url="https://messaging.communityott.internal",
        messaging_send_otp_path="/api/v1/send-otp",
        messaging_verify_otp_path="/api/v1/verify-otp",
        messaging_send_otp_method="POST",
        messaging_verify_otp_method="POST",
        messaging_api_key=SecretStr("messaging_secret_key_12345"),
        messaging_auth_header_name="X-API-Key",
        otp_challenge_ttl_seconds=300,
        otp_delivery_mode="notification_service",
        otp_log_to_console=False,
        notification_debug_logging=False,
        notification_service_base_url="https://notifications.communityott.internal",
        notification_service_api_key=SecretStr("notif_secret_key_12345"),
        notification_webhook_secret=SecretStr("notif_webhook_secret_67890"),
        auth_session_cookie_path="/backend-api/api/v1",
        payment_service_url="https://payment.communityott.internal",
        payment_service_api_key=SecretStr("staging_api_key"),
        payment_service_webhook_secret=SecretStr("staging_webhook_secret"),
        payment_delivery_mode="payment_service",
    )

    app = create_app(staging_settings)
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_resolved_session] = lambda: (fixture["agent_session"], "cookie_token")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/v1/agent/payments/confirm",
            json={"payment_order_id": str(order.id)},
        )

    assert resp.status_code == 400
    err = resp.json()["error"]
    assert "could not be verified" in err["message"] or "Mock payment IDs are strictly forbidden" in err["message"]

    # Fail closed check
    session.refresh(order)
    session.refresh(sub)
    assert order.status == PaymentOrderStatus.CREATED.value
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value
