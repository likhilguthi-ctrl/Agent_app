from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from unittest.mock import MagicMock
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
from app.modules.messaging.webhook_service import NotificationWebhookService
from app.modules.tenants.models import Tenant

TEST_WEBHOOK_SECRET = "test_signing_secret_min_32_characters_for_hmac!"


def compute_signature(timestamp: str, raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    msg = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def webhook_test_app(tmp_path):
    db_file = tmp_path / "test_webhook_tunnel.sqlite"
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
            slug="test-community",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(t)
        session.flush()
        # Pre-seed a NotificationMessage in QUEUED status
        msg = NotificationMessage(
            id=uuid4(),
            tenant_id=tenant_id,
            message_id="msg_synthetic_001",
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
        environment="test",
        database_url=None,
        notification_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        notification_webhook_timestamp_tolerance_seconds=300,
        notification_webhook_callback_url="https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status",
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
        "tenant_id": tenant_id,
        "settings": settings,
    }


# =============================================================================
# 1. Valid Signed Webhook Event (Accepted, Verified & Persisted)
# =============================================================================
@pytest.mark.anyio
async def test_valid_signed_webhook_event_accepted_and_persisted(webhook_test_app):
    app = webhook_test_app["app"]
    factory = webhook_test_app["session_factory"]
    now = datetime.now(UTC)

    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_synthetic_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    headers = {
        "Content-Type": "application/json",
        "X-Notification-Timestamp": ts,
        "X-Notification-Signature": sig,
        "X-Event-ID": event_id,
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "processed"
    assert data["event_id"] == event_id
    assert data["message_id"] == "msg_synthetic_001"

    # Verify PostgreSQL / DB persistence
    with factory() as session:
        # 1. NotificationMessage updated to DELIVERED
        msg = session.execute(
            select(NotificationMessage).where(NotificationMessage.message_id == "msg_synthetic_001")
        ).scalar_one()
        assert msg.status == "DELIVERED"

        # 2. NotificationWebhookEvent recorded with processing_status=processed
        evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one()
        assert evt.status == "DELIVERED"
        assert evt.channel == "WHATSAPP"
        assert evt.processing_status == "processed"


# =============================================================================
# 2. Missing or Invalid Signature Rejected
# =============================================================================
@pytest.mark.anyio
async def test_missing_or_invalid_signature(webhook_test_app):
    app = webhook_test_app["app"]
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_synthetic_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # A. Missing signature header
        res_no_sig = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Event-ID": event_id,
            },
        )
        assert res_no_sig.status_code == 400
        assert "X-Notification-Signature header is required" in res_no_sig.text

        # B. Invalid signature prefix (not sha256=)
        res_fmt = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": "md5=1234567890abcdef",
                "X-Event-ID": event_id,
            },
        )
        assert res_fmt.status_code == 401
        assert "Signature format must be sha256=" in res_fmt.text

        # C. Tampered signature / wrong key
        bad_sig = compute_signature(ts, raw_body, secret="wrong_secret_key_1234567890123456")
        res_bad = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": bad_sig,
                "X-Event-ID": event_id,
            },
        )
        assert res_bad.status_code == 401
        assert "Webhook HMAC signature verification failed" in res_bad.text


# =============================================================================
# 3. Expired Timestamp Rejected
# =============================================================================
@pytest.mark.anyio
async def test_expired_timestamp(webhook_test_app):
    app = webhook_test_app["app"]
    event_id = str(uuid4())
    # 350 seconds in the past (> 300s tolerance)
    old_time = datetime.now(UTC) - timedelta(seconds=350)
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_synthetic_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": old_time.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(old_time.timestamp()))
    sig = compute_signature(ts, raw_body)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
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


