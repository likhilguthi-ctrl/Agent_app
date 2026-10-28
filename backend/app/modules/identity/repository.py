from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.identity.contracts import CommunityMembership, TenantMembershipLookup
from app.modules.identity.models import TenantMembership, User
from app.modules.tenants.models import Tenant


class SQLAlchemyTenantMembershipRepository:
    """Tenant-first active membership lookup using the shared PostgreSQL session factory."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def find_by_tenant_and_mobile(
        self,
        tenant_id: UUID,
        mobile_e164: str,
    ) -> CommunityMembership | None:
        statement = (
            select(
                TenantMembership.tenant_id.label("tenant_id"),
                TenantMembership.id.label("membership_id"),
                TenantMembership.user_id.label("user_id"),
                TenantMembership.status.label("status"),
            )
            .join(User, User.id == TenantMembership.user_id)
            .join(Tenant, Tenant.id == TenantMembership.tenant_id)
            .where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.mobile_e164 == mobile_e164,
                TenantMembership.status == "active",
                User.status == "active",
                Tenant.status == "active",
            )
        )
        with self._session_factory() as session:
            row = session.execute(statement).one_or_none()
        return CommunityMembership(**row._mapping) if row is not None else None


def get_tenant_membership_lookup() -> TenantMembershipLookup:
    return SQLAlchemyTenantMembershipRepository()
