from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.audit.errors import AuditStorageUnavailable
from app.modules.audit.models import AuditEvent


@dataclass(frozen=True, slots=True)
class AuditEventSummary:
    id: UUID
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    actor_category: str
    action: str
    outcome: str
    target_entity_type: str | None


class SQLAlchemyAuditEventRepository:
    """Read minimal audit summaries within one resolved tenant."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        try:
            self._session_factory = session_factory or get_session_factory()
        except RuntimeError as exc:
            raise AuditStorageUnavailable from exc

    def list_for_tenant(
        self, *, tenant_id: UUID, limit: int, offset: int
    ) -> list[AuditEventSummary]:
        statement = (
            select(
                AuditEvent.id,
                AuditEvent.event_type,
                AuditEvent.occurred_at,
                AuditEvent.recorded_at,
                AuditEvent.actor_category,
                AuditEvent.action,
                AuditEvent.outcome,
                AuditEvent.target_entity_type,
            )
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.recorded_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
        try:
            with self._session_factory() as session:
                rows = session.execute(statement).mappings().all()
                return [AuditEventSummary(**row) for row in rows]
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuditStorageUnavailable from exc


def get_audit_event_repository() -> SQLAlchemyAuditEventRepository:
    return SQLAlchemyAuditEventRepository()
