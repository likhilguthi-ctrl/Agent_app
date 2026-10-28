from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.request_id import request_id_context
from app.db.session import get_db
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.messaging.models import NotificationMessage
from app.modules.messaging.webhook_service import (
    NotificationWebhookResponse,
    NotificationWebhookService,
)

logger = logging.getLogger(__name__)


def _log_incoming_webhook_debug(
    settings: Settings,
    request: Request,
    event_id: str,
    event_status: str,
    signature_result: str,
    processing_status: str,
    http_status: int,
    response_body: dict[str, Any] | None = None,
) -> None:
    """Structured development-only terminal display for incoming status webhooks."""
    if not getattr(settings, "can_debug_log_notifications", False):
        return
    try:
        import sys

        border = "=" * 70
        request_id = (
            getattr(request.state, "request_id", None)
            or request.headers.get("x-request-id")
            or "n/a"
        )
        is_proxy = (
            "x-forwarded-host" in request.headers
            or (request.headers.get("host", "").endswith(":3000"))
            or ("/backend-api/" in request.headers.get("referer", ""))
        )
        ingress = "Next.js Proxy (/backend-api/...)" if is_proxy else "Direct Backend (/api/v1/...)"

        lines = [
            f"\n{border}",
            "[DEV DEBUG] Incoming Notification Webhook",
            "-" * 70,
            f"  Request ID     : {request_id}",
            f"  Ingress Path   : {ingress}",
            f"  Event ID       : {event_id}",
            f"  Event Status   : {event_status}",
            f"  Signature Check: {signature_result}",
            f"  Processing     : {processing_status}",
            f"  HTTP Response  : {http_status}",
        ]
        if response_body:
            safe_keys = {"status", "event_id", "message_id", "reason", "error", "message"}
            safe_body = {k: v for k, v in response_body.items() if k in safe_keys}
            lines.append(f"  Response Body  : {json.dumps(safe_body)}")
        lines.append(f"{border}\n")
        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()
    except Exception:
        pass


notification_webhooks_router = APIRouter(
    prefix="/webhooks/notifications",
    tags=["notification_webhooks"],
)

notifications_router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
)


class NotificationStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: str = Field(alias="messageId")
    channel: str
    status: str
    message_type: str | None = Field(default=None, alias="messageType")
    provider_message_id: str | None = Field(default=None, alias="providerMessageId")
    purpose: str | None = None
    occurred_at: datetime | None = Field(default=None, alias="occurredAt")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    failure_reason: str | None = Field(default=None, alias="failureReason")


@notification_webhooks_router.post(
    "/status",
    response_model=NotificationWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Notification Microservice status webhook receiver",
)
async def receive_notification_status_webhook(
    request: Request,
    x_notification_timestamp: Annotated[str | None, Header(alias="X-Notification-Timestamp")] = None,
    x_notification_signature: Annotated[str | None, Header(alias="X-Notification-Signature")] = None,
    x_event_id: Annotated[str | None, Header(alias="X-Event-ID")] = None,
    db: Session = Depends(get_db),
) -> NotificationWebhookResponse:
    """Receive and verify status event callbacks from the external Notification Microservice."""
    raw_body = await request.body()
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    webhook_service = NotificationWebhookService(settings)
    req_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("x-request-id")
        or request_id_context.get()
        or "unknown"
    )

    try:
        result = webhook_service.process_webhook(
            session=db,
            raw_body=raw_body,
            timestamp_header=x_notification_timestamp,
            signature_header=x_notification_signature,
            event_id_header=x_event_id,
        )

        incoming_status = "UNKNOWN"
        try:
            p = json.loads(raw_body.decode("utf-8"))
            incoming_status = str(p.get("status") or "UNKNOWN")
        except Exception:
            pass

        logger.info(
            "notification_webhook_result request_id=%s http_status=200 event_id=%s message_id=%s event_status=%s outcome=%s",
            req_id,
            result.event_id,
            result.message_id or "unknown",
            incoming_status,
            result.status,
        )

        _log_incoming_webhook_debug(
            settings=settings,
            request=request,
            event_id=result.event_id,
            event_status=incoming_status,
            signature_result="VALID (HMAC-SHA256 verified)",
            processing_status=result.status,
            http_status=200,
            response_body={
                "status": result.status,
                "event_id": result.event_id,
                "message_id": result.message_id,
                "reason": result.reason,
            },
        )
        return result
    except HTTPException as exc:
        sig_result = (
            "FAILED (INVALID_SIGNATURE)"
            if exc.status_code == 401
            else ("MISSING_SIGNATURE" if "MISSING_SIGNATURE" in str(exc.detail) else "VERIFICATION_ERROR")
        )
        event_id_candidate = x_event_id or "UNKNOWN"
        message_id_candidate = "UNKNOWN"
        event_status_candidate = "UNKNOWN"
        try:
            p = json.loads(raw_body.decode("utf-8"))
            if not event_id_candidate or event_id_candidate == "UNKNOWN":
                event_id_candidate = str(p.get("eventId") or p.get("event_id") or "UNKNOWN")
            message_id_candidate = str(p.get("messageId") or p.get("message_id") or "UNKNOWN")
            event_status_candidate = str(p.get("status") or "UNKNOWN")
        except Exception:
            pass

        sanitized_error = "UNKNOWN"
        if isinstance(exc.detail, dict):
            sanitized_error = str(exc.detail.get("code") or exc.detail.get("error") or exc.status_code)
        else:
            sanitized_error = str(exc.detail)

        logger.warning(
            "notification_webhook_result request_id=%s http_status=%d event_id=%s message_id=%s event_status=%s outcome=rejected error=%s",
            req_id,
            exc.status_code,
            event_id_candidate,
            message_id_candidate,
            event_status_candidate,
            sanitized_error,
        )

        _log_incoming_webhook_debug(
            settings=settings,
            request=request,
            event_id=event_id_candidate,
            event_status=event_status_candidate,
            signature_result=sig_result,
            processing_status="rejected",
            http_status=exc.status_code,
            response_body=exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)},
        )
        raise exc
    except Exception as exc:
        event_id_candidate = x_event_id or "UNKNOWN"
        message_id_candidate = "UNKNOWN"
        event_status_candidate = "UNKNOWN"
        try:
            p = json.loads(raw_body.decode("utf-8"))
            if not event_id_candidate or event_id_candidate == "UNKNOWN":
                event_id_candidate = str(p.get("eventId") or p.get("event_id") or "UNKNOWN")
            message_id_candidate = str(p.get("messageId") or p.get("message_id") or "UNKNOWN")
            event_status_candidate = str(p.get("status") or "UNKNOWN")
        except Exception:
            pass

        logger.error(
            "notification_webhook_result request_id=%s http_status=500 event_id=%s message_id=%s event_status=%s outcome=failed error=%s",
            req_id,
            event_id_candidate,
            message_id_candidate,
            event_status_candidate,
            type(exc).__name__,
        )

        _log_incoming_webhook_debug(
            settings=settings,
            request=request,
            event_id=event_id_candidate,
            event_status="ERROR",
            signature_result="ERROR",
            processing_status="error",
            http_status=500,
            response_body={"error": "INTERNAL_SERVER_ERROR", "message": str(exc)},
        )
        raise exc


