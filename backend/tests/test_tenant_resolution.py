from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import Annotated
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.tenant_router import create_tenant_router
from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.main import create_app
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.hostname import normalize_hostname
from app.modules.tenants.repository import (
    SQLAlchemyTenantDomainRepository,
    TenantDomainRecord,
    get_tenant_domain_repository,
)


TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")
DOMAIN_A = UUID("00000000-0000-0000-0000-00000000001a")
DOMAIN_B = UUID("00000000-0000-0000-0000-00000000001b")


class InMemoryTenantDomainRepository:
    def __init__(self, records: Mapping[str, TenantDomainRecord]) -> None:
        self.records = records

    def find_by_hostname(self, hostname: str) -> TenantDomainRecord | None:
        return self.records.get(hostname)


def domain_record(
    *,
    domain_id: UUID = DOMAIN_A,
    tenant_id: UUID = TENANT_A,
    slug: str = "community-a",
    hostname: str = "community-a.example.com",
    domain_active: bool = True,
    verification_status: str = "verified",
    tenant_status: str = "active",
    activation_outcome_acknowledged: bool = True,
) -> TenantDomainRecord:
    return TenantDomainRecord(
        domain_id=domain_id,
        tenant_id=tenant_id,
        tenant_slug=slug,
        hostname=hostname,
        domain_is_active=domain_active,
        verification_status=verification_status,
        tenant_status=tenant_status,
        activation_outcome_acknowledged=activation_outcome_acknowledged,
    )


def tenant_test_app(
    records: Mapping[str, TenantDomainRecord],
    *,
    trusted_proxy_cidrs: str = "",
) -> FastAPI:
    app = create_app(
        Settings(
            _env_file=None,
            frontend_origins="",
            trusted_proxy_cidrs=trusted_proxy_cidrs,
        )
    )
    repository = InMemoryTenantDomainRepository(records)
    app.dependency_overrides[get_tenant_domain_repository] = lambda: repository

    router = create_tenant_router()

    @router.get("/context")
    async def context_endpoint(
        tenant: Annotated[TenantContext, Depends(get_tenant_context)],
    ) -> dict[str, str]:
        await asyncio.sleep(0.005)
        return {
            "tenant_id": str(tenant.tenant_id),
            "tenant_slug": tenant.tenant_slug,
            "hostname": tenant.hostname,
            "domain_id": str(tenant.domain_id),
        }

    app.include_router(router, prefix="/tenant-test")
    return app


@pytest.mark.anyio
async def test_verified_domain_resolves_active_tenant_context() -> None:
    app = tenant_test_app({"community-a.example.com": domain_record()})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/tenant-test/context",
            headers={"Host": "COMMUNITY-A.Example.com:8443"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": str(TENANT_A),
        "tenant_slug": "community-a",
        "hostname": "community-a.example.com",
        "domain_id": str(DOMAIN_A),
    }


@pytest.mark.parametrize(
    ("authority", "expected"),
    [
        ("Community-A.Example.COM:8443", "community-a.example.com"),
        ("community-a.example.com.", "community-a.example.com"),
        ("BÜCHER.example:443", "xn--bcher-kva.example"),
        ("community.example。", "community.example"),
    ],
)
def test_hostname_normalization(authority: str, expected: str) -> None:
    assert normalize_hostname(authority) == expected


@pytest.mark.parametrize(
    "authority",
    [
        None,
        "",
        "not a hostname",
        "community.example:invalid",
        "community.example:",
        "community.example/path",
        "user@community.example",
        "community..example",
        "community.example,other.example",
        "127.0.0.1",
        "[::1]:8000",
    ],
)
def test_malformed_or_non_domain_hostname_is_rejected(authority: str | None) -> None:
    with pytest.raises(ValueError):
        normalize_hostname(authority)


def test_tenant_context_is_immutable() -> None:
    context = TenantContext(
        tenant_id=TENANT_A,
        tenant_slug="community-a",
        hostname="community-a.example.com",
        domain_id=DOMAIN_A,
    )
    with pytest.raises(FrozenInstanceError):
        context.tenant_slug = "community-b"  # type: ignore[misc]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "records",
    [
        {},
        {
            "community-a.example.com": domain_record(
                verification_status="pending",
            )
        },
        {
            "community-a.example.com": domain_record(
                domain_active=False,
            )
        },
        {
            "community-a.example.com": domain_record(
                tenant_status="inactive",
            )
        },
    ],
    ids=["unknown", "unverified", "inactive-domain", "inactive-tenant"],
)
async def test_unresolvable_domain_states_fail_with_same_safe_response(
    records: Mapping[str, TenantDomainRecord],
) -> None:
    app = tenant_test_app(records)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/tenant-test/context",
            headers={"Host": "community-a.example.com", "X-Request-ID": "tenant-failure-1"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "TENANT_NOT_RESOLVED",
            "message": "Tenant context could not be resolved.",
            "requestId": "tenant-failure-1",
        }
    }


