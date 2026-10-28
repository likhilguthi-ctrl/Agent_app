from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.authentication.models import OtpChallenge
from app.modules.messaging.models import NotificationMessage, NotificationWebhookEvent

logger = logging.getLogger(__name__)

# Documented valid notification statuses
VALID_STATUSES: frozenset[str] = frozenset(
    {
        "QUEUED",
        "PROCESSING",
        "PROVIDER_ACCEPTED",
        "RETRY_PENDING",
        "DELIVERED",
        "READ",
        "FAILED",
        "EXPIRED",
        "UNKNOWN_OUTCOME",
    }
)

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "DELIVERED",
        "READ",
        "FAILED",
        "EXPIRED",
        "UNKNOWN_OUTCOME",
    }
)


def _normalize_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class NotificationWebhookResponse(BaseModel):
    status: str
    event_id: str
    message_id: str | None = None
    reason: str | None = None


class NotificationWebhookService:
    """Production-grade HMAC verification, deduplication, and status updater for Notification Microservice."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def verify_signature(
        self,
        raw_body: bytes,
        timestamp_header: str | None,
        signature_header: str | None,
    ) -> datetime:
        """Verify HMAC-SHA256(timestamp + "." + raw_body, secret) and timestamp replay tolerance."""
        # 1. Webhook secret requirement (fail closed if missing)
        secret_obj = self._settings.notification_webhook_secret
        if secret_obj is None or not secret_obj.get_secret_value().strip():
            logger.error("notification_webhook_secret_missing")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "WEBHOOK_SECRET_UNCONFIGURED",
                    "message": "Notification webhook secret is not configured on the server.",
                },
            )
        secret_bytes = secret_obj.get_secret_value().encode("utf-8")

        # 2. Required headers
        if not timestamp_header:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "MISSING_TIMESTAMP", "message": "X-Notification-Timestamp header is required."},
            )
        if not signature_header:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "MISSING_SIGNATURE", "message": "X-Notification-Signature header is required."},
            )

        # 3. Signature shape verification (must be sha256=<hex>)
        if not signature_header.startswith("sha256="):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "INVALID_SIGNATURE_FORMAT", "message": "Signature format must be sha256=<hex digest>."},
            )

        # 4. Timestamp format & replay tolerance
        timestamp_str = timestamp_header.strip()
        parsed_ts: datetime | None = None
        try:
            # Handle ISO-8601 strings (e.g. 2026-10-22T10:30:02Z or with offset)
            if "T" in timestamp_str:
                iso_str = timestamp_str.replace("Z", "+00:00")
                parsed_ts = datetime.fromisoformat(iso_str)
            else:
                # Numeric epoch seconds
                parsed_ts = datetime.fromtimestamp(float(timestamp_str), tz=UTC)
        except (ValueError, TypeError, OverflowError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_TIMESTAMP", "message": "Malformed timestamp in X-Notification-Timestamp."},
            ) from exc

        if parsed_ts.tzinfo is None:
            parsed_ts = parsed_ts.replace(tzinfo=UTC)
        else:
            parsed_ts = parsed_ts.astimezone(UTC)

        now = datetime.now(UTC)
        tolerance = float(self._settings.notification_webhook_timestamp_tolerance_seconds)
        age_seconds = abs((now - parsed_ts).total_seconds())
        if age_seconds > tolerance:
            logger.warning(
                "notification_webhook_timestamp_out_of_bounds",
                extra={"age_seconds": age_seconds, "tolerance": tolerance},
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "TIMESTAMP_OUT_OF_BOUNDS",
                    "message": f"Webhook timestamp age ({age_seconds:.1f}s) exceeds allowable tolerance ({tolerance:.0f}s).",
                },
            )

        # 5. Compute HMAC-SHA256 over timestamp + "." + raw_body
        message_to_sign = timestamp_str.encode("utf-8") + b"." + raw_body
        computed_hex = hmac.new(secret_bytes, message_to_sign, hashlib.sha256).hexdigest()
        expected_sig = f"sha256={computed_hex}"

        # 6. Constant-time comparison
        if not hmac.compare_digest(expected_sig, signature_header.strip()):
            logger.warning("notification_webhook_signature_mismatch")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "INVALID_SIGNATURE", "message": "Webhook HMAC signature verification failed."},
            )

        return parsed_ts

    def process_webhook(
        self,
        session: Session,
        raw_body: bytes,
        timestamp_header: str | None,
        signature_header: str | None,
        event_id_header: str | None = None,
    ) -> NotificationWebhookResponse:
        """Verify signature, validate payload, deduplicate event, and update notification state."""
        # Step 1: Cryptographic signature verification before trusting payload
        header_ts = self.verify_signature(
            raw_body=raw_body,
            timestamp_header=timestamp_header,
            signature_header=signature_header,
        )

        # Step 2: Safe JSON parse
        try:
            payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_PAYLOAD", "message": "Malformed JSON payload."},
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_PAYLOAD", "message": "Payload must be a JSON object."},
            )

        # Step 3: Extract & validate fields
        # DEF-02: Require and strictly validate both X-Event-ID header and payload eventId
        if not event_id_header or not event_id_header.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "MISSING_EVENT_ID", "message": "X-Event-ID header is required."},
            )

        payload_event_id = str(payload.get("eventId") or "").strip()
        if not payload_event_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "MISSING_EVENT_ID", "message": "eventId field is required in payload."},
            )

        clean_header_id = event_id_header.strip()
        if clean_header_id != payload_event_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "EVENT_ID_MISMATCH",
                    "message": f"X-Event-ID header '{clean_header_id}' does not match payload eventId '{payload_event_id}'.",
                },
            )
        event_id = payload_event_id

        event_type = str(payload.get("eventType") or "").strip()
        message_id = str(payload.get("messageId") or "").strip()
        service_id = str(payload.get("serviceId") or "").strip()
        incoming_status = str(payload.get("status") or "").strip().upper()
        channel = str(payload.get("channel") or "").strip().upper()
        provider_message_id = payload.get("providerMessageId")
        if provider_message_id is not None:
            provider_message_id = str(provider_message_id).strip()
        failure_info = payload.get("failure")
        failure_reason = None
        if isinstance(failure_info, dict):
            failure_reason = str(failure_info.get("message") or failure_info.get("code") or "")[:256]
        elif failure_info:
            failure_reason = str(failure_info)[:256]

        if event_type != "message.status_changed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_EVENT_TYPE", "message": f"Unsupported eventType '{event_type}'."},
            )
        if not message_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "MISSING_MESSAGE_ID", "message": "messageId field is required."},
            )
        if not service_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "MISSING_SERVICE_ID", "message": "serviceId field is required."},
            )

        # Status validation
        allowed_statuses = set(VALID_STATUSES)
        if self._settings.environment in {"development", "test"}:
            allowed_statuses.add("MOCK_SENT")
        if incoming_status not in allowed_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "UNSUPPORTED_STATUS", "message": f"Unsupported status '{incoming_status}'."},
            )

        # Channel validation
        if channel not in {"SMS", "WHATSAPP", "EMAIL"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "UNSUPPORTED_CHANNEL", "message": f"Unsupported channel '{channel}'."},
            )

        # Occurred at timestamp
        occurred_at = header_ts
        occurred_at_str = payload.get("occurredAt")
        if occurred_at_str:
            try:
                parsed_occ = datetime.fromisoformat(str(occurred_at_str).replace("Z", "+00:00"))
                occurred_at = parsed_occ if parsed_occ.tzinfo else parsed_occ.replace(tzinfo=UTC)
            except Exception:
                pass

        # Step 4: Durable database deduplication check
        existing_evt = session.execute(
            select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == event_id)
        ).scalar_one_or_none()
        if existing_evt is not None:
            logger.info("notification_webhook_duplicate_event_received", extra={"event_id": event_id})
            return NotificationWebhookResponse(
                status="duplicate",
                event_id=event_id,
                message_id=message_id,
                reason="Event ID already processed",
            )

        now = datetime.now(UTC)

        # Step 5: Lookup target message to resolve tenant context securely
        msg_stmt = select(NotificationMessage).where(NotificationMessage.message_id == message_id)
        notification_msg = session.execute(msg_stmt).scalar_one_or_none()

        # Fallback: check OtpChallenge metadata if notification_messages was not previously populated
        otp_challenge: OtpChallenge | None = None
        resolved_tenant_id: UUID | None = None

        if notification_msg is not None:
            resolved_tenant_id = notification_msg.tenant_id
            if notification_msg.challenge_id is not None:
                otp_challenge = session.execute(
                    select(OtpChallenge).where(OtpChallenge.id == notification_msg.challenge_id)
                ).scalar_one_or_none()
        else:
            # Query OtpChallenge for matching notification_message_id in metadata_json
            # Note: Never trust tenant ID supplied by webhook body
            challenges = session.execute(
                select(OtpChallenge).where(OtpChallenge.metadata_json.isnot(None))
            ).scalars().all()
            for c in challenges:
                if c.metadata_json and c.metadata_json.get("notification_message_id") == message_id:
                    otp_challenge = c
                    resolved_tenant_id = c.tenant_id
                    break

        # Step 5: Early arrival / Pending correlation handling (DEF-01)
        # If the outbound HTTP call finishes slightly after the webhook arrives,
        # persist the event as pending ("received") with tenant_id=None.
        # It will be reconciled when the outbound message is recorded or during status check.
        if notification_msg is None and otp_challenge is None:
            logger.info(
                "notification_webhook_early_arrival_pending_correlation",
                extra={"message_id": message_id, "event_id": event_id},
            )
            webhook_record = NotificationWebhookEvent(
                event_id=event_id,
                event_type=event_type,
                service_id=service_id,
                message_id=message_id,
                tenant_id=None,
                status=incoming_status,
                channel=channel,
                provider_message_id=provider_message_id,
                payload=payload,
                processing_status="received",
                error_message="Pending outbound message correlation reconciliation.",
                occurred_at=occurred_at,
                created_at=now,
                processed_at=None,
            )
            session.add(webhook_record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return NotificationWebhookResponse(
                    status="duplicate",
                    event_id=event_id,
                    message_id=message_id,
                    reason="Event ID already processed concurrently",
                )
            except Exception as db_exc:
                session.rollback()
                logger.error("notification_webhook_early_arrival_commit_failed", extra={"error": str(db_exc), "event_id": event_id})
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error": "DATABASE_ERROR", "message": "Failed to record webhook event in database."},
                ) from db_exc

            return NotificationWebhookResponse(
                status="received",
                event_id=event_id,
                message_id=message_id,
                reason="Event received and pending correlation",
            )

        # Reconcile any prior pending early-arriving events for this message_id
        self.reconcile_pending_events(session, message_id, commit=False)

        # Step 6: Apply Status Transitions Safely
        if notification_msg is not None:
            current_status = notification_msg.status
            should_update_status = True

            # Terminal protection:
            if current_status in TERMINAL_STATUSES:
                # Only DELIVERED -> READ is permitted forward progression from a terminal state
                if current_status == "DELIVERED" and incoming_status == "READ":
                    should_update_status = True
                else:
                    # Do not allow out-of-order or regressive event to downgrade terminal state
                    should_update_status = False
                    logger.info(
                        "notification_webhook_terminal_status_preserved",
                        extra={
                            "message_id": message_id,
                            "current_status": current_status,
                            "ignored_incoming_status": incoming_status,
                        },
                    )

            # Out-of-order timestamp check
            stored_occ = _normalize_utc(notification_msg.occurred_at)
            incoming_occ = _normalize_utc(occurred_at)
            if (
                should_update_status
                and current_status != "QUEUED"
                and stored_occ is not None
                and incoming_occ is not None
                and incoming_occ < stored_occ
            ):
                should_update_status = False
                logger.info(
                    "notification_webhook_stale_timestamp_ignored",
                    extra={
                        "message_id": message_id,
                        "stored_occurred_at": str(stored_occ),
                        "incoming_occurred_at": str(incoming_occ),
                    },
                )

            if should_update_status:
                notification_msg.status = incoming_status
                notification_msg.occurred_at = occurred_at
                notification_msg.updated_at = now
                if provider_message_id:
                    notification_msg.provider_message_id = provider_message_id
                if failure_reason:
                    notification_msg.failure_reason = failure_reason

        # Also update linked OTP challenge if present
        if otp_challenge is not None:
            curr_meta = dict(otp_challenge.metadata_json or {})
            curr_meta["notification_status"] = incoming_status
            if provider_message_id:
                curr_meta["provider_message_id"] = provider_message_id
            if failure_reason:
                curr_meta["notification_failure"] = failure_reason
            otp_challenge.metadata_json = curr_meta

        # Step 7: Record webhook event transactionally
        webhook_record = NotificationWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            service_id=service_id,
            message_id=message_id,
            tenant_id=resolved_tenant_id,
            status=incoming_status,
            channel=channel,
            provider_message_id=provider_message_id,
            payload=payload,
            processing_status="processed",
            error_message=None,
            occurred_at=occurred_at,
            created_at=now,
            processed_at=now,
        )
        session.add(webhook_record)

        # Atomic commit with race-condition deduplication protection
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("notification_webhook_concurrent_duplicate_caught", extra={"event_id": event_id})
            return NotificationWebhookResponse(
                status="duplicate",
                event_id=event_id,
                message_id=message_id,
                reason="Event ID already processed concurrently",
            )
        except Exception as db_exc:
            session.rollback()
            logger.error("notification_webhook_commit_failed", extra={"error": str(db_exc), "event_id": event_id})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "DATABASE_ERROR", "message": "Failed to persist webhook event in database."},
            ) from db_exc

        return NotificationWebhookResponse(
            status="processed",
            event_id=event_id,
            message_id=message_id,
        )

    def reconcile_pending_events(self, session: Session, message_id: str, commit: bool = True) -> int:
        """Reconcile any pending/early webhook events for a given message_id into NotificationMessage.

        Durable, safe correlation strategy (DEF-01):
        - Reconciles events recorded with processing_status = 'received'
        - Resolves tenant_id securely from the platform record (never from untrusted webhook payload)
        - Applies terminal state protections and timestamp monotonicity checks
        - Updates NotificationMessage and linked OtpChallenge metadata
        - Marks reconciled events as processing_status = 'processed'
        """
        clean_id = (message_id or "").strip()
        if not clean_id:
            return 0

        pending_events_stmt = (
            select(NotificationWebhookEvent)
            .where(
                NotificationWebhookEvent.message_id == clean_id,
                NotificationWebhookEvent.processing_status == "received",
            )
            .order_by(
                NotificationWebhookEvent.occurred_at.asc(),
                NotificationWebhookEvent.created_at.asc(),
            )
        )
        pending_events = session.execute(pending_events_stmt).scalars().all()
        if not pending_events:
            return 0

        # Lookup platform message or challenge
        msg_stmt = select(NotificationMessage).where(NotificationMessage.message_id == clean_id)
        notification_msg = session.execute(msg_stmt).scalar_one_or_none()

        otp_challenge: OtpChallenge | None = None
        resolved_tenant_id: UUID | None = None

        if notification_msg is not None:
            resolved_tenant_id = notification_msg.tenant_id
            if notification_msg.challenge_id is not None:
                otp_challenge = session.execute(
                    select(OtpChallenge).where(OtpChallenge.id == notification_msg.challenge_id)
                ).scalar_one_or_none()
        else:
            challenges = session.execute(
                select(OtpChallenge).where(OtpChallenge.metadata_json.isnot(None))
            ).scalars().all()
            for c in challenges:
                if c.metadata_json and c.metadata_json.get("notification_message_id") == clean_id:
                    otp_challenge = c
                    resolved_tenant_id = c.tenant_id
                    break

        if notification_msg is None and otp_challenge is None:
            return 0

        now = datetime.now(UTC)
        reconciled_count = 0

        for event in pending_events:
            event.tenant_id = resolved_tenant_id
            incoming_status = event.status
            provider_message_id = event.provider_message_id
            failure_reason = None
            if event.payload and isinstance(event.payload, dict):
                failure_info = event.payload.get("failure")
                if isinstance(failure_info, dict):
                    failure_reason = str(failure_info.get("message") or failure_info.get("code") or "")[:256]
                elif failure_info:
                    failure_reason = str(failure_info)[:256]

            # Status update on notification_msg
            if notification_msg is not None:
                current_status = notification_msg.status
                should_update_status = True

                if current_status in TERMINAL_STATUSES:
                    if current_status == "DELIVERED" and incoming_status == "READ":
                        should_update_status = True
                    else:
                        should_update_status = False

                stored_occ = _normalize_utc(notification_msg.occurred_at)
                incoming_occ = _normalize_utc(event.occurred_at)
                if (
                    should_update_status
                    and current_status != "QUEUED"
                    and stored_occ is not None
                    and incoming_occ is not None
                    and incoming_occ < stored_occ
                ):
                    should_update_status = False

                if should_update_status:
                    notification_msg.status = incoming_status
                    notification_msg.occurred_at = event.occurred_at
                    notification_msg.updated_at = now
                    if provider_message_id:
                        notification_msg.provider_message_id = provider_message_id
                    if failure_reason:
                        notification_msg.failure_reason = failure_reason

            # Status update on linked OtpChallenge
            if otp_challenge is not None:
                curr_meta = dict(otp_challenge.metadata_json or {})
                curr_meta["notification_status"] = incoming_status
                if provider_message_id:
                    curr_meta["provider_message_id"] = provider_message_id
                if failure_reason:
                    curr_meta["notification_failure"] = failure_reason
                otp_challenge.metadata_json = curr_meta

            event.processing_status = "processed"
            event.processed_at = now
            event.error_message = None
            reconciled_count += 1

        if commit:
            try:
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.error(
                    "notification_webhook_reconciliation_commit_failed",
                    extra={"error": str(exc), "message_id": clean_id},
                )
                return 0
        else:
            session.flush()

        logger.info(
            "notification_webhook_reconciled_pending_events",
            extra={"message_id": clean_id, "count": reconciled_count},
        )
        return reconciled_count
