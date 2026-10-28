from __future__ import annotations

from app.core.tenant_context import TenantContext, TenantResolutionError
from app.modules.tenants.hostname import normalize_hostname
from app.modules.tenants.repository import TenantDomainLookup


def resolve_tenant_context(
    authority: str | None,
    repository: TenantDomainLookup,
) -> TenantContext:
    try:
        hostname = normalize_hostname(authority)
    except ValueError as exc:
        raise TenantResolutionError from exc

    domain = repository.find_by_hostname(hostname)
    if (
        domain is None
        or domain.hostname != hostname
        or not domain.domain_is_active
        or domain.verification_status != "verified"
        or domain.tenant_status != "active"
        or not domain.activation_outcome_acknowledged
    ):
        raise TenantResolutionError

    return TenantContext(
        tenant_id=domain.tenant_id,
        tenant_slug=domain.tenant_slug,
        hostname=hostname,
        domain_id=domain.domain_id,
    )
