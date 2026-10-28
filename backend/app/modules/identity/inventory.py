from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.identity.errors import MembershipStorageUnavailable
from app.modules.identity.models import TenantMembership, User


@dataclass(frozen=True, slots=True)
class MembershipSummary:
    id: UUID
    user_id: UUID
    status: str
    user_status: str
    created_at: datetime
    updated_at: datetime


class SQLAlchemyMembershipInventoryRepository:
    """Read minimal membership summaries within the resolved tenant."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        try:
            self._session_factory = session_factory or get_session_factory()
        except RuntimeError as exc:
            raise MembershipStorageUnavailable from exc

    def list_for_tenant(
        self, *, tenant_id: UUID, limit: int, offset: int
    ) -> list[MembershipSummary]:
        statement = (
            select(
                TenantMembership.id,
                TenantMembership.user_id,
                TenantMembership.status,
                User.status.label("user_status"),
                TenantMembership.created_at,
                TenantMembership.updated_at,
            )
            .join(User, User.id == TenantMembership.user_id)
            .where(TenantMembership.tenant_id == tenant_id)
            .order_by(TenantMembership.created_at.desc(), TenantMembership.id)
            .limit(limit)
            .offset(offset)
        )
        try:
            with self._session_factory() as session:
                rows = session.execute(statement).mappings().all()
                return [MembershipSummary(**row) for row in rows]
        except (SQLAlchemyError, RuntimeError) as exc:
            raise MembershipStorageUnavailable from exc


def get_membership_inventory_repository() -> SQLAlchemyMembershipInventoryRepository:
    return SQLAlchemyMembershipInventoryRepository()
