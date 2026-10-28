from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.otp_service import OtpService
from app.modules.messaging.errors import MessagingProviderUnavailable
from app.modules.messaging.notification_service import (
    NotificationDispatchResult,
    NotificationServiceClient,
)


def make_test_settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "environment": "test",
        "otp_delivery_mode": "notification_service",
        "notification_service_url": "http://localhost:8100",
        "notification_service_api_key": SecretStr("test-notification-api-key"),
        "otp_challenge_ttl_seconds": 300,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.anyio
async def test_notification_request_contract_exact_format():
    """Validates the exact HTTP request format specified in FRONTEND_INTEGRATION.md."""
    settings = make_test_settings()
    captured_request: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_request["url"] = str(request.url)
        captured_request["method"] = request.method
        captured_request["headers"] = dict(request.headers)
        captured_request["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            status_code=202,
            json={
                "messageId": "msg_test_contract_123",
                "status": "QUEUED",
                "priority": "P0",
                "channel": "SMS",
                "statusUrl": "/api/v1/messages/msg_test_contract_123",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=5)
        cid = str(uuid4())

        result = await client.send_otp_sms(
            recipient_e164="+919876543210",
            otp="654321",
            purpose="login",
            challenge_id=cid,
            expires_at=expires_at,
        )

        assert result.success is True
        assert result.message_id == "msg_test_contract_123"
        assert result.status == "QUEUED"
        assert result.status_url == "/api/v1/messages/msg_test_contract_123"

        # Assert URL and Method
        assert captured_request["url"] == "http://localhost:8100/api/v1/messages"
        assert captured_request["method"] == "POST"

        # Assert Headers
        headers = captured_request["headers"]
        assert headers["content-type"] == "application/json"
        assert headers["x-api-key"] == "test-notification-api-key"
        assert headers["idempotency-key"] == f"community-ott-login-{cid}"
        assert len(headers["idempotency-key"]) <= 200

        # Assert JSON Body
        body = captured_request["body"]
        assert body["channel"] == "SMS"
        assert body["messageType"] == "LOGIN_OTP"
        assert body["recipient"] == "+919876543210"
        assert body["content"] == {
            "type": "TEXT",
            "text": "Your login verification code is 654321.",
        }
        assert body["expiresAt"] == expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert body["metadata"] == {
            "purpose": "login",
            "challengeId": cid,
        }
        # Verify NO sensitive fields leaked in metadata
        for forbidden in ["password", "aadhaar", "pan", "otp"]:
            assert forbidden not in body["metadata"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "purpose,expected_text",
    [
        ("login", "Your login verification code is 123456."),
        ("admin_registration", "Your Admin registration verification code is 123456."),
        ("agent_registration", "Your Agent registration verification code is 123456."),
        ("user_registration", "Your User registration verification code is 123456."),
    ],
)
async def test_all_four_otp_workflows_purpose_text(purpose: str, expected_text: str):
    """Verifies that all four OTP workflows use tailored text and correct purpose metadata."""
    settings = make_test_settings()
    captured_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            status_code=202,
            json={"messageId": f"msg_{purpose}_1", "status": "QUEUED"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        cid = str(uuid4())

        result = await client.send_otp_sms(
            recipient_e164="+919811223344",
            otp="123456",
            purpose=purpose,
            challenge_id=cid,
        )

        assert result.success is True
        assert result.message_id == f"msg_{purpose}_1"
        assert captured_body["content"]["text"] == expected_text
        assert captured_body["metadata"]["purpose"] == purpose
        assert captured_body["metadata"]["challengeId"] == cid


@pytest.mark.anyio
async def test_idempotency_key_stability_and_isolation():
    """Verifies that retrying the same notification intent reuses the exact same key,

    while a different challenge produces a distinct key, both bounded to <= 200 chars.
    """
    settings = make_test_settings()
    captured_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers.get("idempotency-key", ""))
        return httpx.Response(status_code=202, json={"messageId": "msg_idem", "status": "QUEUED"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        cid1 = str(uuid4())
        cid2 = str(uuid4())

        # Attempt 1 for cid1
        await client.send_otp_sms("+919876543210", "111111", purpose="login", challenge_id=cid1)
        # Retry for cid1 (simulating same-intent retry)
        await client.send_otp_sms("+919876543210", "111111", purpose="login", challenge_id=cid1)
        # Attempt for cid2 (new intent)
        await client.send_otp_sms("+919876543210", "222222", purpose="login", challenge_id=cid2)

        assert len(captured_keys) == 3
        assert captured_keys[0] == captured_keys[1], "Same intent retry must use identical idempotency key"
        assert captured_keys[0] != captured_keys[2], "Different challenge must use distinct idempotency key"
        for k in captured_keys:
            assert len(k) <= 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status_code,expected_status,expected_error_substr",
    [
        (409, "CONFLICT", "idempotency conflict"),
        (400, "REJECTED", "rejected as invalid"),
        (401, "UNAUTHORIZED", "authorization rejected"),
        (403, "FORBIDDEN", "authorization rejected"),
        (500, "FAILED", "HTTP 500"),
        (503, "FAILED", "HTTP 503"),
    ],
)
async def test_notification_error_status_mapping(
    status_code: int, expected_status: str, expected_error_substr: str
):
    """Verifies clean error mapping without leaking internal provider details."""
    settings = make_test_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, json={"error": "upstream_detail_secret"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp_sms(
            recipient_e164="+919876543210",
            otp="123456",
            purpose="login",
            challenge_id=str(uuid4()),
        )

        assert result.success is False
        assert result.message_id is None
        assert result.status == expected_status
        assert expected_error_substr.lower() in result.error.lower()
        # Verify no upstream detail secret leaked
        assert "upstream_detail_secret" not in result.error


@pytest.mark.anyio
async def test_notification_timeout_handling():
    """Verifies that network timeouts return a safe TIMEOUT status."""
    settings = make_test_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Connection timed out waiting for upstream")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp_sms(
            recipient_e164="+919876543210",
            otp="123456",
            purpose="login",
        )

        assert result.success is False
        assert result.status == "TIMEOUT"
        assert "timed out" in result.error.lower()


@pytest.mark.anyio
async def test_notification_connection_error_handling():
    """Verifies that network connection failures return a safe CONNECTION_ERROR status."""
    settings = make_test_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused on port 8100")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp_sms(
            recipient_e164="+919876543210",
            otp="123456",
            purpose="login",
        )

        assert result.success is False
        assert result.status == "CONNECTION_ERROR"
        assert "unreachable" in result.error.lower()


@pytest.mark.anyio
async def test_notification_malformed_json_response():
    """Verifies that malformed non-JSON responses are handled gracefully."""
    settings = make_test_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=202, content=b"NOT_VALID_JSON")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp_sms(
            recipient_e164="+919876543210",
            otp="123456",
            purpose="login",
        )

        assert result.success is True
        assert result.status == "QUEUED"
        assert result.message_id is None


