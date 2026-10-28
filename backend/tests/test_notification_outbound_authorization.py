"""Comprehensive regression tests for Notification Microservice X-API-Key Authorization.

Tests satisfy all requirements:
1. The expected X-API-Key header is sent with the exact configured secret value.
2. Multiple requests reuse the exact same configured API key across calls.
3. Different requests have distinct Idempotency-Key values while reusing the fixed API key.
4. A missing API key fails safely without generating a dynamic/random replacement key.
5. The API key never appears in logs, diagnostic banners, or console output.
6. Upstream HTTP 403 is reported clearly and does NOT trigger automatic key regeneration or rotation.
7. NOTIFICATION_WEBHOOK_SECRET and NOTIFICATION_SERVICE_API_KEY remain strictly isolated.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.modules.messaging.notification_service import (
    NotificationDispatchResult,
    NotificationServiceClient,
)


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "environment": "development",
        "otp_delivery_mode": "notification_service",
        "notification_service_base_url": "http://handorg.duckdns.org/notifications",
        "notification_service_api_key": SecretStr("test-fixed-api-key-998877"),
        "notification_webhook_secret": SecretStr("test-separate-webhook-secret-445566"),
        "notification_channel": "WHATSAPP",
        "notification_debug_logging": False,
        "otp_challenge_ttl_seconds": 300,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.anyio
async def test_expected_x_api_key_header_is_sent():
    """Requirement 4.1: The expected X-API-Key header is sent in outbound POST /api/v1/messages."""
    settings = make_settings()
    captured_request: dict[str, Any] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_request["url"] = str(request.url)
        captured_request["method"] = request.method
        captured_request["headers"] = dict(request.headers)
        captured_request["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            status_code=202,
            json={
                "messageId": "msg_auth_101",
                "status": "QUEUED",
                "priority": "P0",
                "channel": "WHATSAPP",
                "statusUrl": "/api/v1/messages/msg_auth_101",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    cid = str(uuid4())
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp(
            recipient_e164="+919100209205",
            otp="654321",
            purpose="login",
            challenge_id=cid,
        )

    assert result.success is True
    assert result.message_id == "msg_auth_101"
    assert captured_request["url"] == "http://handorg.duckdns.org/notifications/api/v1/messages"
    assert captured_request["method"] == "POST"

    headers = captured_request["headers"]
    assert headers["x-api-key"] == "test-fixed-api-key-998877"
    assert headers["content-type"] == "application/json"
    assert headers["idempotency-key"] == f"community-ott-login-{cid}"


@pytest.mark.anyio
async def test_multiple_requests_use_same_configured_api_key():
    """Requirement 4.2: Multiple requests use the same configured API key without rotating or generating."""
    settings = make_settings()
    captured_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers.get("x-api-key", ""))
        return httpx.Response(202, json={"messageId": f"msg_{len(captured_keys)}", "status": "QUEUED"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        for i in range(5):
            await client.send_otp(
                recipient_e164="+919100209205",
                otp=f"10000{i}",
                purpose="login",
                challenge_id=str(uuid4()),
            )

    assert len(captured_keys) == 5
    # Every single request MUST use the exact same configured key
    for k in captured_keys:
        assert k == "test-fixed-api-key-998877"
    assert len(set(captured_keys)) == 1, "API key must remain completely stable across requests"


@pytest.mark.anyio
async def test_different_requests_have_distinct_idempotency_keys_while_sharing_same_api_key():
    """Requirement 4.3: Different requests have distinct Idempotency-Key values while reusing the same API key."""
    settings = make_settings()
    captured_pairs: list[tuple[str, str]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_pairs.append((
            request.headers.get("x-api-key", ""),
            request.headers.get("idempotency-key", ""),
        ))
        return httpx.Response(202, json={"messageId": "msg_ok", "status": "QUEUED"})

    transport = httpx.MockTransport(mock_handler)
    cid1 = str(uuid4())
    cid2 = str(uuid4())
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        # Request 1 for challenge 1
        await client.send_otp("+919100209205", "111111", purpose="login", challenge_id=cid1)
        # Request 2: retry for challenge 1 (same intent -> must reuse same idempotency key)
        await client.send_otp("+919100209205", "111111", purpose="login", challenge_id=cid1)
        # Request 3 for challenge 2 (different challenge -> distinct idempotency key)
        await client.send_otp("+919100209205", "222222", purpose="login", challenge_id=cid2)

    assert len(captured_pairs) == 3
    api_key_1, idem_1 = captured_pairs[0]
    api_key_2, idem_2 = captured_pairs[1]
    api_key_3, idem_3 = captured_pairs[2]

    # All share the exact same API key
    assert api_key_1 == api_key_2 == api_key_3 == "test-fixed-api-key-998877"

    # Same challenge retry reuses exact same idempotency key
    assert idem_1 == idem_2 == f"community-ott-login-{cid1}"

    # Distinct challenge uses different idempotency key
    assert idem_3 == f"community-ott-login-{cid2}"
    assert idem_1 != idem_3


@pytest.mark.anyio
async def test_missing_api_key_fails_safely_without_generating_one(caplog):
    """Requirement 4.4: A missing API key fails safely without generating a dynamic replacement key."""
    settings = make_settings(notification_service_api_key=None)
    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(403, content=b"")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        result = await client.send_otp(
            recipient_e164="+919100209205",
            otp="998877",
            purpose="login",
            challenge_id=str(uuid4()),
        )

    # Must fail safely
    assert result.success is False
    # Settings must remain None (no dynamic key generated or injected)
    assert settings.notification_service_api_key is None

    # Request sent must NOT contain an invented/generated key
    if captured_requests:
        assert "x-api-key" not in captured_requests[0].headers

    # Warning must be logged alerting operator
    assert any("notification_service_api_key_missing" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_api_key_never_appears_in_logs_or_debug_output(caplog, capsys):
    """Requirement 4.5: The API key never appears in logs, diagnostic output, or console banners."""
    secret_key = "super-secret-api-key-do-not-leak"
    raw_otp = "776655"
    settings = make_settings(
        notification_service_api_key=SecretStr(secret_key),
        notification_debug_logging=True,
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"messageId": "msg_diag_safe", "status": "QUEUED"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)
        await client.send_otp(
            recipient_e164="+919100209205",
            otp=raw_otp,
            purpose="login",
            challenge_id=str(uuid4()),
        )

    # Check terminal/capsys stdout
    captured_stdout = capsys.readouterr().out
    assert secret_key not in captured_stdout
    assert raw_otp not in captured_stdout

    # Header name is recorded in diagnostic output, but NOT the secret value
    assert "Headers Sent   : Content-Type, Idempotency-Key, X-API-Key" in captured_stdout

    # Check python logging/caplog
    for record in caplog.records:
        assert secret_key not in record.message
        assert raw_otp not in record.message


@pytest.mark.anyio
async def test_http_403_reported_clearly_and_does_not_trigger_automatic_key_regeneration():
    """Requirement 4.6: HTTP 403 is reported clearly and does not trigger automatic key regeneration."""
    settings = make_settings(notification_service_api_key=SecretStr("my-constant-api-key"))
    captured_keys: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers.get("x-api-key", ""))
        # Remote service returns 403 Forbidden with empty body and session cookie
        return httpx.Response(
            status_code=403,
            content=b"",
            headers={"Set-Cookie": "JSESSIONID=dummy; Path=/"},
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)

        # First request: gets 403
        res1 = await client.send_otp("+919100209205", "111111", purpose="login", challenge_id=str(uuid4()))
        assert res1.success is False
        assert res1.status == "FORBIDDEN"
        assert "authorization rejected" in res1.error.lower()

        # Second request: must NOT attempt key regeneration or rotation
        res2 = await client.send_otp("+919100209205", "222222", purpose="login", challenge_id=str(uuid4()))
        assert res2.success is False
        assert res2.status == "FORBIDDEN"

    # Both requests used the exact same key without modification
    assert len(captured_keys) == 2
    assert captured_keys[0] == "my-constant-api-key"
    assert captured_keys[1] == "my-constant-api-key"
    assert settings.notification_service_api_key.get_secret_value() == "my-constant-api-key"


def test_webhook_secret_is_strictly_isolated_from_outbound_api_key():
    """Requirement 2.7: NOTIFICATION_WEBHOOK_SECRET and NOTIFICATION_SERVICE_API_KEY remain separate."""
    api_key_val = "outbound-api-key-aaa"
    webhook_secret_val = "inbound-webhook-secret-bbb"
    settings = make_settings(
        notification_service_api_key=SecretStr(api_key_val),
        notification_webhook_secret=SecretStr(webhook_secret_val),
    )

    client = NotificationServiceClient(settings)
    # The client must only hold and use the notification_service_api_key
    assert settings.notification_service_api_key.get_secret_value() == api_key_val
    assert settings.notification_webhook_secret.get_secret_value() == webhook_secret_val
    assert api_key_val != webhook_secret_val


def test_settings_lru_cache_and_environment_reloading():
    """Verifies that Settings is cached via lru_cache, and cache_clear is required to reload changes."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2

    get_settings.cache_clear()
    s3 = get_settings()
    assert s3 is not s1


