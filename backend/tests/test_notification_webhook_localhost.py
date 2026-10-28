from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from uuid import uuid4

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.messaging.models import NotificationMessage, NotificationWebhookEvent
from app.modules.tenants.models import Tenant

TEST_WEBHOOK_SECRET = "local_dev_webhook_signing_secret_min_32_bytes_ok!"


def compute_signature(timestamp: str, raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    msg = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def localhost_webhook_app(tmp_path):
    db_file = tmp_path / "test_webhook_localhost.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_fk(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    now = datetime.now(UTC)
    tenant_id = uuid4()
    with session_factory() as session:
        t = Tenant(
            id=tenant_id,
            slug="local-community",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(t)
        session.flush()
        msg = NotificationMessage(
            id=uuid4(),
            tenant_id=tenant_id,
            message_id="msg_local_test_001",
            channel="WHATSAPP",
            message_type="LOGIN_OTP",
            recipient="+919876543210",
            status="QUEUED",
            priority="P0",
            purpose="login",
            occurred_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(msg)
        session.commit()

    settings = Settings(
        _env_file=None,
        environment="development",
        database_url=None,
        frontend_origins="http://localhost:3000,http://127.0.0.1:3000,http://localhost:5000,http://127.0.0.1:5000",
        notification_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        notification_webhook_timestamp_tolerance_seconds=300,
        notification_webhook_callback_url="http://127.0.0.1:5000/backend-api/api/v1/webhooks/notifications/status",
        auth_session_cookie_secure=False,
    )

    app = create_app(settings)

    def override_get_db():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db

    return {
        "app": app,
        "session_factory": session_factory,
        "settings": settings,
        "tenant_id": tenant_id,
    }


# =============================================================================
# Test A: Backend Health & Availability
# =============================================================================
@pytest.mark.anyio
async def test_backend_health_endpoint(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.get("/api/v1/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


# =============================================================================
# Test B: Webhook Route Routing & Method Enforcement
# =============================================================================
@pytest.mark.anyio
async def test_webhook_route_method_not_allowed_for_get(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.get("/api/v1/webhooks/notifications/status")
        # FastAPI returns 405 Method Not Allowed for GET on a POST-only route
        assert res.status_code == 405


# =============================================================================
# Test C: Valid Signed Event via Localhost Ingress
# =============================================================================
@pytest.mark.anyio
async def test_valid_signed_event_persisted(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    factory = localhost_webhook_app["session_factory"]
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_local_test_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": event_id,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "processed"
        assert data["event_id"] == event_id
        assert data["message_id"] == "msg_local_test_001"

    with factory() as session:
        # Check event persisted
        evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt.status == "DELIVERED"
        assert evt.processing_status == "processed"

        # Check message status updated
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_local_test_001")
        ).scalar_one()
        assert msg.status == "DELIVERED"


# =============================================================================
# Test D: Negative & Validation Rejection Tests
# =============================================================================
@pytest.mark.anyio
async def test_invalid_signature_rejected(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_local_test_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    bad_sig = compute_signature(ts, raw_body, secret="wrong_secret_12345678901234567890")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": bad_sig,
                "X-Event-ID": event_id,
            },
        )
        assert res.status_code == 401
        assert "Webhook HMAC signature verification failed" in res.text


@pytest.mark.anyio
async def test_stale_timestamp_rejected(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    # 400 seconds in the past (> 300s tolerance)
    old_time = datetime.now(UTC) - timedelta(seconds=400)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_local_test_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": old_time.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(old_time.timestamp()))
    sig = compute_signature(ts, raw_body)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": event_id,
            },
        )
        assert res.status_code == 400
        assert "exceeds allowable tolerance" in res.text


@pytest.mark.anyio
async def test_duplicate_event_id_idempotency(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    factory = localhost_webhook_app["session_factory"]
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_local_test_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        # First post
        r1 = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": event_id,
            },
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "processed"

        # Replayed post (exact same event ID)
        r2 = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": event_id,
            },
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"

    with factory() as session:
        # Exactly one event row recorded
        count = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalars().all()
        assert len(count) == 1


@pytest.mark.anyio
async def test_unsupported_status_rejected(localhost_webhook_app):
    app = localhost_webhook_app["app"]
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_local_test_001",
        "status": "INVALID_STATUS_CODE",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": event_id,
            },
        )
        assert res.status_code == 400
        assert "Unsupported status" in res.text


# =============================================================================
# Test E: Localhost Webhook URL Configuration & Cloudflare Independence
# =============================================================================
def test_localhost_webhook_url_and_cloudflare_independence():
    # In local development, loopback HTTP proxy URL is accepted without Cloudflare
    s_local = Settings(
        _env_file=None,
        environment="development",
        database_url=None,
        frontend_origins="http://localhost:3000,http://127.0.0.1:3000,http://localhost:5000,http://127.0.0.1:5000",
        notification_webhook_callback_url="http://127.0.0.1:5000/backend-api/api/v1/webhooks/notifications/status",
    )
    assert s_local.effective_notification_webhook_callback_url == (
        "http://127.0.0.1:5000/backend-api/api/v1/webhooks/notifications/status"
    )
    assert "trycloudflare.com" not in s_local.effective_notification_webhook_callback_url
    assert "5000" in s_local.frontend_origin_list[2]

