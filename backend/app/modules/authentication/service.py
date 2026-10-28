from __future__ import annotations

import logging
import hmac
import secrets
from uuid import UUID, uuid4

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.modules.authentication.abuse_control import (
    AbuseControlUnavailable,
    OtpRateLimited,
    SQLAlchemyOtpAbuseGuard,
)
from app.modules.authentication.otp_login import LoginRejected
from app.modules.authentication.repository import (
    AuthenticationStorageUnavailable,
    IssuedSession,
    SQLAlchemyAuthenticationRepository,
)
from app.modules.identity.contracts import TenantMembershipLookup
from app.modules.messaging.errors import MessagingError
from app.modules.messaging.service import MessagingService, normalize_mobile_e164

logger = logging.getLogger(__name__)


class AuthenticationConfigurationUnavailable(Exception):
    """Required OTP policy is not configured; authentication must remain disabled."""


class OtpAuthenticationService:
    """Community mobile-OTP application service, always bound to trusted tenant context."""

    def __init__(
        self,
        *,
        settings: Settings,
        messaging: MessagingService,
        memberships: TenantMembershipLookup,
        abuse_guard: SQLAlchemyOtpAbuseGuard,
        repository: SQLAlchemyAuthenticationRepository,
    ) -> None:
        self._settings = settings
        self._messaging = messaging
        self._memberships = memberships
        self._abuse_guard = abuse_guard
        self._repository = repository

    async def request_otp(self, tenant: TenantContext, mobile_number: str) -> UUID:
        mobile = normalize_mobile_e164(mobile_number)
        challenge_id = uuid4()
        if self._settings.otp_challenge_ttl_seconds is None:
            raise AuthenticationConfigurationUnavailable

        membership = await run_in_threadpool(
            self._memberships.find_by_tenant_and_mobile,
            tenant.tenant_id,
            mobile,
        )
        if (
            membership is None
            or membership.tenant_id != tenant.tenant_id
            or membership.status != "active"
        ):
            # Same accepted result as an eligible mobile; no account enumeration signal.
            return challenge_id

        try:
            await self._abuse_guard.authorize_send(tenant.tenant_id, mobile)
        except (OtpRateLimited, AbuseControlUnavailable) as exc:
            # Keep public request responses enumeration-resistant and fail closed on storage errors.
            logger.warning(
                "otp_send_suppressed",
                extra={"reason": "rate_limited" if isinstance(exc, OtpRateLimited) else "abuse_store_unavailable"},
            )
            return challenge_id

        await run_in_threadpool(
            self._repository.create_challenge,
            tenant.tenant_id,
            mobile,
            challenge_id,
            self._settings.otp_challenge_ttl_seconds,
        )
        if not self._settings.development_static_otp_enabled:
            try:
                accepted = await self._messaging.send_otp(mobile)
            except MessagingError as exc:
                await run_in_threadpool(
                    self._repository.revoke_challenge, tenant.tenant_id, challenge_id
                )
                # Do not reveal account existence or provider configuration to unauthenticated callers.
                logger.warning(
                    "otp_delivery_unavailable",
                    extra={"provider_error_type": type(exc).__name__},
                )
                return challenge_id
            if not accepted:
                await run_in_threadpool(
                    self._repository.revoke_challenge, tenant.tenant_id, challenge_id
                )
        return challenge_id

    async def verify_otp(
        self,
        tenant: TenantContext,
        *,
        mobile_number: str,
        otp: str,
        challenge_id: UUID,
    ) -> tuple[IssuedSession, str]:
        mobile = normalize_mobile_e164(mobile_number)
        if not await run_in_threadpool(
            self._repository.is_challenge_active,
            tenant.tenant_id,
            mobile,
            challenge_id,
        ):
            raise LoginRejected
        await self._abuse_guard.authorize_verify_attempt(tenant.tenant_id, mobile)

        if self._settings.development_static_otp_enabled:
            configured_code = self._settings.development_static_otp_code
            verified = configured_code is not None and hmac.compare_digest(
                configured_code.get_secret_value(), otp
            )
        else:
            verified = await self._messaging.verify_otp(mobile, otp)
        if not verified:
            raise LoginRejected

        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        issued = await run_in_threadpool(
            self._repository.consume_challenge_and_issue_session,
            tenant_id=tenant.tenant_id,
            mobile_e164=mobile,
            challenge_id=challenge_id,
            raw_session_token=raw_token,
            csrf_token=csrf_token,
            session_ttl_seconds=self._settings.auth_session_ttl_seconds,
        )
        if issued is None:
            # Covers replay, expired challenge and membership removal during verification.
            raise LoginRejected
        return issued, raw_token
