from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.repository import ResolvedSession
from app.modules.agents.models import AgentProfile
from app.modules.support.models import SupportTicket
from app.modules.support.schemas import (
    SupportTicketCreate,
    SupportTicketListResponse,
    SupportTicketSingleResponse,
    SupportTicketSummary,
)

router = APIRouter(prefix="/support", tags=["support"])
support_tickets_router = APIRouter(prefix="/support-tickets", tags=["support tickets"])
audit_service = AuditService()


def _format_ticket(ticket: SupportTicket) -> SupportTicketSummary:
    return SupportTicketSummary(
        id=str(ticket.id),
        employeeRefId=ticket.employee_ref_id or "",
        employeeName=ticket.employee_name,
        message=ticket.message,
        status=ticket.status,
        createdAt=ticket.created_at.isoformat() if ticket.created_at else "",
        resolvedAt=ticket.resolved_at.isoformat() if ticket.resolved_at else None,
    )


@router.get("", response_model=SupportTicketListResponse)
@support_tickets_router.get("", response_model=SupportTicketListResponse)
def list_support_tickets(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    db: Annotated[Session, Depends(get_db)],
    community_id: UUID | None = Query(None),
) -> SupportTicketListResponse:
    resolved, _ = session_data

    stmt = select(SupportTicket).order_by(desc(SupportTicket.created_at))

    if resolved.role == "SUPER_ADMIN":
        if community_id is not None:
            stmt = stmt.where(SupportTicket.tenant_id == community_id)
    elif resolved.role == "ADMIN":
        stmt = stmt.where(SupportTicket.tenant_id == resolved.tenant_id)
    else:
        # Agent or regular user: can only see tickets they submitted
        stmt = stmt.where(
            (SupportTicket.user_id == resolved.user_id)
            | (SupportTicket.employee_ref_id == resolved.username)
        )

    tickets = db.execute(stmt).scalars().all()
    return SupportTicketListResponse(tickets=[_format_ticket(t) for t in tickets])


@router.post("", response_model=SupportTicketSingleResponse, status_code=status.HTTP_201_CREATED)
@support_tickets_router.post("", response_model=SupportTicketSingleResponse, status_code=status.HTTP_201_CREATED)
def create_support_ticket(
    body: SupportTicketCreate,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    db: Annotated[Session, Depends(get_db)],
) -> SupportTicketSingleResponse:
    resolved, _ = session_data

    # Resolve employee / agent details if applicable
    employee_ref_id = None
    employee_name = resolved.username

    agent = db.execute(
        select(AgentProfile).where(AgentProfile.user_id == resolved.user_id)
    ).scalar_one_or_none()

    if agent is not None:
        employee_ref_id = agent.ref_id
        employee_name = f"{agent.first_name} {agent.last_name}".strip() or resolved.username

    now = datetime.now(UTC)
    ticket = SupportTicket(
        id=uuid4(),
        tenant_id=resolved.tenant_id,
        user_id=resolved.user_id,
        employee_ref_id=employee_ref_id,
        employee_name=employee_name,
        message=body.message.strip(),
        status="open",
        created_at=now,
    )
    db.add(ticket)

    audit_service.record_mandatory(
        db,
        AuditEventDraft(
            event_type="support.ticket_created",
            actor_category="tenant_member" if resolved.tenant_id else "platform_operator",
            actor_ref=str(resolved.user_id),
            action="support.ticket_create",
            outcome="success",
            tenant_id=resolved.tenant_id,
            target_entity_type="support_ticket",
            target_entity_ref=str(ticket.id),
        ),
    )
    db.commit()
    db.refresh(ticket)

    return SupportTicketSingleResponse(ticket=_format_ticket(ticket))


@router.patch("/{ticket_id}", response_model=SupportTicketSingleResponse)
@router.patch("/{ticket_id}/resolve", response_model=SupportTicketSingleResponse)
@support_tickets_router.patch("/{ticket_id}", response_model=SupportTicketSingleResponse)
@support_tickets_router.patch("/{ticket_id}/resolve", response_model=SupportTicketSingleResponse)
def resolve_support_ticket(
    ticket_id: UUID,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    db: Annotated[Session, Depends(get_db)],
) -> SupportTicketSingleResponse:
    resolved, _ = session_data

    ticket = db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
    ).scalar_one_or_none()

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "TICKET_NOT_FOUND", "message": "Support ticket not found."},
        )

    # Permission check: Super Admin or Admin of the same tenant
    if resolved.role != "SUPER_ADMIN":
        if resolved.role != "ADMIN" or ticket.tenant_id != resolved.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN", "message": "You cannot resolve this ticket."},
            )

    now = datetime.now(UTC)
    ticket.status = "resolved"
    ticket.resolved_at = now

    audit_service.record_mandatory(
        db,
        AuditEventDraft(
            event_type="support.ticket_resolved",
            actor_category="platform_operator" if resolved.role == "SUPER_ADMIN" else "tenant_member",
            actor_ref=str(resolved.user_id),
            action="support.ticket_resolve",
            outcome="success",
            tenant_id=ticket.tenant_id,
            target_entity_type="support_ticket",
            target_entity_ref=str(ticket.id),
        ),
    )

    db.commit()
    db.refresh(ticket)

    return SupportTicketSingleResponse(ticket=_format_ticket(ticket))
