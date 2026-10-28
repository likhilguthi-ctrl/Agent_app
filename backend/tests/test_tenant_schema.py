from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.tenants.models import Tenant, TenantDomain
from app.modules.tenants.repository import SQLAlchemyTenantDomainRepository
from app.modules.tenants.service import resolve_tenant_context


@pytest.fixture
def isolated_schema_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_hostname_uniqueness_is_enforced_by_database_index(isolated_schema_engine) -> None:
    hostname_index = next(
        index
        for index in TenantDomain.__table__.indexes
        if index.name == "uq_tenant_domains_hostname_normalized"
    )
    assert hostname_index.unique is True

    with Session(isolated_schema_engine) as session:
        tenant = Tenant(id=uuid4(), slug="community-a", status="active")
        session.add(tenant)
        session.flush()
        session.add(
            TenantDomain(
                id=uuid4(),
                tenant_id=tenant.id,
                hostname="community-a.example.com",
                verification_status="verified",
                is_active=True,
                verified_at=datetime.now(UTC),
            )
        )
        session.commit()
        session.add(
            TenantDomain(
                id=uuid4(),
                tenant_id=tenant.id,
                hostname="community-a.example.com",
                verification_status="pending",
                is_active=False,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_sqlalchemy_repository_resolves_only_after_registry_lookup(isolated_schema_engine) -> None:
    tenant_id = uuid4()
    domain_id = uuid4()
    with Session(isolated_schema_engine) as session:
        session.add(Tenant(id=tenant_id, slug="community-b", status="active"))
        session.add(
            TenantDomain(
                id=domain_id,
                tenant_id=tenant_id,
                hostname="community-b.example.com",
                verification_status="verified",
                is_active=True,
                verified_at=datetime.now(UTC),
            )
        )
        session.commit()

        context = resolve_tenant_context(
            "COMMUNITY-B.EXAMPLE.COM:443",
            SQLAlchemyTenantDomainRepository(session),
        )

    assert context.tenant_id == tenant_id
    assert context.domain_id == domain_id
    assert context.hostname == "community-b.example.com"
    assert context.tenant_slug == "community-b"
