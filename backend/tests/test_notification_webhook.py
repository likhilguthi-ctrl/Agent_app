from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession, OtpChallenge
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.models import Role, RolePermission, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import PlatformOperator, PlatformOperatorSession
from app.modules.messaging.models import NotificationMessage, NotificationWebhookEvent
from app.modules.messaging.webhook_service import (
    NotificationWebhookService,
)
from app.modules.tenants.models import Tenant

TEST_WEBHOOK_SECRET = "test_webhook_signing_secret_super_secure_32b"


def compute_signature(timestamp: str, raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    msg = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def webhook_harness(tmp_path):
    database_path = tmp_path / "notif-webhook-test.sqlite"
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
    admin_a_token = "admin_a_session_token_webhook"
    admin_a_digest = hashlib.sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = hashlib.sha256(b"admin_a_csrf_webhook").hexdigest()
    admin_a_user_id = uuid4()
    admin_a_mem_id = uuid4()

    # Tenant B Admin (for tenant isolation tests)
    admin_b_token = "admin_b_session_token_webhook"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf_webhook").hexdigest()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()

    # Platform Super Admin
    super_admin_token = "super_admin_session_token_webhook"
    super_admin_digest = hashlib.sha256(super_admin_token.encode("utf-8")).hexdigest()
    super_admin_csrf = hashlib.sha256(b"super_admin_csrf_webhook").hexdigest()
    super_admin_user_id = uuid4()
    super_admin_mem_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Create Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="tenant-notif-a",
            status="active",
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="tenant-notif-b",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        seed_tenant_role_permissions(session, tenant_a_id)
        seed_tenant_role_permissions(session, tenant_b_id)
        session.flush()

        admin_role_a = session.execute(
            select(Role).where(Role.tenant_id == tenant_a_id, Role.code == "admin")
        ).scalar_one()
        admin_role_b = session.execute(
            select(Role).where(Role.tenant_id == tenant_b_id, Role.code == "admin")
        ).scalar_one()

        # Admin A
        user_a = User(id=admin_a_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_a)
        session.flush()
        mem_a = TenantMembership(
            id=admin_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a_user_id,
            mobile_e164="+919811111111",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_a)
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

        # Admin B
        user_b = User(id=admin_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_b)
        session.flush()
        mem_b = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919822222222",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_b)
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

        # Super Admin / Platform Operator
        super_op = PlatformOperator(
            id=super_admin_user_id,
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin",
            status="active",
        )
        session.add(super_op)
        session.flush()
        session.add(
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_admin_user_id,
                token_digest=super_admin_digest,
                csrf_token=super_admin_csrf,
                authenticated_at=now,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Create sample Notification Messages
        msg_a = NotificationMessage(
            id=uuid4(),
            tenant_id=tenant_a_id,
            message_id="msg_test_tenant_a_123",
            channel="SMS",
            message_type="LOGIN_OTP",
            recipient="+919811111111",
            status="QUEUED",
            priority="P0",
            purpose="login",
            occurred_at=now - timedelta(seconds=10),
            created_at=now - timedelta(seconds=10),
            updated_at=now - timedelta(seconds=10),
        )
        msg_b = NotificationMessage(
            id=uuid4(),
            tenant_id=tenant_b_id,
            message_id="msg_test_tenant_b_456",
            channel="SMS",
            message_type="LOGIN_OTP",
            recipient="+919822222222",
            status="QUEUED",
            priority="P0",
            purpose="login",
            occurred_at=now - timedelta(seconds=10),
            created_at=now - timedelta(seconds=10),
            updated_at=now - timedelta(seconds=10),
        )
        session.add_all([msg_a, msg_b])
        session.commit()

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        notification_service_base_url="https://handorg.duckdns.org/notifications",
        notification_service_api_key=SecretStr("test-api-key"),
        notification_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        notification_webhook_timestamp_tolerance_seconds=300,
        notification_service_id="community-ott",
        otp_delivery_mode="mock",
    )
    app = create_app(settings=settings)

    def override_get_db():
        with factory() as s:
            yield s

    def override_auth_repo():
        return SQLAlchemyAuthenticationRepository(factory)

    def override_authz_repo():
        return SQLAlchemyAuthorizationRepository(factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_authentication_repository] = override_auth_repo
    app.dependency_overrides[get_authorization_repository] = override_authz_repo
    app.dependency_overrides[get_settings] = lambda: settings

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    yield {
        "app": app,
        "client": client,
        "factory": factory,
        "settings": settings,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "admin_a_token": admin_a_token,
        "admin_b_token": admin_b_token,
        "super_admin_token": super_admin_token,
    }


@pytest.mark.anyio
async def test_scenario_01_valid_signed_webhook_accepted(webhook_harness):
    """1. Valid signed webhook accepted and status updated."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
        "providerMessageId": "provider-tx-999",
        "occurredAt": now_str,
        "failure": None,
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": signature,
            "X-Event-ID": event_id,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "processed"
    assert data["event_id"] == event_id
    assert data["message_id"] == "msg_test_tenant_a_123"

    with factory() as session:
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg.status == "DELIVERED"
        assert msg.provider_message_id == "provider-tx-999"

        evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt.processing_status == "processed"
        assert evt.status == "DELIVERED"


@pytest.mark.anyio
async def test_scenario_02_missing_signature_rejected(webhook_harness):
    """2. Missing signature rejected."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_body = json.dumps({"eventId": "evt_1", "eventType": "message.status_changed"}).encode("utf-8")

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_03_invalid_signature_rejected(webhook_harness):
    """3. Invalid signature rejected."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_body = json.dumps({"eventId": "evt_1", "eventType": "message.status_changed"}).encode("utf-8")
    bad_sig = "sha256=" + "0" * 64

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": bad_sig,
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_04_missing_webhook_secret_fails_closed(webhook_harness):
    """4. Missing webhook secret fails closed."""
    no_secret_settings = webhook_harness["settings"].model_copy(
        update={"notification_webhook_secret": None}
    )
    svc = NotificationWebhookService(no_secret_settings)
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with pytest.raises(Exception) as exc_info:
        svc.verify_signature(b"{}", now_str, "sha256=123456")
    assert "WEBHOOK_SECRET_UNCONFIGURED" in str(exc_info.value)


@pytest.mark.anyio
async def test_scenario_05_modified_body_with_original_signature_rejected(webhook_harness):
    """5. Modified body with the original signature rejected."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    original_body = json.dumps({"eventId": "evt_tamper_1", "status": "DELIVERED"}).encode("utf-8")
    signature = compute_signature(now_str, original_body)

    # Attacker tampers body to FAILED
    tampered_body = json.dumps({"eventId": "evt_tamper_1", "status": "FAILED"}).encode("utf-8")

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=tampered_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": signature,
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_06_stale_timestamp_rejected(webhook_harness):
    """6. Stale timestamp rejected."""
    client: httpx.AsyncClient = webhook_harness["client"]
    # 15 minutes in the past (tolerance is 300s = 5m)
    stale_dt = datetime.now(UTC) - timedelta(minutes=15)
    stale_str = stale_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_body = json.dumps({"eventId": "evt_stale_1", "status": "DELIVERED"}).encode("utf-8")
    signature = compute_signature(stale_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": stale_str,
            "X-Notification-Signature": signature,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_07_invalid_payload_and_unsupported_status_rejected(webhook_harness):
    """7. Invalid payload and unsupported status rejected."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Invalid eventType
    body1 = json.dumps({
        "eventId": "evt_inv_1",
        "eventType": "unknown.event",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }).encode("utf-8")
    sig1 = compute_signature(now_str, body1)
    resp1 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=body1,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig1,
            "X-Event-ID": "evt_inv_1",
        },
    )
    assert resp1.status_code == 400
    assert resp1.json()["error"]["code"] == "HTTP_ERROR"

    # Unsupported status
    body2 = json.dumps({
        "eventId": "evt_inv_2",
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "UNSUPPORTED_STATUS_VALUE",
        "channel": "SMS",
    }).encode("utf-8")
    sig2 = compute_signature(now_str, body2)
    resp2 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=body2,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig2,
            "X-Event-ID": "evt_inv_2",
        },
    )
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_08_valid_duplicate_event_does_not_duplicate_persistence(webhook_harness):
    """8. Valid duplicate event does not duplicate persistence or updates."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_dup_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = compute_signature(now_str, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Notification-Timestamp": now_str,
        "X-Notification-Signature": signature,
        "X-Event-ID": event_id,
    }

    # First attempt -> processed
    resp1 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "processed"

    # Second attempt -> duplicate
    resp2 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"
    assert resp2.json()["event_id"] == event_id

    # Verify exactly 1 record in database
    with factory() as session:
        records = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalars().all()
        assert len(records) == 1


@pytest.mark.anyio
async def test_scenario_09_concurrent_duplicate_event_handling(webhook_harness):
    """9. Concurrent duplicate event handling respects database uniqueness constraint."""
    factory: sessionmaker = webhook_harness["factory"]
    settings: Settings = webhook_harness["settings"]

    event_id = f"evt_race_{uuid4().hex}"
    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = compute_signature(now_str, raw_body)

    svc = NotificationWebhookService(settings)

    # Process first in session 1
    with factory() as s1:
        res1 = svc.process_webhook(
            session=s1,
            raw_body=raw_body,
            timestamp_header=now_str,
            signature_header=signature,
            event_id_header=event_id,
        )
        assert res1.status == "processed"

    # Process duplicate in session 2
    with factory() as s2:
        res2 = svc.process_webhook(
            session=s2,
            raw_body=raw_body,
            timestamp_header=now_str,
            signature_header=signature,
            event_id_header=event_id,
        )
        assert res2.status == "duplicate"


@pytest.mark.anyio
async def test_scenario_10_unknown_message_id_cannot_update_another_tenant_data(webhook_harness):
    """10. Unknown message ID is quarantined and cannot update another tenant's data."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_unk_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_completely_foreign_unknown",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": signature,
            "X-Event-ID": event_id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"

    with factory() as session:
        # Check event persisted as received pending correlation without tenant assignment (DEF-01)
        evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt.processing_status == "received"
        assert evt.tenant_id is None

        # Check existing messages are intact
        msg_a = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg_a.status == "QUEUED"


@pytest.mark.anyio
async def test_scenario_11_valid_delivery_status_updates_correct_notification(webhook_harness):
    """11. Valid delivery status updates the correct notification only."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_upd_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": signature,
            "X-Event-ID": event_id,
        },
    )
    assert resp.status_code == 200

    with factory() as session:
        msg_a = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg_a.status == "DELIVERED"

        msg_b = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_b_456")
        ).scalar_one()
        assert msg_b.status == "QUEUED"  # Untouched