@notifications_router.get(
    "/status/{message_id}",
    response_model=NotificationStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve tenant notification delivery status",
)
def get_notification_delivery_status(
    message_id: str,
    caller: Annotated[
        ResolvedSession,
        Depends(require_session_capability(PermissionCode.NOTIFICATION_READ)),
    ],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> NotificationStatusResponse:
    """Retrieve delivery status of a notification message with strict tenant isolation."""
    clean_id = message_id.strip()
    if not clean_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_MESSAGE_ID", "message": "Message ID must not be empty."},
        )

    # DEF-01: Reconcile any pending webhook events for this message before reading status
    webhook_service = NotificationWebhookService(settings)
    webhook_service.reconcile_pending_events(db, clean_id, commit=True)

    # 1. Query notification_messages
    stmt = select(NotificationMessage).where(NotificationMessage.message_id == clean_id)
    msg = db.execute(stmt).scalar_one_or_none()

    if msg is not None:
        # Enforce tenant authorization: Super Admin can access all, otherwise must match caller's tenant
        if caller.role != "SUPER_ADMIN" and msg.tenant_id != caller.tenant_id:
            logger.warning(
                "notification_status_cross_tenant_access_blocked",
                extra={"message_id": clean_id, "caller_tenant": str(caller.tenant_id)},
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "NOTIFICATION_NOT_FOUND", "message": "Notification message not found."},
            )

        return NotificationStatusResponse(
            messageId=msg.message_id,
            channel=msg.channel,
            status=msg.status,
            messageType=msg.message_type,
            providerMessageId=msg.provider_message_id,
            purpose=msg.purpose,
            occurredAt=msg.occurred_at,
            createdAt=msg.created_at,
            failureReason=msg.failure_reason,
        )

    # 2. Fallback: check OtpChallenge metadata
    challenges = db.execute(
        select(OtpChallenge).where(OtpChallenge.metadata_json.isnot(None))
    ).scalars().all()
    for c in challenges:
        if c.metadata_json and c.metadata_json.get("notification_message_id") == clean_id:
            if caller.role != "SUPER_ADMIN" and c.tenant_id != caller.tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "NOTIFICATION_NOT_FOUND", "message": "Notification message not found."},
                )
            return NotificationStatusResponse(
                messageId=clean_id,
                channel="SMS",
                status=c.metadata_json.get("notification_status", "QUEUED"),
                messageType="LOGIN_OTP",
                providerMessageId=c.metadata_json.get("provider_message_id"),
                purpose=c.purpose,
                occurredAt=c.created_at,
                createdAt=c.created_at,
                failureReason=c.metadata_json.get("notification_failure"),
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "NOTIFICATION_NOT_FOUND", "message": "Notification message not found."},
    )
