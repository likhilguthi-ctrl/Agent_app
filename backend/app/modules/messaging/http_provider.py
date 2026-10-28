from __future__ import annotations

from typing import Mapping
from urllib.parse import urljoin

import httpx

from app.core.config import Settings
from app.modules.messaging.contracts import (
    EncodedMessagingRequest,
    MessagingContractCodec,
    MessagingOperation,
    SendOtpCommand,
    SendWhatsAppCommand,
    VerifyOtpCommand,
)
from app.modules.messaging.errors import (
    MessagingConfigurationError,
    MessagingContractError,
    MessagingContractUnavailable,
    MessagingProviderRejected,
    MessagingProviderUnavailable,
)


_RESERVED_CODEC_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "transfer-encoding",
}


class HttpMessagingProvider:
    """HTTP transport with contract-specific mapping supplied through one codec."""

    def __init__(
        self,
        settings: Settings,
        codec: MessagingContractCodec | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._codec = codec
        self._owns_client = client is None
        self._timeout = httpx.Timeout(
            connect=settings.messaging_connect_timeout_seconds,
            read=settings.messaging_read_timeout_seconds,
            write=settings.messaging_read_timeout_seconds,
            pool=settings.messaging_connect_timeout_seconds,
        )
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
        )

    async def send_otp(self, mobile_e164: str) -> bool:
        return await self._execute(
            MessagingOperation.SEND_OTP,
            SendOtpCommand(mobile_e164=mobile_e164),
        )

    async def verify_otp(self, mobile_e164: str, otp: str) -> bool:
        return await self._execute(
            MessagingOperation.VERIFY_OTP,
            VerifyOtpCommand(mobile_e164=mobile_e164, otp=otp),
        )

    async def send_whatsapp(self, command: SendWhatsAppCommand) -> bool:
        return await self._execute(MessagingOperation.SEND_WHATSAPP, command)

    async def _execute(
        self,
        operation: MessagingOperation,
        command: SendOtpCommand | VerifyOtpCommand | SendWhatsAppCommand,
    ) -> bool:
        base_url = self._settings.messaging_api_base_url
        path, method = self._endpoint_configuration(operation)
        if not base_url:
            raise MessagingConfigurationError("MESSAGING_API_BASE_URL is required for this operation.")
        if not path or not method:
            raise MessagingConfigurationError(
                f"Messaging endpoint path and method are required for {operation.value}."
            )
        if self._codec is None:
            raise MessagingContractUnavailable(
                "The messaging API contract mapper is not configured yet."
            )

        try:
            encoded = self._codec.encode(operation, command)
            if (
                not isinstance(encoded, EncodedMessagingRequest)
                or not isinstance(encoded.body, bytes)
            ):
                raise TypeError("Invalid encoded request.")
            headers = self._prepare_headers(encoded.headers)
        except MessagingContractUnavailable:
            raise
        except Exception as exc:
            raise MessagingContractError("The messaging request could not be mapped.") from exc

        url = urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))
        try:
            response = await self._client.request(
                method,
                url,
                content=encoded.body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise MessagingProviderUnavailable("The messaging provider timed out.") from exc
        except httpx.TransportError as exc:
            raise MessagingProviderUnavailable("The messaging provider is unavailable.") from exc

        if not 200 <= response.status_code < 300:
            raise MessagingProviderRejected("The messaging provider rejected the request.")

        try:
            result = self._codec.decode(
                operation,
                response.status_code,
                response.headers,
                response.content,
            )
        except Exception as exc:
            raise MessagingContractError("The messaging provider response could not be interpreted.") from exc
        if type(result) is not bool:
            raise MessagingContractError("The messaging provider response mapping was invalid.")
        return result

    def _endpoint_configuration(self, operation: MessagingOperation) -> tuple[str | None, str | None]:
        config = {
            MessagingOperation.SEND_OTP: (
                self._settings.messaging_send_otp_path,
                self._settings.messaging_send_otp_method,
            ),
            MessagingOperation.VERIFY_OTP: (
                self._settings.messaging_verify_otp_path,
                self._settings.messaging_verify_otp_method,
            ),
            MessagingOperation.SEND_WHATSAPP: (
                self._settings.messaging_send_whatsapp_path,
                self._settings.messaging_send_whatsapp_method,
            ),
        }
        return config[operation]

    def _prepare_headers(self, contract_headers: Mapping[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in contract_headers.items():
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not name
                or any(
                    not (
                        character.isascii()
                        and (character.isalnum() or character in "!#$%&'*+-.^_`|~")
                    )
                    for character in name
                )
                or name.lower() in _RESERVED_CODEC_HEADERS
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("Invalid contract header.")
            result[name] = value

        if self._settings.messaging_api_key is not None:
            header_name = self._settings.messaging_auth_header_name
            if not header_name:
                raise MessagingConfigurationError(
                    "MESSAGING_AUTH_HEADER_NAME is required when MESSAGING_API_KEY is configured."
                )
            for existing in tuple(result):
                if existing.lower() == header_name.lower():
                    del result[existing]
            key = self._settings.messaging_api_key.get_secret_value()
            result[header_name] = f"{self._settings.messaging_auth_value_prefix}{key}"
        return result

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
