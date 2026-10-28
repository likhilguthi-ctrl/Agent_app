from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.api.tenant_router import create_tenant_router
from app.db.session import get_db
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import (
    AuditEventSummary,
    SQLAlchemyAuditEventRepository,
    get_audit_event_repository,
)
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_permission
from app.modules.authorization.service import AuthorizationRequest

router = create_tenant_router(prefix="/audit-events", tags=["audit"])
platform_activity_router = APIRouter(prefix="/platform", tags=["platform audit"])


class AuditEventResponse(BaseModel):
    id: UUID
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    actor_category: str
    action: str
    outcome: str
    target_entity_type: str | None


class AuditEventListResponse(BaseModel):
    events: list[AuditEventResponse]


@router.get("", response_model=AuditEventListResponse)
async def list_audit_events(
    response: Response,
    authorized: Annotated[
        AuthorizationRequest, Depends(require_permission(PermissionCode.AUDIT_READ))
    ],
    repository: Annotated[
        SQLAlchemyAuditEventRepository, Depends(get_audit_event_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> AuditEventListResponse:
    """List a bounded page of minimal audit summaries for the resolved tenant."""
    response.headers["Cache-Control"] = "no-store"
    events: list[AuditEventSummary] = repository.list_for_tenant(
        tenant_id=authorized.tenant.tenant_id,
        limit=limit,
        offset=offset,
    )
    return AuditEventListResponse(
        events=[
            AuditEventResponse(
                id=event.id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                recorded_at=event.recorded_at,
                actor_category=event.actor_category,
                action=event.action,
                outcome=event.outcome,
                target_entity_type=event.target_entity_type,
            )
            for event in events
        ]
    )


class ActivityLogEntry(BaseModel):
    id: str
    actor: str
    action: str
    details: str
    timestamp: str


class ActivityLogResponse(BaseModel):
    activity: list[ActivityLogEntry]


@platform_activity_router.get("/activity", response_model=ActivityLogResponse)
def get_platform_activity(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> ActivityLogResponse:
    resolved, _ = session_data
    if resolved.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Super Admin access is required."},
        )

    stmt = select(AuditEvent).order_by(desc(AuditEvent.occurred_at)).limit(limit)
    rows = db.execute(stmt).scalars().all()
    entries = []
    for row in rows:
        actor = row.actor_ref or row.actor_category or "system"
        action = row.action
        details = ""
        if row.target_entity_type and row.target_entity_ref:
            details = f"{row.target_entity_type}: {row.target_entity_ref}"
        elif row.outcome:
            details = f"Outcome: {row.outcome}"
        entries.append(
            ActivityLogEntry(
                id=str(row.id),
                actor=actor,
                action=action,
                details=details,
                timestamp=row.occurred_at.isoformat() if row.occurred_at else "",
            )
        )
    return ActivityLogResponse(activity=entries)

