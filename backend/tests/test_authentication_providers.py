from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.tenant_context import TenantContext
from app.modules.authentication.providers import (
    AuthenticationProviderUnavailable,
    UnconfiguredAuthenticationProvider,
    VerifiedCredentialProof,
)
from app.modules.authentication.repository import AuthenticatedSession
from app.modules.identity.contracts import CommunityMembership


@pytest.mark.anyio
async def test_unconfigured_provider_fails_closed_at_every_authentication_stage() -> None:
    provider = UnconfiguredAuthenticationProvider()
    tenant_id = uuid4()
    tenant = TenantContext(tenant_id, "provider-test", "provider.example.test", uuid4())
    membership = CommunityMembership(tenant_id, uuid4(), uuid4(), "active")
    authenticated = AuthenticatedSession(
        uuid4(),
        tenant_id,
        membership.user_id,
        membership.membership_id,
        "csrf",
        datetime.now(UTC) + timedelta(minutes=5),
        "active",
        "active",
        "active",
    )

    operations = (
        provider.start_authentication(tenant, username="test-user", password="test-password"),
        provider.verify_otp(tenant, challenge_id=uuid4(), otp="test-otp"),
        provider.resolve_identity(tenant, VerifiedCredentialProof(uuid4())),
        provider.create_session(tenant, membership),
        provider.revoke_session(tenant, authenticated),
    )
    for operation in operations:
        with pytest.raises(AuthenticationProviderUnavailable):
            await operation