@pytest.mark.anyio
async def test_status_lookup_and_attempts():
    """Verifies GET /api/v1/messages/{messageId} and /attempts endpoints."""
    settings = make_test_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/attempts"):
            return httpx.Response(
                status_code=200,
                json=[
                    {
                        "attemptNumber": 1,
                        "provider": "twilio",
                        "status": "PROVIDER_ACCEPTED",
                        "errorCode": None,
                    }
                ],
            )
        return httpx.Response(
            status_code=200,
            json={
                "messageId": "msg_status_test_456",
                "channel": "SMS",
                "status": "MOCK_SENT",
                "priority": "P0",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        status = await client.get_message_status("msg_status_test_456")
        assert status is not None
        assert status["messageId"] == "msg_status_test_456"
        assert status["status"] == "MOCK_SENT"

        attempts = await client.get_message_attempts("msg_status_test_456")
        assert len(attempts) == 1
        assert attempts[0]["provider"] == "twilio"
        assert attempts[0]["status"] == "PROVIDER_ACCEPTED"


@pytest.mark.anyio
async def test_otp_service_stores_message_id_and_fails_safely_in_live_mode():
    """Tests OtpService integration with NotificationServiceClient:

    1. Stores notification message_id in challenge.metadata_json when dispatched.
    2. Raises MessagingProviderUnavailable when dispatch fails in notification_service mode.
    """
    from app.db.session import get_session_factory

    settings = make_test_settings(otp_delivery_mode="notification_service")

    # 1. Success dispatch case:
    def success_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=202,
            json={"messageId": "msg_persisted_999", "status": "QUEUED"},
        )

    transport = httpx.MockTransport(success_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        notif_client = NotificationServiceClient(settings, client=http_client)
        session_factory = get_session_factory()
        otp_service = OtpService(settings, notification_client=notif_client, session_factory=session_factory)

        phone1 = f"+9198{uuid4().int % 100000000:08d}"
        cid, _ = await otp_service.request_otp(phone1, purpose="agent_registration")
        assert cid is not None

        # Verify challenge metadata persisted in PostgreSQL
        with session_factory() as session:
            challenge = session.get(OtpChallenge, cid)
            assert challenge is not None
            assert challenge.metadata_json is not None
            assert challenge.metadata_json.get("notification_message_id") == "msg_persisted_999"
            assert challenge.metadata_json.get("notification_status") == "QUEUED"

    # 2. Failure dispatch case:
    def failure_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=503, json={"error": "Down for maintenance"})

    transport_fail = httpx.MockTransport(failure_handler)
    async with httpx.AsyncClient(transport=transport_fail) as http_client_fail:
        notif_client_fail = NotificationServiceClient(settings, client=http_client_fail)
        otp_service_fail = OtpService(settings, notification_client=notif_client_fail, session_factory=session_factory)

        phone2 = f"+9198{uuid4().int % 100000000:08d}"
        with pytest.raises(MessagingProviderUnavailable):
            await otp_service_fail.request_otp(phone2, purpose="agent_registration")


def test_notification_base_url_configuration_and_trailing_slash_handling():
    """Confirms Settings and NotificationServiceClient construct the exact documented endpoint."""
    # 1. Custom URL without trailing slash
    settings_custom = make_test_settings(notification_service_base_url="http://notification-service:8100")
    client_custom = NotificationServiceClient(settings_custom)
    assert client_custom.base_url == "http://notification-service:8100"
    assert client_custom.messages_endpoint_url == "http://notification-service:8100/api/v1/messages"
    assert client_custom.message_status_url("msg_123") == "http://notification-service:8100/api/v1/messages/msg_123"
    assert client_custom.message_attempts_url("msg_123") == "http://notification-service:8100/api/v1/messages/msg_123/attempts"

    # 2. Custom URL with trailing slash is properly normalized
    settings_slash = make_test_settings(notification_service_base_url="http://notification-service:8100/")
    client_slash = NotificationServiceClient(settings_slash)
    assert client_slash.base_url == "http://notification-service:8100"
    assert client_slash.messages_endpoint_url == "http://notification-service:8100/api/v1/messages"


def test_notification_service_base_url_validation():
    """Confirms invalid URL schemes, malformed URLs, or URLs with credentials are rejected."""
    with pytest.raises(ValueError, match="NOTIFICATION_SERVICE_BASE_URL must be a valid HTTP"):
        Settings(_env_file=None, notification_service_base_url="ftp://invalid-scheme")

    with pytest.raises(ValueError, match="NOTIFICATION_SERVICE_BASE_URL must be a valid HTTP"):
        Settings(_env_file=None, notification_service_base_url="http://user:pass@localhost:8100")


def test_production_fails_safely_when_notification_service_url_is_insecure_or_missing():
    """Ensures production fail-closed validation when otp_delivery_mode is notification_service."""
    prod_common = {
        "environment": "production",
        "database_url": "postgresql+psycopg://app:secret@db.example:5432/app",
        "frontend_origins": "https://portal.example",
        "messaging_api_base_url": "https://gateway.example",
        "messaging_send_otp_path": "/send",
        "messaging_send_otp_method": "POST",
        "messaging_verify_otp_path": "/verify",
        "messaging_verify_otp_method": "POST",
        "otp_challenge_ttl_seconds": 300,
        "auth_session_cookie_secure": True,
        "otp_delivery_mode": "notification_service",
        "notification_service_api_key": SecretStr("prod-secret-key"),
        "notification_webhook_secret": SecretStr("prod-webhook-secret-key-32chars"),
    }
    # Missing NOTIFICATION_SERVICE_BASE_URL in production
    with pytest.raises(ValueError, match="NOTIFICATION_SERVICE_BASE_URL is required in production"):
        Settings(_env_file=None, **prod_common, notification_service_base_url=None)

    # Insecure (HTTP) NOTIFICATION_SERVICE_BASE_URL in production
    with pytest.raises(ValueError, match="Production and staging notification service endpoints must use HTTPS"):
        Settings(_env_file=None, **prod_common, notification_service_base_url="http://insecure-host:8100")

    # Missing NOTIFICATION_SERVICE_API_KEY in production when live delivery is enabled
    with pytest.raises(ValueError, match="NOTIFICATION_SERVICE_API_KEY is required"):
        Settings(
            _env_file=None,
            **{k: v for k, v in prod_common.items() if k != "notification_service_api_key"},
            notification_service_base_url="https://notifications.internal:8100",
            notification_service_api_key=None,
        )

    # Secure HTTPS NOTIFICATION_SERVICE_BASE_URL in production with API key succeeds
    valid_prod = Settings(
        _env_file=None,
        **prod_common,
        notification_service_base_url="https://notifications.internal:8100",
    )
    assert valid_prod.notification_base_url == "https://notifications.internal:8100"
    assert NotificationServiceClient(valid_prod).messages_endpoint_url == "https://notifications.internal:8100/api/v1/messages"


@pytest.mark.anyio
async def test_notification_request_uses_configured_base_url_with_mock_client():
    """Tests that send_otp_sms uses the exact configured root URL with a mock HTTP client."""
    settings = make_test_settings(
        notification_service_base_url="http://custom-notification-host:9000",
        otp_delivery_mode="notification_service",
    )
    called_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_urls.append(str(request.url))
        return httpx.Response(
            status_code=202,
            json={"messageId": "msg_custom_url_test", "status": "QUEUED"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        res = await client.send_otp_sms(
            recipient_e164="+919876543210",
            otp="112233",
            purpose="login",
        )
        assert res.success is True
        assert len(called_urls) == 1
        assert called_urls[0] == "http://custom-notification-host:9000/api/v1/messages"


@pytest.mark.anyio
async def test_whatsapp_template_payload_and_headers():
    """Verify that default WHATSAPP notification dispatch generates exact template schema and required headers."""
    import json
    settings = make_test_settings(
        notification_service_base_url="https://notifications.internal",
        notification_service_api_key="secret-api-key-test",
        notification_channel="WHATSAPP",
        otp_delivery_mode="notification_service",
    )
    captured_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": body,
        })
        return httpx.Response(
            status_code=202,
            json={"messageId": "msg_wa_test_01", "status": "QUEUED", "statusUrl": "/api/v1/messages/msg_wa_test_01"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp(
            recipient_e164="+919100209205",
            otp="654321",
            purpose="login",
            challenge_id="ch_unique_ref_99",
        )
        assert result.success is True
        assert result.message_id == "msg_wa_test_01"
        assert result.status == "QUEUED"
        assert result.channel == "WHATSAPP"

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req["url"] == "https://notifications.internal/api/v1/messages"
        assert req["headers"]["x-api-key"] == "secret-api-key-test"
        assert req["headers"]["idempotency-key"] == "community-ott-login-ch_unique_ref_99"
        assert req["headers"]["content-type"] == "application/json"

        body = req["body"]
        assert body["channel"] == "WHATSAPP"
        assert body["messageType"] == "LOGIN_OTP"
        assert body["recipient"] == "+919100209205"
        assert body["content"] == {
            "type": "TEMPLATE",
            "templateCode": "LOGIN_OTP",
            "locale": "en",
            "variables": {
                "otp": "654321",
            },
        }
        assert body["metadata"]["referenceId"] == "ch_unique_ref_99"


@pytest.mark.anyio
async def test_send_otp_mock_mode_whatsapp_template():
    """Verify that send_otp in mock mode correctly stores template content and sets channel=WHATSAPP."""
    settings = make_test_settings(
        notification_channel="WHATSAPP",
        otp_delivery_mode="mock",
    )
    client = NotificationServiceClient(settings)
    result = await client.send_otp(
        recipient_e164="+919100209205",
        otp="987654",
        purpose="login",
        challenge_id="ch_mock_123",
    )
    assert result.success is True
    assert result.status == "MOCK_SENT"
    assert result.channel == "WHATSAPP"
    assert len(client.mock_messages) == 1
    mock_msg = client.mock_messages[0]
    assert mock_msg["channel"] == "WHATSAPP"
    assert mock_msg["messageType"] == "LOGIN_OTP"
    assert mock_msg["recipient"] == "+919100209205"
    assert mock_msg["content"]["type"] == "TEMPLATE"
    assert mock_msg["content"]["templateCode"] == "LOGIN_OTP"
    assert mock_msg["content"]["variables"]["otp"] == "987654"
    assert mock_msg["metadata"]["referenceId"] == "ch_mock_123"


@pytest.mark.anyio
async def test_send_otp_sms_channel_explicit():
    """Verify that explicit SMS channel or send_otp_sms generates text payload."""
    import json
    settings = make_test_settings(
        notification_service_base_url="https://notifications.internal",
        notification_service_api_key="secret-api-key-test",
        otp_delivery_mode="notification_service",
    )
    captured_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(body)
        return httpx.Response(status_code=202, json={"messageId": "msg_sms_01", "status": "QUEUED"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp(
            recipient_e164="+919876543210",
            otp="123456",
            purpose="login",
            challenge_id="ch_sms_456",
            channel="SMS",
        )
        assert result.success is True
        assert result.channel == "SMS"
        assert len(captured_requests) == 1
        body = captured_requests[0]
        assert body["channel"] == "SMS"
        assert body["content"]["type"] == "TEXT"
        assert "123456" in body["content"]["text"]
        assert body["metadata"]["challengeId"] == "ch_sms_456"


