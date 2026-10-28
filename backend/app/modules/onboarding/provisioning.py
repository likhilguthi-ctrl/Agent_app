from __future__ import annotations

from typing import Protocol
from uuid import UUID


class CommunityAdminProvisioningUnavailable(Exception):
    """The verified nominee/invitation contract is not configured."""


class CommunityAdminProvisioner(Protocol):
    """Boundary for a future identity-verified, invitation-accepted admin grant."""

    def provision_after_invitation_acceptance(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        nominee_identity_id: UUID,
        invitation_id: UUID,
    ) -> UUID: ...


class UnavailableCommunityAdminProvisioner:
    """Fail-closed implementation while nominee proof and invitation rules are undecided."""

    def provision_after_invitation_acceptance(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        nominee_identity_id: UUID,
        invitation_id: UUID,
    ) -> UUID:
        del request_id, tenant_id, nominee_identity_id, invitation_id
        raise CommunityAdminProvisioningUnavailable
