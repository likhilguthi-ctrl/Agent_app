from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.core.request_id import request_id_context

logger = logging.getLogger(__name__)


def mask_recipient(recipient: str) -> str:
    """Masks recipient phone number or email for secure terminal logging."""
    clean = recipient.strip()
    if clean.startswith("+91") and len(clean) == 13:
        return f"+91 {clean[3:8]} ****{clean[-2:]}"
    if len(clean) >= 8:
        prefix = clean[: len(clean) - 6]
        suffix = clean[-2:]
        return f"{prefix}****{suffix}"
    return "***"


def classify_connection_error(exc: Exception) -> tuple[str, str]:
    """Distinguish connection refused, DNS failure, TLS failure, or general connection error."""
    import socket
    import ssl

    curr: BaseException | None = exc
    while curr is not None:
        if isinstance(curr, ssl.SSLError):
            return "TLS_FAILURE", f"TLS handshake/certificate verification failed: {curr}"
        if isinstance(curr, socket.gaierror):
            return "DNS_FAILURE", f"DNS host resolution failed: {curr}"
        if isinstance(curr, ConnectionRefusedError):
            return "CONNECTION_REFUSED", f"TCP connection refused by target host: {curr}"
        curr_str = str(curr).lower()
        if "connection refused" in curr_str or "10061" in curr_str or "errno 111" in curr_str:
            return "CONNECTION_REFUSED", f"TCP connection refused by target host: {curr}"
        if "name or service not known" in curr_str or "getaddrinfo failed" in curr_str or "nodename nor servname provided" in curr_str:
            return "DNS_FAILURE", f"DNS host resolution failed: {curr}"
        if "certificate" in curr_str or "ssl" in curr_str or "handshake" in curr_str:
            return "TLS_FAILURE", f"TLS failure: {curr}"
        curr = curr.__cause__ or curr.__context__

    return "CONNECTION_ERROR", f"Connection error: {exc}"


@dataclass(frozen=True, slots=True)
class NotificationDispatchResult:
    success: bool
    message_id: str | None
    status: str
    status_url: str | None = None
    error: str | None = None
    raw_response: dict[str, Any] | None = None
    channel: str = "WHATSAPP"


