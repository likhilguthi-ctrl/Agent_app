from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Trusted tenant identity resolved from an active, verified hostname mapping."""

    tenant_id: UUID
    tenant_slug: str
    hostname: str
    domain_id: UUID


class TenantResolutionError(Exception):
    """Raised when a request cannot be mapped to an active verified tenant."""
