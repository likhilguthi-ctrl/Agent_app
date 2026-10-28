from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import SQLAlchemyTenantMembershipRepository
from app.modules.tenants.models import Tenant


@pytest.fixture
def identity_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_mobile_identifier_is_unique_inside_tenant_but_reusable_across_tenants(
    identity_session_factory,
) -> None:
    tenant_a_id = uuid4()
    tenant_b_id = uuid4()
    user_a = User(id=uuid4(), status="active")
    user_b = User(id=uuid4(), status="active")
    with identity_session_factory() as session:
        session.add_all(
            [
                Tenant(id=tenant_a_id, slug="community-a", status="active"),
                Tenant(id=tenant_b_id, slug="community-b", status="active"),
                user_a,
                user_b,
            ]
        )
        session.flush()
        session.add_all(
            [
                TenantMembership(
                    id=uuid4(), tenant_id=tenant_a_id, user_id=user_a.id,
                    mobile_e164="+14155550123", status="active",
                ),
                TenantMembership(
                    id=uuid4(), tenant_id=tenant_b_id, user_id=user_b.id,
                    mobile_e164="+14155550123", status="active",
                ),
            ]
        )
        session.commit()

        session.add(User(id=(duplicate_user_id := uuid4()), status="active"))
        session.flush()
        session.add(
            TenantMembership(
                id=uuid4(), tenant_id=tenant_a_id, user_id=duplicate_user_id,
                mobile_e164="+14155550123", status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_community_identity_cannot_be_linked_to_multiple_tenants(identity_session_factory) -> None:
    user_id = uuid4()
    tenant_a_id, tenant_b_id = uuid4(), uuid4()
    with identity_session_factory() as session:
        session.add_all(
            [
                Tenant(id=tenant_a_id, slug="identity-a", status="active"),
                Tenant(id=tenant_b_id, slug="identity-b", status="active"),
                User(id=user_id, status="active"),
            ]
        )
        session.flush()
        session.add(
            TenantMembership(
                id=uuid4(), tenant_id=tenant_a_id, user_id=user_id,
                mobile_e164="+14155550123", status="active",
            )
        )
        session.commit()
        session.add(
            TenantMembership(
                id=uuid4(), tenant_id=tenant_b_id, user_id=user_id,
                mobile_e164="+14155550123", status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_repository_requires_active_user_membership_in_resolved_tenant(
    identity_session_factory,
) -> None:
    tenant_a_id, tenant_b_id = uuid4(), uuid4()
    user_id, membership_id = uuid4(), uuid4()
    with identity_session_factory() as session:
        session.add_all(
            [
                Tenant(id=tenant_a_id, slug="repo-a", status="active"),
                Tenant(id=tenant_b_id, slug="repo-b", status="active"),
                User(id=user_id, status="active"),
            ]
        )
        session.flush()
        session.add(
            TenantMembership(
                id=membership_id,
                tenant_id=tenant_a_id,
                user_id=user_id,
                mobile_e164="+14155550123",
                status="active",
            )
        )
        session.commit()

    lookup = SQLAlchemyTenantMembershipRepository(identity_session_factory)
    active = lookup.find_by_tenant_and_mobile(tenant_a_id, "+14155550123")
    other_tenant = lookup.find_by_tenant_and_mobile(tenant_b_id, "+14155550123")

    assert active is not None
    assert active.tenant_id == tenant_a_id
    assert active.membership_id == membership_id
    assert active.user_id == user_id
    assert other_tenant is None