# =============================================================================
# 4. Duplicate Event ID Idempotency
# =============================================================================
@pytest.mark.anyio
async def test_duplicate_event_id(webhook_test_app):
    app = webhook_test_app["app"]
    factory = webhook_test_app["session_factory"]
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_synthetic_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    headers = {
        "Content-Type": "application/json",
        "X-Notification-Timestamp": ts,
        "X-Notification-Signature": sig,
        "X-Event-ID": event_id,
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # First delivery: processed
        res1 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
        assert res1.status_code == 200
        assert res1.json()["status"] == "processed"

        # Duplicate delivery: duplicate
        res2 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
        assert res2.status_code == 200
        assert res2.json()["status"] == "duplicate"

    # Only 1 event record in database
    with factory() as session:
        evts = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalars().all()
        assert len(evts) == 1


# =============================================================================
# 5. Database Failure & 500 Rollback (Sender Retries)
# =============================================================================
def test_database_failure_and_retry_behavior(webhook_test_app):
    settings = webhook_test_app["settings"]
    service = NotificationWebhookService(settings)
    now = datetime.now(UTC)
    event_id = str(uuid4())
    payload = {
        "eventId": event_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_synthetic_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    mock_session = MagicMock(spec=Session)
    mock_session.execute.return_value.scalar_one_or_none.return_value = None
    mock_session.commit.side_effect = RuntimeError("PostgreSQL connection lost / disk full")

    with pytest.raises(Exception) as exc_info:
        service.process_webhook(
            session=mock_session,
            raw_body=raw_body,
            timestamp_header=ts,
            signature_header=sig,
            event_id_header=event_id,
        )

    assert exc_info.value.status_code == 500
    assert "DATABASE_ERROR" in str(exc_info.value.detail)
    mock_session.rollback.assert_called()


# =============================================================================
# 6. Invalid Event Payload (Malformed JSON, Missing Fields, Bad Status)
# =============================================================================
@pytest.mark.anyio
async def test_invalid_event_payload(webhook_test_app):
    app = webhook_test_app["app"]
    now = datetime.now(UTC)
    ts = str(int(now.timestamp()))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # A. Malformed JSON
        bad_json = b"not-valid-json{"
        sig_bad_json = compute_signature(ts, bad_json)
        res_json = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=bad_json,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig_bad_json,
                "X-Event-ID": "evt_1",
            },
        )
        assert res_json.status_code == 400
        assert "Malformed JSON payload" in res_json.text

        # B. Missing eventId in payload
        payload_no_id = {"eventType": "message.status_changed", "messageId": "msg_1", "status": "DELIVERED"}
        body_no_id = json.dumps(payload_no_id).encode("utf-8")
        sig_no_id = compute_signature(ts, body_no_id)
        res_no_id = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=body_no_id,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig_no_id,
                "X-Event-ID": "evt_1",
            },
        )
        assert res_no_id.status_code == 400
        assert "eventId field is required in payload" in res_no_id.text

        # C. Mismatched X-Event-ID header and payload eventId
        payload_mismatch = {
            "eventId": "event_payload_id",
            "eventType": "message.status_changed",
            "serviceId": "community-ott",
            "messageId": "msg_synthetic_001",
            "status": "DELIVERED",
            "channel": "WHATSAPP",
        }
        body_mismatch = json.dumps(payload_mismatch).encode("utf-8")
        sig_mismatch = compute_signature(ts, body_mismatch)
        res_mismatch = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=body_mismatch,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig_mismatch,
                "X-Event-ID": "event_header_id_different",
            },
        )
        assert res_mismatch.status_code == 400
        assert "does not match payload eventId" in res_mismatch.text

        # D. Unsupported status
        payload_bad_status = {
            "eventId": "evt_bad_status",
            "eventType": "message.status_changed",
            "serviceId": "community-ott",
            "messageId": "msg_synthetic_001",
            "status": "NONEXISTENT_STATUS",
            "channel": "WHATSAPP",
        }
        body_bad_status = json.dumps(payload_bad_status).encode("utf-8")
        sig_bad_status = compute_signature(ts, body_bad_status)
        res_bad_status = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=body_bad_status,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig_bad_status,
                "X-Event-ID": "evt_bad_status",
            },
        )
        assert res_bad_status.status_code == 400
        assert "Unsupported status" in res_bad_status.text


