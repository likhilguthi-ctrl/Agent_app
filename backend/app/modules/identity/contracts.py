from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CommunityMembership:
    tenant_id: UUID
    membership_id: UUID
    user_id: UUID
    status: str


class TenantMembershipLookup(Protocol):
    """Lookup an identity only through its tenant-scoped mobile identifier."""

    def find_by_tenant_and_mobile(
        self,
        tenant_id: UUID,
        mobile_e164: str,
    ) -> CommunityMembership | None: ...
