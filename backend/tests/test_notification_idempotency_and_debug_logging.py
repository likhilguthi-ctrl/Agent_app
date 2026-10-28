"""Tests for Notification Idempotency-Key and Development Debug Logging.

Covers:
- Stable idempotency key generation across retries
- Distinct idempotency keys for different challenges/purposes
- Idempotency key maximum length <= 200 characters
- Rejection of NOTIFICATION_DEBUG_LOGGING in production, staging, and test
- Sanitized outbound notification request and response logging in development
- Accurate error classification (ConnectTimeout, ReadTimeout, DNS_FAILURE, TLS_FAILURE, CONNECTION_REFUSED)
- Sanitized incoming webhook request and response logging
- Verification that no API keys, webhook secrets, raw signatures, or raw OTPs leak in stdout
- Verification that logging does not interfere with HMAC verification or PostgreSQL persistence
"""
from __future__ import annotations

import json
import re
import socket
import ssl
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
from httpx import ASGITransport, AsyncClient
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.modules.authentication.models import OtpChallenge
from app.modules.messaging.api import notification_webhooks_router
from app.modules.messaging.models import NotificationMessage, NotificationWebhookEvent
from app.modules.messaging.notification_service import (
    NotificationServiceClient,
    classify_connection_error,
    mask_recipient,
)
from app.modules.messaging.webhook_service import NotificationWebhookService


@pytest.fixture
def sqlite_factory(tmp_path):
    db_file = tmp_path / "test_notif_debug.sqlite"
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
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Section 1: Idempotency-Key Generation & Stability
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_idempotency_key_format_stability_and_isolation():
    """Outgoing requests must generate a stable key: community-ott-<purpose>-<challenge_id>."""
    captured_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers.get("idempotency-key", ""))
        return httpx.Response(202, json={"messageId": "msg_idem_1", "status": "QUEUED"})

    transport = httpx.MockTransport(mock_handler)
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_delivery_mode="notification_service",
        notification_service_base_url="https://notifications.internal",
        notification_service_api_key=SecretStr("test-key"),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        cid1 = str(uuid4())
        cid2 = str(uuid4())

        # Attempt 1 for cid1
        await client.send_otp("+919100209205", "123456", purpose="login", challenge_id=cid1)
        # Attempt 2 for cid1 (retry of same logical challenge)
        await client.send_otp("+919100209205", "123456", purpose="login", challenge_id=cid1)
        # Attempt 3 for cid2 (genuinely distinct challenge)
        await client.send_otp("+919100209205", "654321", purpose="login", challenge_id=cid2)

    assert len(captured_keys) == 3

    # Retries of the same logical message must reuse identical key
    assert captured_keys[0] == captured_keys[1]
    assert captured_keys[0] == f"community-ott-login-{cid1}"

    # Distinct challenges must produce different keys
    assert captured_keys[0] != captured_keys[2]
    assert captured_keys[2] == f"community-ott-login-{cid2}"

    # Keys must be <= 200 characters
    for key in captured_keys:
        assert len(key) <= 200


@pytest.mark.anyio
async def test_idempotency_key_max_length_boundary():
    """Verify idempotency key enforces maximum length <= 200 characters."""
    captured_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers.get("idempotency-key", ""))
        return httpx.Response(202, json={"messageId": "msg_len", "status": "QUEUED"})

    transport = httpx.MockTransport(mock_handler)
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_delivery_mode="notification_service",
        notification_service_base_url="https://notifications.internal",
    )

    very_long_cid = "a" * 250
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        await client.send_otp("+919100209205", "123456", purpose="login", challenge_id=very_long_cid)

    assert len(captured_keys) == 1
    assert len(captured_keys[0]) == 200
    assert captured_keys[0].startswith("community-ott-login-")


# ---------------------------------------------------------------------------
# Section 2 & 4: Configuration Gate & Rejection in Production/Staging
# ---------------------------------------------------------------------------

def test_notification_debug_logging_disabled_by_default():
    settings = Settings(_env_file=None, environment="development")
    assert settings.notification_debug_logging is False
    assert settings.can_debug_log_notifications is False


def test_notification_debug_logging_allowed_in_development():
    settings = Settings(_env_file=None, environment="development", notification_debug_logging=True)
    assert settings.notification_debug_logging is True
    assert settings.can_debug_log_notifications is True


