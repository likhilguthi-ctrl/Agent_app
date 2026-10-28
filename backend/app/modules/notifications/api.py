from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.repository import ResolvedSession
from app.modules.notifications.schemas import (
    InAppNotificationListResponse,
    InAppNotificationSummary,
    MarkAllReadResponse,
    UnreadCountResponse,
)
from app.modules.notifications.service import InAppNotificationService

router = APIRouter(prefix="/platform/notifications", tags=["platform notifications"])
notification_service = InAppNotificationService()


def require_super_admin(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> ResolvedSession:
    resolved, _ = session_data
    if resolved.role != "SUPER_ADMIN" or resolved.account_type != "platform_operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Super Admin privileges required."},
        )
    return resolved


def require_super_admin_csrf(
    request: Request,
    response: Response,
    session: Annotated[ResolvedSession, Depends(require_super_admin)],
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token", max_length=64)] = None,
) -> ResolvedSession:
    import secrets
    response.headers["Cache-Control"] = "no-store"
    settings = getattr(request.app.state, "settings", None)
    origin = request.headers.get("origin")
    if (
        csrf_header is None
        or not secrets.compare_digest(csrf_header, session.csrf_token)
        or (settings and origin and origin not in settings.frontend_origin_list)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "CSRF_VERIFICATION_FAILED", "message": "CSRF validation failed."},
        )
    return session


@router.get(
    "",
    response_model=InAppNotificationListResponse,
    summary="List in-app notifications for caller Super Admin",
)
def list_super_admin_notifications(
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> InAppNotificationListResponse:
    return notification_service.list_notifications(
        session,
        operator_id=caller.user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="Get unread notification count for caller Super Admin",
)
def get_unread_notification_count(
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> UnreadCountResponse:
    return notification_service.get_unread_count(session, operator_id=caller.user_id)


@router.patch(
    "/{notification_id}/read",
    response_model=InAppNotificationSummary,
    summary="Mark in-app notification as read",
)
def mark_notification_read(
    notification_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> InAppNotificationSummary:
    return notification_service.mark_as_read(
        session,
        notification_id=notification_id,
        operator_id=caller.user_id,
    )


@router.post(
    "/mark-all-read",
    response_model=MarkAllReadResponse,
    summary="Mark all in-app notifications as read for caller Super Admin",
)
def mark_all_notifications_read(
    caller: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> MarkAllReadResponse:
    return notification_service.mark_all_as_read(session, operator_id=caller.user_id)
