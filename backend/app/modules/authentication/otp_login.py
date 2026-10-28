from __future__ import annotations

from typing import Protocol
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from app.core.tenant_context import TenantContext
from app.modules.identity.contracts import CommunityMembership, TenantMembershipLookup
from app.modules.messaging.service import MessagingService, normalize_mobile_e164


class LoginRejected(Exception):
    """Generic login failure that does not reveal account or tenant details."""

    def __init__(self) -> None:
        super().__init__("Login could not be completed.")


class OtpAbuseGuard(Protocol):
    """Legacy unit-test port; its split check/record calls are not production-safe."""

    async def check_send_allowed(self, tenant_id: UUID, mobile_e164: str) -> None: ...

    async def record_send_attempt(self, tenant_id: UUID, mobile_e164: str) -> None: ...

    async def check_verify_allowed(self, tenant_id: UUID, mobile_e164: str) -> None: ...

    async def record_failed_verification(self, tenant_id: UUID, mobile_e164: str) -> None: ...

    async def clear_failed_verifications(self, tenant_id: UUID, mobile_e164: str) -> None: ...


class SessionIssuer(Protocol):
    """Port for the reviewed session mechanism; no token/cookie policy is assumed here."""

    async def issue_for_active_membership(
        self,
        tenant: TenantContext,
        membership: CommunityMembership,
    ) -> object: ...


class OtpLoginCoordinator:
    """Legacy protocol-level test seam, not registered on the public API.

    Public authentication routes use OtpAuthenticationService, the persistent
    challenge/rate-limit/session implementation. Do not wire this coordinator to
    production endpoints: its interface predates challenge replay protection.
    """

    def __init__(
        self,
        messaging: MessagingService,
        memberships: TenantMembershipLookup,
        abuse_guard: OtpAbuseGuard,
        sessions: SessionIssuer,
    ) -> None:
        self._messaging = messaging
        self._memberships = memberships
        self._abuse_guard = abuse_guard
        self._sessions = sessions

    async def request_otp(self, tenant: TenantContext, mobile_number: str) -> None:
        mobile = normalize_mobile_e164(mobile_number)
        await self._abuse_guard.check_send_allowed(tenant.tenant_id, mobile)
        await self._abuse_guard.record_send_attempt(tenant.tenant_id, mobile)

        membership = await run_in_threadpool(
            self._memberships.find_by_tenant_and_mobile,
            tenant.tenant_id,
            mobile,
        )
        if membership is None or not self._is_active_in_tenant(membership, tenant):
            # Keep the outward operation generic: no SMS and no account-existence result.
            return None
        await self._messaging.send_otp(mobile)
        return None

    async def verify_otp_and_issue_session(
        self,
        tenant: TenantContext,
        mobile_number: str,
        otp: str,
    ) -> object:
        mobile = normalize_mobile_e164(mobile_number)
        await self._abuse_guard.check_verify_allowed(tenant.tenant_id, mobile)
        verified = await self._messaging.verify_otp(mobile, otp)
        if not verified:
            await self._abuse_guard.record_failed_verification(tenant.tenant_id, mobile)
            raise LoginRejected

        membership = await run_in_threadpool(
            self._memberships.find_by_tenant_and_mobile,
            tenant.tenant_id,
            mobile,
        )
        if membership is None or not self._is_active_in_tenant(membership, tenant):
            raise LoginRejected

        await self._abuse_guard.clear_failed_verifications(tenant.tenant_id, mobile)
        return await self._sessions.issue_for_active_membership(tenant, membership)

    @staticmethod
    def _is_active_in_tenant(
        membership: CommunityMembership,
        tenant: TenantContext,
    ) -> bool:
        return membership.tenant_id == tenant.tenant_id and membership.status == "active"
