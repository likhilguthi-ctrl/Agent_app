from __future__ import annotations

import json
from dataclasses import asdict

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.modules.messaging.contracts import (
    EncodedMessagingRequest,
    MessagingOperation,
)
from app.modules.messaging.errors import (
    MessagingConfigurationError,
    MessagingContractError,
    MessagingContractUnavailable,
    MessagingProviderRejected,
    MessagingProviderUnavailable,
)
from app.modules.messaging.factory import create_messaging_provider
from app.modules.messaging.http_provider import HttpMessagingProvider
from app.modules.messaging.service import MessagingService, normalize_mobile_e164
from app.modules.messaging.testing import MockMessagingProvider


class TestJsonCodec:
    """Test-only wire mapping; it is not the pending MSG91 contract."""

    def encode(self, operation, command):
        return EncodedMessagingRequest(
            body=json.dumps(
                {"operation": operation.value, "command": asdict(command)},
                separators=(",", ":"),
            ).encode(),
            headers={"Content-Type": "application/test+json"},
        )

    def decode(self, operation, status_code, headers, body):
        parsed = json.loads(body)
        return parsed["accepted"]


def configured_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        messaging_api_base_url="https://gateway.example.test",
        messaging_send_otp_path="/fake/otp/send",
        messaging_send_otp_method="POST",
        messaging_verify_otp_path="/fake/otp/verify",
        messaging_verify_otp_method="POST",
        messaging_send_whatsapp_path="/fake/whatsapp/send",
        messaging_send_whatsapp_method="POST",
        **overrides,
    )


def test_provider_settings_accept_unset_contract_in_development() -> None:
    settings = Settings(_env_file=None)
    assert settings.messaging_provider == "http"
    assert settings.messaging_api_base_url is None
    assert settings.messaging_send_otp_path is None


@pytest.mark.parametrize(
    "settings_values",
    [
        {"messaging_api_base_url": "ftp://gateway.test"},
        {"messaging_api_base_url": "https://user:password@gateway.test"},
        {"messaging_send_otp_path": "https://other.test/send"},
        {"messaging_verify_otp_path": "//other.test/verify"},
        {"messaging_send_whatsapp_path": "/send?redirect=other"},
        {"messaging_send_otp_method": "GET"},
        {"messaging_connect_timeout_seconds": 0},
        {"messaging_read_timeout_seconds": 121},
        {"messaging_api_key": "secret", "messaging_auth_header_name": "Host"},
        {"messaging_api_key": "secret"},
    ],
)
def test_messaging_settings_validate_urls_paths_auth_and_timeouts(settings_values) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **settings_values)


def test_production_requires_http_provider_and_complete_otp_transport_configuration() -> None:
    production = {
        "environment": "production",
        "database_url": "postgresql+psycopg://app:good-secret@db.example:5432/app",
        "frontend_origins": "https://portal.example",
    }
    with pytest.raises(ValidationError, match="MESSAGING_API_BASE_URL is required"):
        Settings(_env_file=None, **production)

    with pytest.raises(ValidationError, match="MESSAGING_PROVIDER=mock"):
        Settings(_env_file=None, **production, messaging_provider="mock")

    with pytest.raises(ValidationError, match="OTP endpoint paths and methods"):
        Settings(
            _env_file=None,
            **production,
            messaging_api_base_url="https://gateway.example",
        )


@pytest.mark.anyio
async def test_mock_provider_is_restricted_to_test_environment_and_rejects_unconfigured_codes() -> None:
    with pytest.raises(ValidationError, match="only in automated test"):
        Settings(_env_file=None, environment="development", messaging_provider="mock")

    test_settings = Settings(_env_file=None, environment="test", messaging_provider="mock")
    provider = create_messaging_provider(test_settings)
    assert not await provider.verify_otp("+14155550123", "anything")
    await provider.aclose()
    with pytest.raises(RuntimeError, match="only in test configuration"):
        MockMessagingProvider(environment="production")