@pytest.mark.anyio
async def test_active_onboarding_domain_waits_for_activation_outcome_acknowledgement() -> None:
    app = tenant_test_app(
        {
            "community-a.example.com": domain_record(
                activation_outcome_acknowledged=False,
            )
        }
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/tenant-test/context",
            headers={"Host": "community-a.example.com"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TENANT_NOT_RESOLVED"


def test_sql_tenant_lookup_binds_resolution_to_completion_receipt() -> None:
    class EmptyResult:
        def one_or_none(self):
            return None

    class CapturingSession:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return EmptyResult()

    session = CapturingSession()
    repository = SQLAlchemyTenantDomainRepository(session)  # type: ignore[arg-type]

    assert repository.find_by_hostname("community-a.example.com") is None
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "tenant_activation_attempts" in compiled
    assert "completion_audit_event_id" in compiled
    assert "audit_outbox_events" in compiled
    assert "acknowledgement_ref" in compiled
    assert "platform.tenant.activation.completed" in compiled


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["", "malformed host"])
async def test_missing_or_malformed_host_fails_closed_for_tenant_router(host: str) -> None:
    app = tenant_test_app({"community-a.example.com": domain_record()})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/tenant-test/context", headers={"Host": host})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TENANT_NOT_RESOLVED"
    assert response.headers.get("x-request-id")


@pytest.mark.anyio
async def test_client_supplied_tenant_id_cannot_override_resolved_context() -> None:
    app = tenant_test_app({"community-a.example.com": domain_record()})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/tenant-test/context?tenant_id={TENANT_B}",
            headers={
                "Host": "community-a.example.com",
                "X-Tenant-ID": str(TENANT_B),
            },
        )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(TENANT_A)
    assert response.json()["tenant_slug"] == "community-a"


@pytest.mark.anyio
async def test_health_endpoints_remain_platform_level_without_registry_access() -> None:
    app = create_app(Settings(_env_file=None, frontend_origins=""))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_health = await client.get("/health")
        versioned_health = await client.get("/api/v1/health")

    assert root_health.status_code == versioned_health.status_code == 200
    assert root_health.json() == versioned_health.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_concurrent_tenant_requests_keep_context_isolated() -> None:
    app = tenant_test_app(
        {
            "community-a.example.com": domain_record(),
            "community-b.example.com": domain_record(
                domain_id=DOMAIN_B,
                tenant_id=TENANT_B,
                slug="community-b",
                hostname="community-b.example.com",
            ),
        }
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response_a, response_b = await asyncio.gather(
            client.get("/tenant-test/context", headers={"Host": "community-a.example.com"}),
            client.get("/tenant-test/context", headers={"Host": "community-b.example.com"}),
        )

    assert response_a.json()["tenant_id"] == str(TENANT_A)
    assert response_a.json()["hostname"] == "community-a.example.com"
    assert response_b.json()["tenant_id"] == str(TENANT_B)
    assert response_b.json()["hostname"] == "community-b.example.com"


@pytest.mark.anyio
async def test_forwarded_host_is_ignored_without_explicit_trusted_proxy() -> None:
    app = tenant_test_app(
        {
            "community-a.example.com": domain_record(),
            "community-b.example.com": domain_record(
                domain_id=DOMAIN_B,
                tenant_id=TENANT_B,
                slug="community-b",
                hostname="community-b.example.com",
            ),
        }
    )
    transport = httpx.ASGITransport(app=app, client=("10.0.0.4", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/tenant-test/context",
            headers={
                "Host": "community-a.example.com",
                "X-Forwarded-Host": "community-b.example.com",
                "Forwarded": "host=community-b.example.com",
            },
        )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(TENANT_A)


@pytest.mark.anyio
async def test_forwarded_host_is_used_only_from_configured_proxy_network() -> None:
    app = tenant_test_app(
        {
            "community-a.example.com": domain_record(),
            "community-b.example.com": domain_record(
                domain_id=DOMAIN_B,
                tenant_id=TENANT_B,
                slug="community-b",
                hostname="community-b.example.com",
            ),
        },
        trusted_proxy_cidrs="10.0.0.0/24",
    )
    transport = httpx.ASGITransport(app=app, client=("10.0.0.4", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/tenant-test/context",
            headers={
                "Host": "internal-proxy.example.net",
                "X-Forwarded-Host": "community-b.example.com:443",
            },
        )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(TENANT_B)
    assert response.json()["hostname"] == "community-b.example.com"


def test_trusted_proxy_configuration_rejects_malformed_networks() -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_CIDRS"):
        Settings(_env_file=None, trusted_proxy_cidrs="everyone")
    with pytest.raises(ValidationError, match="must not trust every IP"):
        Settings(_env_file=None, trusted_proxy_cidrs="0.0.0.0/0")
