from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.tenant_context import TenantContext
from app.modules.authentication.otp_login import (
    LoginRejected,
    OtpLoginCoordinator,
)
from app.modules.identity.contracts import CommunityMembership
from app.modules.messaging.service import MessagingService
from app.modules.messaging.testing import MockMessagingProvider


MOBILE = "+14155550123"
TEST_OTP = "test-otp-for-automated-tests"


def tenant_context(tenant_id: UUID | None = None) -> TenantContext:
    identifier = tenant_id or uuid4()
    return TenantContext(
        tenant_id=identifier,
        tenant_slug=f"community-{str(identifier)[:8]}",
        hostname=f"{str(identifier)[:8]}.example.test",
        domain_id=uuid4(),
    )


class FakeMembershipLookup:
    def __init__(self, memberships: dict[tuple[UUID, str], CommunityMembership]) -> None:
        self.memberships = memberships
        self.lookups: list[tuple[UUID, str]] = []

    def find_by_tenant_and_mobile(
        self,
        tenant_id: UUID,
        mobile_e164: str,
    ) -> CommunityMembership | None:
        self.lookups.append((tenant_id, mobile_e164))
        return self.memberships.get((tenant_id, mobile_e164))


class FakeAbuseGuard:
    def __init__(self) -> None:
        self.send_checks: list[tuple[UUID, str]] = []
        self.send_attempts: list[tuple[UUID, str]] = []
        self.verify_checks: list[tuple[UUID, str]] = []
        self.failed_verifications: list[tuple[UUID, str]] = []
        self.cleared_failures: list[tuple[UUID, str]] = []

    async def check_send_allowed(self, tenant_id: UUID, mobile_e164: str) -> None:
        self.send_checks.append((tenant_id, mobile_e164))

    async def record_send_attempt(self, tenant_id: UUID, mobile_e164: str) -> None:
        self.send_attempts.append((tenant_id, mobile_e164))

    async def check_verify_allowed(self, tenant_id: UUID, mobile_e164: str) -> None:
        self.verify_checks.append((tenant_id, mobile_e164))

    async def record_failed_verification(self, tenant_id: UUID, mobile_e164: str) -> None:
        self.failed_verifications.append((tenant_id, mobile_e164))

    async def clear_failed_verifications(self, tenant_id: UUID, mobile_e164: str) -> None:
        self.cleared_failures.append((tenant_id, mobile_e164))


class FakeSessionIssuer:
    def __init__(self) -> None:
        self.issued_for: list[tuple[UUID, UUID]] = []

    async def issue_for_active_membership(
        self,
        tenant: TenantContext,
        membership: CommunityMembership,
    ) -> object:
        self.issued_for.append((tenant.tenant_id, membership.membership_id))
        return {"test_session": True, "tenant_id": str(tenant.tenant_id)}


def build_flow(
    tenant: TenantContext,
    *,
    verified: bool = True,
    membership: CommunityMembership | None = None,
) -> tuple[OtpLoginCoordinator, FakeMembershipLookup, FakeAbuseGuard, FakeSessionIssuer, MockMessagingProvider]:
    provider = MockMessagingProvider(
        {MOBILE: TEST_OTP} if verified else {},
        environment="test",
    )
    memberships = FakeMembershipLookup(
        {(tenant.tenant_id, MOBILE): membership}
        if membership is not None
        else {}
    )
    guard = FakeAbuseGuard()
    issuer = FakeSessionIssuer()
    flow = OtpLoginCoordinator(
        MessagingService(provider),
        memberships,
        guard,
        issuer,
    )
    return flow, memberships, guard, issuer, provider


@pytest.mark.anyio
async def test_active_tenant_membership_and_verified_otp_are_required_before_session() -> None:
    tenant = tenant_context()
    membership = CommunityMembership(tenant.tenant_id, uuid4(), uuid4(), "active")
    flow, _, guard, issuer, _ = build_flow(tenant, membership=membership)

    session = await flow.verify_otp_and_issue_session(tenant, "+1 (415) 555-0123", TEST_OTP)

    assert session["test_session"] is True
    assert issuer.issued_for == [(tenant.tenant_id, membership.membership_id)]
    assert guard.cleared_failures == [(tenant.tenant_id, MOBILE)]


@pytest.mark.anyio
async def test_failed_otp_never_looks_up_membership_or_issues_session() -> None:
    tenant = tenant_context()
    membership = CommunityMembership(tenant.tenant_id, uuid4(), uuid4(), "active")
    flow, memberships, guard, issuer, _ = build_flow(tenant, verified=False, membership=membership)

    with pytest.raises(LoginRejected, match="Login could not be completed"):
        await flow.verify_otp_and_issue_session(tenant, MOBILE, "wrong-test-code")

    assert memberships.lookups == []
    assert issuer.issued_for == []
    assert guard.failed_verifications == [(tenant.tenant_id, MOBILE)]


@pytest.mark.anyio
async def test_verified_otp_cannot_issue_session_for_membership_in_another_tenant() -> None:
    tenant_a = tenant_context()
    tenant_b = tenant_context()
    membership_a = CommunityMembership(tenant_a.tenant_id, uuid4(), uuid4(), "active")
    provider = MockMessagingProvider({MOBILE: TEST_OTP}, environment="test")
    guard = FakeAbuseGuard()
    issuer = FakeSessionIssuer()

    class WrongTenantLookup:
        def find_by_tenant_and_mobile(
            self,
            tenant_id: UUID,
            mobile_e164: str,
        ) -> CommunityMembership:
            return membership_a

    flow = OtpLoginCoordinator(
        MessagingService(provider),
        WrongTenantLookup(),
        guard,
        issuer,
    )

    with pytest.raises(LoginRejected):
        await flow.verify_otp_and_issue_session(tenant_b, MOBILE, TEST_OTP)

    assert issuer.issued_for == []


@pytest.mark.anyio
async def test_inactive_membership_is_rejected_after_otp_verification() -> None:
    tenant = tenant_context()
    membership = CommunityMembership(tenant.tenant_id, uuid4(), uuid4(), "inactive")
    flow, _, _, issuer, _ = build_flow(tenant, membership=membership)

    with pytest.raises(LoginRejected):
        await flow.verify_otp_and_issue_session(tenant, MOBILE, TEST_OTP)

    assert issuer.issued_for == []


@pytest.mark.anyio
async def test_otp_request_is_generic_and_does_not_send_to_nonmember() -> None:
    tenant = tenant_context()
    flow, memberships, guard, _, provider = build_flow(tenant)

    await flow.request_otp(tenant, "+1 415 555 0123")

    assert memberships.lookups == [(tenant.tenant_id, MOBILE)]
    assert provider.sent_otp_requests == []
    assert guard.send_attempts == [(tenant.tenant_id, MOBILE)]


@pytest.mark.anyio
async def test_otp_request_sends_only_after_tenant_membership_lookup() -> None:
    tenant = tenant_context()
    membership = CommunityMembership(tenant.tenant_id, uuid4(), uuid4(), "active")
    flow, memberships, _, _, provider = build_flow(tenant, membership=membership)

    await flow.request_otp(tenant, MOBILE)

    assert memberships.lookups == [(tenant.tenant_id, MOBILE)]
    assert provider.sent_otp_requests == [MOBILE]