class NotificationServiceClient:
    """Unified client for dispatching notifications through the Notification Microservice.

    Contract defined in docs/implementation/FRONTEND_INTEGRATION.md:
    POST /api/v1/messages
    Headers:
        X-API-Key: <service-api-key>
        Idempotency-Key: <unique-notification-intent-key>
        Content-Type: application/json
    Body (WHATSAPP):
        {
            "channel": "WHATSAPP",
            "messageType": "LOGIN_OTP",
            "recipient": "+919876543210",
            "content": {
                "type": "TEMPLATE",
                "templateCode": "LOGIN_OTP",
                "locale": "en",
                "variables": {
                    "otp": "123456"
                }
            },
            "metadata": {
                "referenceId": "<unique reference ID>"
            }
        }
    Body (SMS):
        {
            "channel": "SMS",
            "messageType": "LOGIN_OTP",
            "recipient": "+919876543210",
            "content": {
                "type": "TEXT",
                "text": "Your login verification code is 123456."
            },
            "metadata": {
                "referenceId": "<unique reference ID>"
            }
        }
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._mock_messages: list[dict[str, Any]] = []

    @property
    def mock_messages(self) -> list[dict[str, Any]]:
        return self._mock_messages

    @property
    def api_key(self) -> str | None:
        """Fixed X-API-Key loaded from .env configuration."""
        if self._settings.notification_service_api_key is not None:
            val = self._settings.notification_service_api_key.get_secret_value().strip()
            return val or None
        return None

    @property
    def has_api_key(self) -> bool:
        """Indicates whether a non-empty X-API-Key is configured from .env."""
        return self.api_key is not None

    @property
    def base_url(self) -> str:
        """Configured root URL from environment (NOTIFICATION_SERVICE_BASE_URL)."""
        return self._settings.notification_base_url

    @property
    def messages_endpoint_url(self) -> str:
        """Documented endpoint: {NOTIFICATION_SERVICE_BASE_URL}/api/v1/messages."""
        return f"{self.base_url}/api/v1/messages"

    def message_status_url(self, message_id: str) -> str:
        """Documented endpoint: {NOTIFICATION_SERVICE_BASE_URL}/api/v1/messages/{messageId}."""
        return f"{self.base_url}/api/v1/messages/{message_id}"

    def message_attempts_url(self, message_id: str) -> str:
        """Documented endpoint: {NOTIFICATION_SERVICE_BASE_URL}/api/v1/messages/{messageId}/attempts."""
        return f"{self.base_url}/api/v1/messages/{message_id}/attempts"

    async def send_otp(
        self,
        recipient_e164: str,
        otp: str,
        purpose: str = "login",
        challenge_id: str | None = None,
        idempotency_key: str | None = None,
        expires_at: datetime | None = None,
        channel: Literal["WHATSAPP", "SMS"] | None = None,
        template_code: str = "LOGIN_OTP",
        request_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> NotificationDispatchResult:
        mode = self._settings.otp_delivery_mode
        active_channel: str = (channel or getattr(self._settings, "notification_channel", "WHATSAPP")).upper()
        if active_channel not in {"WHATSAPP", "SMS"}:
            active_channel = "WHATSAPP"
        req_id = request_id or request_id_context.get() or "n/a"
        cid = str(challenge_id) if challenge_id else "n/a"

        # 1. Development dummy mode (restricted to development/test)
        if mode == "development_dummy":
            if self._settings.environment in {"production", "staging"}:
                raise RuntimeError("development_dummy OTP mode is strictly forbidden in production/staging.")
            logger.info(
                "development_dummy_otp_generated",
                extra={
                    "recipient": recipient_e164,
                    "purpose": purpose,
                    "channel": active_channel,
                    "mode": "development_dummy",
                },
            )
            msg_id = f"dev_{uuid4().hex}"
            logger.info(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=200 outcome=ACCEPTED message_id=%s",
                req_id,
                cid,
                active_channel,
                msg_id,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": 200,
                    "outcome": "ACCEPTED",
                    "message_id": msg_id,
                    "mode": "development_dummy",
                },
            )
            return NotificationDispatchResult(
                success=True,
                message_id=msg_id,
                status="MOCK_SENT",
                channel=active_channel,
                status_url=f"/api/v1/messages/{msg_id}",
            )

        # 2. Automated test mock mode
        if mode == "mock":
            msg_id = f"mock_{uuid4().hex}"
            if active_channel == "WHATSAPP":
                mock_entry: dict[str, Any] = {
                    "channel": "WHATSAPP",
                    "messageType": "LOGIN_OTP",
                    "recipient": recipient_e164,
                    "otp": otp,
                    "purpose": purpose,
                    "challenge_id": challenge_id,
                    "content": {
                        "type": "TEMPLATE",
                        "templateCode": template_code,
                        "locale": "en",
                        "variables": {"otp": otp},
                    },
                    "metadata": {
                        "referenceId": challenge_id or str(uuid4()),
                    },
                    "expires_at": expires_at.isoformat() if expires_at else None,
                }
            else:
                mock_entry = {
                    "channel": "SMS",
                    "messageType": "LOGIN_OTP",
                    "recipient": recipient_e164,
                    "otp": otp,
                    "purpose": purpose,
                    "challenge_id": challenge_id,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                }

            self._mock_messages.append(mock_entry)
            logger.info("mock_otp_recorded", extra={"recipient": recipient_e164, "purpose": purpose, "channel": active_channel})
            logger.info(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=200 outcome=ACCEPTED message_id=%s",
                req_id,
                cid,
                active_channel,
                msg_id,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": 200,
                    "outcome": "ACCEPTED",
                    "message_id": msg_id,
                    "mode": "mock",
                },
            )
            return NotificationDispatchResult(
                success=True,
                message_id=msg_id,
                status="MOCK_SENT",
                channel=active_channel,
                status_url=f"/api/v1/messages/{msg_id}",
            )

        # 3. Live Notification Microservice
        url = self.messages_endpoint_url

        # Idempotency key: stable for the same challenge intent, max 200 chars, no sensitive data
        if idempotency_key:
            idem_key = idempotency_key.strip()[:200]
        elif challenge_id:
            clean_purpose = re.sub(r"[^A-Za-z0-9_-]", "", purpose) or "login"
            clean_cid = re.sub(r"[^A-Za-z0-9_-]", "", str(challenge_id))
            if dispatch_id:
                clean_did = re.sub(r"[^A-Za-z0-9_-]", "", str(dispatch_id))
                idem_key = f"community-ott-{clean_purpose}-{clean_cid}-{clean_did}"[:200]
            else:
                idem_key = f"community-ott-{clean_purpose}-{clean_cid}"[:200]
        else:
            clean_purpose = re.sub(r"[^A-Za-z0-9_-]", "", purpose) or "login"
            idem_key = f"community-ott-{clean_purpose}-{uuid4().hex}"[:200]

        # Check API key configuration loaded from .env only
        has_api_key = self.has_api_key
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Idempotency-Key": idem_key,
        }
        if has_api_key and self.api_key:
            headers["X-API-Key"] = self.api_key
        else:
            logger.warning(
                "notification_service_api_key_missing request_id=%s challenge_id=%s channel=%s note=%s",
                req_id,
                cid,
                active_channel,
                "NOTIFICATION_SERVICE_API_KEY (or X_API_KEY) is not configured in .env; request will lack X-API-Key header.",
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "note": "missing_api_key",
                },
            )

        # Build message body according to channel contract
        if active_channel == "WHATSAPP":
            message_body: dict[str, Any] = {
                "channel": "WHATSAPP",
                "messageType": "LOGIN_OTP",
                "recipient": recipient_e164,
                "content": {
                    "type": "TEMPLATE",
                    "templateCode": template_code,
                    "locale": "en",
                    "variables": {
                        "otp": otp,
                    },
                },
                "metadata": {
                    "referenceId": challenge_id or str(uuid4()),
                },
            }
        else:
            # Build purpose-tailored text
            if purpose == "admin_registration":
                message_text = f"Your Admin registration verification code is {otp}."
            elif purpose == "agent_registration":
                message_text = f"Your Agent registration verification code is {otp}."
            elif purpose == "user_registration":
                message_text = f"Your User registration verification code is {otp}."
            else:
                message_text = f"Your login verification code is {otp}."

            message_body = {
                "channel": "SMS",
                "messageType": "LOGIN_OTP",
                "recipient": recipient_e164,
                "content": {
                    "type": "TEXT",
                    "text": message_text,
                },
            }
            if challenge_id:
                message_body["metadata"] = {
                    "purpose": purpose,
                    "challengeId": challenge_id,
                }

        # Optional expiresAt in ISO-8601 UTC format
        if expires_at is not None:
            if expires_at.tzinfo is None:
                utc_expires = expires_at.replace(tzinfo=UTC)
            else:
                utc_expires = expires_at.astimezone(UTC)
            message_body["expiresAt"] = utc_expires.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Prepare non-sensitive payload summary and header names for safe diagnostics
        header_names = list(headers.keys())
        non_sensitive_payload: dict[str, Any] = {
            "channel": active_channel,
            "messageType": message_body.get("messageType"),
            "recipient": mask_recipient(recipient_e164),
        }
        content_info = message_body.get("content")
        if isinstance(content_info, dict):
            c_summary: dict[str, Any] = {"type": content_info.get("type")}
            if "templateCode" in content_info:
                c_summary["templateCode"] = content_info.get("templateCode")
            if "locale" in content_info:
                c_summary["locale"] = content_info.get("locale")
            if "variables" in content_info and isinstance(content_info["variables"], dict):
                c_summary["variable_keys"] = sorted(list(content_info["variables"].keys()))
            non_sensitive_payload["content"] = c_summary
        if "metadata" in message_body and isinstance(message_body["metadata"], dict):
            safe_meta_keys = {"referenceId", "purpose", "challengeId"}
            non_sensitive_payload["metadata"] = {
                k: v for k, v in message_body["metadata"].items() if k in safe_meta_keys
            }
        if "expiresAt" in message_body:
            non_sensitive_payload["expiresAt"] = message_body.get("expiresAt")

        start_time = time.perf_counter()
        try:
            owns_client = self._client is None
            http_client = self._client or httpx.AsyncClient(timeout=5.0)
            try:
                response = await http_client.post(url, json=message_body, headers=headers)
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                status_code = response.status_code

                if status_code == 202:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    msg_id = data.get("messageId")
                    logger.info(
                        "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=202 outcome=ACCEPTED message_id=%s",
                        req_id,
                        cid,
                        active_channel,
                        msg_id or "n/a",
                        extra={
                            "request_id": req_id,
                            "challenge_id": cid,
                            "channel": active_channel,
                            "http_status": 202,
                            "outcome": "ACCEPTED",
                            "message_id": msg_id,
                            "note": "queued_unconfirmed",
                        },
                    )
                    self._log_outbound_debug(
                        method="POST",
                        url=url,
                        channel=active_channel,
                        message_type="LOGIN_OTP",
                        idempotency_key=idem_key,
                        recipient=recipient_e164,
                        elapsed_ms=elapsed_ms,
                        outcome=f"HTTP 202 QUEUED ({elapsed_ms:.1f}ms)",
                        detail="Queued by notification service; delivery unconfirmed until webhook receipt.",
                        response_body=data,
                        header_names=header_names,
                        non_sensitive_payload=non_sensitive_payload,
                        http_status=202,
                    )
                    return NotificationDispatchResult(
                        success=True,
                        message_id=msg_id,
                        status=data.get("status", "QUEUED"),
                        channel=active_channel,
                        status_url=data.get("statusUrl"),
                        raw_response=data,
                    )
                elif 200 <= status_code < 300:
                    try:
                        data = response.json() if response.content else {}
                    except Exception:
                        data = {}
                    msg_id = data.get("messageId")
                    logger.info(
                        "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=%d outcome=ACCEPTED message_id=%s",
                        req_id,
                        cid,
                        active_channel,
                        status_code,
                        msg_id or "n/a",
                        extra={
                            "request_id": req_id,
                            "challenge_id": cid,
                            "channel": active_channel,
                            "http_status": status_code,
                            "outcome": "ACCEPTED",
                            "message_id": msg_id,
                        },
                    )
                    self._log_outbound_debug(
                        method="POST",
                        url=url,
                        channel=active_channel,
                        message_type="LOGIN_OTP",
                        idempotency_key=idem_key,
                        recipient=recipient_e164,
                        elapsed_ms=elapsed_ms,
                        outcome=f"HTTP {status_code} SENT ({elapsed_ms:.1f}ms)",
                        detail="Accepted by notification service.",
                        response_body=data,
                        header_names=header_names,
                        non_sensitive_payload=non_sensitive_payload,
                        http_status=status_code,
                    )
                    return NotificationDispatchResult(
                        success=True,
                        message_id=msg_id,
                        status=data.get("status", "SENT"),
                        channel=active_channel,
                        status_url=data.get("statusUrl"),
                        raw_response=data,
                    )
                elif status_code == 409:
                    try:
                        err_body = response.json()
                    except Exception:
                        err_body = None
                    logger.warning(
                        "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=409 outcome=REJECTED error=IDEMPOTENCY_CONFLICT",
                        req_id,
                        cid,
                        active_channel,
                        extra={
                            "request_id": req_id,
                            "challenge_id": cid,
                            "channel": active_channel,
                            "http_status": 409,
                            "outcome": "REJECTED",
                            "error": "IDEMPOTENCY_CONFLICT",
                        },
                    )
                    self._log_outbound_debug(
                        method="POST",
                        url=url,
                        channel=active_channel,
                        message_type="LOGIN_OTP",
                        idempotency_key=idem_key,
                        recipient=recipient_e164,
                        elapsed_ms=elapsed_ms,
                        outcome=f"HTTP 409 CONFLICT ({elapsed_ms:.1f}ms)",
                        detail="Idempotency key collision or conflict reported by upstream.",
                        response_body=err_body,
                        header_names=header_names,
                        non_sensitive_payload=non_sensitive_payload,
                        http_status=409,
                    )
                    logger.warning(
                        "notification_service_idempotency_conflict",
                        extra={"status_code": status_code, "idempotency_key": idem_key},
                    )
                    return NotificationDispatchResult(
                        success=False,
                        message_id=None,
                        status="CONFLICT",
                        channel=active_channel,
                        error="Notification request idempotency conflict.",
                    )
                elif status_code in {401, 403}:
                    try:
                        err_body = response.json() if response.content else {}
                    except Exception:
                        err_body = {"raw_text": response.text[:200]} if response.text else {}
                    err_code = "UNAUTHORIZED" if status_code == 401 else "FORBIDDEN"
                    logger.warning(
                        "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=%d outcome=REJECTED error=%s header_names=%s has_api_key=%s",
                        req_id,
                        cid,
                        active_channel,
                        status_code,
                        err_code,
                        ",".join(header_names),
                        has_api_key,
                        extra={
                            "request_id": req_id,
                            "challenge_id": cid,
                            "channel": active_channel,
                            "http_status": status_code,
                            "outcome": "REJECTED",
                            "error": err_code,
                            "header_names": header_names,
                            "has_api_key": has_api_key,
                        },
                    )
                    self._log_outbound_debug(
                        method="POST",
                        url=url,
                        channel=active_channel,
                        message_type="LOGIN_OTP",
                        idempotency_key=idem_key,
                        recipient=recipient_e164,
                        elapsed_ms=elapsed_ms,
                        outcome=f"HTTP {status_code} {err_code} ({elapsed_ms:.1f}ms)",
                        detail="Service authorization rejected by Notification Microservice.",
                        response_body=err_body,
                        header_names=header_names,
                        non_sensitive_payload=non_sensitive_payload,
                        http_status=status_code,
                    )
                    logger.warning(
                        "notification_service_auth_rejected",
                        extra={"status_code": status_code},
                    )
                    return NotificationDispatchResult(
                        success=False,
                        message_id=None,
                        status="UNAUTHORIZED" if status_code == 401 else "FORBIDDEN",
                        channel=active_channel,
                        error="Notification service authorization rejected.",
                    )
                elif status_code == 400:
                    try:
                        err_body = response.json()
                    except Exception:
                        err_body = None
                    logger.warning(
                        "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=400 outcome=REJECTED error=BAD_REQUEST",
                        req_id,
                        cid,
                        active_channel,
                        extra={
                            "request_id": req_id,
                            "challenge_id": cid,
                            "channel": active_channel,
                            "http_status": 400,
                            "outcome": "REJECTED",
                            "error": "BAD_REQUEST",
                        },
                    )
                    self._log_outbound_debug(
                        method="POST",
                        url=url,
                        channel=active_channel,
                        message_type="LOGIN_OTP",
                        idempotency_key=idem_key,
                        recipient=recipient_e164,
                        elapsed_ms=elapsed_ms,
                        outcome=f"HTTP 400 BAD_REQUEST ({elapsed_ms:.1f}ms)",
                        detail="Notification request rejected as invalid.",
                        response_body=err_body,
                        header_names=header_names,
                        non_sensitive_payload=non_sensitive_payload,
                        http_status=400,
                    )
                    logger.warning(
                        "notification_service_bad_request",
                        extra={"status_code": status_code},
                    )
                    return NotificationDispatchResult(
                        success=False,
                        message_id=None,
                        status="REJECTED",
                        channel=active_channel,
                        error="Notification request rejected as invalid.",
                    )
                else:
                    try:
                        err_body = response.json()
                    except Exception:
                        err_body = None
                    logger.warning(
                        "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=%d outcome=REJECTED error=HTTP_%d",
                        req_id,
                        cid,
                        active_channel,
                        status_code,
                        status_code,
                        extra={
                            "request_id": req_id,
                            "challenge_id": cid,
                            "channel": active_channel,
                            "http_status": status_code,
                            "outcome": "REJECTED",
                            "error": f"HTTP_{status_code}",
                        },
                    )
                    self._log_outbound_debug(
                        method="POST",
                        url=url,
                        channel=active_channel,
                        message_type="LOGIN_OTP",
                        idempotency_key=idem_key,
                        recipient=recipient_e164,
                        elapsed_ms=elapsed_ms,
                        outcome=f"HTTP {status_code} FAILED ({elapsed_ms:.1f}ms)",
                        detail=f"Notification service returned HTTP {status_code}.",
                        response_body=err_body,
                        header_names=header_names,
                        non_sensitive_payload=non_sensitive_payload,
                        http_status=status_code,
                    )
                    logger.warning(
                        "notification_service_rejected",
                        extra={"status_code": status_code},
                    )
                    return NotificationDispatchResult(
                        success=False,
                        message_id=None,
                        status="FAILED",
                        channel=active_channel,
                        error=f"Notification service returned HTTP {status_code}",
                    )
            finally:
                if owns_client:
                    await http_client.aclose()
        except httpx.ConnectTimeout:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=none outcome=TIMED_OUT error=CONNECT_TIMEOUT",
                req_id,
                cid,
                active_channel,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": None,
                    "outcome": "TIMED_OUT",
                    "error": "CONNECT_TIMEOUT",
                },
            )
            self._log_outbound_debug(
                method="POST",
                url=url,
                channel=active_channel,
                message_type="LOGIN_OTP",
                idempotency_key=idem_key,
                recipient=recipient_e164,
                elapsed_ms=elapsed_ms,
                outcome="ERROR - CONNECT_TIMEOUT",
                detail=f"TCP connection to notification service timed out after {elapsed_ms:.1f}ms.",
                header_names=header_names,
                non_sensitive_payload=non_sensitive_payload,
            )
            logger.warning("notification_service_timeout", extra={"error_type": "connect_timeout"})
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="TIMEOUT",
                channel=active_channel,
                error="Notification service connection timed out.",
            )
        except httpx.ReadTimeout:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=none outcome=TIMED_OUT error=READ_TIMEOUT",
                req_id,
                cid,
                active_channel,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": None,
                    "outcome": "TIMED_OUT",
                    "error": "READ_TIMEOUT",
                },
            )
            self._log_outbound_debug(
                method="POST",
                url=url,
                channel=active_channel,
                message_type="LOGIN_OTP",
                idempotency_key=idem_key,
                recipient=recipient_e164,
                elapsed_ms=elapsed_ms,
                outcome="ERROR - READ_TIMEOUT",
                detail=f"HTTP response read timed out after {elapsed_ms:.1f}ms.",
                header_names=header_names,
                non_sensitive_payload=non_sensitive_payload,
            )
            logger.warning("notification_service_timeout", extra={"error_type": "read_timeout"})
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="TIMEOUT",
                channel=active_channel,
                error="Notification service response read timed out.",
            )
        except httpx.TimeoutException:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=none outcome=TIMED_OUT error=TIMEOUT",
                req_id,
                cid,
                active_channel,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": None,
                    "outcome": "TIMED_OUT",
                    "error": "TIMEOUT",
                },
            )
            self._log_outbound_debug(
                method="POST",
                url=url,
                channel=active_channel,
                message_type="LOGIN_OTP",
                idempotency_key=idem_key,
                recipient=recipient_e164,
                elapsed_ms=elapsed_ms,
                outcome="ERROR - TIMEOUT",
                detail=f"Notification service request timed out after {elapsed_ms:.1f}ms.",
                header_names=header_names,
                non_sensitive_payload=non_sensitive_payload,
            )
            logger.warning("notification_service_timeout", extra={"error_type": "timeout"})
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="TIMEOUT",
                channel=active_channel,
                error="Notification service request timed out.",
            )
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            err_cat, err_detail = classify_connection_error(exc)
            logger.error(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=none outcome=FAILED error=%s",
                req_id,
                cid,
                active_channel,
                err_cat,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": None,
                    "outcome": "FAILED",
                    "error": err_cat,
                },
            )
            self._log_outbound_debug(
                method="POST",
                url=url,
                channel=active_channel,
                message_type="LOGIN_OTP",
                idempotency_key=idem_key,
                recipient=recipient_e164,
                elapsed_ms=elapsed_ms,
                outcome=f"ERROR - {err_cat}",
                detail=f"{err_detail} ({elapsed_ms:.1f}ms)",
                header_names=header_names,
                non_sensitive_payload=non_sensitive_payload,
            )
            logger.warning("notification_service_connection_error", extra={"error_type": err_cat})
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="CONNECTION_ERROR",
                channel=active_channel,
                error=f"Notification service unreachable ({err_cat}).",
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            err_name = type(exc).__name__
            logger.error(
                "notification_dispatch_result request_id=%s challenge_id=%s channel=%s http_status=none outcome=FAILED error=%s",
                req_id,
                cid,
                active_channel,
                err_name,
                extra={
                    "request_id": req_id,
                    "challenge_id": cid,
                    "channel": active_channel,
                    "http_status": None,
                    "outcome": "FAILED",
                    "error": err_name,
                },
            )
            self._log_outbound_debug(
                method="POST",
                url=url,
                channel=active_channel,
                message_type="LOGIN_OTP",
                idempotency_key=idem_key,
                recipient=recipient_e164,
                elapsed_ms=elapsed_ms,
                outcome=f"ERROR - {type(exc).__name__}",
                detail=str(exc),
                header_names=header_names,
                non_sensitive_payload=non_sensitive_payload,
            )
            logger.error("notification_service_error", extra={"error_type": type(exc).__name__})
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="FAILED",
                channel=active_channel,
                error="Notification service unavailable.",
            )

    def _log_outbound_debug(
        self,
        method: str,
        url: str,
        channel: str,
        message_type: str,
        idempotency_key: str,
        recipient: str,
        elapsed_ms: float,
        outcome: str,
        detail: str | None = None,
        response_body: dict[str, Any] | None = None,
        header_names: list[str] | None = None,
        non_sensitive_payload: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        """Structured development-only terminal display for outbound notification requests."""
        if not getattr(self._settings, "can_debug_log_notifications", False):
            return
        try:
            import json
            import sys

            border = "=" * 70
            masked = mask_recipient(recipient)
            hdr_str = ", ".join(header_names) if header_names else "n/a"
            lines = [
                f"\n{border}",
                "[DEV DEBUG] Outbound Notification Request",
                "-" * 70,
                f"  Endpoint       : {method} {url}",
                f"  Channel / Type : {channel} / {message_type}",
                f"  Headers Sent   : {hdr_str}",
                f"  Idempotency-Key: {idempotency_key}",
                f"  Recipient      : {masked}",
            ]
            if non_sensitive_payload:
                lines.append(f"  Payload Fields : {json.dumps(non_sensitive_payload)}")
            lines.extend([
                f"  Elapsed Time   : {elapsed_ms:.1f}ms",
                f"  Outcome        : {outcome}",
            ])
            if http_status is not None:
                lines.append(f"  HTTP Status    : {http_status}")
            if detail:
                lines.append(f"  Details        : {detail}")
            if response_body:
                safe_keys = {"messageId", "status", "channel", "statusUrl", "error", "reason", "code", "priority"}
                safe_body = {k: v for k, v in response_body.items() if k in safe_keys}
                lines.append(f"  Response Body  : {json.dumps(safe_body)}")
            elif response_body is not None and not response_body:
                lines.append("  Response Body  : (empty)")
            lines.append(f"{border}\n")
            sys.stdout.write("\n".join(lines))
            sys.stdout.flush()
        except Exception:
            pass

    async def send_otp_sms(
        self,
        recipient_e164: str,
        otp: str,
        purpose: str = "login",
        challenge_id: str | None = None,
        idempotency_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> NotificationDispatchResult:
        """Backward-compatible SMS OTP dispatch helper."""
        return await self.send_otp(
            recipient_e164=recipient_e164,
            otp=otp,
            purpose=purpose,
            challenge_id=challenge_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            channel="SMS",
        )

    async def send_email(
        self,
        recipient_email: str,
        subject: str | None = None,
        text_content: str | None = None,
        message_type: str = "ACCOUNT_UPDATE",
        template_code: str | None = None,
        variables: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        reference_id: str | None = None,
    ) -> NotificationDispatchResult:
        """Dispatches an email notification via the external Notification Microservice.

        Contract per docs/implementation/FRONTEND_INTEGRATION.md:
        - channel: "EMAIL"
        - recipient: email address (max 320 chars)
        - messageType: "ACCOUNT_UPDATE" (or other allowed uppercase type)
        - content: TEXT or TEMPLATE
        """
        mode = self._settings.otp_delivery_mode
        req_id = request_id or request_id_context.get() or "n/a"
        ref_id = reference_id or str(uuid4())

        if mode == "development_dummy":
            if self._settings.environment in {"production", "staging"}:
                raise RuntimeError("development_dummy mode is strictly forbidden in production/staging.")
            msg_id = f"dummy_email_{uuid4().hex[:12]}"
            logger.info("development_dummy_email_generated", extra={"recipient": recipient_email, "mode": mode})
            return NotificationDispatchResult(
                success=True,
                message_id=msg_id,
                status="MOCK_SENT",
                channel="EMAIL",
                status_url=f"/api/v1/messages/{msg_id}",
            )

        if mode == "mock":
            msg_id = f"mock_email_{uuid4().hex}"
            mock_entry: dict[str, Any] = {
                "channel": "EMAIL",
                "messageType": message_type,
                "recipient": recipient_email,
                "subject": subject,
                "content": {
                    "type": "TEMPLATE" if template_code else "TEXT",
                    "templateCode": template_code,
                    "text": text_content or "",
                    "variables": variables or {},
                },
                "metadata": {
                    "referenceId": ref_id,
                },
            }
            self._mock_messages.append(mock_entry)
            logger.info("mock_email_recorded", extra={"recipient": recipient_email, "channel": "EMAIL"})
            return NotificationDispatchResult(
                success=True,
                message_id=msg_id,
                status="MOCK_SENT",
                channel="EMAIL",
                status_url=f"/api/v1/messages/{msg_id}",
            )

        # Live Notification Microservice
        url = self.messages_endpoint_url
        if idempotency_key:
            idem_key = idempotency_key.strip()[:200]
        else:
            clean_type = re.sub(r"[^A-Za-z0-9_-]", "", message_type) or "email"
            idem_key = f"community-ott-{clean_type}-{uuid4().hex}"[:200]

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Idempotency-Key": idem_key,
        }
        if self.has_api_key and self.api_key:
            headers["X-API-Key"] = self.api_key

        content_payload: dict[str, Any] = (
            {"type": "TEMPLATE", "templateCode": template_code, "variables": variables or {}}
            if template_code
            else {"type": "TEXT", "text": text_content or ""}
        )
        body = {
            "channel": "EMAIL",
            "messageType": message_type,
            "recipient": recipient_email,
            "content": content_payload,
            "metadata": {
                "referenceId": ref_id,
            },
        }

        owns_client = self._client is None
        timeout_val = float(getattr(self._settings, "messaging_read_timeout_seconds", 5.0))
        http_client = self._client or httpx.AsyncClient(timeout=timeout_val)
        start_time = time.perf_counter()
        header_names = list(headers.keys())
        non_sensitive_payload = {
            "channel": "EMAIL",
            "messageType": message_type,
            "recipient": mask_recipient(recipient_email),
            "content_type": content_payload.get("type"),
            "metadata": {"referenceId": ref_id},
        }

        try:
            response = await http_client.post(url, json=body, headers=headers)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = response.status_code

            if status_code in {200, 201, 202}:
                data = response.json() if response.content else {}
                msg_id = data.get("messageId")
                status_str = data.get("status", "QUEUED")
                self._log_outbound_debug(
                    method="POST",
                    url=url,
                    channel="EMAIL",
                    message_type=message_type,
                    idempotency_key=idem_key,
                    recipient=recipient_email,
                    elapsed_ms=elapsed_ms,
                    outcome=f"HTTP {status_code} {status_str} ({elapsed_ms:.1f}ms)",
                    detail="Queued by notification service; delivery unconfirmed until webhook receipt.",
                    response_body=data,
                    header_names=header_names,
                    non_sensitive_payload=non_sensitive_payload,
                    http_status=status_code,
                )
                return NotificationDispatchResult(
                    success=True,
                    message_id=msg_id,
                    status=status_str,
                    channel="EMAIL",
                    status_url=data.get("statusUrl"),
                    raw_response=data,
                )
            elif status_code == 401:
                self._log_outbound_debug(
                    method="POST",
                    url=url,
                    channel="EMAIL",
                    message_type=message_type,
                    idempotency_key=idem_key,
                    recipient=recipient_email,
                    elapsed_ms=elapsed_ms,
                    outcome=f"HTTP 401 UNAUTHORIZED ({elapsed_ms:.1f}ms)",
                    detail="Service authentication rejected by Notification Microservice.",
                    header_names=header_names,
                    non_sensitive_payload=non_sensitive_payload,
                    http_status=401,
                )
                return NotificationDispatchResult(
                    success=False,
                    message_id=None,
                    status="UNAUTHORIZED",
                    channel="EMAIL",
                    error="Notification service authentication failed.",
                )
            elif status_code == 403:
                self._log_outbound_debug(
                    method="POST",
                    url=url,
                    channel="EMAIL",
                    message_type=message_type,
                    idempotency_key=idem_key,
                    recipient=recipient_email,
                    elapsed_ms=elapsed_ms,
                    outcome=f"HTTP 403 FORBIDDEN ({elapsed_ms:.1f}ms)",
                    detail="Service authorization rejected by Notification Microservice for EMAIL channel.",
                    header_names=header_names,
                    non_sensitive_payload=non_sensitive_payload,
                    http_status=403,
                )
                return NotificationDispatchResult(
                    success=False,
                    message_id=None,
                    status="FORBIDDEN",
                    channel="EMAIL",
                    error="Notification service authorization rejected for email delivery.",
                )
            else:
                self._log_outbound_debug(
                    method="POST",
                    url=url,
                    channel="EMAIL",
                    message_type=message_type,
                    idempotency_key=idem_key,
                    recipient=recipient_email,
                    elapsed_ms=elapsed_ms,
                    outcome=f"HTTP {status_code} FAILED ({elapsed_ms:.1f}ms)",
                    detail=f"Email dispatch returned unexpected HTTP {status_code}.",
                    header_names=header_names,
                    non_sensitive_payload=non_sensitive_payload,
                    http_status=status_code,
                )
                return NotificationDispatchResult(
                    success=False,
                    message_id=None,
                    status="FAILED",
                    channel="EMAIL",
                    error=f"Email dispatch returned HTTP {status_code}",
                )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self._log_outbound_debug(
                method="POST",
                url=url,
                channel="EMAIL",
                message_type=message_type,
                idempotency_key=idem_key,
                recipient=recipient_email,
                elapsed_ms=elapsed_ms,
                outcome=f"ERROR: {type(exc).__name__}",
                detail=str(exc),
                header_names=header_names,
                non_sensitive_payload=non_sensitive_payload,
            )
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="FAILED",
                channel="EMAIL",
                error=f"Email dispatch failed: {str(exc)}",
            )
        finally:
            if owns_client:
                await http_client.aclose()

    async def send_text_message(
        self,
        *,
        recipient: str,
        channel: Literal["SMS", "WHATSAPP"],
        text: str,
        message_type: str = "WELCOME",
        idempotency_key: str | None = None,
        reference_id: str | None = None,
    ) -> NotificationDispatchResult:
        mode = self._settings.otp_delivery_mode
        ref_id = reference_id or str(uuid4())

        if mode in {"development_dummy", "mock"}:
            msg_id = f"mock_{channel.lower()}_{uuid4().hex[:12]}"
            mock_entry = {
                "channel": channel,
                "messageType": message_type,
                "recipient": recipient,
                "text": text,
                "metadata": {"referenceId": ref_id},
            }
            self._mock_messages.append(mock_entry)
            logger.info("mock_text_message_recorded", extra={"recipient": mask_recipient(recipient), "channel": channel})
            return NotificationDispatchResult(
                success=True,
                message_id=msg_id,
                status="MOCK_SENT",
                channel=channel,
                status_url=f"/api/v1/messages/{msg_id}",
            )

        url = self.messages_endpoint_url
        clean_type = re.sub(r"[^A-Za-z0-9_-]", "", message_type) or "message"
        idem_key = idempotency_key.strip()[:200] if idempotency_key else f"community-ott-{clean_type}-{uuid4().hex}"[:200]

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Idempotency-Key": idem_key,
        }
        if self.has_api_key and self.api_key:
            headers["X-API-Key"] = self.api_key

        body = {
            "channel": channel,
            "messageType": message_type,
            "recipient": recipient,
            "content": {
                "type": "TEXT",
                "text": text,
            },
            "metadata": {
                "referenceId": ref_id,
            },
        }

        owns_client = self._client is None
        timeout_val = float(getattr(self._settings, "messaging_read_timeout_seconds", 5.0))
        http_client = self._client or httpx.AsyncClient(timeout=timeout_val)

        try:
            response = await http_client.post(url, json=body, headers=headers)
            if response.status_code in (200, 201, 202):
                data = response.json()
                msg_id = data.get("messageId") or str(uuid4())
                return NotificationDispatchResult(
                    success=True,
                    message_id=msg_id,
                    status=data.get("status", "SENT"),
                    channel=channel,
                    status_url=f"/api/v1/messages/{msg_id}",
                    raw_response=data,
                )
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="FAILED",
                channel=channel,
                error=f"Dispatch failed with status {response.status_code}: {response.text[:200]}",
            )
        except Exception as exc:
            return NotificationDispatchResult(
                success=False,
                message_id=None,
                status="CONNECTION_ERROR",
                channel=channel,
                error=str(exc),
            )
        finally:
            if owns_client:
                await http_client.aclose()

    async def send_subscriber_welcome(
        self,
        *,
        subscriber_name: str,
        mobile_e164: str,
        whatsapp_number: str | None = None,
        whatsapp_consent: bool = False,
        email: str | None = None,
        plan_name: str = "Standard",
        amount_paid: Any = 0,
        community_name: str = "Community OTT",
    ) -> dict[str, NotificationDispatchResult | None]:
        results: dict[str, NotificationDispatchResult | None] = {
            "sms": None,
            "email": None,
            "whatsapp": None,
        }

        # 1. SMS Welcome (Always sent on payment confirmation)
        sms_text = (
            f"Welcome to {community_name}, {subscriber_name}! "
            f"Your subscription ({plan_name} - Rs. {amount_paid}) is now active. "
            f"Enjoy your streaming experience!"
        )
        try:
            results["sms"] = await self.send_text_message(
                recipient=mobile_e164,
                channel="SMS",
                message_type="WELCOME",
                text=sms_text,
            )
        except Exception as exc:
            logger.warning("welcome_sms_failed", extra={"error": str(exc), "recipient": mask_recipient(mobile_e164)})

        # 2. Email Welcome (Optional)
        if email and email.strip():
            email_text = (
                f"Hello {subscriber_name},\n\n"
                f"Welcome to {community_name}! Your subscription to the {plan_name} plan "
                f"(Rs. {amount_paid}) has been successfully activated.\n\n"
                f"Thank you for joining our community!\n\n"
                f"- The {community_name} Team"
            )
            try:
                results["email"] = await self.send_email(
                    recipient_email=email.strip(),
                    subject=f"Welcome to {community_name}! Subscription Activated",
                    text_content=email_text,
                    message_type="WELCOME",
                )
            except Exception as exc:
                logger.warning("welcome_email_failed", extra={"error": str(exc)})

        # 3. WhatsApp Welcome (Strict Consent Check: ONLY if whatsapp_consent is True)
        if whatsapp_consent:
            wa_recipient = (whatsapp_number or mobile_e164).strip()
            wa_text = (
                f"Welcome to {community_name}, {subscriber_name}! 🎉\n\n"
                f"Your subscription ({plan_name} - Rs. {amount_paid}) is now active.\n\n"
                f"Happy streaming!"
            )
            try:
                results["whatsapp"] = await self.send_text_message(
                    recipient=wa_recipient,
                    channel="WHATSAPP",
                    message_type="WELCOME",
                    text=wa_text,
                )
            except Exception as exc:
                logger.warning("welcome_whatsapp_failed", extra={"error": str(exc)})
        else:
            logger.info("welcome_whatsapp_skipped_no_consent", extra={"recipient": mask_recipient(mobile_e164)})

        return results


    async def get_message_status(self, message_id: str) -> dict[str, Any] | None:
        """Retrieves message status from GET /api/v1/messages/{messageId}."""
        if not message_id:
            return None

        if self._settings.otp_delivery_mode in {"development_dummy", "mock"}:
            return {
                "messageId": message_id,
                "status": "MOCK_SENT",
                "channel": getattr(self._settings, "notification_channel", "WHATSAPP"),
                "priority": "P0",
            }

        url = self.message_status_url(message_id)
        headers: dict[str, str] = {}
        if self.has_api_key and self.api_key:
            headers["X-API-Key"] = self.api_key

        owns_client = self._client is None
        http_client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            response = await http_client.get(url, headers=headers)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception:
            return None
        finally:
            if owns_client:
                await http_client.aclose()

    async def get_message_attempts(self, message_id: str) -> list[dict[str, Any]]:
        """Retrieves message attempts from GET /api/v1/messages/{messageId}/attempts."""
        if not message_id:
            return []

        if self._settings.otp_delivery_mode in {"development_dummy", "mock"}:
            return [
                {
                    "attemptNumber": 1,
                    "provider": "mock",
                    "status": "MOCK_SENT",
                    "failureCode": None,
                }
            ]

        url = self.message_attempts_url(message_id)
        headers: dict[str, str] = {}
        if self.has_api_key and self.api_key:
            headers["X-API-Key"] = self.api_key

        owns_client = self._client is None
        http_client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            response = await http_client.get(url, headers=headers)
            if response.status_code == 200:
                return response.json()
            return []
        except Exception:
            return []
        finally:
            if owns_client:
                await http_client.aclose()