@pytest.mark.anyio
async def test_scenario_12_provider_accepted_status_is_not_treated_as_delivered(webhook_harness):
    """12. Provider-accepted status is not treated as delivered."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_prov_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "PROVIDER_ACCEPTED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": signature,
            "X-Event-ID": event_id,
        },
    )
    assert resp.status_code == 200

    with factory() as session:
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg.status == "PROVIDER_ACCEPTED"
        assert msg.status != "DELIVERED"


@pytest.mark.anyio
async def test_scenario_13_out_of_order_events_cannot_regress_terminal_states(webhook_harness):
    """13. Out-of-order events cannot regress terminal states."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    # First: advance to DELIVERED
    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    evt_del = f"evt_term_{uuid4().hex}"
    body_del = json.dumps({
        "eventId": evt_del,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }).encode("utf-8")
    sig_del = compute_signature(now_str, body_del)
    r1 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=body_del,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig_del,
            "X-Event-ID": evt_del,
        },
    )
    assert r1.status_code == 200

    with factory() as session:
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg.status == "DELIVERED"

    # Out-of-order event arrives: PROVIDER_ACCEPTED (must NOT regress to PROVIDER_ACCEPTED)
    evt_ooo = f"evt_ooo_{uuid4().hex}"
    body_ooo = json.dumps({
        "eventId": evt_ooo,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "PROVIDER_ACCEPTED",
        "channel": "SMS",
    }).encode("utf-8")
    sig_ooo = compute_signature(now_str, body_ooo)
    r2 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=body_ooo,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig_ooo,
            "X-Event-ID": evt_ooo,
        },
    )
    assert r2.status_code == 200

    with factory() as session:
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg.status == "DELIVERED"  # Still DELIVERED!

    # Forward progression to READ is permitted
    evt_read = f"evt_read_{uuid4().hex}"
    body_read = json.dumps({
        "eventId": evt_read,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "READ",
        "channel": "SMS",
    }).encode("utf-8")
    sig_read = compute_signature(now_str, body_read)
    r3 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=body_read,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig_read,
            "X-Event-ID": evt_read,
        },
    )
    assert r3.status_code == 200

    with factory() as session:
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_test_tenant_a_123")
        ).scalar_one()
        assert msg.status == "READ"