def test_notification_debug_logging_rejected_in_production():
    with pytest.raises(ValidationError, match="NOTIFICATION_DEBUG_LOGGING is not permitted in production or staging"):
        Settings(
            _env_file=None,
            environment="production",
            notification_debug_logging=True,
            database_url="postgresql+psycopg://app:sec123@localhost:5432/community_ott_prod",
            frontend_origins="https://portal.example.com",
        )


def test_notification_debug_logging_rejected_in_staging():
    with pytest.raises(ValidationError, match="NOTIFICATION_DEBUG_LOGGING is not permitted in production or staging"):
        Settings(
            _env_file=None,
            environment="staging",
            notification_debug_logging=True,
            database_url="postgresql+psycopg://app:sec123@localhost:5432/community_ott_staging",
            frontend_origins="https://portal.example.com",
        )


def test_notification_debug_logging_rejected_in_test():
    with pytest.raises(ValidationError, match="NOTIFICATION_DEBUG_LOGGING is allowed only in development"):
        Settings(
            _env_file=None,
            environment="test",
            notification_debug_logging=True,
        )


# ---------------------------------------------------------------------------
# Section 3: Outbound Notification Debug Logging & Error Classification
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_outbound_debug_logging_success_and_sanitization(capsys):
    """Outbound logging prints method, url, channel, idempotency key, masked recipient, and no secrets."""
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_delivery_mode="notification_service",
        notification_service_base_url="https://handorg.duckdns.org/notifications",
        notification_service_api_key=SecretStr("super-secret-api-key-999"),
        notification_debug_logging=True,
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"messageId": "msg_success_123", "status": "QUEUED"})

    transport = httpx.MockTransport(mock_handler)
    cid = str(uuid4())
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        res = await client.send_otp(
            recipient_e164="+919100209205",
            otp="987654",
            purpose="login",
            challenge_id=cid,
        )

    assert res.success is True
    assert res.message_id == "msg_success_123"

    captured = capsys.readouterr().out
    assert "[DEV DEBUG] Outbound Notification Request" in captured
    assert "Endpoint       : POST https://handorg.duckdns.org/notifications/api/v1/messages" in captured
    assert "Channel / Type : WHATSAPP / LOGIN_OTP" in captured
    assert f"Idempotency-Key: community-ott-login-{cid}" in captured
    assert "Recipient      : +91 91002 ****05" in captured
    assert "HTTP 202 QUEUED" in captured
    assert "Queued by notification service; delivery unconfirmed until webhook receipt" in captured
    assert 'Response Body  : {"messageId": "msg_success_123", "status": "QUEUED"}' in captured

    assert "Headers Sent   : Content-Type, Idempotency-Key, X-API-Key" in captured
    assert "Payload Fields :" in captured
    assert "templateCode" in captured

    # Critical Security Invariants: No secrets leaked in terminal stdout
    assert "super-secret-api-key-999" not in captured
    assert "987654" not in captured  # Raw OTP must never appear in outbound request debug log!


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exception_to_raise,expected_error_category",
    [
        (httpx.ConnectTimeout("Connect timeout to host"), "CONNECT_TIMEOUT"),
        (httpx.ReadTimeout("Read timeout from host"), "READ_TIMEOUT"),
        (httpx.ConnectError("Connection refused", request=MagicMock()), "CONNECTION_REFUSED"),
        (httpx.ConnectError("Failed to resolve host name or service not known", request=MagicMock()), "DNS_FAILURE"),
        (httpx.ConnectError("SSL certificate verify failed", request=MagicMock()), "TLS_FAILURE"),
    ],
)
async def test_outbound_debug_logging_error_categories(capsys, exception_to_raise, expected_error_category):
    """Accurately distinguishes ConnectTimeout, ReadTimeout, Connection Refused, DNS, and TLS errors."""
    settings = Settings(
        _env_file=None,
        environment="development",
        otp_delivery_mode="notification_service",
        notification_service_base_url="https://handorg.duckdns.org/notifications",
        notification_debug_logging=True,
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise exception_to_raise

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        res = await client.send_otp(
            recipient_e164="+919100209205",
            otp="123456",
            purpose="login",
            challenge_id=str(uuid4()),
        )

    assert res.success is False
    captured = capsys.readouterr().out
    assert "[DEV DEBUG] Outbound Notification Request" in captured
    assert f"ERROR - {expected_error_category}" in captured


# ---------------------------------------------------------------------------
# Section 3: Incoming Webhook Debug Logging
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_incoming_webhook_debug_logging_success(sqlite_factory, capsys):
    """Incoming webhook debug logging shows event ID, status, valid signature, processing result, and response."""
    secret = "test-webhook-secret-32-bytes-minimum!!"
    settings = Settings(
        _env_file=None,
        environment="development",
        notification_webhook_secret=SecretStr(secret),
        notification_debug_logging=True,
        frontend_origins="http://localhost:3000",
    )

    app = create_app(settings)
    app.state.test_factory = sqlite_factory

    def override_get_db():
        db = sqlite_factory()
        try:
            yield db
        finally:
            db.close()

    from app.db.session import get_db
    app.dependency_overrides[get_db] = override_get_db

    # Seed notification message
    with sqlite_factory.begin() as session:
        session.add(
            NotificationMessage(
                message_id="msg_wh_debug_01",
                channel="WHATSAPP",
                message_type="LOGIN_OTP",
                recipient="+919100209205",
                status="QUEUED",
                priority="P0",
                occurred_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    timestamp = str(int(datetime.now(UTC).timestamp()))
    payload_dict = {
        "eventId": "evt_debug_wh_100",
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_wh_debug_01",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
    }
    raw_body = json.dumps(payload_dict).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + raw_body, hashlib.sha256).hexdigest()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Host": "localhost:3000", "X-Forwarded-Host": "localhost:3000"},
    ) as client:
        resp = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": timestamp,
                "X-Notification-Signature": f"sha256={sig}",
                "X-Event-ID": "evt_debug_wh_100",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"

    captured = capsys.readouterr().out
    assert "[DEV DEBUG] Incoming Notification Webhook" in captured
    assert "Event ID       : evt_debug_wh_100" in captured
    assert "Event Status   : DELIVERED" in captured
    assert "Signature Check: VALID (HMAC-SHA256 verified)" in captured
    assert "Processing     : processed" in captured
    assert "HTTP Response  : 200" in captured
    assert 'Response Body  : {"status": "processed", "event_id": "evt_debug_wh_100", "message_id": "msg_wh_debug_01"' in captured

    # Critical Security Invariants: No raw signatures or secrets in stdout
    assert f"sha256={sig}" not in captured
    assert secret not in captured


@pytest.mark.anyio
async def test_incoming_webhook_debug_logging_invalid_signature_and_duplicate(sqlite_factory, capsys):
    """Incoming webhook debug logging handles invalid signatures and duplicate events accurately."""
    secret = "test-webhook-secret-32-bytes-minimum!!"
    settings = Settings(
        _env_file=None,
        environment="development",
        notification_webhook_secret=SecretStr(secret),
        notification_debug_logging=True,
    )

    app = create_app(settings)
    app.state.test_factory = sqlite_factory

    def override_get_db():
        db = sqlite_factory()
        try:
            yield db
        finally:
            db.close()

    from app.db.session import get_db
    app.dependency_overrides[get_db] = override_get_db

    timestamp = str(int(datetime.now(UTC).timestamp()))
    payload_dict = {
        "eventId": "evt_debug_bad_sig",
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
    }
    raw_body = json.dumps(payload_dict).encode("utf-8")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
    ) as client:
        # Invalid signature attempt
        resp_bad = await client.post(
            "/api/v1/webhooks/notifications/status",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": timestamp,
                "X-Notification-Signature": "sha256=tampered00000000000000000000000000000000000000000000000000000000",
                "X-Event-ID": "evt_debug_bad_sig",
            },
        )
        assert resp_bad.status_code == 401

        captured_bad = capsys.readouterr().out
        assert "[DEV DEBUG] Incoming Notification Webhook" in captured_bad
        assert "Signature Check: FAILED (INVALID_SIGNATURE)" in captured_bad
        assert "Processing     : rejected" in captured_bad
        assert "HTTP Response  : 401" in captured_bad
        assert "tampered" not in captured_bad
