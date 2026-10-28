from __future__ import annotations

from ipaddress import ip_address

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.tenant_context import TenantContext, TenantResolutionError
from app.modules.tenants.repository import TenantDomainLookup, get_tenant_domain_repository
from app.modules.tenants.service import resolve_tenant_context


def _is_trusted_proxy(remote_address: str | None, settings: Settings) -> bool:
    if not remote_address or not settings.trusted_proxy_network_list:
        return False
    try:
        address = ip_address(remote_address)
    except ValueError:
        return False
    return any(
        address.version == network.version and address in network
        for network in settings.trusted_proxy_network_list
    )


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise TenantResolutionError
    return values[0]


def get_tenant_context(
    request: Request,
    repository: TenantDomainLookup = Depends(get_tenant_domain_repository),
) -> TenantContext:
    """Resolve request host through the verified registry; never accept a tenant ID."""
    settings: Settings = request.app.state.settings
    proxy_address = request.client.host if request.client is not None else None

    if _is_trusted_proxy(proxy_address, settings):
        forwarded_hosts = request.headers.getlist("x-forwarded-host")
        if len(forwarded_hosts) > 1:
            raise TenantResolutionError
        if forwarded_hosts:
            authority = forwarded_hosts[0]
        else:
            authority = _single_header(request, "host")
    else:
        authority = _single_header(request, "host")

    return resolve_tenant_context(authority, repository)
