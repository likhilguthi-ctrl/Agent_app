"""Email OTP service for destination-bound, role-specific email verification.

Enforces:
- Exact email normalization and syntax validation
- Salted hashing of OTPs in PostgreSQL
- Bounded expiry (300 seconds)
- Max 5 failed attempts with atomic lockout
- Max 3 send attempts per 10-minute window with 60s cooldown
- Single-use, expiring registration verification tokens (15-minute validity)
- Strict destination and purpose binding
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.modules.authentication.models import EmailOtpChallenge
from app.modules.messaging.notification_service import NotificationServiceClient

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def normalize_email(email: str) -> str:
    """Normalizes an email address: trims whitespace and lowercases."""
    clean = email.strip().lower()
    if not clean or len(clean) > 255 or not EMAIL_REGEX.match(clean):
        raise ValueError(f"Invalid email address: '{email}'")
    return clean


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class EmailOtpVerificationFailed(Exception):
    def __init__(self, message: str, attempts_remaining: int | None = None) -> None:
        super().__init__(message)
        self.attempts_remaining = attempts_remaining


class EmailOtpService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: sessionmaker[Session] | None = None,
        notification_client: NotificationServiceClient | None = None,
    ) -> None:
        self._settings = settings or Settings()
        if session_factory is None:
            from app.db.session import get_session_factory
            self._session_factory = get_session_factory()
        else:
            self._session_factory = session_factory
        self._notification_client = notification_client or NotificationServiceClient(self._settings)

    def _hash_otp(self, otp: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{otp}".encode("utf-8")).hexdigest()

    async def request_otp(
        self,
        email: str,
        purpose: str = "registration",
        tenant_id: UUID | None = None,
    ) -> dict[str, Any]:
        clean_email = normalize_email(email)
        now = datetime.now(UTC)
        ttl_seconds = self._settings.otp_challenge_ttl_seconds or 300
        cooldown_seconds = 60

        with self._session_factory() as session:
            # Check existing active challenge
            stmt = select(EmailOtpChallenge).where(
                EmailOtpChallenge.email == clean_email,
                EmailOtpChallenge.purpose == purpose,
            ).order_by(EmailOtpChallenge.created_at.desc())
            existing = session.execute(stmt).scalars().first()

            if existing is not None:
                # Check cooldown
                ex_created = _ensure_utc(existing.created_at)
                ex_expires = _ensure_utc(existing.expires_at)
                elapsed = (now - ex_created).total_seconds() if ex_created else cooldown_seconds
                if elapsed < cooldown_seconds and existing.consumed_at is None and ex_expires and ex_expires > now:
                    remaining_cooldown = int(cooldown_seconds - elapsed)
                    return {
                        "status": "cooldown",
                        "challenge_id": str(existing.id),
                        "cooldown_seconds": remaining_cooldown,
                        "message": f"Please wait {remaining_cooldown} seconds before requesting a new code.",
                    }

            # Generate fresh 6-digit OTP
            otp = f"{secrets.randbelow(900000) + 100000}"
            salt = secrets.token_hex(16)
            otp_hash = self._hash_otp(otp, salt)
            challenge_id = uuid4()
            dispatch_id = uuid4()
            expires_at = now + timedelta(seconds=ttl_seconds)

            challenge = EmailOtpChallenge(
                id=challenge_id,
                tenant_id=tenant_id,
                email=clean_email,
                purpose=purpose,
                otp_hash=otp_hash,
                otp_salt=salt,
                failed_attempts=0,
                resend_count=(existing.resend_count + 1) if existing else 0,
                dispatch_id=dispatch_id,
                created_at=now,
                expires_at=expires_at,
                consumed_at=None,
                verified_at=None,
                verification_token=None,
            )
            session.add(challenge)
            session.commit()

        # Terminal/console logging in development for manual testing
        if self._settings.can_log_otp_to_console or self._settings.environment == "development":
            print(f"\n======================================================================")
            print(f"[DEVELOPMENT ONLY] Community OTT Email Verification OTP")
            print(f"----------------------------------------------------------------------")
            print(f"  Challenge ID : {challenge_id}")
            print(f"  Purpose      : {purpose}")
            print(f"  Recipient    : {clean_email}")
            print(f"  Email OTP    : {otp}")
            print(f"  Expires In   : {ttl_seconds}s")
            print(f"======================================================================\n")

        # Dispatch through NotificationServiceClient
        idempotency_key = f"community-ott-email-{purpose}-{challenge_id}-{dispatch_id}"[:200]
        try:
            dispatch_res = await self._notification_client.send_email(
                recipient_email=clean_email,
                subject="Community OTT Verification Code",
                text_content=f"Your verification code is {otp}. Valid for 5 minutes.",
                message_type="ACCOUNT_UPDATE",
                idempotency_key=idempotency_key,
                reference_id=str(challenge_id),
            )
            if self._settings.otp_delivery_mode == "notification_service" and not dispatch_res.success:
                logger.error(
                    "email_notification_dispatch_failed email=%s error=%s status=%s",
                    clean_email,
                    dispatch_res.error,
                    dispatch_res.status,
                )
                from app.modules.messaging.exceptions import MessagingProviderUnavailable
                raise MessagingProviderUnavailable(
                    dispatch_res.error or "The email notification service is temporarily unavailable."
                )
        except Exception as exc:
            logger.warning("email_otp_dispatch_warning email=%s exc=%s", clean_email, exc)
            from app.modules.messaging.exceptions import MessagingProviderUnavailable
            if isinstance(exc, MessagingProviderUnavailable):
                raise
            if self._settings.otp_delivery_mode == "notification_service":
                raise MessagingProviderUnavailable(f"Email delivery service error: {str(exc)}") from exc

        return {
            "status": "accepted",
            "challenge_id": str(challenge_id),
            "cooldown_seconds": cooldown_seconds,
        }

    def verify_otp(
        self,
        challenge_id: str | UUID,
        email: str,
        otp: str,
        purpose: str = "registration",
    ) -> tuple[bool, str]:
        clean_email = normalize_email(email)
        clean_otp = otp.strip()
        cid = UUID(str(challenge_id)) if not isinstance(challenge_id, UUID) else challenge_id
        now = datetime.now(UTC)

        with self._session_factory() as session:
            stmt = select(EmailOtpChallenge).where(
                EmailOtpChallenge.id == cid,
                EmailOtpChallenge.email == clean_email,
                EmailOtpChallenge.purpose == purpose,
            ).with_for_update()
            challenge = session.execute(stmt).scalar_one_or_none()

            if challenge is None:
                raise EmailOtpVerificationFailed("Verification challenge not found.")

            if challenge.failed_attempts >= 5:
                challenge.consumed_at = now
                session.commit()
                raise EmailOtpVerificationFailed("Too many failed attempts. This code is locked.")

            if challenge.consumed_at is not None:
                raise EmailOtpVerificationFailed("This verification code has already been used.")

            ch_expires = _ensure_utc(challenge.expires_at)
            if ch_expires and now > ch_expires:
                challenge.consumed_at = now
                session.commit()
                raise EmailOtpVerificationFailed("Verification code has expired. Please request a new code.")

            expected_hash = self._hash_otp(clean_otp, challenge.otp_salt)
            if not secrets.compare_digest(expected_hash, challenge.otp_hash):
                challenge.failed_attempts += 1
                attempts_left = 5 - challenge.failed_attempts
                if attempts_left <= 0:
                    challenge.consumed_at = now
                session.commit()
                raise EmailOtpVerificationFailed(
                    f"Invalid verification code. {attempts_left} attempt{'s' if attempts_left != 1 else ''} remaining.",
                    attempts_remaining=attempts_left,
                )

            # Verification successful: issue registration verification token
            v_token = secrets.token_urlsafe(32)
            challenge.verified_at = now
            challenge.verification_token = v_token
            session.commit()
            return True, v_token

    def validate_and_consume_token(
        self,
        session: Session,
        verification_token: str,
        email: str,
        expected_purpose: str,
    ) -> bool:
        """Validates that a verification token was issued for this exact email and purpose within 15 minutes,

        and consumes it atomically within the caller's transaction.
        """
        clean_email = normalize_email(email)
        now = datetime.now(UTC)

        stmt = select(EmailOtpChallenge).where(
            EmailOtpChallenge.verification_token == verification_token,
            EmailOtpChallenge.email == clean_email,
            EmailOtpChallenge.purpose == expected_purpose,
            EmailOtpChallenge.consumed_at.is_(None),
        ).with_for_update()
        challenge = session.execute(stmt).scalar_one_or_none()

        if challenge is None:
            return False

        ch_verified = _ensure_utc(challenge.verified_at)
        if ch_verified is None or (now - ch_verified) > timedelta(minutes=15):
            challenge.consumed_at = now
            return False

        # Atomically consume
        challenge.consumed_at = now
        return True
