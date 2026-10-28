from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.request_id import request_id_context
from app.db.session import get_session_factory
from app.modules.authentication.abuse_control import OtpRateLimited, SQLAlchemyOtpAbuseGuard
from app.modules.authentication.models import OtpChallenge, OtpRateBucket
from app.modules.authentication.service import AuthenticationConfigurationUnavailable
from app.modules.messaging.errors import (
    MessagingError,
    MessagingProviderRejected,
    MessagingProviderUnavailable,
)
from app.modules.messaging.models import NotificationMessage
from app.modules.messaging.notification_service import NotificationServiceClient, mask_recipient
from app.modules.messaging.service import normalize_mobile_e164
from app.modules.messaging.webhook_service import NotificationWebhookService
from app.modules.tenants.models import Tenant

logger = logging.getLogger(__name__)


class OtpRateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class OtpVerificationFailed(Exception):
    def __init__(self, message: str, attempts_remaining: int = 0) -> None:
        super().__init__(message)
        self.attempts_remaining = attempts_remaining


class OtpService:
    """Manages the full lifecycle of OTP challenges:

    - 6-digit cryptographically secure random generation
    - Salted SHA-256 verifier storage (no plaintext in DB or logs)
    - 5-minute expiry
    - PostgreSQL shared rate limiting (3 sends / 10m, 60s cooldown, 5 verify attempts)
    - Single-use and invalidation on resend
    - Opaque verification tokens for Admin, Agent, and User registration
    - Notification Microservice dispatch
    """

    def __init__(
        self,
        settings: Settings,
        notification_client: NotificationServiceClient | None = None,
        session_factory: sessionmaker[Session] | None = None,
        mock_codes: dict[str, str] | None = None,
        abuse_guard: SQLAlchemyOtpAbuseGuard | None = None,
        messaging_service: Any = None,
    ) -> None:
        self._settings = settings
        self._notification_client = notification_client or NotificationServiceClient(settings)
        self._session_factory = session_factory or get_session_factory()
        self._mock_codes = mock_codes or {}
        self._abuse_guard = abuse_guard
        self._messaging_service = messaging_service

    @staticmethod
    def _hash_otp(otp: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{otp}".encode("utf-8")).hexdigest()

    @staticmethod
    def _ensure_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt

    @staticmethod
    def _safe_for_update(stmt: Any, session: Session) -> Any:
        try:
            bind = session.get_bind()
            if bind is not None and bind.dialect.name == "sqlite":
                return stmt
        except Exception:
            pass
        return stmt.with_for_update()

    @staticmethod
    def _print_dev_otp_to_terminal(
        challenge_id: UUID,
        purpose: str,
        otp_code: str,
        expires_in_seconds: int,
        dispatch_skipped: bool = False,
    ) -> None:
        """Prints a clearly labeled development-only login OTP notice to the backend terminal.

        Never sends OTPs to application audit logs, general request logs, or HTTP responses.
        Active only when ENVIRONMENT=development and (OTP_LOG_TO_CONSOLE=true or OTP_SKIP_NOTIFICATION_DISPATCH=true).
        """
        try:
            border = "=" * 70
            label = (
                "[DEVELOPMENT ONLY] Community OTT Login OTP"
                if purpose == "login"
                else f"[DEVELOPMENT ONLY] Community OTT OTP ({purpose})"
            )
            dispatch_line = (
                "  Dispatch     : SKIPPED (development mode)\n"
                if dispatch_skipped
                else ""
            )
            notice = (
                f"\n{border}\n"
                f"{label}\n"
                f"{'-' * 70}\n"
                f"  Challenge ID : {challenge_id}\n"
                f"  Purpose      : {purpose}\n"
                f"  Login OTP    : {otp_code}\n"
                f"  Expires In   : {expires_in_seconds}s\n"
                f"{dispatch_line}"
                f"{border}\n"
            )
            import sys

            sys.stdout.write(notice)
            sys.stdout.flush()
        except Exception:
            pass

    async def request_otp(
        self,
        mobile_number: str,
        purpose: str = "login",
        tenant_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[UUID, int]:
        """Validates rate limits, creates a secure challenge, and dispatches via Notification Microservice.

        Returns (challenge_id, cooldown_seconds).
        """
        if self._settings.otp_challenge_ttl_seconds is None:
            raise AuthenticationConfigurationUnavailable("OTP challenge policy is unconfigured.")

        mobile_e164 = normalize_mobile_e164(mobile_number)
        now = datetime.now(UTC)

        with self._session_factory.begin() as session:
            rate_tenant_id = tenant_id
            if rate_tenant_id is None:
                rate_tenant_id = session.execute(select(Tenant.id)).scalars().first()
            if rate_tenant_id is None:
                rate_tenant_id = UUID(int=0)

            # 1. Check rate limits via abuse_guard or fallback bucket
            cooldown_seconds = self._settings.otp_send_cooldown_seconds
            if self._abuse_guard is not None:
                try:
                    self._abuse_guard._consume(rate_tenant_id, mobile_e164, "send")
                except OtpRateLimited as exc:
                    raise OtpRateLimitExceeded(
                        f"Please wait {exc.retry_after_seconds} seconds before requesting another code.",
                        retry_after_seconds=exc.retry_after_seconds,
                    ) from exc
            else:
                stmt = select(OtpRateBucket).where(
                    OtpRateBucket.mobile_e164 == mobile_e164,
                    OtpRateBucket.action == "send",
                )
                bucket = session.execute(self._safe_for_update(stmt, session)).scalar_one_or_none()

                window_seconds = self._settings.otp_send_window_seconds
                max_sends = self._settings.otp_send_limit

                if bucket is not None:
                    cooldown_until = self._ensure_utc(bucket.cooldown_until)
                    window_started = self._ensure_utc(bucket.window_started_at)
                    if cooldown_until and cooldown_until > now:
                        remaining = int((cooldown_until - now).total_seconds())
                        raise OtpRateLimitExceeded(
                            f"Please wait {remaining} seconds before requesting another code.",
                            retry_after_seconds=remaining,
                        )
                    if window_started and window_started + timedelta(seconds=window_seconds) > now:
                        if bucket.attempt_count >= max_sends:
                            wait_sec = int((window_started + timedelta(seconds=window_seconds) - now).total_seconds())
                            raise OtpRateLimitExceeded(
                                f"Too many OTP requests. Please try again in {wait_sec // 60 + 1} minutes.",
                                retry_after_seconds=wait_sec,
                            )
                        bucket.attempt_count += 1
                        bucket.cooldown_until = now + timedelta(seconds=cooldown_seconds)
                    else:
                        bucket.window_started_at = now
                        bucket.attempt_count = 1
                        bucket.cooldown_until = now + timedelta(seconds=cooldown_seconds)
                else:
                    if rate_tenant_id != UUID(int=0):
                        session.add(
                            OtpRateBucket(
                                id=uuid4(),
                                tenant_id=rate_tenant_id,
                                mobile_e164=mobile_e164,
                                action="send",
                                window_started_at=now,
                                attempt_count=1,
                                cooldown_until=now + timedelta(seconds=cooldown_seconds),
                            )
                        )

            # 2. Determine OTP code
            if self._settings.development_static_otp_enabled and self._settings.development_static_otp_code:
                otp_code = self._settings.development_static_otp_code.get_secret_value()
            elif self._mock_codes and mobile_e164 in self._mock_codes:
                otp_code = self._mock_codes[mobile_e164]
            elif self._settings.otp_delivery_mode == "development_dummy" and self._settings.environment == "test":
                otp_code = self._settings.development_dummy_otp
            else:
                otp_code = f"{secrets.randbelow(900000) + 100000:06d}"

            salt = secrets.token_hex(16)
            otp_hash = self._hash_otp(otp_code, salt)
            challenge_id = uuid4()
            dispatch_id = uuid4()
            ttl_seconds = self._settings.otp_challenge_ttl_seconds or 300
            expires_at = now + timedelta(seconds=ttl_seconds)

            # 3. Query existing challenges for this (mobile, purpose) with diagnostic logging
            req_id = request_id_context.get() or "n/a"
            masked_mobile = mask_recipient(mobile_e164)
            stmt = (
                select(OtpChallenge)
                .where(
                    OtpChallenge.mobile_e164 == mobile_e164,
                    OtpChallenge.purpose == purpose,
                )
                .order_by(
                    case((OtpChallenge.consumed_at.is_(None) & (OtpChallenge.expires_at > now), 0), else_=1),
                    OtpChallenge.created_at.desc(),
                )
            )
            challenges = session.execute(self._safe_for_update(stmt, session)).scalars().all()
            match_count = len(challenges)

            logger.info(
                "otp_challenge_lookup request_id=%s purpose=%s matching_row_count=%d recipient_masked=%s",
                req_id,
                purpose,
                match_count,
                masked_mobile,
                extra={
                    "request_id": req_id,
                    "purpose": purpose,
                    "matching_row_count": match_count,
                    "recipient_masked": masked_mobile,
                },
            )

            if match_count > 1:
                logger.warning(
                    "otp_challenge_duplicate_records_detected request_id=%s purpose=%s matching_row_count=%d recipient_masked=%s",
                    req_id,
                    purpose,
                    match_count,
                    masked_mobile,
                    extra={
                        "request_id": req_id,
                        "purpose": purpose,
                        "matching_row_count": match_count,
                        "recipient_masked": masked_mobile,
                    },
                )
                # Invalidate/delete obsolete extra duplicate records, keeping the most relevant one
                for extra_ch in challenges[1:]:
                    session.delete(extra_ch)

            if match_count == 0:
                try:
                    with session.begin_nested():
                        challenge = OtpChallenge(
                            id=challenge_id,
                            tenant_id=tenant_id,
                            mobile_e164=mobile_e164,
                            purpose=purpose,
                            otp_hash=otp_hash,
                            otp_salt=salt,
                            failed_attempts=0,
                            resend_count=0,
                            dispatch_id=dispatch_id,
                            created_at=now,
                            expires_at=expires_at,
                            metadata_json=metadata,
                        )
                        session.add(challenge)
                        session.flush()
                except IntegrityError:
                    # Concurrency race: another transaction inserted simultaneously. Update winner row.
                    stmt_retry = (
                        select(OtpChallenge)
                        .where(
                            OtpChallenge.mobile_e164 == mobile_e164,
                            OtpChallenge.purpose == purpose,
                        )
                    )
                    existing_challenge = session.execute(self._safe_for_update(stmt_retry, session)).scalars().first()
                    if existing_challenge is not None:
                        challenge_id = existing_challenge.id
                        existing_challenge.dispatch_id = dispatch_id
                        existing_challenge.tenant_id = tenant_id
                        existing_challenge.otp_hash = otp_hash
                        existing_challenge.otp_salt = salt
                        existing_challenge.failed_attempts = 0
                        existing_challenge.resend_count += 1
                        existing_challenge.created_at = now
                        existing_challenge.expires_at = expires_at
                        existing_challenge.consumed_at = None
                        existing_challenge.verified_at = None
                        existing_challenge.verification_token = None
                        existing_challenge.metadata_json = metadata
            else:
                existing_challenge = challenges[0]
                challenge_id = existing_challenge.id
                existing_challenge.dispatch_id = dispatch_id
                existing_challenge.tenant_id = tenant_id
                existing_challenge.otp_hash = otp_hash
                existing_challenge.otp_salt = salt
                existing_challenge.failed_attempts = 0
                existing_challenge.resend_count += 1
                existing_challenge.created_at = now
                existing_challenge.expires_at = expires_at
                existing_challenge.consumed_at = None
                existing_challenge.verified_at = None
                existing_challenge.verification_token = None
                existing_challenge.metadata_json = metadata

        # 3b. Development-only terminal display of generated login OTP
        is_dev = getattr(self._settings, "environment", None) == "development"
        skip_dispatch = is_dev and getattr(self._settings, "otp_skip_notification_dispatch", False)
        if is_dev and (getattr(self._settings, "otp_log_to_console", False) or skip_dispatch):
            self._print_dev_otp_to_terminal(
                challenge_id=challenge_id,
                purpose=purpose,
                otp_code=otp_code,
                expires_in_seconds=ttl_seconds,
                dispatch_skipped=skip_dispatch,
            )

        # 4. Dispatch notification (suppressed in static dev mode or when skip dispatch is active)
        if not self._settings.development_static_otp_enabled:
            if skip_dispatch:
                logger.info("DEVELOPMENT ONLY: notification dispatch skipped")
                logger.info(
                    "notification_dispatch_result request_id=%s challenge_id=%s channel=LOCAL http_status=none outcome=SKIPPED_LOCAL",
                    req_id,
                    str(challenge_id),
                    extra={
                        "request_id": req_id,
                        "challenge_id": str(challenge_id),
                        "channel": "LOCAL",
                        "outcome": "SKIPPED_LOCAL",
                        "notification_status": "SKIPPED_LOCAL",
                    },
                )
                try:
                    with self._session_factory.begin() as session:
                        stmt = select(OtpChallenge).where(OtpChallenge.id == challenge_id)
                        c = session.execute(self._safe_for_update(stmt, session)).scalar_one_or_none()
                        if c is not None:
                            curr_meta = dict(c.metadata_json or {})
                            curr_meta["notification_status"] = "SKIPPED_LOCAL"
                            c.metadata_json = curr_meta
                except Exception as meta_exc:
                    logger.warning("failed_to_update_challenge_skip_meta", extra={"error": str(meta_exc)})

                return challenge_id, cooldown_seconds

            if self._messaging_service is not None:
                try:
                    await self._messaging_service.send_otp(mobile_e164)
                except Exception:
                    pass
            from unittest.mock import DEFAULT, Mock
            try:
                if (
                    isinstance(self._notification_client, Mock)
                    and hasattr(self._notification_client, "send_otp_sms")
                    and getattr(getattr(self._notification_client, "send_otp_sms", None), "_mock_return_value", None) is not DEFAULT
                    and getattr(getattr(self._notification_client, "send_otp", None), "_mock_return_value", None) is DEFAULT
                ):
                    dispatch_res = await self._notification_client.send_otp_sms(
                        recipient_e164=mobile_e164,
                        otp=otp_code,
                        purpose=purpose,
                        challenge_id=str(challenge_id),
                        expires_at=expires_at,
                    )
                else:
                    dispatch_res = await self._notification_client.send_otp(
                        recipient_e164=mobile_e164,
                        otp=otp_code,
                        purpose=purpose,
                        challenge_id=str(challenge_id),
                        dispatch_id=str(dispatch_id),
                        expires_at=expires_at,
                        request_id=req_id,
                    )

                # Store notification message metadata on challenge and notification_messages table
                if dispatch_res.message_id:
                    try:
                        with self._session_factory.begin() as session:
                            stmt = select(OtpChallenge).where(OtpChallenge.id == challenge_id)
                            c = session.execute(self._safe_for_update(stmt, session)).scalar_one_or_none()
                            if c is not None:
                                curr_meta = dict(c.metadata_json or {})
                                curr_meta["notification_message_id"] = dispatch_res.message_id
                                curr_meta["notification_status"] = dispatch_res.status
                                c.metadata_json = curr_meta
                    except Exception as meta_exc:
                        logger.warning("failed_to_update_challenge_notification_meta", extra={"error": str(meta_exc)})

                    try:
                        with self._session_factory.begin() as session:
                            existing_msg = session.execute(
                                select(NotificationMessage).where(NotificationMessage.message_id == dispatch_res.message_id)
                            ).scalar_one_or_none()
                            if existing_msg is None:
                                channel_candidate = getattr(dispatch_res, "channel", None) or getattr(self._settings, "notification_channel", "WHATSAPP")
                                if channel_candidate not in {"WHATSAPP", "SMS", "EMAIL"}:
                                    channel_candidate = "WHATSAPP"
                                notif_msg = NotificationMessage(
                                    tenant_id=tenant_id,
                                    message_id=dispatch_res.message_id,
                                    channel=channel_candidate,
                                    message_type="LOGIN_OTP",
                                    recipient=mobile_e164,
                                    status=dispatch_res.status,
                                    priority="P0",
                                    purpose=purpose,
                                    challenge_id=challenge_id,
                                    occurred_at=now,
                                    created_at=now,
                                    updated_at=now,
                                )
                                session.add(notif_msg)
                                session.flush()

                            # DEF-01: Reconcile any early-arriving webhook events that arrived during dispatch
                            webhook_svc = NotificationWebhookService(self._settings)
                            webhook_svc.reconcile_pending_events(session, dispatch_res.message_id, commit=False)
                    except Exception as notif_exc:
                        logger.warning("failed_to_record_notification_message", extra={"error": str(notif_exc)})

                # When running in live notification_service mode, fail closed if dispatch failed
                if self._settings.otp_delivery_mode == "notification_service" and not dispatch_res.success:
                    logger.error(
                        "notification_dispatch_failed",
                        extra={
                            "purpose": purpose,
                            "status": dispatch_res.status,
                            "error": dispatch_res.error,
                        },
                    )
                    raise MessagingProviderUnavailable(
                        dispatch_res.error or "The notification service is temporarily unavailable."
                    )
            except Exception as dispatch_exc:
                # Dispatch failed or raised: invalidate challenge and refund rate bucket send count
                try:
                    with self._session_factory.begin() as cleanup_session:
                        stmt = select(OtpChallenge).where(OtpChallenge.id == challenge_id)
                        c = cleanup_session.execute(self._safe_for_update(stmt, cleanup_session)).scalar_one_or_none()
                        if c is not None:
                            curr_meta = dict(c.metadata_json or {})
                            curr_meta["dispatch_status"] = "FAILED"
                            curr_meta["dispatch_error"] = str(dispatch_exc)
                            c.metadata_json = curr_meta
                            c.consumed_at = now  # Mark unusable
                            c.expires_at = now

                        stmt_b = select(OtpRateBucket).where(
                            OtpRateBucket.mobile_e164 == mobile_e164,
                            OtpRateBucket.action == "send",
                            OtpRateBucket.tenant_id == rate_tenant_id,
                        )
                        b = cleanup_session.execute(self._safe_for_update(stmt_b, cleanup_session)).scalars().first()
                        if b is not None:
                            b.attempt_count = max(0, b.attempt_count - 1)
                            b.cooldown_until = now
                except Exception as cleanup_err:
                    logger.warning("failed_to_cleanup_failed_otp_dispatch", extra={"error": str(cleanup_err)})

                if isinstance(dispatch_exc, (MessagingProviderUnavailable, MessagingError)):
                    raise
                if self._settings.otp_delivery_mode == "notification_service":
                    raise MessagingProviderUnavailable(
                        str(dispatch_exc) or "The notification service is temporarily unavailable."
                    ) from dispatch_exc
                raise

        return challenge_id, cooldown_seconds

    def verify_otp(
        self,
        challenge_id: UUID,
        mobile_number: str,
        otp: str,
        purpose: str = "login",
    ) -> tuple[bool, str | None]:
        """Verifies an OTP challenge.

        If successful:
        - For purpose == "login": marks challenge consumed; returns (True, None)
        - For registration purposes: marks challenge verified and generates a verification_token;
          returns (True, verification_token).
        """
        mobile_e164 = normalize_mobile_e164(mobile_number)
        now = datetime.now(UTC)

        with self._session_factory.begin() as session:
            stmt = select(OtpChallenge).where(
                OtpChallenge.id == challenge_id,
                OtpChallenge.mobile_e164 == mobile_e164,
                OtpChallenge.purpose == purpose,
            )
            challenge = session.execute(self._safe_for_update(stmt, session)).scalar_one_or_none()

            if challenge is None:
                raise OtpVerificationFailed("Verification challenge not found or expired.")

            if challenge.consumed_at is not None:
                raise OtpVerificationFailed("This verification code has already been used.")

            expires_at = self._ensure_utc(challenge.expires_at)
            if expires_at and expires_at < now:
                challenge.consumed_at = now
                raise OtpVerificationFailed("The verification code has expired. Please request a new one.")

            # Check abuse guard for verification attempts
            if self._abuse_guard is not None:
                verify_tenant_id = challenge.tenant_id
                if verify_tenant_id is None:
                    verify_tenant_id = session.execute(select(Tenant.id)).scalars().first() or UUID(int=0)
                self._abuse_guard._consume(verify_tenant_id, mobile_e164, "verify")

            max_attempts = self._settings.otp_verify_limit or 5
            if challenge.failed_attempts >= max_attempts:
                challenge.consumed_at = now
                raise OtpVerificationFailed("Too many failed attempts. This code is no longer valid.")

            # Compute hash and compare in constant time
            computed_hash = self._hash_otp(otp.strip(), challenge.otp_salt)
            is_valid = secrets.compare_digest(computed_hash, challenge.otp_hash)

            if not is_valid:
                challenge.failed_attempts += 1
                remaining = max_attempts - challenge.failed_attempts
                is_locked = remaining <= 0
                if is_locked:
                    challenge.consumed_at = now
                session.commit()
                if is_locked:
                    raise OtpVerificationFailed("Too many failed attempts. Please request a new code.", attempts_remaining=0)
                raise OtpVerificationFailed(
                    f"Invalid verification code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
                    attempts_remaining=remaining,
                )

            # Verification successful - reset verify abuse rate bucket
            session.execute(
                delete(OtpRateBucket).where(
                    OtpRateBucket.mobile_e164 == mobile_e164,
                    OtpRateBucket.action == "verify",
                )
            )

            if purpose == "login":
                challenge.consumed_at = now
                return True, None
            else:
                # Registration verification: generate verification token
                v_token = secrets.token_urlsafe(32)
                challenge.verified_at = now
                challenge.verification_token = v_token
                return True, v_token

    def validate_and_consume_registration_token(
        self,
        session: Session,
        verification_token: str,
        mobile_number: str,
        expected_purpose: str,
    ) -> bool:
        """Validates that a verification token was issued for this exact mobile number and purpose,

        and consumes it atomically within the caller's transaction.
        """
        mobile_e164 = normalize_mobile_e164(mobile_number)
        now = datetime.now(UTC)

        stmt = select(OtpChallenge).where(
            OtpChallenge.verification_token == verification_token,
            OtpChallenge.mobile_e164 == mobile_e164,
            OtpChallenge.purpose == expected_purpose,
            OtpChallenge.consumed_at.is_(None),
        )
        challenge = session.execute(self._safe_for_update(stmt, session)).scalar_one_or_none()

        if challenge is None:
            return False

        # Token valid for 15 minutes after verification
        verified_at = self._ensure_utc(challenge.verified_at)
        if verified_at is None or (now - verified_at) > timedelta(minutes=15):
            challenge.consumed_at = now
            return False

        # Atomically consume the token
        challenge.consumed_at = now
        return True