# =============================================================================
# 7. Monotonic Terminal Status Progression
# =============================================================================
@pytest.mark.anyio
async def test_monotonic_status_progression(webhook_test_app):
    app = webhook_test_app["app"]
    factory = webhook_test_app["session_factory"]
    now = datetime.now(UTC)

    # 1. Transition QUEUED -> DELIVERED
    evt_1 = str(uuid4())
    p1 = {
        "eventId": evt_1,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_synthetic_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    b1 = json.dumps(p1).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig1 = compute_signature(ts, b1)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=b1,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig1,
                "X-Event-ID": evt_1,
            },
        )
        assert r1.status_code == 200

        with factory() as session:
            msg = session.execute(
                select(NotificationMessage).where(NotificationMessage.message_id == "msg_synthetic_001")
            ).scalar_one()
            assert msg.status == "DELIVERED"

        # 2. Out-of-order QUEUED event cannot regress DELIVERED
        evt_2 = str(uuid4())
        p2 = {
            "eventId": evt_2,
            "eventType": "message.status_changed",
            "serviceId": "community-ott",
            "messageId": "msg_synthetic_001",
            "status": "QUEUED",
            "channel": "WHATSAPP",
            "occurredAt": (now - timedelta(seconds=10)).isoformat(),
        }
        b2 = json.dumps(p2).encode("utf-8")
        sig2 = compute_signature(ts, b2)
        r2 = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=b2,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig2,
                "X-Event-ID": evt_2,
            },
        )
        assert r2.status_code == 200

        with factory() as session:
            msg = session.execute(
                select(NotificationMessage).where(NotificationMessage.message_id == "msg_synthetic_001")
            ).scalar_one()
            # Status MUST stay DELIVERED, not regressed to QUEUED!
            assert msg.status == "DELIVERED"

        # 3. Forward progression DELIVERED -> READ is permitted
        evt_3 = str(uuid4())
        p3 = {
            "eventId": evt_3,
            "eventType": "message.status_changed",
            "serviceId": "community-ott",
            "messageId": "msg_synthetic_001",
            "status": "READ",
            "channel": "WHATSAPP",
            "occurredAt": (now + timedelta(seconds=5)).isoformat(),
        }
        b3 = json.dumps(p3).encode("utf-8")
        sig3 = compute_signature(ts, b3)
        r3 = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=b3,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig3,
                "X-Event-ID": evt_3,
            },
        )
        assert r3.status_code == 200

        with factory() as session:
            msg = session.execute(
                select(NotificationMessage).where(NotificationMessage.message_id == "msg_synthetic_001")
            ).scalar_one()
            assert msg.status == "READ"


# =============================================================================
# 8. Webhook URL Configuration & Fail-Closed Validation
# =============================================================================
def test_webhook_url_configuration_and_production_validation():
    prod_db = "postgresql+psycopg://postgres:postgres@localhost:5432/community_ott"
    # A. Configurable callback URL property
    s_dev = Settings(
        _env_file=None,
        environment="development",
        notification_webhook_callback_url="https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status",
    )
    assert s_dev.effective_notification_webhook_callback_url == "https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status"

    # B. In production, non-HTTPS callback URL must fail
    with pytest.raises(ValueError, match="NOTIFICATION_WEBHOOK_CALLBACK_URL must use HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            database_url=prod_db,
            frontend_origins="https://app.avanflix.com",
            notification_service_base_url="https://notification.example.com",
            notification_service_api_key=SecretStr("key"),
            notification_webhook_secret=SecretStr("secret-1234567890123456"),
            notification_webhook_callback_url="http://insecure-tunnel.example.com/api/v1/webhooks/notifications/status",
            auth_session_cookie_secure=True,
            otp_challenge_ttl_seconds=300,
            messaging_api_base_url="https://msg.example.com",
            messaging_send_otp_path="/send",
            messaging_verify_otp_path="/verify",
            messaging_send_otp_method="POST",
            messaging_verify_otp_method="POST",
            messaging_api_key=SecretStr("msg-key"),
            messaging_auth_header_name="X-API-Key",
        )