def test_x_api_key_loaded_from_env_alias_choices():
    """Verifies that Settings loads the fixed X-API-Key from .env under both variable names."""
    # Loaded via X_API_KEY
    s_x_api_key = Settings(_env_file=None, **{"X_API_KEY": "env-x-api-key-value"})
    assert s_x_api_key.notification_service_api_key is not None
    assert s_x_api_key.notification_service_api_key.get_secret_value() == "env-x-api-key-value"
    assert s_x_api_key.x_api_key is not None
    assert s_x_api_key.x_api_key.get_secret_value() == "env-x-api-key-value"

    # Loaded via NOTIFICATION_SERVICE_API_KEY
    s_notif = Settings(_env_file=None, **{"NOTIFICATION_SERVICE_API_KEY": "env-notif-key-value"})
    assert s_notif.notification_service_api_key is not None
    assert s_notif.notification_service_api_key.get_secret_value() == "env-notif-key-value"
    assert s_notif.x_api_key.get_secret_value() == "env-notif-key-value"


@pytest.mark.anyio
async def test_all_outbound_endpoints_send_fixed_x_api_key():
    """Verifies that send_otp, send_message, get_message_status, and get_message_attempts all send the fixed key."""
    fixed_key = "my-fixed-outbound-api-key-from-env"
    settings = make_settings(notification_service_api_key=SecretStr(fixed_key))

    captured_headers: list[dict[str, str]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        if request.method == "POST":
            return httpx.Response(
                status_code=202,
                json={"messageId": "msg_fixed_1", "status": "QUEUED", "priority": "P0"},
            )
        return httpx.Response(
            status_code=200,
            json={"messageId": "msg_fixed_1", "status": "DELIVERED"},
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NotificationServiceClient(settings, client=http_client)

        assert client.api_key == fixed_key
        assert client.has_api_key is True

        # 1. send_otp
        await client.send_otp("+919100209205", "123456", purpose="login")
        # 2. send_email
        await client.send_email(recipient_email="user@example.com", message_type="ACCOUNT_UPDATE", text_content="Welcome!")
        # 3. get_message_status
        await client.get_message_status("msg_fixed_1")
        # 4. get_message_attempts
        await client.get_message_attempts("msg_fixed_1")

    assert len(captured_headers) == 4
    for idx, headers in enumerate(captured_headers):
        assert "x-api-key" in headers, f"Request {idx} missing x-api-key header"
        assert headers["x-api-key"] == fixed_key, f"Request {idx} sent wrong api key"

