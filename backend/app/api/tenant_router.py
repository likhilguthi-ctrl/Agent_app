from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends

from app.modules.tenants.dependencies import get_tenant_context


def create_tenant_router(
    *,
    prefix: str = "",
    tags: Sequence[str] | None = None,
) -> APIRouter:
    """Create a router that fails closed unless a trusted tenant resolves."""
    return APIRouter(
        prefix=prefix,
        tags=list(tags) if tags is not None else None,
        dependencies=[Depends(get_tenant_context)],
    )