@pytest.mark.anyio
async def test_scenario_14_frontend_status_endpoint_authorization(webhook_harness):
    """14. Frontend status endpoint enforces authentication and tenant authorization."""
    client: httpx.AsyncClient = webhook_harness["client"]
    admin_a_token = webhook_harness["admin_a_token"]
    admin_b_token = webhook_harness["admin_b_token"]
    super_admin_token = webhook_harness["super_admin_token"]

    # 1. Unauthenticated -> 401
    resp_unauth = await client.get("/api/v1/notifications/status/msg_test_tenant_a_123")
    assert resp_unauth.status_code == 401

    # 2. Tenant B Admin accessing Tenant A message -> 404 NOT FOUND (no cross-tenant leakage)
    resp_cross = await client.get(
        "/api/v1/notifications/status/msg_test_tenant_a_123",
        cookies={"community_session": admin_b_token},
    )
    assert resp_cross.status_code == 404
    assert resp_cross.json()["error"]["code"] == "HTTP_ERROR"

    # 3. Tenant A Admin accessing Tenant A message -> 200 OK with details
    resp_ok = await client.get(
        "/api/v1/notifications/status/msg_test_tenant_a_123",
        cookies={"community_session": admin_a_token},
    )
    assert resp_ok.status_code == 200
    data = resp_ok.json()
    assert data["messageId"] == "msg_test_tenant_a_123"
    assert data["channel"] == "SMS"
    assert data["status"] in {"QUEUED", "DELIVERED", "READ", "PROVIDER_ACCEPTED"}

    # 4. Super Admin accessing Tenant A message -> 200 OK
    resp_super = await client.get(
        "/api/v1/notifications/status/msg_test_tenant_a_123",
        cookies={"community_session": super_admin_token},
    )
    assert resp_super.status_code == 200
    assert resp_super.json()["messageId"] == "msg_test_tenant_a_123"


