from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
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
from app.modules.commissions.models import CommissionJournal
from app.modules.identity.models import TenantMembership, User
from app.modules.payments.client import RazorpayClientAdapter
from app.modules.payments.models import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    PaymentWebhookEvent,
)
from app.modules.payments.service import PaymentService, get_payment_service
from app.modules.subscriptions.models import (
    MemberSubscription,
    PlanStatus,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.tenants.models import Tenant


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def payment_harness(tmp_path):
    database_path = tmp_path / "payments-test.sqlite"
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

    # Tenant A Admin
    admin_a_token = "admin_a_session_token_payments"
    admin_a_digest = hashlib.sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = hashlib.sha256(b"admin_a_csrf_pay").hexdigest()
    admin_a_user_id = uuid4()
    admin_a_mem_id = uuid4()

    # Tenant A Agent
    agent_a_token = "agent_a_session_token_payments"
    agent_a_digest = hashlib.sha256(agent_a_token.encode("utf-8")).hexdigest()
    agent_a_csrf = hashlib.sha256(b"agent_a_csrf_pay").hexdigest()
    agent_a_user_id = uuid4()
    agent_a_mem_id = uuid4()

    # Tenant A Subscriber
    subscriber_a_user_id = uuid4()
    subscriber_a_mem_id = uuid4()

    # Tenant B Admin (for cross-tenant checks)
    admin_b_token = "admin_b_session_token_payments"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf_pay").hexdigest()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Create Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="comm-pay-a",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="comm-pay-b",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=12.0,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        # Seed RBAC roles and permissions
        seed_tenant_role_permissions(session, tenant_a_id)
        seed_tenant_role_permissions(session, tenant_b_id)
        session.flush()

        admin_role_a = session.execute(
            select(Role).where(Role.tenant_id == tenant_a_id, Role.code == "admin")
        ).scalar_one()
        agent_role_a = session.execute(
            select(Role).where(Role.tenant_id == tenant_a_id, Role.code == "agent")
        ).scalar_one()
        admin_role_b = session.execute(
            select(Role).where(Role.tenant_id == tenant_b_id, Role.code == "admin")
        ).scalar_one()

        # Admin A User & Membership
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

        # Agent A User & Membership
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

        # Subscriber A User & Membership
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

        # Admin B User & Membership (Tenant B)
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

        # Subscription Plan
        plan = SubscriptionPlan(
            id=uuid4(),
            code="PLAN_ANNUAL_VIP",
            name="Annual VIP Pass",
            description="All access annual subscription",
            benefits={"max_screens": 4, "resolution": "4K"},
            duration_days=365,
            status=PlanStatus.ACTIVE.value,
            nominal_price=Decimal("2499.00"),
            currency="INR",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        # Subscription with manual agreed amount 1499.50
        sub_id = uuid4()
        sub = MemberSubscription(
            id=sub_id,
            tenant_id=tenant_a_id,
            subscriber_membership_id=subscriber_a_mem_id,
            plan_id=plan.id,
            agreed_amount=Decimal("1499.50"),
            currency="INR",
            starts_at=now,
            expires_at=now + timedelta(days=365),
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            snapshot_plan_name="Annual VIP Pass",
            snapshot_duration_days=365,
            created_by_user_id=admin_a_user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.commit()

    settings = Settings(
        app_env="testing",
        jwt_secret_key="test-secret-key-for-payments-testing",
        csrf_secret_key="test-csrf-secret-key-for-pay",
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

    mock_adapter = RazorpayClientAdapter(
        key_id="rzp_test_fixture_key",
        key_secret="fixture_secret_1234567890",
        webhook_secret="fixture_webhook_secret_9876543210",
        is_mock=True,
    )

    from app.modules.payments import client as payments_client_mod

    payments_client_mod._default_adapter = mock_adapter

    return {
        "engine": engine,
        "factory": factory,
        "app": app,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "admin_a_token": admin_a_token,
        "admin_a_csrf": admin_a_csrf,
        "agent_a_token": agent_a_token,
        "agent_a_csrf": agent_a_csrf,
        "admin_b_token": admin_b_token,
        "admin_b_csrf": admin_b_csrf,
        "subscriber_a_mem_id": subscriber_a_mem_id,
        "subscription_id": sub_id,
        "plan": plan,
        "adapter": mock_adapter,
    }


@pytest.mark.anyio
async def test_order_creation_converts_manual_agreed_amount_to_exact_paise(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["subscription_id"] == str(h["subscription_id"])
        assert data["amount_paise"] == 149950  # 1499.50 * 100
        assert Decimal(str(data["amount"])) == Decimal("1499.50")
        assert data["currency"] == "INR"
        assert data["status"] == "created"
        assert data["gateway_order_id"].startswith("order_")
        assert data["key_id"] == "rzp_test_fixture_key"

    # Confirm zero financial ledger postings exist
    with h["factory"]() as session:
        journal_count = session.execute(select(func.count(CommissionJournal.id))).scalar_one()
        assert journal_count == 0, "Order creation must never post entries to financial ledger"


@pytest.mark.anyio
async def test_order_creation_by_agent_with_payment_manage_permission(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['agent_a_token']}",
                "X-CSRF-Token": h["agent_a_csrf"],
            },
        )
        assert res.status_code == 201
        assert res.json()["amount_paise"] == 149950


@pytest.mark.anyio
async def test_order_creation_rejects_already_settled_subscription(payment_harness):
    h = payment_harness
    with h["factory"]() as session:
        sub = session.get(MemberSubscription, h["subscription_id"])
        sub.status = SubscriptionStatus.ACTIVE.value
        sub.payment_status = SubscriptionPaymentStatus.SETTLED.value
        session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_cross_tenant_subscription_order_rejected(payment_harness):
    h = payment_harness
    # Admin B attempts to create order for subscription in Tenant A
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_b_token']}",
                "X-CSRF-Token": h["admin_b_csrf"],
            },
        )
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_idempotent_order_creation_with_key(payment_harness):
    h = payment_harness
    idempotency_key = "idem_order_xyz_12345678"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        res1 = await client.post(
            "/api/v1/payments/orders",
            json={
                "subscription_id": str(h["subscription_id"]),
                "idempotency_key": idempotency_key,
            },
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert res1.status_code == 201
        order1 = res1.json()

        res2 = await client.post(
            "/api/v1/payments/orders",
            json={
                "subscription_id": str(h["subscription_id"]),
                "idempotency_key": idempotency_key,
            },
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert res2.status_code == 201
        order2 = res2.json()

        assert order1["id"] == order2["id"]
        assert order1["gateway_order_id"] == order2["gateway_order_id"]

    with h["factory"]() as session:
        orders_count = session.execute(select(func.count(PaymentOrder.id))).scalar_one()
        assert orders_count == 1


@pytest.mark.anyio
async def test_valid_signature_verification_activates_subscription(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        # 1. Create order
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert order_res.status_code == 201
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_{uuid4().hex[:14]}"

        # 2. Authentic HMAC-SHA256 signature
        secret = h["adapter"].key_secret
        msg = f"{order_id}|{payment_id}"
        valid_sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

        # 3. Verify
        verify_res = await client.post(
            "/api/v1/payments/verify",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": valid_sig,
            },
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert verify_res.status_code == 200, verify_res.text
        vdata = verify_res.json()
        assert vdata["success"] is True
        assert vdata["payment_id"] == payment_id
        assert vdata["order_id"] == order_id
        assert vdata["subscription_status"] == SubscriptionStatus.ACTIVE.value
        assert vdata["payment_status"] == SubscriptionPaymentStatus.SETTLED.value

    # Verify subscription in database
    with h["factory"]() as session:
        sub = session.get(MemberSubscription, h["subscription_id"])
        assert sub.status == SubscriptionStatus.ACTIVE.value
        assert sub.payment_status == SubscriptionPaymentStatus.SETTLED.value

        order = session.execute(
            select(PaymentOrder).where(PaymentOrder.gateway_order_id == order_id)
        ).scalar_one()
        assert order.status == PaymentOrderStatus.PAID.value

        tx = session.execute(
            select(PaymentTransaction).where(PaymentTransaction.gateway_payment_id == payment_id)
        ).scalar_one()
        assert tx.status == PaymentTransactionStatus.CAPTURED.value
        assert tx.amount_paise == 149950


@pytest.mark.anyio
async def test_invalid_signature_rejected(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_{uuid4().hex[:14]}"
        tampered_sig = "0000000000000000000000000000000000000000000000000000000000000000"

        res = await client.post(
            "/api/v1/payments/verify",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": tampered_sig,
            },
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "HTTP_ERROR"

    with h["factory"]() as session:
        sub = session.get(MemberSubscription, h["subscription_id"])
        assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value
        assert sub.payment_status == SubscriptionPaymentStatus.UNPAID.value


@pytest.mark.anyio
async def test_duplicate_payment_verification_is_idempotent(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_idempotent_{uuid4().hex[:10]}"
        secret = h["adapter"].key_secret
        sig = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

        res1 = await client.post(
            "/api/v1/payments/verify",
            json={"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": sig},
            headers={"Cookie": f"community_session={h['admin_a_token']}", "X-CSRF-Token": h["admin_a_csrf"]},
        )
        assert res1.status_code == 200

        res2 = await client.post(
            "/api/v1/payments/verify",
            json={"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": sig},
            headers={"Cookie": f"community_session={h['admin_a_token']}", "X-CSRF-Token": h["admin_a_csrf"]},
        )
        assert res2.status_code == 200
        assert res2.json()["success"] is True

    with h["factory"]() as session:
        tx_count = session.execute(select(func.count(PaymentTransaction.id))).scalar_one()
        assert tx_count == 1


@pytest.mark.anyio
async def test_cross_tenant_payment_verification_rejected(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_{uuid4().hex[:10]}"
        sig = "any_signature"

        # Admin B in Tenant B attempts to verify order from Tenant A
        res = await client.post(
            "/api/v1/payments/verify",
            json={"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": sig},
            headers={"Cookie": f"community_session={h['admin_b_token']}", "X-CSRF-Token": h["admin_b_csrf"]},
        )
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_webhook_valid_signature_payment_captured_activates_subscription(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={
                "Cookie": f"community_session={h['admin_a_token']}",
                "X-CSRF-Token": h["admin_a_csrf"],
            },
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_wh_{uuid4().hex[:10]}"
        event_id = f"evt_{uuid4().hex[:10]}"

        event_payload = {
            "id": event_id,
            "entity": "event",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": 149950,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(event_payload).encode("utf-8")
        wh_secret = h["adapter"].webhook_secret
        sig = hmac.new(wh_secret.encode(), raw_body, hashlib.sha256).hexdigest()

        res = await client.post(
            "/api/v1/payments/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": sig,
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "processed"

    with h["factory"]() as session:
        sub = session.get(MemberSubscription, h["subscription_id"])
        assert sub.status == SubscriptionStatus.ACTIVE.value
        assert sub.payment_status == SubscriptionPaymentStatus.SETTLED.value

        wh_event = session.execute(
            select(PaymentWebhookEvent).where(PaymentWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert wh_event.status == "processed"


@pytest.mark.anyio
async def test_webhook_invalid_signature_rejected(payment_harness):
    h = payment_harness
    event_payload = {"id": "evt_fraud", "event": "payment.captured"}
    raw_body = json.dumps(event_payload).encode("utf-8")
    invalid_sig = "invalid_sig_hex_0000000000000000000000000000000000000000"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        res = await client.post(
            "/api/v1/payments/webhook",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": invalid_sig},
        )
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_webhook_deduplication_and_retry_safety(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={"Cookie": f"community_session={h['admin_a_token']}", "X-CSRF-Token": h["admin_a_csrf"]},
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_dedup_{uuid4().hex[:10]}"
        event_id = f"evt_dedup_{uuid4().hex[:10]}"

        event_payload = {
            "id": event_id,
            "entity": "event",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": 149950,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(event_payload).encode("utf-8")
        wh_secret = h["adapter"].webhook_secret
        sig = hmac.new(wh_secret.encode(), raw_body, hashlib.sha256).hexdigest()

        # First delivery
        res1 = await client.post(
            "/api/v1/payments/webhook",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "processed"

        # Duplicate delivery
        res2 = await client.post(
            "/api/v1/payments/webhook",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "duplicate"


@pytest.mark.anyio
async def test_webhook_payment_failed_does_not_activate_subscription(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={"Cookie": f"community_session={h['admin_a_token']}", "X-CSRF-Token": h["admin_a_csrf"]},
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_fail_{uuid4().hex[:10]}"

        event_payload = {
            "id": f"evt_fail_{uuid4().hex[:10]}",
            "entity": "event",
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": 149950,
                        "currency": "INR",
                        "status": "failed",
                        "error_code": "INSUFFICIENT_FUNDS",
                        "error_description": "Card balance insufficient",
                    }
                }
            },
        }
        raw_body = json.dumps(event_payload).encode("utf-8")
        wh_secret = h["adapter"].webhook_secret
        sig = hmac.new(wh_secret.encode(), raw_body, hashlib.sha256).hexdigest()

        res = await client.post(
            "/api/v1/payments/webhook",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
        assert res.status_code == 200

    with h["factory"]() as session:
        sub = session.get(MemberSubscription, h["subscription_id"])
        assert sub.status == SubscriptionStatus.PENDING_PAYMENT.value
        assert sub.payment_status == SubscriptionPaymentStatus.UNPAID.value

        failed_tx = session.execute(
            select(PaymentTransaction).where(PaymentTransaction.gateway_payment_id == payment_id)
        ).scalar_one()
        assert failed_tx.status == PaymentTransactionStatus.FAILED.value
        assert failed_tx.error_code == "INSUFFICIENT_FUNDS"


@pytest.mark.anyio
async def test_convergence_of_checkout_verify_and_webhook(payment_harness):
    h = payment_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
    ) as client:
        order_res = await client.post(
            "/api/v1/payments/orders",
            json={"subscription_id": str(h["subscription_id"])},
            headers={"Cookie": f"community_session={h['admin_a_token']}", "X-CSRF-Token": h["admin_a_csrf"]},
        )
        order_id = order_res.json()["gateway_order_id"]
        payment_id = f"pay_conv_{uuid4().hex[:10]}"
        secret = h["adapter"].key_secret
        sig = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

        # Step 1: Checkout verification occurs first
        res_checkout = await client.post(
            "/api/v1/payments/verify",
            json={"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": sig},
            headers={"Cookie": f"community_session={h['admin_a_token']}", "X-CSRF-Token": h["admin_a_csrf"]},
        )
        assert res_checkout.status_code == 200

        # Step 2: Webhook arrives later for the same payment
        event_payload = {
            "id": f"evt_conv_{uuid4().hex[:10]}",
            "entity": "event",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": 149950,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(event_payload).encode("utf-8")
        wh_secret = h["adapter"].webhook_secret
        wh_sig = hmac.new(wh_secret.encode(), raw_body, hashlib.sha256).hexdigest()

        res_wh = await client.post(
            "/api/v1/payments/webhook",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": wh_sig},
        )
        assert res_wh.status_code == 200

    # Converged state: single transaction, active subscription
    with h["factory"]() as session:
        tx_count = session.execute(select(func.count(PaymentTransaction.id))).scalar_one()
        assert tx_count == 1
        sub = session.get(MemberSubscription, h["subscription_id"])
        assert sub.status == SubscriptionStatus.ACTIVE.value
        assert sub.payment_status == SubscriptionPaymentStatus.SETTLED.value