@pytest.mark.anyio
async def test_http_provider_uses_configurable_paths_headers_and_test_codec() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accepted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = configured_settings(
        messaging_api_key=SecretStr("server-secret"),
        messaging_auth_header_name="X-Configured-Credential",
        messaging_auth_value_prefix="Key ",
    )
    provider = HttpMessagingProvider(settings, codec=TestJsonCodec(), client=client)
    try:
        service = MessagingService(provider)
        assert await service.send_otp("+1 (415) 555-0123")
    finally:
        await client.aclose()

    assert seen[0].url == "https://gateway.example.test/fake/otp/send"
    assert seen[0].headers["x-configured-credential"] == "Key server-secret"
    assert json.loads(seen[0].content)["command"]["mobile_e164"] == "+14155550123"


@pytest.mark.anyio
async def test_http_provider_fails_clearly_for_missing_endpoint_or_unfinished_contract() -> None:
    provider_without_endpoint = HttpMessagingProvider(Settings(_env_file=None), codec=TestJsonCodec())
    with pytest.raises(MessagingConfigurationError, match="BASE_URL"):
        await provider_without_endpoint.send_otp("+14155550123")
    await provider_without_endpoint.aclose()

    provider_without_codec = HttpMessagingProvider(configured_settings())
    with pytest.raises(MessagingContractUnavailable, match="mapper is not configured"):
        await provider_without_codec.send_otp("+14155550123")
    await provider_without_codec.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_http_provider_maps_timeout_and_connection_failures_without_details(failure) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise failure("private URL and credentials should not appear")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpMessagingProvider(configured_settings(), codec=TestJsonCodec(), client=client)
    try:
        with pytest.raises(MessagingProviderUnavailable) as error:
            await provider.send_otp("+14155550123")
        assert "private URL" not in str(error.value)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_http_provider_maps_non_success_status_without_echoing_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="private auth token leaked")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpMessagingProvider(configured_settings(), codec=TestJsonCodec(), client=client)
    try:
        with pytest.raises(MessagingProviderRejected) as error:
            await provider.send_otp("+14155550123")
        assert "private auth token" not in str(error.value)
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("body", [b"not-json", b'{"accepted":"yes"}', b"{}"])
async def test_http_provider_rejects_malformed_or_unexpected_contract_response(body) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpMessagingProvider(configured_settings(), codec=TestJsonCodec(), client=client)
    try:
        with pytest.raises(MessagingContractError):
            await provider.verify_otp("+14155550123", "test-code")
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_whatsapp_operation_uses_independent_configured_adapter_operation() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accepted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpMessagingProvider(configured_settings(), codec=TestJsonCodec(), client=client)
    try:
        service = MessagingService(provider)
        accepted = await service.send_whatsapp("+14155550123", {"test-only-payload": "opaque"})
    finally:
        await client.aclose()
    assert accepted
    assert str(seen[0].url).endswith("/fake/whatsapp/send")
    assert json.loads(seen[0].content)["operation"] == MessagingOperation.SEND_WHATSAPP.value


@pytest.mark.parametrize(
    "value",
    ["4155550123", "+014155550123", "+1 415 555 0123 999 999"],
)
def test_mobile_numbers_require_and_normalize_e164_country_code(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_mobile_e164(value)


@pytest.mark.parametrize("value", ["+14155550123", "+44 20 7946 0958"])
def test_mobile_numbers_normalize_to_e164(value: str) -> None:
    assert normalize_mobile_e164(value) == value.replace(" ", "")


@pytest.mark.anyio
async def test_mock_provider_accepts_only_explicit_test_otp_and_does_not_return_it() -> None:
    provider = MockMessagingProvider({"+14155550123": "test-code"}, environment="test")
    assert await provider.send_otp("+14155550123")
    assert not await provider.verify_otp("+14155550123", "other-code")
    assert await provider.verify_otp("+14155550123", "test-code")
    assert provider.sent_otp_requests == ["+14155550123"]
    assert "test-code" not in repr(provider.sent_otp_requests)