@pytest.mark.anyio
async def test_scenario_15_matching_event_id_accepted(webhook_harness):
    """15. DEF-02: Matching X-Event-ID header and payload eventId accepted."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_match_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig,
            "X-Event-ID": event_id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    assert resp.json()["event_id"] == event_id


@pytest.mark.anyio
async def test_scenario_16_missing_event_id_header_rejected(webhook_harness):
    """16. DEF-02: Missing X-Event-ID header is rejected with HTTP 400 MISSING_EVENT_ID."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_no_header_{uuid4().hex}"
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig,
            # X-Event-ID omitted
        },
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_17_missing_payload_event_id_rejected(webhook_harness):
    """17. DEF-02: Missing payload eventId is rejected with HTTP 400 MISSING_EVENT_ID."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = f"evt_no_body_{uuid4().hex}"
    payload = {
        # eventId omitted from body
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig,
            "X-Event-ID": event_id,
        },
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_18_mismatched_event_id_rejected(webhook_harness):
    """18. DEF-02: Mismatched X-Event-ID header and payload eventId rejected with HTTP 400 EVENT_ID_MISMATCH."""
    client: httpx.AsyncClient = webhook_harness["client"]
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header_event_id = f"evt_hdr_{uuid4().hex}"
    body_event_id = f"evt_body_{uuid4().hex}"
    payload = {
        "eventId": body_event_id,
        "eventType": "message.status_changed",
        "messageId": "msg_test_tenant_a_123",
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(now_str, raw_body)

    resp = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig,
            "X-Event-ID": header_event_id,
        },
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "HTTP_ERROR"


@pytest.mark.anyio
async def test_scenario_19_early_webhook_arrival_and_reconciliation(webhook_harness):
    """19. DEF-01: Early webhook arrives before outbound message is committed, then reconciled."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]
    admin_a_token = webhook_harness["admin_a_token"]
    tenant_a_id = webhook_harness["tenant_a_id"]

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    early_msg_id = f"msg_early_{uuid4().hex}"
    event_id = f"evt_early_{uuid4().hex}"

    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": early_msg_id,
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
        "providerMessageId": "provider_tx_early_123",
        "occurredAt": now_str,
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(now_str, raw_body)

    # 1. Early arrival before NotificationMessage exists in DB
    resp_wh = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": now_str,
            "X-Notification-Signature": sig,
            "X-Event-ID": event_id,
        },
    )
    assert resp_wh.status_code == 200
    wh_data = resp_wh.json()
    assert wh_data["status"] == "received"
    assert wh_data["event_id"] == event_id

    # Verify event stored as received with tenant_id=None
    with factory() as session:
        evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt.processing_status == "received"
        assert evt.tenant_id is None

    # 2. Platform outbound call finishes and records NotificationMessage in QUEUED status
    with factory() as session:
        msg = NotificationMessage(
            id=uuid4(),
            tenant_id=tenant_a_id,
            message_id=early_msg_id,
            channel="SMS",
            message_type="LOGIN_OTP",
            recipient="+919811111111",
            status="QUEUED",
            priority="P0",
            purpose="login",
            occurred_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(msg)
        session.commit()

    # 3. Status check endpoint queries message and triggers reconciliation
    resp_status = await client.get(
        f"/api/v1/notifications/status/{early_msg_id}",
        cookies={"community_session": admin_a_token},
    )
    assert resp_status.status_code == 200
    status_data = resp_status.json()
    assert status_data["messageId"] == early_msg_id
    assert status_data["status"] == "DELIVERED"
    assert status_data["providerMessageId"] == "provider_tx_early_123"

    # Verify event updated to processed with tenant ownership
    with factory() as session:
        evt_after = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt_after.processing_status == "processed"
        assert evt_after.tenant_id == tenant_a_id


@pytest.mark.anyio
async def test_scenario_20_duplicate_early_webhook_arrivals_handled(webhook_harness):
    """20. DEF-01: Duplicate early webhook arrivals handled cleanly (received then duplicate)."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]

    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    early_msg_id = f"msg_early_dup_{uuid4().hex}"
    event_id = f"evt_early_dup_{uuid4().hex}"

    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "messageId": early_msg_id,
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(now_str, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Notification-Timestamp": now_str,
        "X-Notification-Signature": sig,
        "X-Event-ID": event_id,
    }

    # 1. First early attempt -> status "received"
    resp1 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "received"

    # 2. Duplicate early attempt -> status "duplicate"
    resp2 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"

    with factory() as session:
        events = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.anyio
async def test_scenario_21_chronological_reconciliation_multiple_early_events(webhook_harness):
    """21. DEF-01: Multiple early events reconciled in strict chronological order."""
    client: httpx.AsyncClient = webhook_harness["client"]
    factory: sessionmaker = webhook_harness["factory"]
    admin_a_token = webhook_harness["admin_a_token"]
    tenant_a_id = webhook_harness["tenant_a_id"]

    early_msg_id = f"msg_early_seq_{uuid4().hex}"
    t1 = datetime.now(UTC) - timedelta(seconds=20)
    t2 = datetime.now(UTC) - timedelta(seconds=10)
    t1_str = t1.strftime("%Y-%m-%dT%H:%M:%SZ")
    t2_str = t2.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Early Event 1: PROVIDER_ACCEPTED at t1
    evt1_id = f"evt_seq_1_{uuid4().hex}"
    payload1 = {
        "eventId": evt1_id,
        "eventType": "message.status_changed",
        "messageId": early_msg_id,
        "serviceId": "community-ott",
        "status": "PROVIDER_ACCEPTED",
        "channel": "SMS",
        "providerMessageId": "prov_tx_111",
        "occurredAt": t1_str,
    }
    raw1 = json.dumps(payload1).encode("utf-8")
    sig1 = compute_signature(t1_str, raw1)
    r1 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw1,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": t1_str,
            "X-Notification-Signature": sig1,
            "X-Event-ID": evt1_id,
        },
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "received"

    # Early Event 2: DELIVERED at t2
    evt2_id = f"evt_seq_2_{uuid4().hex}"
    payload2 = {
        "eventId": evt2_id,
        "eventType": "message.status_changed",
        "messageId": early_msg_id,
        "serviceId": "community-ott",
        "status": "DELIVERED",
        "channel": "SMS",
        "providerMessageId": "prov_tx_222",
        "occurredAt": t2_str,
    }
    raw2 = json.dumps(payload2).encode("utf-8")
    sig2 = compute_signature(t2_str, raw2)
    r2 = await client.post(
        "/api/v1/webhooks/notifications/status",
        content=raw2,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": t2_str,
            "X-Notification-Signature": sig2,
            "X-Event-ID": evt2_id,
        },
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "received"

    # Outbound creates NotificationMessage in QUEUED status
    with factory() as session:
        msg = NotificationMessage(
            id=uuid4(),
            tenant_id=tenant_a_id,
            message_id=early_msg_id,
            channel="SMS",
            message_type="LOGIN_OTP",
            recipient="+919811111111",
            status="QUEUED",
            priority="P0",
            purpose="login",
            occurred_at=t1 - timedelta(seconds=5),
            created_at=t1 - timedelta(seconds=5),
            updated_at=t1 - timedelta(seconds=5),
        )
        session.add(msg)
        session.commit()

    # Admin queries status endpoint -> triggers chronological reconciliation
    resp = await client.get(
        f"/api/v1/notifications/status/{early_msg_id}",
        cookies={"community_session": admin_a_token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "DELIVERED"
    assert data["providerMessageId"] == "prov_tx_222"

    with factory() as session:
        e1 = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == evt1_id)
        ).scalar_one()
        e2 = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == evt2_id)
        ).scalar_one()
        assert e1.processing_status == "processed"
        assert e2.processing_status == "processed"
        assert e1.tenant_id == tenant_a_id
        assert e2.tenant_id == tenant_a_id


@pytest.mark.anyio
async def test_scenario_22_otp_service_reconciliation_integration(webhook_harness):
    """22. DEF-01: OtpService automatically reconciles early webhooks upon completing request_otp."""
    from unittest.mock import MagicMock
    from app.modules.authentication.otp_service import OtpService
    from app.modules.messaging.notification_service import NotificationDispatchResult, NotificationServiceClient

    factory: sessionmaker = webhook_harness["factory"]
    settings: Settings = webhook_harness["settings"]
    tenant_a_id = webhook_harness["tenant_a_id"]

    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    race_msg_id = f"msg_otp_race_{uuid4().hex}"
    event_id = f"evt_otp_race_{uuid4().hex}"

    # 1. Simulate early webhook received and recorded as pending in DB
    with factory() as session:
        early_event = NotificationWebhookEvent(
            event_id=event_id,
            event_type="message.status_changed",
            service_id="community-ott",
            message_id=race_msg_id,
            tenant_id=None,
            status="DELIVERED",
            channel="SMS",
            provider_message_id="mock_prov_carrier_888",
            payload={"eventId": event_id, "messageId": race_msg_id, "status": "DELIVERED"},
            processing_status="received",
            error_message="Pending outbound message correlation reconciliation.",
            occurred_at=now,
            created_at=now,
            processed_at=None,
        )
        session.add(early_event)
        session.commit()

    # 2. Mock notification client to return the same race_msg_id as if external service returned it
    from unittest.mock import AsyncMock
    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp_sms = AsyncMock(
        return_value=NotificationDispatchResult(
            success=True,
            message_id=race_msg_id,
            status="QUEUED",
        )
    )

    otp_settings = settings.model_copy(
        update={
            "otp_challenge_ttl_seconds": 300,
            "otp_cooldown_seconds": 60,
            "otp_max_sends_per_window": 5,
            "otp_window_seconds": 900,
            "development_static_otp_enabled": False,
        }
    )
    otp_service = OtpService(
        settings=otp_settings,
        notification_client=mock_client,
        session_factory=factory,
    )

    # 3. Execute request_otp
    challenge_id, _ = await otp_service.request_otp(
        mobile_number="+919811111111",
        tenant_id=tenant_a_id,
        purpose="login",
    )

    # 4. Verify that request_otp automatically reconciled the early webhook
    with factory() as session:
        # NotificationMessage status was updated from QUEUED to DELIVERED
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == race_msg_id)
        ).scalar_one()
        assert msg.status == "DELIVERED"
        assert msg.provider_message_id == "mock_prov_carrier_888"
        assert msg.tenant_id == tenant_a_id

        # OtpChallenge metadata also updated
        ch = session.execute(
            select(OtpChallenge).where(OtpChallenge.id == challenge_id)
        ).scalar_one()
        assert ch.metadata_json is not None
        assert ch.metadata_json.get("notification_status") == "DELIVERED"
        assert ch.metadata_json.get("notification_message_id") == race_msg_id

        # Webhook event was marked processed and linked to tenant
        evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt.processing_status == "processed"
        assert evt.tenant_id == tenant_a_id
        assert evt.processed_at is not None
